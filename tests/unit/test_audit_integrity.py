from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from uuid import UUID

import pytest
import sqlalchemy as sa

from cognistore.core.audit import AuditContext, AuditEvent
from cognistore.core.audit_integrity import GENESIS_HASH, AuditCheckpoint, digest
from cognistore.core.catalog import Catalog
from cognistore.db import MigrationManager, SQLCatalog
from cognistore.db.audit_integrity import remove_guards
from cognistore.db.schema import audit_events, audit_integrity_entries, audit_integrity_head
from cognistore.db.sqlite_import import SQLiteCatalogImportError, import_sqlite_catalog


def event(number: int = 1) -> AuditEvent:
    return AuditEvent.create(
        "manual.action",
        "succeeded",
        AuditContext("request", "user", "operator"),
        event_id=f"00000000-0000-0000-0000-{number:012d}",
        occurred_at="2020-01-01T00:00:00Z",
        recorded_at="2020-01-01T00:00:01Z",
        details={"count": number},
    )


@pytest.fixture(params=["memory", "sqlite"])
def catalog(request, tmp_path):
    value = Catalog() if request.param == "memory" else SQLCatalog(tmp_path / "audit.db")
    yield value
    if isinstance(value, SQLCatalog):
        value.close()


def defeat_guards(catalog):
    if isinstance(catalog, SQLCatalog):
        with catalog.engine.begin() as connection:
            remove_guards(connection)


@pytest.mark.parametrize("attack", ["payload", "missing", "tail", "head", "reorder"])
def test_detects_privileged_corruption(catalog, attack):
    for number in range(1, 4):
        catalog.append_audit_event(event(number))
    checkpoint = catalog.audit_checkpoint()
    defeat_guards(catalog)
    if isinstance(catalog, SQLCatalog):
        with catalog.engine.begin() as connection:
            if attack == "payload":
                connection.execute(sa.update(audit_events).values(details={"count": 999}))
            elif attack == "missing":
                connection.execute(
                    sa.delete(audit_events).where(
                        audit_events.c.event_id == UUID(event(2).event_id)
                    )
                )
            elif attack == "tail":
                connection.execute(
                    sa.delete(audit_integrity_entries).where(
                        audit_integrity_entries.c.sequence == 3
                    )
                )
            elif attack == "head":
                connection.execute(sa.delete(audit_integrity_head))
            else:
                connection.execute(
                    sa.update(audit_integrity_entries)
                    .where(audit_integrity_entries.c.sequence == 2)
                    .values(previous_hash=GENESIS_HASH)
                )
    else:
        if attack == "payload":
            catalog._audit_events[event(1).event_id] = replace(
                catalog._audit_events[event(1).event_id], details={"count": 999}
            )
        elif attack == "missing":
            catalog._audit_events.pop(event(2).event_id)
        elif attack == "tail":
            catalog._audit_integrity_entries.pop()
        elif attack == "head":
            catalog._audit_integrity_head = AuditCheckpoint("default", 0, GENESIS_HASH)
        else:
            catalog._audit_integrity_entries[1]["previous_hash"] = GENESIS_HASH
    result = catalog.verify_audit_integrity(checkpoint)
    assert not result.valid
    assert result.issues


def test_checkpoint_detects_rewritten_local_history(catalog):
    catalog.append_audit_event(event())
    checkpoint = catalog.audit_checkpoint()
    defeat_guards(catalog)
    if isinstance(catalog, SQLCatalog):
        with catalog.engine.begin() as connection:
            connection.execute(sa.delete(audit_events))
            connection.execute(sa.delete(audit_integrity_entries))
            connection.execute(
                sa.update(audit_integrity_head).values(sequence=0, entry_hash=GENESIS_HASH)
            )
    else:
        catalog._audit_events.clear()
        catalog._audit_integrity_entries.clear()
        catalog._audit_integrity_head = AuditCheckpoint("default", 0, GENESIS_HASH)
    assert (
        catalog.verify_audit_integrity().valid
    )  # A local unanchored rewrite cannot be authenticated.
    assert not catalog.verify_audit_integrity(checkpoint).valid


