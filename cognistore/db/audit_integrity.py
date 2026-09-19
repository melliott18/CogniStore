"""SQL implementation of the tenant-local immutable audit evidence ledger."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from cognistore.core.audit_integrity import (
    GENESIS_HASH,
    AuditCheckpoint,
    digest,
    event_digest,
    make_entry,
    move_state_issues,
    tombstone_payload,
)

from .schema import (
    audit_event_tombstones,
    audit_events,
    audit_integrity_entries,
    audit_integrity_head,
    audit_move_heads,
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def read_head(connection: Connection, tenant_id: str) -> AuditCheckpoint:
    row = connection.execute(sa.select(audit_integrity_head)).mappings().one()
    return AuditCheckpoint(tenant_id, row["sequence"], row["entry_hash"])


def append_entry(
    connection: Connection,
    tenant_id: str,
    *,
    kind: str,
    event_id: str,
    payload_digest: str,
    recorded_at: str | None = None,
    move_id: str | None = None,
    move_sequence: int | None = None,
) -> None:
    head = read_head(connection, tenant_id)
    entry = make_entry(
        tenant_id,
        head.sequence + 1,
        head.entry_hash,
        kind=kind,
        event_id=event_id,
        payload_digest=payload_digest,
        recorded_at=recorded_at or now(),
        move_id=move_id,
        move_sequence=move_sequence,
    )
    insert_entry(connection, entry)


def insert_entry(connection: Connection, entry: dict[str, Any]) -> None:
    connection.execute(
        sa.insert(audit_integrity_entries).values(**{**entry, "event_id": UUID(entry["event_id"])})
    )
    connection.execute(
        sa.update(audit_integrity_head)
        .where(audit_integrity_head.c.singleton == 1)
        .values(sequence=entry["sequence"], entry_hash=entry["entry_hash"])
    )


def initialize_baseline(connection: Connection, tenant_id: str) -> None:
    # Called only by migration or validated import into an empty destination.
    from .catalog import SQLCatalog

    for row in connection.execute(
        sa.select(audit_events).order_by(audit_events.c.recorded_at, audit_events.c.event_id)
    ).mappings():
        event = SQLCatalog._audit_event_from_row(row)
        append_entry(
            connection,
            tenant_id,
            kind="baseline_event",
            event_id=event.event_id,
            payload_digest=event_digest(event),
            move_id=event.move_id,
            move_sequence=row["move_sequence"],
        )
    for row in connection.execute(
        sa.select(audit_event_tombstones).order_by(audit_event_tombstones.c.event_id)
    ).mappings():
        payload = tombstone_payload(dict(row))
        append_entry(
            connection,
            tenant_id,
            kind="baseline_tombstone",
            event_id=payload["event_id"],
            payload_digest=digest(payload),
        )
    for row in connection.execute(
        sa.select(audit_move_heads).order_by(audit_move_heads.c.move_id)
    ).mappings():
        payload = {
            "move_id": row["move_id"],
            "last_sequence": row["last_sequence"],
            "last_event_id": str(row["last_event_id"]),
        }
        append_entry(
            connection,
            tenant_id,
            kind="baseline_move_head",
            event_id=payload["last_event_id"],
            payload_digest=digest(payload),
            move_id=row["move_id"],
            move_sequence=row["last_sequence"],
        )


def read_snapshot(connection: Connection, tenant_id: str):
    try:
        return _read_snapshot(connection, tenant_id)
    except (ValueError, TypeError, KeyError, OverflowError):
        # SQLAlchemy decodes JSON/UUID columns before yielding a row. Treat a
        # malformed stored value as failed verification, not a reader crash.
        try:
            head = read_head(connection, tenant_id)
        except (ValueError, TypeError, sa.exc.NoResultFound):
            head = AuditCheckpoint(tenant_id, 0, GENESIS_HASH)
        return [], {}, {}, head, ["invalid_storage_payload"]


def _read_snapshot(connection: Connection, tenant_id: str):
    from .catalog import SQLCatalog

    issues = []
    try:
        head = read_head(connection, tenant_id)
    except (sa.exc.NoResultFound, ValueError):
        head = AuditCheckpoint(tenant_id, 0, GENESIS_HASH)
        issues.append("invalid_or_missing_head")
    entries = [
        {**row, "event_id": str(row["event_id"])}
        for row in connection.execute(
            sa.select(audit_integrity_entries).order_by(audit_integrity_entries.c.sequence)
        ).mappings()
    ]
    events = {}
    sequences = {}
    for row in connection.execute(sa.select(audit_events)).mappings():
        sequences[str(row["event_id"])] = row["move_sequence"]
        try:
            events[str(row["event_id"])] = SQLCatalog._audit_event_from_row(row)
        except (ValueError, TypeError, KeyError):
            issues.append("invalid_event_payload:" + str(row["event_id"]))
    tombstones = {
        str(row["event_id"]): tombstone_payload(dict(row))
        for row in connection.execute(sa.select(audit_event_tombstones)).mappings()
    }
    heads = {
        row["move_id"]: (row["last_sequence"], str(row["last_event_id"]))
        for row in connection.execute(sa.select(audit_move_heads)).mappings()
    }
    issues.extend(move_state_issues(entries, events, heads, sequences))
    return entries, events, tombstones, head, issues


def read_export(
    connection: Connection,
    tenant_id: str,
    *,
    after_sequence: int = 0,
    limit: int = 1000,
    checkpoint=None,
) -> dict[str, Any]:
    """Read one checkpoint-bound page without materializing the tenant's history."""
    from dataclasses import asdict

    from cognistore.core.audit_integrity import parse_checkpoint

    from .catalog import SQLCatalog

    if type(after_sequence) is not int or after_sequence < 0:
        raise ValueError("after_sequence must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    head = read_head(connection, tenant_id)
    anchor = head if checkpoint is None else parse_checkpoint(checkpoint)
    if anchor.tenant_id != tenant_id:
        raise ValueError("checkpoint belongs to another tenant")
    if anchor.sequence > head.sequence or after_sequence > anchor.sequence:
        raise ValueError("export bounds exceed the current audit head")
    anchor_hash = (
        GENESIS_HASH
        if anchor.sequence == 0
        else connection.execute(
            sa.select(audit_integrity_entries.c.entry_hash).where(
                audit_integrity_entries.c.sequence == anchor.sequence
            )
        ).scalar_one_or_none()
    )
    if anchor_hash != anchor.entry_hash:
        raise ValueError("export checkpoint no longer matches audit history")

    births = audit_integrity_entries.alias("births")
    removals = audit_integrity_entries.alias("removals")
    changed = connection.execute(
        sa.select(births.c.sequence)
        .select_from(births.join(removals, births.c.event_id == removals.c.event_id))
        .where(
            births.c.kind.in_(("event", "baseline_event")),
            births.c.sequence <= anchor.sequence,
            removals.c.kind == "retention",
            removals.c.sequence > anchor.sequence,
        )
        .limit(1)
    ).first()
    if changed is not None:
        raise ValueError("audit retention changed the export snapshot; restart the export")

    rows = (
        connection.execute(
            sa.select(audit_integrity_entries)
            .where(
                audit_integrity_entries.c.sequence > after_sequence,
                audit_integrity_entries.c.sequence <= anchor.sequence,
            )
            .order_by(audit_integrity_entries.c.sequence)
            .limit(limit)
        )
        .mappings()
        .all()
    )
    identifiers = {row["event_id"] for row in rows}
    events = {}
    tombstones = {}
    if identifiers:
        for row in connection.execute(
            sa.select(audit_events).where(audit_events.c.event_id.in_(identifiers))
        ).mappings():
            try:
                events[row["event_id"]] = asdict(SQLCatalog._audit_event_from_row(row))
            except (ValueError, TypeError, KeyError):
                raise ValueError("audit history contains invalid event payloads") from None
        tombstones = {
            row["event_id"]: tombstone_payload(dict(row))
            for row in connection.execute(
                sa.select(audit_event_tombstones).where(
                    audit_event_tombstones.c.event_id.in_(identifiers)
                )
            ).mappings()
        }
    records = []
    next_sequence = after_sequence
    for row in rows:
        if row["sequence"] != next_sequence + 1:
            raise ValueError("audit export contains missing evidence")
        records.append(
            {
                **row,
                "event_id": str(row["event_id"]),
                "event": events.get(row["event_id"]),
                "tombstone": tombstones.get(row["event_id"]),
            }
        )
        next_sequence = row["sequence"]
    if not records and next_sequence < anchor.sequence:
        raise ValueError("audit export contains missing evidence")
    return {
        "records": records,
        "checkpoint": asdict(anchor),
        "next_sequence": next_sequence,
        "complete": next_sequence == anchor.sequence,
    }


def install_guards(connection: Connection) -> None:
    if connection.dialect.name == "sqlite":
        # SQLite REPLACE otherwise bypasses DELETE triggers when a raw
        # connection uses the default recursive_triggers=OFF setting.
        for table, key in (
            ("audit_events", "event_id"),
            ("audit_event_tombstones", "event_id"),
            ("audit_integrity_entries", "sequence"),
            ("audit_integrity_head", "singleton"),
        ):
            connection.exec_driver_sql(
                f"CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table} "
                f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {key}=NEW.{key}) "
                "BEGIN SELECT RAISE(ABORT, 'audit history cannot be replaced'); END"
            )
        connection.exec_driver_sql("""CREATE TRIGGER audit_events_no_replace_move BEFORE INSERT ON audit_events
            WHEN NEW.move_id IS NOT NULL AND NEW.move_sequence IS NOT NULL AND EXISTS (
                SELECT 1 FROM audit_events WHERE move_id=NEW.move_id AND move_sequence=NEW.move_sequence)
            BEGIN SELECT RAISE(ABORT, 'audit move history cannot be replaced'); END""")
        for table in ("audit_events", "audit_event_tombstones", "audit_integrity_entries"):
            connection.exec_driver_sql(
                f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'audit history is append-only'); END"
            )
            if table != "audit_events":
                connection.exec_driver_sql(
                    f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'audit history is append-only'); END"
                )
        connection.exec_driver_sql("""CREATE TRIGGER audit_events_retention_delete BEFORE DELETE ON audit_events
            WHEN cognistore_audit_retention() != 1 OR NOT EXISTS (
                SELECT 1 FROM audit_event_tombstones t JOIN audit_integrity_entries i ON i.event_id = t.event_id
                WHERE t.event_id = OLD.event_id AND i.kind = 'retention')
            BEGIN SELECT RAISE(ABORT, 'audit deletion requires authorized retention evidence'); END""")
        connection.exec_driver_sql("""CREATE TRIGGER audit_integrity_head_immutable_delete BEFORE DELETE ON audit_integrity_head
            BEGIN SELECT RAISE(ABORT, 'audit head cannot be deleted'); END""")
        connection.exec_driver_sql("""CREATE TRIGGER audit_integrity_head_monotonic BEFORE UPDATE ON audit_integrity_head
            WHEN NEW.singleton != OLD.singleton OR NEW.sequence != OLD.sequence + 1 OR NOT EXISTS (
                SELECT 1 FROM audit_integrity_entries WHERE sequence = NEW.sequence
                    AND entry_hash = NEW.entry_hash AND previous_hash = OLD.entry_hash)
            BEGIN SELECT RAISE(ABORT, 'audit head must advance with evidence'); END""")
    else:
        schema = connection.exec_driver_sql("SELECT current_schema()").scalar_one()
        quoted_schema = connection.dialect.identifier_preparer.quote(schema)
        connection.exec_driver_sql(f"""CREATE FUNCTION cognistore_audit_guard() RETURNS trigger LANGUAGE plpgsql
        SET search_path = {quoted_schema}, pg_temp AS $$
        BEGIN
            IF TG_TABLE_NAME = 'audit_events' AND TG_OP = 'DELETE' THEN
                IF current_user = (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID)
                   AND EXISTS (SELECT 1 FROM audit_event_tombstones t JOIN audit_integrity_entries i ON i.event_id=t.event_id
                               WHERE t.event_id=OLD.event_id AND i.kind='retention') THEN RETURN OLD; END IF;
                RAISE EXCEPTION 'audit deletion requires authorized retention evidence';
            END IF;
            RAISE EXCEPTION 'audit history is append-only';
        END $$""")
        for table in ("audit_events", "audit_event_tombstones", "audit_integrity_entries"):
            connection.exec_driver_sql(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION cognistore_audit_guard()"
                if table != "audit_events"
                else f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION cognistore_audit_guard()"
            )
        connection.exec_driver_sql(
            "CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events FOR EACH STATEMENT EXECUTE FUNCTION cognistore_audit_guard()"
        )
        connection.exec_driver_sql(f"""CREATE FUNCTION cognistore_audit_head_guard() RETURNS trigger LANGUAGE plpgsql
        SET search_path = {quoted_schema}, pg_temp AS $$
        BEGIN
            IF TG_OP != 'UPDATE' THEN RAISE EXCEPTION 'audit head cannot be deleted'; END IF;
            IF NEW.singleton != OLD.singleton OR NEW.sequence != OLD.sequence + 1 OR NOT EXISTS (
                SELECT 1 FROM audit_integrity_entries WHERE sequence=NEW.sequence AND entry_hash=NEW.entry_hash AND previous_hash=OLD.entry_hash)
            THEN RAISE EXCEPTION 'audit head must advance with evidence'; END IF;
            RETURN NEW;
        END $$""")
        connection.exec_driver_sql(
            "CREATE TRIGGER audit_integrity_head_monotonic BEFORE UPDATE OR DELETE ON audit_integrity_head FOR EACH ROW EXECUTE FUNCTION cognistore_audit_head_guard()"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER audit_integrity_head_no_truncate BEFORE TRUNCATE ON audit_integrity_head FOR EACH STATEMENT EXECUTE FUNCTION cognistore_audit_guard()"
        )
        # Only the trusted owner or an explicitly granted maintenance role can
        # execute retention. Runtime roles receive no PUBLIC execution grant.
        connection.exec_driver_sql(f"""CREATE FUNCTION cognistore_prune_audit_events(identifiers uuid[]) RETURNS bigint
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = {quoted_schema}, pg_temp AS $$
        DECLARE removed bigint;
        BEGIN
            DELETE FROM audit_events WHERE event_id = ANY(identifiers);
            GET DIAGNOSTICS removed = ROW_COUNT;
            RETURN removed;
        END $$""")
        connection.exec_driver_sql(
            "REVOKE ALL ON FUNCTION cognistore_prune_audit_events(uuid[]) FROM PUBLIC"
        )


