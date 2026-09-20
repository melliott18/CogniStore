"""PostgreSQL session locks preserve API mutation ordering across workers."""

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa

from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver
from tests.unit.test_api_authentication import identity_provider as identity_provider
from tests.unit.test_api_object_mutations import (
    BUCKET,
    KEY,
    ORIGINAL,
    PATH,
    REPLACEMENT,
    _assert_audit,
    _assert_error,
    _client,
    _put,
)
from tests.unit.test_object_mutation_fence import (
    assert_independent_catalog_handles,
    assert_process_exit_releases_reservation,
    assert_process_reservations,
    assert_tenants_and_catalog_reads_remain_independent,
    reserve,
)

pytestmark = pytest.mark.integration


def test_postgres_independent_handles_share_reservations(postgres_dsn):
    with SQLCatalog(postgres_dsn) as catalog, SQLCatalog(postgres_dsn) as other:
        assert_independent_catalog_handles(catalog, other)


def test_postgres_reservation_is_shared_by_separate_worker_processes(postgres_dsn):
    with SQLCatalog(postgres_dsn) as catalog:
        assert_process_reservations(catalog, postgres_dsn)


def test_postgres_worker_exit_releases_reservation(postgres_dsn):
    with SQLCatalog(postgres_dsn) as catalog:
        assert_process_exit_releases_reservation(catalog, postgres_dsn)


def test_postgres_distinct_tenants_and_catalog_reads_remain_independent(postgres_dsn):
    with SQLCatalog(postgres_dsn) as catalog:
        assert_tenants_and_catalog_reads_remain_independent(catalog)


@pytest.mark.parametrize("lock_function", ["pg_advisory_lock_shared", "pg_try_advisory_lock"])
def test_postgres_uncertain_lock_acquisition_discards_session(postgres_dsn, lock_function):
    with SQLCatalog(postgres_dsn) as catalog, SQLCatalog(postgres_dsn) as other:
        armed = False
        injected = False
        invalidations = []

        def observe_acquisition(_connection, _cursor, statement, *_args):
            nonlocal armed
            if not injected and statement.startswith(f"SELECT {lock_function}("):
                armed = True

        def fail_acquisition_commit(_connection):
            nonlocal armed, injected
            if armed:
                armed, injected = False, True
                # The server already granted its session lock. An uncertain
                # commit must close this session, not return it to the pool.
                raise RuntimeError("uncertain lock acquisition")

        def record_invalidation(*_args):
            invalidations.append(True)

        sa.event.listen(catalog._legal_hold_engine, "after_cursor_execute", observe_acquisition)
        sa.event.listen(catalog._legal_hold_engine, "commit", fail_acquisition_commit)
        sa.event.listen(catalog._legal_hold_engine, "invalidate", record_invalidation)
        try:
            with pytest.raises(RuntimeError, match="uncertain lock acquisition"):
                reserve(catalog)
            assert injected and invalidations
            # Neither another handle nor the original pool may retain a
            # reservation after an uncertain acquisition has failed.
            assert reserve(other)
            assert reserve(catalog)
        finally:
            sa.event.remove(catalog._legal_hold_engine, "after_cursor_execute", observe_acquisition)
            sa.event.remove(catalog._legal_hold_engine, "commit", fail_acquisition_commit)
            sa.event.remove(catalog._legal_hold_engine, "invalidate", record_invalidation)


