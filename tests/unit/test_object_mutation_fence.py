"""Object mutation reservations span backend effects without holding SQL writes."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from cognistore.auth.tenancy import TenantIsolationError, tenant_context
from cognistore.core.catalog import Catalog, ObjectMutationConflictError
from cognistore.db import SQLCatalog


@pytest.fixture(params=["memory", "sqlite", "sqlite-memory"])
def catalog(request, tmp_path):
    if request.param == "memory":
        yield Catalog()
    else:
        locator = ":memory:" if request.param == "sqlite-memory" else tmp_path / "catalog.db"
        with SQLCatalog(locator) as result:
            yield result


def reserve(catalog, bucket="bucket", key="key"):
    with catalog.object_mutation(bucket, key):
        return True


def assert_same_key_conflicts_and_other_keys_progress(catalog, other):
    if os.name == "nt" and getattr(catalog, "backend", None) == "sqlite":
        pytest.skip("Windows SQLite serializes at the surrounding legal-hold fence")
    with ThreadPoolExecutor(max_workers=1) as worker:
        with catalog.object_mutation("bucket", "key"):
            # Conflict must be returned while the original request is still
            # inside its backend/catalog span, rather than waiting for it.
            conflict = worker.submit(reserve, other)
            with pytest.raises(ObjectMutationConflictError):
                conflict.result(timeout=5)
            assert worker.submit(reserve, other, "bucket", "other").result(timeout=5)
            assert worker.submit(reserve, other, "other", "key").result(timeout=5)
        assert worker.submit(reserve, other).result(timeout=5)


def test_same_key_conflicts_without_blocking_unrelated_objects(catalog):
    assert_same_key_conflicts_and_other_keys_progress(catalog, catalog)


def test_reservation_is_not_reentrant_and_failed_attempt_does_not_release_owner(catalog):
    with catalog.object_mutation("bucket", "key"):
        for _ in range(2):
            with pytest.raises(ObjectMutationConflictError):
                reserve(catalog)
    assert reserve(catalog)


def test_exception_releases_reservation(catalog):
    with pytest.raises(RuntimeError, match="backend failed"):
        with catalog.object_mutation("bucket", "key"):
            raise RuntimeError("backend failed")
    assert reserve(catalog)


def assert_tenants_and_catalog_reads_remain_independent(catalog):
    alice, bob = catalog.for_tenant("alice"), catalog.for_tenant("bob")
    with ThreadPoolExecutor(max_workers=1) as worker:
        with alice.object_mutation("bucket", "key"):
            assert worker.submit(reserve, bob).result(timeout=5)
            assert worker.submit(alice.get, "bucket", "key").result(timeout=5) is None
    with tenant_context("bob"):
        with pytest.raises(TenantIsolationError):
            reserve(alice)


def test_distinct_tenants_and_catalog_reads_remain_independent(catalog):
    assert_tenants_and_catalog_reads_remain_independent(catalog)


def test_nested_catalog_publication_keeps_legal_hold_guard_live(catalog):
    with catalog.destructive_operation("bucket", "key", operation="put_object"):
        with catalog.object_mutation("bucket", "key"):
            catalog.upsert("bucket", "key", size=7, tier="hot")
            assert catalog.get("bucket", "key").size == 7
            catalog.delete("bucket", "key")
            assert catalog.get("bucket", "key") is None


def assert_independent_catalog_handles(catalog, other):
    assert_same_key_conflicts_and_other_keys_progress(catalog, other)
    with catalog.object_mutation("bucket", "key"):
        with pytest.raises(ObjectMutationConflictError):
            reserve(other)
    assert reserve(other)
    with catalog.destructive_operation("bucket", "key", operation="put_object"):
        with catalog.object_mutation("bucket", "key"):
            # Catalog transactions and existing legal-hold guards remain
            # reentrant across handles even though API reservations are not.
            other.upsert("bucket", "key", size=7, tier="hot")
    assert other.get("bucket", "key").size == 7


def test_sqlite_independent_handles_share_reservations(tmp_path):
    path = tmp_path / "catalog.db"
    with SQLCatalog(path) as catalog, SQLCatalog(path) as other:
        assert_independent_catalog_handles(catalog, other)


_CHILD_ATTEMPT = """
import sys
from cognistore.core.catalog import ObjectMutationConflictError
from cognistore.db import SQLCatalog
with SQLCatalog(sys.argv[1]) as catalog:
    try:
        with catalog.object_mutation('bucket', 'key'):
            print('acquired', flush=True)
    except ObjectMutationConflictError:
        print('conflict', flush=True)
    with catalog.object_mutation('bucket', 'other'):
        catalog.upsert('bucket', 'other', size=5, tier='hot')
        print('unrelated published', flush=True)
"""


def assert_process_reservations(catalog, locator):
    if os.name == "nt" and getattr(catalog, "backend", None) == "sqlite":
        pytest.skip("Windows SQLite serializes at the surrounding legal-hold fence")

    def attempt():
        result = subprocess.run(
            [sys.executable, "-c", _CHILD_ATTEMPT, str(locator)],
            capture_output=True, text=True, timeout=20, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.splitlines()

    with catalog.destructive_operation("bucket", "key", operation="delete_object"):
        with catalog.object_mutation("bucket", "key"):
            assert attempt() == ["conflict", "unrelated published"]
            catalog.upsert("bucket", "key", size=7, tier="hot")
    assert attempt() == ["acquired", "unrelated published"]
    assert catalog.get("bucket", "other").size == 5


def test_sqlite_reservation_is_shared_by_separate_worker_processes(tmp_path):
    path = tmp_path / "catalog.db"
    with SQLCatalog(path) as catalog:
        assert_process_reservations(catalog, path)


def assert_process_exit_releases_reservation(catalog, locator):
    child_code = """
import os
import sys
from cognistore.db import SQLCatalog
catalog = SQLCatalog(sys.argv[1])
with catalog.destructive_operation('bucket', 'key', operation='put_object'):
    with catalog.object_mutation('bucket', 'key'):
        print('locked', flush=True)
        os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", child_code, str(locator)],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "locked"
    assert reserve(catalog)


def test_sqlite_worker_exit_releases_reservation(tmp_path):
    path = tmp_path / "catalog.db"
    with SQLCatalog(path) as catalog:
        assert_process_exit_releases_reservation(catalog, path)