def remove_guards(connection: Connection) -> None:
    if connection.dialect.name == "sqlite":
        names = [
            f"{table}_immutable_{verb}"
            for table in ("audit_events", "audit_event_tombstones", "audit_integrity_entries")
            for verb in ("update", "delete")
            if not (table == "audit_events" and verb == "delete")
        ]
        names += [
            f"{table}_no_replace"
            for table in (
                "audit_events",
                "audit_event_tombstones",
                "audit_integrity_entries",
                "audit_integrity_head",
            )
        ]
        names += [
            "audit_events_no_replace_move",
            "audit_events_retention_delete",
            "audit_integrity_head_immutable_delete",
            "audit_integrity_head_monotonic",
        ]
        for name in names:
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
    else:
        for table in ("audit_events", "audit_event_tombstones", "audit_integrity_entries"):
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
        connection.exec_driver_sql(
            "DROP TRIGGER IF EXISTS audit_events_no_truncate ON audit_events"
        )
        connection.exec_driver_sql(
            "DROP TRIGGER IF EXISTS audit_integrity_head_monotonic ON audit_integrity_head"
        )
        connection.exec_driver_sql(
            "DROP TRIGGER IF EXISTS audit_integrity_head_no_truncate ON audit_integrity_head"
        )
        connection.exec_driver_sql("DROP FUNCTION cognistore_prune_audit_events(uuid[])")
        connection.exec_driver_sql("DROP FUNCTION cognistore_audit_guard()")
        connection.exec_driver_sql("DROP FUNCTION cognistore_audit_head_guard()")
