from __future__ import annotations

import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from cognistore.core.audit import (
    ORPHAN_CLEANUP_EVENT_TYPES,
    AuditContext,
    AuditEvent,
    AuditRetentionPolicy,
)
from cognistore.core.catalog import Catalog
from cognistore.core.move_jobs import MoveJobState
from cognistore.db import SQLCatalog


@pytest.fixture(params=("memory", "sqlite"))
def catalogs(request, tmp_path):
    retention = AuditRetentionPolicy(max_age_seconds=1)
    if request.param == "memory":
        catalog = Catalog(audit_retention=retention)
        yield catalog, catalog
    else:
        path = tmp_path / "catalog.db"
        with SQLCatalog(path, audit_retention=retention) as catalog, SQLCatalog(path) as other:
            yield catalog, other


def claim(catalog):
    return catalog.claim_move_job(
        "cleanup-race", src_tier="hot", dst_tier="cold", bucket="bucket", key="key",
        expected_size=4, source_metadata={}, owner_id="worker",
        now="2026-01-01T00:00:00Z", lease_expires_at="2026-01-01T01:00:00Z",
    )


def test_orphan_journal_survives_expiry_and_explicit_retention(catalogs):
    catalog, _ = catalogs
    context = AuditContext(str(uuid4()), "user", "operator")
    events = [catalog.append_audit_event(AuditEvent.create(
        event_type, "succeeded", context, occurred_at="2020-01-01T00:00:00Z",
    )) for event_type in sorted(ORPHAN_CLEANUP_EVENT_TYPES)]
    assert all(event.expires_at is None for event in events)
    assert catalog.prune_expired_audit_events("2030-01-01T00:00:00Z") == 0
    assert catalog.prune_audit_events("2030-01-01T00:00:00Z") == 0
    assert all(catalog.get_audit_event(event.event_id) == event for event in events)
    assert catalog.verify_audit_integrity().valid


def test_cleanup_reference_lookup_overprotects_bucket_and_path_aliases(catalogs):
    catalog, _ = catalogs
    catalog.upsert("BUCKET", "Café/dir/../FILE", 4, "hot")
    catalog.upsert("bucket", "other", 4, "hot")
    with catalog.orphan_cleanup_fence():
        records = catalog.orphan_cleanup_references("bucket", "cafe\u0301/file")
    assert [(row.bucket, row.key) for row in records] == [("BUCKET", "Café/dir/../FILE")]


def test_cleanup_revision_survives_reference_creation_and_removal(catalogs):
    catalog, other = catalogs
    initial = catalog.orphan_cleanup_revision("bucket", "key")
    other.upsert("BUCKET", "KEY", 4, "hot")
    referenced = catalog.orphan_cleanup_revision("bucket", "key")
    other.delete("BUCKET", "KEY")
    assert initial < referenced < catalog.orphan_cleanup_revision("bucket", "key")
    assert catalog.orphan_cleanup_references("bucket", "key") == []


def test_cleanup_target_revision_ignores_unrelated_objects(catalogs):
    catalog, other = catalogs
    other.upsert("bucket", "key", 4, "hot")
    other.delete("bucket", "key")
    target = catalog.orphan_cleanup_revision("bucket", "key")
    total = catalog.orphan_cleanup_revision()
    other.upsert("bucket", "unrelated", 4, "hot")
    other.delete("bucket", "unrelated")
    other.upsert("other-bucket", "key", 4, "hot")
    assert catalog.orphan_cleanup_revision("bucket", "key") == target
    assert catalog.orphan_cleanup_revision() > total


def test_cleanup_target_revision_includes_unicode_and_path_aliases(catalogs):
    catalog, other = catalogs
    initial = catalog.orphan_cleanup_revision("bucket", "cafe\u0301/file")
    other.upsert("BUCKET", "Café/dir/../FILE", 4, "hot")
    other.delete("BUCKET", "Café/dir/../FILE")
    assert catalog.orphan_cleanup_revision("bucket", "cafe\u0301/file") > initial


@pytest.mark.parametrize("scope", [("bucket", None), (None, "key"), ("", "key")])
def test_cleanup_revision_rejects_partial_or_empty_scope(catalogs, scope):
    catalog, _ = catalogs
    with pytest.raises(ValueError):
        catalog.orphan_cleanup_revision(*scope)


