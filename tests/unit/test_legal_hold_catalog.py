from __future__ import annotations

import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from cognistore.core.catalog import Catalog
from cognistore.core.legal_holds import LegalHoldError
from cognistore.db import MigrationManager, SQLCatalog
from tests.conformance.legal_holds import LegalHoldConformance, actor


class TestMemoryLegalHolds(LegalHoldConformance):
    @pytest.fixture
    def catalog(self):
        return Catalog()


class TestSQLiteLegalHolds(LegalHoldConformance):
    @pytest.fixture
    def catalog(self, tmp_path):
        with SQLCatalog(tmp_path / "catalog.db") as catalog:
            yield catalog


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_hold_and_audit_changes_are_atomic_on_audit_failure(backend, tmp_path, monkeypatch):
    catalog = Catalog() if backend == "memory" else SQLCatalog(tmp_path / "catalog.db")
    target = "append_audit_event" if backend == "memory" else "_insert_audit_event"
    original = getattr(catalog, target)

    def reject(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    try:
        monkeypatch.setattr(catalog, target, reject)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            catalog.place_legal_hold("bucket", key="key", reason="preserve", context=actor())
        assert catalog.list_legal_holds() == []
        monkeypatch.setattr(catalog, target, original)
        catalog.upsert("bucket", "key", size=4, tier="hot")
        hold = catalog.place_legal_hold("bucket", key="key", reason="preserve", context=actor())
        monkeypatch.setattr(catalog, target, reject)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            catalog.release_legal_hold(hold.hold_id, reason="done", context=actor())
        assert catalog.list_legal_holds(active_only=True) == [hold]
        with pytest.raises(RuntimeError, match="audit unavailable"):
            catalog.delete("bucket", "key")
        assert catalog.get("bucket", "key").size == 4
    finally:
        if isinstance(catalog, SQLCatalog):
            catalog.close()


def test_sqlite_restart_retains_hold_and_refuses_lossy_downgrade(tmp_path):
    path = tmp_path / "catalog.db"
    with SQLCatalog(path) as catalog:
        hold = catalog.place_legal_hold("bucket", prefix="case/", reason="preserve", context=actor())
    with SQLCatalog(path) as catalog:
        assert catalog.list_legal_holds() == [hold]
        with pytest.raises(LegalHoldError):
            catalog.delete("bucket", "case/file")
        catalog.release_legal_hold(hold.hold_id, reason="done", context=actor())
        with pytest.raises(RuntimeError, match="legal hold history"):
            MigrationManager().downgrade(catalog.engine, "0012_tenant_ownership")
        assert len(catalog.list_legal_holds()) == 1


def assert_serialized_pair(catalog, other):
    """Hold placement waits for physical effects and nested catalog commits."""
    started = threading.Event()

    def place():
        started.set()
        return other.place_legal_hold("bucket", key="key", reason="preserve", context=actor())

    with ThreadPoolExecutor(max_workers=1) as worker:
        with catalog.destructive_operation("bucket", "key", operation="overwrite"):
            future = worker.submit(place)
            assert started.wait(5)
            assert not future.done()
            # Reentrant, including across independent handles to one database.
            other.upsert("bucket", "key", size=4, tier="hot")
            assert not future.done()
        hold = future.result(timeout=10)
    assert catalog.list_legal_holds(active_only=True) == [hold]
    with pytest.raises(LegalHoldError):
        catalog.delete("bucket", "key")
    assert catalog.get("bucket", "key").size == 4


def test_memory_serializes_hold_and_destructive_operation():
    catalog = Catalog()
    assert_serialized_pair(catalog, catalog)


def test_sqlite_serializes_independent_catalog_handles(tmp_path):
    path = tmp_path / "catalog.db"
    with SQLCatalog(path) as catalog, SQLCatalog(path) as other:
        assert_serialized_pair(catalog, other)


def test_sqlite_fence_is_cross_process_and_keeps_nested_writes_live(tmp_path):
    path = tmp_path / "catalog.db"
    with SQLCatalog(path) as catalog:
        assert_process_fence(catalog, path)


def assert_process_fence(catalog, locator):
    child_code = """
import sys
from cognistore.db import SQLCatalog
with SQLCatalog(sys.argv[1]) as catalog:
    with catalog.destructive_operation('bucket', 'key', operation='overwrite'):
        print('locked', flush=True)
        sys.stdin.readline()
        catalog.upsert('bucket', 'key', size=4, tier='hot')
"""
    process = subprocess.Popen(
        [sys.executable, "-c", child_code, str(locator)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        with ThreadPoolExecutor(max_workers=1) as worker:
            started = threading.Event()

            def place():
                started.set()
                return catalog.place_legal_hold("bucket", reason="preserve", context=actor())

            future = worker.submit(place)
            assert started.wait(5)
            assert not future.done()
            output, error = process.communicate("continue\n", timeout=15)
            assert process.returncode == 0, output + error
            hold = future.result(timeout=10)
        assert catalog.list_legal_holds(active_only=True) == [hold]
        with pytest.raises(LegalHoldError):
            catalog.delete("bucket", "key")
        assert catalog.get("bucket", "key").size == 4
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
