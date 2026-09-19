"""An archive must prove every record through its independently retained head."""

from copy import deepcopy
from dataclasses import asdict

import pytest

from cognistore.core.audit import AuditContext, AuditEvent
from cognistore.core.audit_export import verify_audit_export
from cognistore.core.catalog import Catalog
from cognistore.db import MigrationManager, SQLCatalog


@pytest.fixture(params=["memory", "sqlite"])
def archive(request, tmp_path):
    catalog = Catalog() if request.param == "memory" else SQLCatalog(tmp_path / "archive.db")
    try:
        for number in range(3):
            catalog.append_audit_event(AuditEvent.create(
                "manual.action", "succeeded", AuditContext(f"request-{number}", "operator", "archiver"),
                occurred_at="2020-01-01T00:00:00Z", details={"number": number},
            ))
        # An archive must distinguish authorized payload retention from missing data.
        catalog.prune_expired_audit_events("2025-01-01T00:00:00Z", limit=1)
        checkpoint = asdict(catalog.audit_checkpoint())
        records = []
        sequence = 0
        while True:
            page = catalog.export_audit_evidence(
                after_sequence=sequence, limit=2, checkpoint=checkpoint,
            )
            records.extend(page["records"])
            if page["complete"]:
                break
            sequence = page["next_sequence"]
        yield records, checkpoint
    finally:
        if isinstance(catalog, SQLCatalog):
            catalog.close()


def test_complete_paginated_archive_verifies_offline_with_retention(archive):
    records, checkpoint = archive
    result = verify_audit_export(records, checkpoint)
    assert result.valid and result.anchored
    assert result.pruned_events == 1
    assert result.checked_entries == len(records) == checkpoint["sequence"]


@pytest.mark.parametrize("attack", ["payload", "middle", "suffix", "reorder", "tombstone", "duplicate"])
def test_offline_verification_rejects_altered_or_incomplete_archives(archive, attack):
    records, checkpoint = deepcopy(archive)
    if attack == "payload":
        record = next(item for item in records if item["event"] is not None)
        record["event"]["details"] = {"forged": True}
    elif attack == "middle":
        del records[1]
    elif attack == "suffix":
        records.pop()
    elif attack == "reorder":
        records[0], records[1] = records[1], records[0]
    elif attack == "duplicate":
        records.insert(1, deepcopy(records[0]))
    else:
        for record in records:
            if record["tombstone"] is not None:
                record["tombstone"]["replay_digest"] = "a" * 64
    result = verify_audit_export(records, checkpoint)
    assert not result.valid
    assert result.issues


def test_offline_verification_rejects_cross_tenant_checkpoint(archive):
    records, checkpoint = deepcopy(archive)
    checkpoint["tenant_id"] = "other-tenant"
    assert not verify_audit_export(records, checkpoint).valid


def test_offline_verification_rejects_malformed_archive(archive):
    records, checkpoint = deepcopy(archive)
    records[0]["event"] = {"arbitrary": "object"}
    with pytest.raises(ValueError, match="invalid audit export payload"):
        verify_audit_export(records, checkpoint)


@pytest.mark.parametrize("attack", ["extra-tombstone-field", "missing-event-field", "noncanonical-time"])
def test_offline_verification_requires_canonical_complete_payloads(archive, attack):
    records, checkpoint = deepcopy(archive)
    if attack == "extra-tombstone-field":
        record = next(item for item in records if item["tombstone"] is not None)
        record["tombstone"]["ignored"] = "unverified evidence"
    else:
        record = next(item for item in records if item["event"] is not None)
        if attack == "missing-event-field":
            record["event"].pop("bucket")
        else:
            record["event"]["occurred_at"] = record["event"]["occurred_at"].replace("Z", "+00:00")
    with pytest.raises(ValueError, match="invalid audit export payload"):
        verify_audit_export(records, checkpoint)


def test_offline_verification_bounds_stream_consumption_by_checkpoint(archive):
    records, checkpoint = archive

    def overflow():
        yield from records
        yield records[-1]
        raise AssertionError("must stop consuming after the first excess record")

    result = verify_audit_export(overflow(), checkpoint)
    assert not result.valid and "export_exceeds_checkpoint" in result.issues


def test_offline_verification_accepts_empty_genesis_and_rejects_noniterable():
    checkpoint = {"tenant_id": "empty", "sequence": 0, "entry_hash": "0" * 64}
    assert verify_audit_export([], checkpoint).valid
    with pytest.raises(ValueError, match="must be iterable"):
        verify_audit_export(None, checkpoint)


def test_offline_verification_accepts_legacy_baseline_with_pruned_move_head(tmp_path):
    path = tmp_path / "baseline.db"
    with SQLCatalog(path) as catalog:
        for number in range(2):
            catalog.append_audit_event(AuditEvent.create(
                "move.prepared", "started", AuditContext(f"request-{number}", "system", "migration"),
                move_id=f"move-{number}", occurred_at="2020-01-01T00:00:00Z",
            ))
        catalog.prune_expired_audit_events("2025-01-01T00:00:00Z", limit=1)
        MigrationManager().downgrade(catalog.engine, "0012_tenant_ownership")
    with SQLCatalog(path) as catalog:
        archive = catalog.export_audit_evidence()
        assert {record["kind"] for record in archive["records"]} >= {
            "baseline_event", "baseline_tombstone", "baseline_move_head",
        }
        result = verify_audit_export(archive["records"], archive["checkpoint"])
        assert result.valid and result.anchored and result.pruned_events == 1