def test_sqlite_cleanup_revision_survives_restart(tmp_path):
    path = tmp_path / "catalog.db"
    with SQLCatalog(path) as catalog:
        catalog.upsert("bucket", "key", 4, "hot")
        catalog.delete("bucket", "key")
        revision = catalog.orphan_cleanup_revision("bucket", "key")
    with SQLCatalog(path) as catalog:
        assert catalog.orphan_cleanup_revision("BUCKET", "KEY") == revision > 0


@pytest.mark.parametrize("mutation", ["reference", "claim", "transition"])
def test_cleanup_fence_serializes_reference_and_job_writers(catalogs, mutation):
    catalog, other = catalogs
    if mutation == "transition":
        claim(catalog)
    started = threading.Event()

    def mutate():
        started.set()
        if mutation == "reference":
            return other.upsert("bucket", "key", 4, "hot")
        if mutation == "claim":
            return claim(other)
        return other.transition_move_job(
            "cleanup-race", expected_state=MoveJobState.PREPARED,
            to_state=MoveJobState.FAILED, owner_id="worker", reason="failure",
            now="2026-01-01T00:00:01Z", lease_expires_at=None,
        )

    with ThreadPoolExecutor(max_workers=1) as workers:
        with catalog.orphan_cleanup_fence():
            future = workers.submit(mutate)
            assert started.wait(5)
            assert not future.done()
        future.result(timeout=10)


def test_cleanup_fence_allows_move_lease_heartbeat(catalogs):
    catalog, other = catalogs
    claim(catalog)
    with ThreadPoolExecutor(max_workers=1) as workers:
        with catalog.orphan_cleanup_fence():
            future = workers.submit(
                other.renew_move_job_lease, "cleanup-race", owner_id="worker",
                expected_state=MoveJobState.PREPARED, now="2026-01-01T00:00:01Z",
                lease_expires_at="2026-01-01T02:00:00Z",
            )
            assert future.result(timeout=10).state == MoveJobState.PREPARED


def test_cleanup_waits_for_scanner_reference_publication(catalogs, tmp_path, monkeypatch):
    from cognistore.core.scanner import scan_catalog
    from cognistore.drivers.posix_driver import PosixDriver

    catalog, other = catalogs
    driver = PosixDriver(str(tmp_path / "storage"))
    driver.put_object("bucket", "key", b"data")
    publishing, publish, cleanup_started = (threading.Event() for _ in range(3))
    original = catalog.upsert_scan_observation

    def paused_publish(*args, **kwargs):
        publishing.set()
        assert publish.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(catalog, "upsert_scan_observation", paused_publish)

    def inspect_cleanup():
        cleanup_started.set()
        with other.orphan_cleanup_fence():
            return other.orphan_cleanup_references("bucket", "key")

    with ThreadPoolExecutor(max_workers=2) as workers:
        scan = workers.submit(
            scan_catalog, tier="hot", bucket="bucket", driver=driver, catalog=catalog,
        )
        try:
            assert publishing.wait(10)
            cleanup = workers.submit(inspect_cleanup)
            assert cleanup_started.wait(5)
            assert not cleanup.done()
        finally:
            publish.set()
        assert len(scan.result(timeout=10)) == 1
        assert len(cleanup.result(timeout=10)) == 1


def test_sqlite_cleanup_fences_move_claim_in_another_process(tmp_path):
    path = tmp_path / "catalog.db"
    code = """
import sys
from cognistore.db import SQLCatalog
with SQLCatalog(sys.argv[1]) as catalog:
    print('ready', flush=True)
    sys.stdin.readline()
    print('claiming', flush=True)
    catalog.claim_move_job(
        'claim', src_tier='hot', dst_tier='cold', bucket='bucket', key='key',
        expected_size=4, source_metadata={}, owner_id='worker',
        now='2026-01-01T00:00:00Z', lease_expires_at='2026-01-01T01:00:00Z',
    )
    print('claimed', flush=True)
"""
    with SQLCatalog(path) as catalog:
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(path)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            assert process.stdout.readline().strip() == "ready"
            with ThreadPoolExecutor(max_workers=1) as workers:
                with catalog.orphan_cleanup_fence():
                    process.stdin.write("go\n")
                    process.stdin.flush()
                    assert process.stdout.readline().strip() == "claiming"
                    future = workers.submit(process.stdout.readline)
                    assert not future.done()
                assert future.result(timeout=10).strip() == "claimed"
            _, error = process.communicate(timeout=10)
            assert process.returncode == 0, error
            assert catalog.get_move_job("claim") is not None
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
