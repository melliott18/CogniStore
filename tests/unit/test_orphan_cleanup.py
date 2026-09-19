"""Safety and recovery contracts for explicit, generation-bound reclamation."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa

from cognistore.core.audit import AuditContext, AuditEventType, AuditQuery
from cognistore.core.catalog import Catalog, CatalogStore
from cognistore.core.consistency import ScanScope
from cognistore.core.content_identity import ContentIdentityBuilder, ObjectContent
from cognistore.core.orphan_cleanup import OrphanCleanup
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.db.schema import content_blobs
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.drivers.tenancy import TenantStorageDriver

_KEY = "unreferenced.bin"
_PAYLOAD = b"unreferenced bytes"
_GRACE = 60
_RETENTION = 60


@dataclass
class Clock:
    instant: datetime

    def __call__(self) -> datetime:
        return self.instant

    def advance(self, seconds: float) -> None:
        self.instant += timedelta(seconds=seconds)


@pytest.fixture(params=("memory", "sqlite"))
def catalog(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[CatalogStore]:
    if request.param == "memory":
        yield Catalog()
    else:
        with SQLiteCatalog(tmp_path / "catalog.sqlite3") as store:
            yield store


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime.now(timezone.utc).replace(microsecond=0))


@pytest.fixture
def driver(tmp_path: Path, clock: Clock) -> PosixDriver:
    backend = PosixDriver(str(tmp_path / "hot"))
    _put_old(backend, _KEY, _PAYLOAD, clock)
    return backend


@pytest.fixture
def context() -> AuditContext:
    return AuditContext(str(uuid4()), "user", "operator")


def _put_old(driver: PosixDriver, key: str, payload: bytes, clock: Clock) -> None:
    driver.put_object("bucket", key, payload)
    modified = (clock.instant - timedelta(days=30)).timestamp()
    os.utime(driver.base / "bucket" / key, (modified, modified))


def _service(catalog: CatalogStore, driver: PosixDriver, clock: Clock) -> OrphanCleanup:
    return OrphanCleanup(
        catalog, {"hot": driver}, ScanScope("default", "bucket", "", ("hot",)),
        binding_id="trusted-test-binding", clock=clock,
    )


def _run(service: OrphanCleanup, *, key: str = _KEY, **options):
    return service.run(
        key, "hot", grace_period_seconds=_GRACE, retention_seconds=_RETENTION, **options,
    )


def _quarantine(service: OrphanCleanup, context: AuditContext, *, key: str = _KEY) -> str:
    result = _run(service, key=key, stage="quarantine", context=context)
    assert result["status"] == "quarantined", result
    assert result["dry_run"] is False
    assert isinstance(result["candidate_id"], str)
    return result["candidate_id"]


def _publish_content(catalog: CatalogStore, key: str, content: ObjectContent) -> None:
    assert catalog.upsert_scan_observation(
        "bucket", key, size=content.size, tier="hot", generation=f"hot:{key}",
        metadata={}, fence=catalog.capture_scan_fence("bucket", key), content=content,
    )


def test_unrelated_catalog_activity_preserves_candidate(catalog, driver, clock, context):
    service = _service(catalog, driver, clock)
    candidate = _quarantine(service, context)
    catalog.upsert("bucket", "unrelated", size=1, tier="hot")
    catalog.delete("bucket", "unrelated")
    clock.advance(_GRACE)
    assert _run(service, stage="execute", candidate_id=candidate, context=context)["status"] == "deleted"


def test_slow_inspection_does_not_consume_quarantine_grace(
    catalog, driver, clock, context, monkeypatch,
):
    service = _service(catalog, driver, clock)
    original = driver.stat_object

    def slow_stat(*args, **kwargs):
        clock.advance(_GRACE * 2)
        return original(*args, **kwargs)

    monkeypatch.setattr(driver, "stat_object", slow_stat)
    candidate = _quarantine(service, context)
    monkeypatch.setattr(driver, "stat_object", original)
    assert _run(service, stage="execute", candidate_id=candidate, context=context)["status"] == "blocked"
    clock.advance(_GRACE)
    assert _run(service, stage="execute", candidate_id=candidate, context=context)["status"] == "deleted"


def test_default_report_is_read_only_and_explains_eligibility(catalog, driver, clock, monkeypatch):
    service = _service(catalog, driver, clock)
    before = driver.stat_object("bucket", _KEY)

    def mutation(*args, **kwargs):
        pytest.fail("report attempted a catalog or storage mutation")

    monkeypatch.setattr(catalog, "append_audit_event", mutation)
    monkeypatch.setattr(driver, "delete_object", mutation)
    monkeypatch.setattr(driver, "delete_object_if_generation", mutation)
    result = _run(service)
    assert result["dry_run"] is True
    assert result["stage"] == "report"
    assert result["status"] == "eligible"
    assert result["blockers"] == []
    assert set(result["conditions"]) == {
        "tenant", "audit", "catalog", "cas", "jobs", "legal_holds", "backend", "retention", "quarantine",
    }
    assert result["conditions"]["tenant"]["resolved"] is True
    assert result["conditions"]["catalog"]["clear"] is True
    assert result["conditions"]["retention"]["satisfied"] is True
    assert result["candidate_id"] is None
    assert catalog.list_audit_events() == []
    assert catalog.get("bucket", _KEY) is None
    assert driver.stat_object("bucket", _KEY) == before


def test_quarantine_requires_full_grace_then_deletes_exactly_once(catalog, driver, clock, context):
    service = _service(catalog, driver, clock)
    candidate_id = _quarantine(service, context)
    assert driver.get_object("bucket", _KEY) == _PAYLOAD
    clock.advance(_GRACE - 0.001)
    waiting = _run(service, stage="execute", candidate_id=candidate_id, context=context)
    assert waiting["status"] == "blocked"
    assert "grace_period" in waiting["blockers"]
    assert driver.get_object("bucket", _KEY) == _PAYLOAD

    clock.advance(0.001)
    deleted = _run(service, stage="execute", candidate_id=candidate_id, context=context)
    assert deleted["status"] == "deleted", deleted
    assert list(driver.list_objects("bucket")) == []
    repeated = _run(service, stage="execute", candidate_id=candidate_id, context=context)
    assert repeated["status"] == "already_deleted"

    events = catalog.list_audit_events(AuditQuery(correlation_id=candidate_id))
    assert {event.event_type for event in events} == {
        AuditEventType.ORPHAN_QUARANTINED, AuditEventType.ORPHAN_DELETE_STARTED,
        AuditEventType.ORPHAN_DELETED,
    }
    assert len(events) == 3
    assert {event.actor_id for event in events} == {context.actor_id}
    assert {event.object_key for event in events} == {_KEY}
    assert all(event.expires_at is None for event in events)
    by_type = {event.event_type: event for event in events}
    assert by_type[AuditEventType.ORPHAN_DELETED].causation_id == by_type[AuditEventType.ORPHAN_DELETE_STARTED].event_id


def test_execute_without_quarantine_never_deletes(catalog, driver, clock, context):
    service = _service(catalog, driver, clock)
    with pytest.raises(ValueError):
        _run(service, stage="execute", context=context)
    assert driver.get_object("bucket", _KEY) == _PAYLOAD
    assert catalog.list_audit_events() == []


@pytest.mark.parametrize("field", ["grace_period_seconds", "retention_seconds"])
@pytest.mark.parametrize("seconds", [0, 1e-12])
def test_cleanup_rejects_zero_or_unrepresentable_wait(driver, clock, context, field, seconds):
    catalog = Catalog()
    with pytest.raises(ValueError, match=field):
        _service(catalog, driver, clock).run(
            _KEY, "hot", stage="quarantine", context=context, **{field: seconds},
        )
    assert catalog.list_audit_events() == []
    assert driver.get_object("bucket", _KEY) == _PAYLOAD


@pytest.mark.parametrize("key", [".cognistore-tenants/private", ".COGNISTORE-TENANTS/private"])
def test_reserved_tenant_directory_alias_is_rejected(driver, clock, key):
    with pytest.raises(ValueError, match="relative object key"):
        _run(_service(Catalog(), driver, clock), key=key)


def test_invalid_audit_chain_blocks_report_and_execution(catalog, driver, clock, context, monkeypatch):
    service = _service(catalog, driver, clock)
    candidate_id = _quarantine(service, context)
    clock.advance(_GRACE)
    invalid = replace(catalog.verify_audit_integrity(), valid=False)
    monkeypatch.setattr(catalog, "verify_audit_integrity", lambda: invalid)
    result = _run(service)
    assert result["status"] == "blocked"
    assert "audit_integrity" in result["blockers"]
    with pytest.raises(ValueError, match="audit chain"):
        _run(service, stage="execute", candidate_id=candidate_id, context=context)
    assert driver.get_object("bucket", _KEY) == _PAYLOAD


def test_retention_uses_backend_age_before_quarantine(catalog, driver, clock, context):
    modified = clock.instant.timestamp()
    os.utime(driver.base / "bucket" / _KEY, (modified, modified))
    service = _service(catalog, driver, clock)
    result = _run(service, stage="quarantine", context=context)
    assert result["status"] == "blocked"
    assert "retention_window" in result["blockers"]
    assert catalog.list_audit_events() == []
    clock.advance(_RETENTION)
    candidate_id = _quarantine(service, context)
    assert _run(service, stage="execute", candidate_id=candidate_id, context=context)["status"] == "blocked"
    assert driver.get_object("bucket", _KEY) == _PAYLOAD


@pytest.mark.parametrize("protection", ["catalog", "move", "hold"])
def test_each_protection_blocks_quarantine(catalog, driver, clock, context, protection):
    if protection == "catalog":
        catalog.upsert("bucket", _KEY, len(_PAYLOAD), tier="hot")
    elif protection == "move":
        catalog.claim_move_job(
            "active-move", src_tier="hot", dst_tier="cold", bucket="bucket", key=_KEY,
            expected_size=len(_PAYLOAD), source_metadata={}, owner_id="worker",
            now=clock.instant.isoformat(),
            lease_expires_at=(clock.instant + timedelta(hours=1)).isoformat(),
        )
    else:
        catalog.place_legal_hold("bucket", key=_KEY, reason="investigation", context=context)
    before = catalog.list_audit_events()
    result = _run(_service(catalog, driver, clock), stage="quarantine", context=context)
    assert result["status"] == "blocked"
    assert {"catalog": "catalog_reference", "move": "active_job", "hold": "legal_hold"}[protection] in result["blockers"]
    assert catalog.list_audit_events() == before
    assert driver.get_object("bucket", _KEY) == _PAYLOAD


@pytest.mark.parametrize("reference", ["object", "chunk"])
def test_cas_references_prevent_reclamation(catalog, driver, clock, context, reference):
    payload = b"aaaabbbb"
    content = ContentIdentityBuilder(chunk_size=4).build(BytesIO(payload), expected_size=len(payload))
    _publish_content(catalog, "live-object", content)
    key = content.cas_key if reference == "object" else content.chunks[0].cas_key
    _put_old(driver, key, payload if reference == "object" else payload[:4], clock)
    result = _run(_service(catalog, driver, clock), key=key, stage="quarantine", context=context)
    assert result["status"] == "blocked"
    assert "cas_reference" in result["blockers"]
    assert catalog.get("bucket", key) is None
    assert driver.get_object("bucket", key)


def test_corrupt_cas_counter_is_not_evidence_of_absence(catalog, driver, clock, context):
    content = ContentIdentityBuilder(chunk_size=4).build(BytesIO(b"aaaabbbb"), expected_size=8)
    _publish_content(catalog, "live-object", content)
    if isinstance(catalog, SQLiteCatalog):
        with catalog.engine.begin() as connection:
            connection.execute(sa.update(content_blobs).where(
                content_blobs.c.sha256 == content.sha256,
            ).values(reference_count=0))
    else:
        catalog._content_blobs[content.sha256].reference_count = 0
    _put_old(driver, content.cas_key, b"aaaabbbb", clock)
    result = _run(_service(catalog, driver, clock), key=content.cas_key, stage="quarantine", context=context)
    assert result["status"] == "blocked"
    assert "cas_inconsistent" in result["blockers"]
    assert driver.get_object("bucket", content.cas_key) == b"aaaabbbb"


@pytest.mark.parametrize("protection", ["catalog", "hold"])
def test_new_protection_after_quarantine_invalidates_candidate(catalog, driver, clock, context, protection):
    service = _service(catalog, driver, clock)
    candidate_id = _quarantine(service, context)
    if protection == "catalog":
        catalog.upsert("bucket", _KEY, len(_PAYLOAD), tier="hot")
    else:
        catalog.place_legal_hold("bucket", key=_KEY, reason="investigation", context=context)
    clock.advance(_GRACE)
    result = _run(service, stage="execute", candidate_id=candidate_id, context=context)
    assert result["status"] == "invalidated"
    assert result["blockers"]
    assert driver.get_object("bucket", _KEY) == _PAYLOAD


def test_generation_replacement_invalidates_quarantine(catalog, driver, clock, context):
    service = _service(catalog, driver, clock)
    candidate_id = _quarantine(service, context)
    _put_old(driver, _KEY, b"replacement", clock)
    clock.advance(_GRACE)
    result = _run(service, stage="execute", candidate_id=candidate_id, context=context)
    assert result["status"] == "invalidated"
    assert driver.get_object("bucket", _KEY) == b"replacement"
    assert any(event.event_type == AuditEventType.ORPHAN_INVALIDATED for event in catalog.list_audit_events())


def test_conditional_delete_rejects_replacement_after_final_stat(catalog, driver, clock, context, monkeypatch):
    service = _service(catalog, driver, clock)
    candidate_id = _quarantine(service, context)
    clock.advance(_GRACE)
    original = driver.delete_object_if_generation

    def replace_before_delete(bucket, key, generation):
        driver.put_object(bucket, key, b"newly published bytes")
        return original(bucket, key, generation)

    monkeypatch.setattr(driver, "delete_object_if_generation", replace_before_delete)
    result = _run(service, stage="execute", candidate_id=candidate_id, context=context)
    assert result["status"] == "invalidated"
    assert driver.get_object("bucket", _KEY) == b"newly published bytes"


def test_reference_created_then_removed_requires_new_quarantine(catalog, driver, clock, context):
    service = _service(catalog, driver, clock)
    candidate_id = _quarantine(service, context)
    catalog.upsert("bucket", _KEY, len(_PAYLOAD), tier="hot")
    catalog.delete("bucket", _KEY)
    clock.advance(_GRACE)
    result = _run(service, stage="execute", candidate_id=candidate_id, context=context)
    assert result["status"] == "invalidated"
    assert driver.get_object("bucket", _KEY) == _PAYLOAD


@pytest.mark.parametrize("metadata", [
    {"generation": None}, {"size": -1}, {"mtime": None}, {"mtime": float("nan")},
])
def test_unproven_backend_metadata_blocks_cleanup(catalog, driver, clock, context, monkeypatch, metadata):
    original = driver.stat_object
    monkeypatch.setattr(driver, "stat_object", lambda *args: {**original(*args), **metadata})
    result = _run(_service(catalog, driver, clock), stage="quarantine", context=context)
    assert result["status"] == "blocked"
    assert "backend_error" in result["blockers"]
    assert catalog.list_audit_events() == []
    assert driver.get_object("bucket", _KEY) == _PAYLOAD


def test_no_unconditional_delete_fallback(catalog, driver, clock, context, monkeypatch):
    monkeypatch.setattr(driver, "capabilities", replace(driver.capabilities, conditional_delete=False))
    result = _run(_service(catalog, driver, clock), stage="quarantine", context=context)
    assert result["status"] == "blocked"
    assert "conditional_delete_unavailable" in result["blockers"]
    assert catalog.list_audit_events() == []
    assert driver.get_object("bucket", _KEY) == _PAYLOAD


def test_changed_binding_cannot_execute_existing_quarantine(catalog, driver, clock, context):
    candidate_id = _quarantine(_service(catalog, driver, clock), context)
    clock.advance(_GRACE)
    changed = OrphanCleanup(
        catalog, {"hot": driver}, ScanScope("default", "bucket", "", ("hot",)),
        binding_id="replacement-trusted-binding", clock=clock,
    )
    with pytest.raises(ValueError, match="quarantine"):
        _run(changed, stage="execute", candidate_id=candidate_id, context=context)
    assert driver.get_object("bucket", _KEY) == _PAYLOAD


def test_deletion_failure_is_visible_and_retryable(catalog, driver, clock, context, monkeypatch):
    service = _service(catalog, driver, clock)
    candidate_id = _quarantine(service, context)
    clock.advance(_GRACE)
    original = driver.delete_object_if_generation

    def fail(*args, **kwargs):
        raise OSError("private backend failure detail")

    monkeypatch.setattr(driver, "delete_object_if_generation", fail)
    result = _run(service, stage="execute", candidate_id=candidate_id, context=context)
    assert result["status"] == "failed"
    assert driver.get_object("bucket", _KEY) == _PAYLOAD
    events = catalog.list_audit_events()
    assert any(event.event_type == AuditEventType.ORPHAN_DELETE_FAILED for event in events)
    assert "private backend failure detail" not in str(events)
    monkeypatch.setattr(driver, "delete_object_if_generation", original)
    assert _run(service, stage="execute", candidate_id=candidate_id, context=context)["status"] == "deleted"


def test_audit_intent_failure_prevents_deletion(catalog, driver, clock, context, monkeypatch):
    service = _service(catalog, driver, clock)
    candidate_id = _quarantine(service, context)
    clock.advance(_GRACE)
    original = catalog.append_audit_event

    def fail_intent(event):
        if event.event_type == AuditEventType.ORPHAN_DELETE_STARTED:
            raise OSError("audit unavailable")
        return original(event)

    monkeypatch.setattr(catalog, "append_audit_event", fail_intent)
    with pytest.raises(OSError, match="audit unavailable"):
        _run(service, stage="execute", candidate_id=candidate_id, context=context)
    assert driver.get_object("bucket", _KEY) == _PAYLOAD
    assert [event.event_type for event in catalog.list_audit_events()] == [AuditEventType.ORPHAN_QUARANTINED]


def test_post_delete_audit_failure_preserves_recoverable_started_state(catalog, driver, clock, context, monkeypatch):
    service = _service(catalog, driver, clock)
    candidate_id = _quarantine(service, context)
    clock.advance(_GRACE)
    original = catalog.append_audit_event

    def fail_outcome(event):
        if event.event_type == AuditEventType.ORPHAN_DELETED:
            raise OSError("audit unavailable")
        return original(event)

    monkeypatch.setattr(catalog, "append_audit_event", fail_outcome)
    with pytest.raises(OSError, match="audit unavailable"):
        _run(service, stage="execute", candidate_id=candidate_id, context=context)
    assert list(driver.list_objects("bucket")) == []
    assert {event.event_type for event in catalog.list_audit_events()} == {
        AuditEventType.ORPHAN_QUARANTINED, AuditEventType.ORPHAN_DELETE_STARTED,
    }
    monkeypatch.setattr(catalog, "append_audit_event", original)
    result = _run(service, stage="execute", candidate_id=candidate_id, context=context)
    assert result["status"] in {"deleted", "already_deleted"}
    assert any(event.event_type == AuditEventType.ORPHAN_DELETED for event in catalog.list_audit_events())


@pytest.mark.parametrize("mismatch", ["catalog", "raw_driver", "driver_tenant"])
def test_unresolved_tenant_is_rejected_before_deletion(catalog, driver, clock, context, mismatch, monkeypatch):
    scope = ScanScope("tenant-a", "bucket", "", ("hot",))
    tenant_catalog = catalog if mismatch == "catalog" else catalog.for_tenant("tenant-a")
    tenant_driver = (
        driver if mismatch == "raw_driver"
        else TenantStorageDriver(driver, "tenant-b" if mismatch == "driver_tenant" else "tenant-a")
    )
    service = OrphanCleanup(
        tenant_catalog, {"hot": tenant_driver}, scope, binding_id="trusted-test-binding", clock=clock,
    )

    def backend_read(*args, **kwargs):
        pytest.fail("unresolved tenant must not inspect backend objects")

    monkeypatch.setattr(driver, "stat_object", backend_read)
    result = _run(service, stage="quarantine", context=context)
    assert result["status"] == "blocked"
    assert "unresolved_tenant" in result["blockers"]
    assert catalog.list_audit_events() == []
    assert driver.get_object("bucket", _KEY) == _PAYLOAD


def test_nondefault_cleanup_preserves_other_tenant_namespace(catalog, driver, clock, context):
    catalog.upsert("bucket", _KEY, len(_PAYLOAD), tier="hot")
    tenant_catalog = catalog.for_tenant("tenant-a")
    tenant_driver = TenantStorageDriver(driver, "tenant-a")
    tenant_driver.put_object("bucket", _KEY, b"tenant-specific orphan")
    modified = (clock.instant - timedelta(days=30)).timestamp()
    os.utime(driver.base / "bucket" / (tenant_driver._prefix + _KEY), (modified, modified))
    service = OrphanCleanup(
        tenant_catalog, {"hot": tenant_driver},
        ScanScope("tenant-a", "bucket", "", ("hot",)), binding_id="tenant-a-binding", clock=clock,
    )
    candidate_id = _quarantine(service, context)
    clock.advance(_GRACE)
    assert _run(service, stage="execute", candidate_id=candidate_id, context=context)["status"] == "deleted"
    assert list(tenant_driver.list_objects("bucket")) == []
    assert driver.get_object("bucket", _KEY) == _PAYLOAD
    assert catalog.get("bucket", _KEY) is not None
    assert catalog.list_audit_events() == []
    assert len(tenant_catalog.list_audit_events()) == 3


def test_sqlite_quarantine_survives_reopening(tmp_path, driver, clock, context):
    path = tmp_path / "persistent.sqlite3"
    with SQLiteCatalog(path) as catalog:
        candidate_id = _quarantine(_service(catalog, driver, clock), context)
    clock.advance(_GRACE)
    with SQLiteCatalog(path) as reopened:
        result = _run(_service(reopened, driver, clock), stage="execute", candidate_id=candidate_id, context=context)
        assert result["status"] == "deleted"
        assert reopened.verify_audit_integrity().valid
    assert list(driver.list_objects("bucket")) == []