def test_retention_preserves_chain_checkpoint_and_replay(catalog):
    original = catalog.append_audit_event(event())
    checkpoint = catalog.audit_checkpoint()
    assert catalog.prune_expired_audit_events("2025-01-01T00:00:00Z") == 1
    assert catalog.append_audit_event(event()) == original
    result = catalog.verify_audit_integrity(asdict(checkpoint))
    assert result.valid and result.anchored and result.pruned_events == 1
    assert result.checked_entries == 3
    assert catalog.list_audit_events()[0].event_type == "audit.retention"
    with pytest.raises(ValueError, match="retention changed"):
        catalog.export_audit_evidence(checkpoint=checkpoint)
    assert (
        catalog.export_audit_evidence()["records"][1]["tombstone"]["event_id"] == original.event_id
    )


def test_retention_cannot_launder_corrupted_payload(catalog):
    catalog.append_audit_event(event())
    defeat_guards(catalog)
    if isinstance(catalog, SQLCatalog):
        with catalog.engine.begin() as connection:
            connection.execute(sa.update(audit_events).values(details={"count": 999}))
    else:
        catalog._audit_events[event().event_id] = replace(
            catalog._audit_events[event().event_id], details={"count": 999}
        )
    with pytest.raises(ValueError, match="fails integrity"):
        catalog.prune_expired_audit_events("2025-01-01T00:00:00Z")


def test_export_is_bounded_by_checkpoint_and_payload_is_verifiable(catalog):
    catalog.append_audit_event(event())
    checkpoint = catalog.audit_checkpoint()
    catalog.append_audit_event(event(2))
    exported = catalog.export_audit_evidence(checkpoint=checkpoint, limit=1)
    assert exported["complete"] and len(exported["records"]) == 1
    record = exported["records"][0]
    assert digest(record["event"]) == record["payload_digest"]
    record["event"]["details"]["count"] = 99
    assert catalog.verify_audit_integrity().valid
    with pytest.raises(ValueError, match="another tenant"):
        catalog.verify_audit_integrity(replace(checkpoint, tenant_id="other"))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET outcome='failed'",
        "DELETE FROM audit_events",
        "UPDATE audit_integrity_entries SET payload_digest='tampered'",
        "DELETE FROM audit_integrity_entries",
        "DELETE FROM audit_integrity_head",
        "UPDATE audit_integrity_head SET sequence=0",
        "INSERT OR REPLACE INTO audit_events SELECT * FROM audit_events",
        "INSERT OR REPLACE INTO audit_integrity_entries SELECT * FROM audit_integrity_entries",
        "INSERT OR REPLACE INTO audit_integrity_head SELECT * FROM audit_integrity_head",
    ],
)
def test_raw_sqlite_mutations_and_replace_are_denied(tmp_path, statement):
    database = tmp_path / "guard.db"
    with SQLCatalog(database) as catalog:
        catalog.append_audit_event(event())
        with sqlite3.connect(database) as raw:
            assert raw.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
            with pytest.raises(sqlite3.Error):
                raw.execute(statement)
        assert catalog.verify_audit_integrity().valid


def test_sqlite_cross_connection_concurrent_append_is_contiguous(tmp_path):
    database = tmp_path / "concurrent.db"
    with SQLCatalog(database):
        pass

    def append(number):
        with SQLCatalog(database) as catalog:
            catalog.append_audit_event(event(number))
            catalog.append_audit_event(event(number))

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(append, range(1, 25)))
    with SQLCatalog(database, read_only=True) as catalog:
        result = catalog.verify_audit_integrity()
        assert result.valid and result.checked_entries == 24


def test_migration_baseline_and_import_preserve_evidence(tmp_path):
    source = tmp_path / "source.db"
    with SQLCatalog(source) as catalog:
        catalog.append_audit_event(event())
        MigrationManager().downgrade(catalog.engine, "0012_tenant_ownership")
    with SQLCatalog(source) as catalog:
        assert catalog.verify_audit_integrity().valid
        assert catalog.export_audit_evidence()["records"][0]["kind"] == "baseline_event"
        checkpoint = catalog.audit_checkpoint()
    with SQLCatalog(tmp_path / "destination.db") as destination:
        import_sqlite_catalog(source, destination)
        assert destination.audit_checkpoint() == checkpoint
        assert destination.verify_audit_integrity(checkpoint).valid


