"""Hold lifecycle and denial evidence remain atomic and permanently verifiable."""

import pytest

from cognistore.core.audit import AuditContext, AuditEvent
from cognistore.core.legal_holds import LegalHoldError
from cognistore.db.catalog import SQLCatalog


class LegalHoldAuditIntegrityConformance:
    def test_hold_evidence_survives_retention_with_valid_integrity(self, catalog):
        context = AuditContext("case", "user", "operator")
        catalog.upsert("bucket", "key", size=4, tier="hot")
        hold = catalog.place_legal_hold("bucket", key="key", reason="preserve", context=context)
        with pytest.raises(LegalHoldError):
            catalog.delete("bucket", "key")
        catalog.release_legal_hold(hold.hold_id, reason="case closed", context=context)
        checkpoint = catalog.audit_checkpoint()
        assert checkpoint.sequence == 3
        catalog.append_audit_event(AuditEvent.create(
            "manual.action", "succeeded", context,
            occurred_at="2000-01-01T00:00:00Z", recorded_at="2000-01-01T00:00:00Z",
        ))
        assert catalog.prune_expired_audit_events("2999-01-01T00:00:00Z") == 1
        assert catalog.prune_audit_events("2999-01-01T00:00:00Z") == 0
        retained = catalog.list_audit_events()
        assert {event.event_type for event in retained} == {
            "legal_hold.placed", "legal_hold.denied", "legal_hold.released", "audit.retention",
        }
        assert all(event.expires_at is None for event in retained
                   if event.event_type.startswith("legal_hold."))
        integrity = catalog.verify_audit_integrity(checkpoint)
        assert integrity.valid and integrity.anchored
        assert integrity.checked_entries == 6 and integrity.pruned_events == 1
        exported = catalog.export_audit_evidence()
        preserved_events = [record["event"] for record in exported["records"]
                            if (record.get("event") or {}).get("event_type", "").startswith("legal_hold.")]
        assert len(preserved_events) == 3
        assert catalog.list_legal_holds()[0].released_reason == "case closed"

    @pytest.mark.parametrize("operation", ["place", "release", "denial"])
    def test_partial_integrity_append_failure_rolls_back_and_fails_closed(
        self, catalog, monkeypatch, operation,
    ):
        context = AuditContext("case", "user", "operator")
        catalog.upsert("bucket", "key", size=4, tier="hot")
        hold = None if operation == "place" else catalog.place_legal_hold(
            "bucket", key="key", reason="preserve", context=context,
        )
        checkpoint = catalog.audit_checkpoint()
        events = catalog.list_audit_events()
        if isinstance(catalog, SQLCatalog):
            from cognistore.db import catalog as module
            original = module.append_entry
            target, attribute = module, "append_entry"
        else:
            original = catalog._append_audit_evidence
            target, attribute = catalog, "_append_audit_evidence"

        def fail_after_append(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("evidence append failed")

        with monkeypatch.context() as patch:
            patch.setattr(target, attribute, fail_after_append)
            with pytest.raises(RuntimeError, match="evidence append failed"):
                if operation == "place":
                    catalog.place_legal_hold("bucket", reason="preserve", context=context)
                elif operation == "release":
                    catalog.release_legal_hold(hold.hold_id, reason="done", context=context)
                else:
                    catalog.delete("bucket", "key")
        assert catalog.get("bucket", "key").size == 4
        assert catalog.list_legal_holds() == ([] if hold is None else [hold])
        assert catalog.list_audit_events() == events
        assert catalog.audit_checkpoint() == checkpoint
        assert catalog.verify_audit_integrity(checkpoint).valid
