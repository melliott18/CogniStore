"""Deterministic #162 regression for a scan publishing after an API mutation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from hashlib import sha256
from multiprocessing import get_context
from pathlib import Path
from threading import Event

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.core.catalog import Catalog
from cognistore.core.scanner import scan_catalog
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver
from tests.unit.test_api_authentication import identity_provider as identity_provider
from tests.unit.test_api_authorization import authorization


@pytest.mark.parametrize("catalog_kind", [
    "memory", "sqlite", pytest.param("postgres", marks=pytest.mark.integration),
])
@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_scan_publication_cannot_supersede_a_completed_api_mutation(
    tmp_path: Path, request, identity_provider, monkeypatch, catalog_kind: str, method: str,
) -> None:
    """Pause after scan validation, then let authenticated API work settle.

    Safe outcomes are an explicit API conflict while the scan owns the key,
    or rejection of the stale scan observation. Old scan content must never
    reappear after a successful DELETE/replacement PUT.
    """
    authenticator, token, _ = identity_provider
    with ExitStack() as stack:
        if catalog_kind == "memory":
            catalog = scan_store = Catalog()
        else:
            database = (
                tmp_path / "catalog.db" if catalog_kind == "sqlite"
                else request.getfixturevalue("postgres_dsn")
            )
            catalog = stack.enter_context(SQLCatalog(database))
            scan_store = stack.enter_context(SQLCatalog(database, migrate=False))
        driver = PosixDriver(str(tmp_path / "hot"))
        client = stack.enter_context(TestClient(create_app(
            CogniStoreGateway(catalog, {"hot": driver}), authentication=authenticator,
            authorization=authorization("writer"),
        ), headers={"Authorization": "Bearer " + token()}))
        target = "/v1/objects/hot/bucket/item"
        original, replacement = b"original scan bytes", b"successful replacement has different bytes"
        assert client.put(target, content=original).status_code == 201
        entered, release = Event(), Event()
        publish = scan_store.upsert_scan_observation

        def pause_before_publication(*args, **kwargs):
            entered.set()
            if not release.wait(15):
                raise AssertionError("scan publication was not released")
            return publish(*args, **kwargs)

        monkeypatch.setattr(scan_store, "upsert_scan_observation", pause_before_publication)
        with ThreadPoolExecutor(max_workers=1) as executor:
            scan = executor.submit(
                scan_catalog, tier="hot", bucket="bucket", driver=driver, catalog=scan_store,
            )
            try:
                assert entered.wait(15)
                response = client.request(
                    method, target, content=replacement if method == "PUT" else None,
                )
                assert response.status_code in ((201, 409) if method == "PUT" else (204, 409))
            finally:
                release.set()
            scan.result(timeout=15)

        record = catalog.get("bucket", "item")
        if response.status_code == 409:
            assert response.json()["error"]["retryable"] is True
            assert record is not None and record.size == len(original)
            assert record.metadata["sha256"] == sha256(original).hexdigest()
            assert driver.get_object("bucket", "item") == original
            retried = client.request(
                method, target, content=replacement if method == "PUT" else None,
            )
            assert retried.status_code == (201 if method == "PUT" else 204)
            record = catalog.get("bucket", "item")
        if method == "PUT":
            assert record is not None and record.size == len(replacement)
            # A PUT does not calculate content identity, so stale scan identity
            # must be absent rather than silently describing overwritten bytes.
            assert record.metadata.get("sha256") != sha256(original).hexdigest()
            assert driver.get_object("bucket", "item") == replacement
            assert client.get(target).content == replacement
        else:
            assert record is None
            with pytest.raises(FileNotFoundError):
                driver.get_object("bucket", "item")


def _scan_process(database: str, storage: str, channel) -> None:
    """A separately connected worker paused after validation, before publish."""
    try:
        with SQLCatalog(database, migrate=False) as catalog:
            publish = catalog.upsert_scan_observation
            owner_pids = []

            def record_owner(connection, _cursor, statement, *_args):
                if statement.startswith("SELECT pg_try_advisory_lock("):
                    owner_pids.append(connection.connection.driver_connection.info.backend_pid)

            if catalog.backend == "postgresql":
                sa.event.listen(catalog._legal_hold_engine, "after_cursor_execute", record_owner)

            def paused(*args, **kwargs):
                channel.send(("ready", owner_pids[-1] if owner_pids else None))
                if not channel.poll(30) or channel.recv() != "release":
                    raise AssertionError("scan worker was not released")
                return publish(*args, **kwargs)

            catalog.upsert_scan_observation = paused
            observed = scan_catalog(
                tier="hot", bucket="bucket", driver=PosixDriver(storage), catalog=catalog,
            )
            channel.send(("published", len(observed)))
    except Exception as exc:
        channel.send(("failed", type(exc).__name__))
    finally:
        channel.close()


@pytest.mark.parametrize("catalog_kind,lose_session", [
    ("sqlite", False),
    pytest.param("postgres", False, marks=pytest.mark.integration),
    pytest.param("postgres", True, marks=pytest.mark.integration),
])
@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_scan_fence_survives_process_boundary_and_fails_closed_on_session_loss(
    tmp_path: Path, request, identity_provider, catalog_kind: str, lose_session: bool, method: str,
) -> None:
    authenticator, token, _ = identity_provider
    database = (
        tmp_path / "catalog.db" if catalog_kind == "sqlite"
        else request.getfixturevalue("postgres_dsn")
    )
    root = str(tmp_path / "hot")
    with SQLCatalog(database) as catalog:
        driver = PosixDriver(root)
        with TestClient(create_app(
            CogniStoreGateway(catalog, {"hot": driver}), authentication=authenticator,
            authorization=authorization("writer"),
        ), headers={"Authorization": "Bearer " + token()}) as client:
            target = "/v1/objects/hot/bucket/item"
            original, replacement = b"old scan", b"replacement after lock loss"
            assert client.put(target, content=original).status_code == 201
            context = get_context("spawn")
            parent, child = context.Pipe()
            process = context.Process(target=_scan_process, args=(str(database), root, child))
            process.start()
            child.close()
            released = False
            try:
                assert parent.poll(20), "scan process never reached publication"
                stage, pid = parent.recv()
                assert stage == "ready", (stage, pid)
                if lose_session:
                    assert pid is not None
                    # Terminate only this disposable worker's session. Its stale
                    # observation must not finalize through a fresh connection.
                    with catalog.engine.begin() as connection:
                        assert connection.execute(
                            sa.text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": pid},
                        ).scalar_one()
                response = client.request(
                    method, target, content=replacement if method == "PUT" else None,
                )
                assert response.status_code == (
                    (201 if method == "PUT" else 204) if lose_session else 409
                ), response.text
                settled = catalog.get("bucket", "item")
                parent.send("release")
                released = True
                assert parent.poll(20), "scan process never settled"
                stage, detail = parent.recv()
                if lose_session:
                    assert stage == "failed", (stage, detail)
                    assert catalog.get("bucket", "item") == settled
                    if method == "PUT":
                        assert settled is not None and settled.size == len(replacement)
                        assert client.get(target).content == replacement
                    else:
                        assert settled is None
                        assert client.get(target).status_code == 404
                else:
                    assert (stage, detail) == ("published", 1)
                    assert driver.get_object("bucket", "item") == original
            finally:
                if not released and process.is_alive():
                    parent.send("release")
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
                parent.close()
            assert process.exitcode == 0


@pytest.mark.parametrize("catalog_kind", [
    "sqlite", pytest.param("postgres", marks=pytest.mark.integration),
])
def test_scan_skips_key_while_api_owns_mutation_fence(
    tmp_path: Path, request, catalog_kind: str,
) -> None:
    database = (
        tmp_path / "catalog.db" if catalog_kind == "sqlite"
        else request.getfixturevalue("postgres_dsn")
    )
    with SQLCatalog(database) as catalog, SQLCatalog(database, migrate=False) as scanner:
        driver = PosixDriver(str(tmp_path / "hot"))
        driver.put_object("bucket", "item", b"initial")
        catalog.upsert("bucket", "item", 7, "hot")
        before = catalog.get("bucket", "item")
        with catalog.object_mutation("bucket", "item"):
            assert scan_catalog(tier="hot", bucket="bucket", driver=driver, catalog=scanner) == []
            assert scanner.get("bucket", "item") == before
        assert len(scan_catalog(tier="hot", bucket="bucket", driver=driver, catalog=scanner)) == 1