def test_import_rejects_missing_tail_and_rolls_back(tmp_path):
    source = tmp_path / "source.db"
    with SQLCatalog(source) as catalog:
        catalog.append_audit_event(event())
        defeat_guards(catalog)
        with catalog.engine.begin() as connection:
            connection.execute(sa.delete(audit_integrity_entries))
    with SQLCatalog(tmp_path / "destination.db") as destination:
        with pytest.raises(SQLiteCatalogImportError, match="head does not match"):
            import_sqlite_catalog(source, destination)
        assert destination.audit_checkpoint().sequence == 0
        assert destination.verify_audit_integrity().valid


def test_retention_finishes_for_future_cutoffs(catalog):
    catalog.append_audit_event(event())
    assert catalog.prune_audit_events("2999-01-01T00:00:00Z", limit=1) == 1
    assert catalog.prune_audit_events("2999-01-01T00:00:00Z", limit=1) == 0
    assert catalog.prune_expired_audit_events("2999-01-01T00:00:00Z", limit=1) == 0
    assert catalog.verify_audit_integrity().valid


def test_retention_failure_is_atomic_in_memory(monkeypatch):
    catalog = Catalog()
    catalog.append_audit_event(event())
    before = catalog.audit_checkpoint()

    def fail(_event):
        raise RuntimeError("injected summary failure")

    monkeypatch.setattr(catalog, "append_audit_event", fail)
    with pytest.raises(RuntimeError, match="injected"):
        catalog.prune_audit_events("2999-01-01T00:00:00Z")
    assert catalog.audit_checkpoint() == before
    assert catalog.get_audit_event(event().event_id) is not None
    assert catalog.verify_audit_integrity().valid


def test_mutated_move_head_is_detected_after_payload_retention(catalog):
    catalog.append_audit_event(replace(event(), move_id="move"))
    catalog.append_audit_event(replace(event(2), move_id="move"))
    catalog.prune_audit_events("2025-01-01T00:00:00Z")
    assert catalog.verify_audit_integrity().valid
    if isinstance(catalog, SQLCatalog):
        with catalog.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE audit_move_heads SET last_sequence=1")
    else:
        catalog._audit_move_heads["move"] = (1, event().event_id)
    assert "move_head_mismatch" in catalog.verify_audit_integrity().issues


def test_sqlite_replace_cannot_overwrite_move_unique_key(tmp_path):
    database = tmp_path / "move-replace.db"
    with SQLCatalog(database) as catalog:
        catalog.append_audit_event(replace(event(), move_id="move"))
        with sqlite3.connect(database) as raw:
            columns = [row[1] for row in raw.execute("PRAGMA table_info(audit_events)")]
            row = list(raw.execute("SELECT * FROM audit_events").fetchone())
            row[columns.index("event_id")] = "00000000000000000000000000000099"
            with pytest.raises(sqlite3.IntegrityError, match="cannot be replaced"):
                raw.execute(
                    "INSERT OR REPLACE INTO audit_events VALUES ("
                    + ",".join("?" for _ in row)
                    + ")",
                    row,
                )
        assert catalog.verify_audit_integrity().valid


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_integrity_entries SET sequence='invalid'",
        "UPDATE audit_integrity_entries SET event_id='invalid'",
        "UPDATE audit_events SET details='invalid JSON'",
        "UPDATE audit_events SET event_id='invalid'",
    ],
)
def test_malformed_sqlite_values_fail_verification_without_crashing(tmp_path, statement):
    with SQLCatalog(tmp_path / "malformed.db") as catalog:
        catalog.append_audit_event(event())
        defeat_guards(catalog)
        with catalog.engine.begin() as connection:
            connection.exec_driver_sql(statement)
        result = catalog.verify_audit_integrity()
        assert not result.valid
        assert result.issues
