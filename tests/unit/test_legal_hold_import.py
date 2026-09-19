from pathlib import Path

import pytest
import sqlalchemy as sa

from cognistore.core.audit import AuditContext
from cognistore.core.legal_holds import LegalHoldError
from cognistore.db.audit_integrity import remove_guards
from cognistore.db.catalog import SQLCatalog
from cognistore.db.schema import audit_events, audit_integrity_entries, legal_holds
from cognistore.db.sqlite_import import SQLiteCatalogImportError, import_sqlite_catalog


def test_import_preserves_active_and_released_holds(tmp_path: Path) -> None:
    source_path = tmp_path / "source.sqlite3"
    actor = AuditContext(actor_type="user", actor_id="custodian", correlation_id="test-import")
    with SQLCatalog(source_path) as source:
        active = source.place_legal_hold("bucket", key="item", reason="preserve", context=actor)
        released = source.place_legal_hold("bucket", prefix="old/", reason="preserve", context=actor)
        released = source.release_legal_hold(released.hold_id, reason="withdrawn", context=actor)
        events = source.list_audit_events()
        checkpoint = source.audit_checkpoint()
    with SQLCatalog(tmp_path / "destination.sqlite3") as destination:
        report = import_sqlite_catalog(source_path, destination)
        assert report.legal_holds == 2
        assert destination.list_legal_holds() == [active, released]
        assert destination.list_audit_events() == events
        assert destination.audit_checkpoint() == checkpoint
        assert destination.verify_audit_integrity(checkpoint).valid
        with pytest.raises(LegalHoldError):
            destination.delete("bucket", "item")
        assert destination.prune_audit_events("2099-01-01T00:00:00Z") == 0
        assert destination.verify_audit_integrity(checkpoint).valid
        destination.release_legal_hold(active.hold_id, reason="case closed", context=actor)
        assert destination.audit_checkpoint().sequence > checkpoint.sequence
        assert destination.verify_audit_integrity(checkpoint).valid


@pytest.mark.parametrize("damage", ["drop", "scope", "owner", "active"])
def test_import_rejects_missing_or_corrupt_hold_state(tmp_path: Path, damage: str) -> None:
    source_path = tmp_path / "source.sqlite3"
    with SQLCatalog(source_path) as source:
        source.place_legal_hold(
            "bucket", reason="preserve",
            context=AuditContext(actor_type="user", actor_id="custodian", correlation_id="test"),
        )
        with source.engine.begin() as connection:
            if damage == "drop":
                connection.exec_driver_sql("DROP TABLE legal_holds")
            elif damage == "active":
                evidence = connection.execute(sa.select(legal_holds.c.evidence)).scalar_one()
                evidence["active"] = False
                connection.execute(sa.update(legal_holds).values(evidence=evidence))
            else:
                connection.execute(sa.update(legal_holds).values(
                    **({"bucket": "other"} if damage == "scope" else {"tenant_id": "other"}),
                ))
    with SQLCatalog(tmp_path / "destination.sqlite3") as destination:
        with pytest.raises(SQLiteCatalogImportError):
            import_sqlite_catalog(source_path, destination)
        assert destination.list_legal_holds() == []
        assert destination.list_audit_events() == []
        assert destination.audit_checkpoint().sequence == 0
        assert destination.verify_audit_integrity().valid


@pytest.mark.parametrize("damage", ["audit_payload", "integrity_tail", "integrity_tables"])
def test_import_rejects_corrupt_hold_audit_chain_atomically(tmp_path: Path, damage: str) -> None:
    source_path = tmp_path / "source.sqlite3"
    with SQLCatalog(source_path) as source:
        source.place_legal_hold(
            "bucket", key="item", reason="preserve",
            context=AuditContext(actor_type="user", actor_id="custodian", correlation_id="test"),
        )
        with source.engine.begin() as connection:
            remove_guards(connection)
            if damage == "audit_payload":
                connection.execute(sa.update(audit_events).values(outcome="rejected"))
            elif damage == "integrity_tail":
                connection.execute(sa.delete(audit_integrity_entries))
            else:
                connection.exec_driver_sql("DROP TABLE audit_integrity_entries")
                connection.exec_driver_sql("DROP TABLE audit_integrity_head")
    with SQLCatalog(tmp_path / "destination.sqlite3") as destination:
        with pytest.raises(SQLiteCatalogImportError, match="audit integrity"):
            import_sqlite_catalog(source_path, destination)
        assert destination.list_legal_holds() == []
        assert destination.list_audit_events() == []
        assert destination.audit_checkpoint().sequence == 0
        assert destination.verify_audit_integrity().valid
