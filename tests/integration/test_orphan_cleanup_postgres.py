"""Optional PostgreSQL coverage for permanent cleanup state and alias fencing."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from cognistore.core.audit import AuditContext, AuditEventType, AuditQuery
from cognistore.core.consistency import ScanScope
from cognistore.core.orphan_cleanup import OrphanCleanup
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver

pytestmark = pytest.mark.integration

_GRACE = 60
_RETENTION = 60
_PAYLOAD = b"orphaned backend bytes"
_CLEANUP_EVENTS = frozenset({
    AuditEventType.ORPHAN_QUARANTINED, AuditEventType.ORPHAN_DELETE_STARTED,
    AuditEventType.ORPHAN_DELETE_FAILED, AuditEventType.ORPHAN_DELETED,
    AuditEventType.ORPHAN_INVALIDATED,
})


@dataclass
class Clock:
    instant: datetime

    def __call__(self) -> datetime:
        return self.instant


def _driver(tmp_path: Path, key: str, clock: Clock) -> PosixDriver:
    driver = PosixDriver(str(tmp_path / "hot"))
    driver.put_object("bucket", key, _PAYLOAD)
    modified = (clock.instant - timedelta(days=30)).timestamp()
    os.utime(driver.base / "bucket" / key, (modified, modified))
    return driver


def _run(catalog, driver, clock, key, **options):
    service = OrphanCleanup(
        catalog, {"hot": driver}, ScanScope("default", "bucket", "", ("hot",)),
        binding_id="postgres-cleanup-test", clock=clock,
    )
    return service.run(
        key, "hot", grace_period_seconds=_GRACE, retention_seconds=_RETENTION, **options,
    )


def test_postgres_cleanup_reopens_retries_and_retains_audit_forever(
    postgres_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "orphan.bin"
    clock = Clock(datetime.now(timezone.utc).replace(microsecond=0))
    driver = _driver(tmp_path, key, clock)
    context = AuditContext(str(uuid4()), "user", "operator")
    with SQLCatalog(postgres_dsn) as catalog:
        quarantined = _run(catalog, driver, clock, key, stage="quarantine", context=context)
        assert quarantined["status"] == "quarantined"
        candidate_id = quarantined["candidate_id"]

    with SQLCatalog(postgres_dsn) as reopened:
        waiting = _run(
            reopened, driver, clock, key, stage="execute", candidate_id=candidate_id, context=context,
        )
        assert waiting["status"] == "blocked"
        assert "grace_period" in waiting["blockers"]
        assert reopened.list_legal_holds(active_only=True) == []
        clock.instant += timedelta(seconds=_GRACE)

        def fail_delete(*args, **kwargs):
            raise OSError("temporary backend failure")

        with monkeypatch.context() as patch:
            patch.setattr(driver, "delete_object_if_generation", fail_delete)
            failed = _run(
                reopened, driver, clock, key, stage="execute", candidate_id=candidate_id,
                context=context,
            )
        assert failed["status"] == "failed"
        assert failed["retryable"] is True
        assert driver.get_object("bucket", key) == _PAYLOAD

    with SQLCatalog(postgres_dsn) as recovered:
        query = AuditQuery(correlation_id=candidate_id, event_types=_CLEANUP_EVENTS)
        assert {event.event_type for event in recovered.list_audit_events(query)} == {
            AuditEventType.ORPHAN_QUARANTINED, AuditEventType.ORPHAN_DELETE_STARTED,
            AuditEventType.ORPHAN_DELETE_FAILED,
        }
        deleted = _run(
            recovered, driver, clock, key, stage="execute", candidate_id=candidate_id,
            context=context,
        )
        assert deleted["status"] == "deleted"
        assert list(driver.list_objects("bucket")) == []
        events = recovered.list_audit_events(query)
        assert len(events) == 5
        assert all(event.expires_at is None for event in events)
        future = clock.instant + timedelta(days=365)
        recovered.prune_expired_audit_events(future)
        recovered.prune_audit_events(future)
        assert recovered.list_audit_events(query) == events
        assert recovered.verify_audit_integrity().valid


def test_postgres_alias_reference_add_remove_invalidates_persisted_quarantine(
    postgres_dsn: str, tmp_path: Path,
) -> None:
    key = "café.bin"
    clock = Clock(datetime.now(timezone.utc).replace(microsecond=0))
    driver = _driver(tmp_path, key, clock)
    context = AuditContext(str(uuid4()), "user", "operator")
    with SQLCatalog(postgres_dsn) as catalog:
        revision = catalog.orphan_cleanup_revision("bucket", key)
        quarantined = _run(catalog, driver, clock, key, stage="quarantine", context=context)
        assert quarantined["status"] == "quarantined"
        candidate_id = quarantined["candidate_id"]

    # Case and Unicode aliases share cleanup protection even when PostgreSQL
    # preserves their exact spelling as distinct catalog coordinates.
    with SQLCatalog(postgres_dsn) as writer:
        writer.upsert("BUCKET", "CAFE\u0301.BIN", len(_PAYLOAD), tier="hot")
        writer.delete("BUCKET", "CAFE\u0301.BIN")
        assert writer.orphan_cleanup_references("bucket", key) == []
        assert writer.orphan_cleanup_revision("bucket", key) > revision

    clock.instant += timedelta(seconds=_GRACE)
    with SQLCatalog(postgres_dsn) as reopened:
        result = _run(
            reopened, driver, clock, key, stage="execute", candidate_id=candidate_id, context=context,
        )
        assert result["status"] == "invalidated"
        assert "observation_changed" in result["blockers"]
        assert driver.get_object("bucket", key) == _PAYLOAD
        events = reopened.list_audit_events(AuditQuery(correlation_id=candidate_id))
        assert {event.event_type for event in events} == {
            AuditEventType.ORPHAN_QUARANTINED, AuditEventType.ORPHAN_INVALIDATED,
        }
        assert reopened.verify_audit_integrity().valid