def test_postgres_finalization_uses_lock_session_without_transaction_during_io(
    postgres_dsn, monkeypatch,
):
    with SQLCatalog(postgres_dsn) as catalog:
        lock_sessions = []
        mutation_pids = []
        lock_object = catalog._lock_object

        def capture_lock_session(connection, _cursor, statement, *_args):
            if statement.startswith("SELECT pg_try_advisory_lock("):
                lock_sessions.append(connection)

        def capture_finalization_session(connection, bucket, key):
            mutation_pids.append(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            return lock_object(connection, bucket, key)

        sa.event.listen(catalog._legal_hold_engine, "after_cursor_execute", capture_lock_session)
        monkeypatch.setattr(catalog, "_lock_object", capture_finalization_session)
        with catalog.object_mutation("bucket", "key"):
            session = lock_sessions[-1]
            owner_pid = session.connection.driver_connection.info.backend_pid
            assert not session.in_transaction()
            catalog.upsert("bucket", "key", size=4, tier="hot")
            assert not session.in_transaction()
            assert catalog.get("bucket", "key").size == 4
            catalog.delete("bucket", "key")
            assert not session.in_transaction()
            assert catalog.get("bucket", "key") is None
            assert mutation_pids == [owner_pid, owner_pid]


@pytest.mark.parametrize("operation", ["upsert", "delete"])
def test_postgres_read_only_handle_cannot_borrow_writable_mutation_session(
    postgres_dsn, operation,
):
    with SQLCatalog(postgres_dsn) as catalog:
        catalog.upsert("bucket", "key", size=4, tier="hot")
        original = catalog.get("bucket", "key")
        with SQLCatalog(postgres_dsn, read_only=True) as read_only:
            with catalog.object_mutation("bucket", "key"):
                with pytest.raises(PermissionError, match="read-only catalog"):
                    if operation == "upsert":
                        read_only.upsert("bucket", "key", size=99, tier="hot")
                    else:
                        read_only.delete("bucket", "key")
                assert catalog.get("bucket", "key") == original
                catalog.upsert("bucket", "key", size=5, tier="hot")
        assert catalog.get("bucket", "key").size == 5


_REPLACEMENT_PUT_WORKER = """
import json
import sys
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver
from tests.unit.test_api_authentication import identity_provider
from tests.unit.test_api_object_mutations import _client, _put
fixture = identity_provider.__wrapped__()
identity = next(fixture)
try:
    with SQLCatalog(sys.argv[1]) as catalog:
        with _client(catalog, PosixDriver(sys.argv[2]), identity) as client:
            response = _put(client, request_id='replacement-after-session-loss')
            print(json.dumps({'status': response.status_code, 'body': response.json()}), flush=True)
finally:
    try:
        next(fixture)
    except StopIteration:
        pass
"""


def test_postgres_lost_delete_lock_cannot_finalize_over_replacement(
    postgres_dsn, tmp_path, identity_provider, monkeypatch,
):
    root = str(tmp_path / "hot")
    with SQLCatalog(postgres_dsn) as catalog:
        driver = PosixDriver(root)
        with _client(catalog, driver, identity_provider) as client:
            assert _put(client, ORIGINAL, request_id="seed").status_code == 201
            entered, release = threading.Event(), threading.Event()
            owner_pids = []
            remove = driver.delete_object_if_generation

            def capture_lock_owner(connection, _cursor, statement, *_args):
                if statement.startswith("SELECT pg_try_advisory_lock("):
                    owner_pids.append(connection.connection.driver_connection.info.backend_pid)

            def pause_after_delete(*args, **kwargs):
                result = remove(*args, **kwargs)
                assert result is True
                entered.set()
                assert release.wait(30), "DELETE was not released after session termination"
                return result

            sa.event.listen(catalog._legal_hold_engine, "after_cursor_execute", capture_lock_owner)
            monkeypatch.setattr(driver, "delete_object_if_generation", pause_after_delete)
            with ThreadPoolExecutor(max_workers=1) as worker:
                deleting = worker.submit(
                    client.delete, PATH, headers={"X-Request-ID": "lost-session-delete"},
                )
                try:
                    assert entered.wait(10)
                    assert len(owner_pids) == 1
                    with pytest.raises(FileNotFoundError):
                        driver.stat_object(BUCKET, KEY)
                    # Kill only the session retaining this API mutation lock.
                    # A fresh worker can then publish while stale DELETE is
                    # still paused immediately before catalog finalization.
                    with catalog.engine.begin() as connection:
                        assert connection.execute(
                            sa.text("SELECT pg_terminate_backend(:pid, 5000)"),
                            {"pid": owner_pids[0]},
                        ).scalar_one()
                    replacement = subprocess.run(
                        [sys.executable, "-c", _REPLACEMENT_PUT_WORKER, str(postgres_dsn), root],
                        capture_output=True, text=True, timeout=20, check=False,
                    )
                    assert replacement.returncode == 0, replacement.stdout + replacement.stderr
                    published = json.loads(replacement.stdout)
                    assert published["status"] == 201, published
                    replacement_record = catalog.get(BUCKET, KEY)
                    assert replacement_record.size == len(REPLACEMENT)
                    assert client.get(PATH).content == REPLACEMENT
                finally:
                    release.set()
                stale_response = deleting.result(timeout=10)
            _assert_error(stale_response, 503, "backend_unavailable", retryable=True)
            assert catalog.get(BUCKET, KEY) == replacement_record
            assert client.get(PATH).content == REPLACEMENT
            assert driver.get_object(BUCKET, KEY) == REPLACEMENT
            _assert_audit(catalog, "lost-session-delete", "delete_object", "failed")
