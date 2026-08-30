"""Offline import of prototype SQLite catalogs into the SQL catalog schema."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from cognistore.core.audit import (
    AuditContext,
    AuditEvent,
    AuditEventType,
    AuditOutcome,
    AuditRetentionPolicy,
    audit_text_identity,
    canonical_audit_timestamp,
    redact_audit_event,
    stable_audit_event_id,
)
from cognistore.utils.redaction import redact, redact_text

from .catalog import SQLCatalog
from .schema import (
    audit_event_tombstones,
    audit_events,
    audit_move_heads,
    move_job_claim_fences,
    move_job_transitions,
    move_jobs,
    object_mutation_fences,
    object_placements,
    objects,
    pools,
    tiers,
)

_MIGRATED_AT = "1970-01-01T00:00:00.000000Z"
_DEFAULT_BATCH_SIZE = 1000
_SOURCE_LAYOUT = Literal["legacy", "normalized"]

_DESTINATION_DATA_TABLES = (
    audit_event_tombstones,
    audit_events,
    audit_move_heads,
    tiers,
    pools,
    objects,
    object_placements,
    object_mutation_fences,
    move_job_claim_fences,
    move_jobs,
    move_job_transitions,
)


class SQLiteCatalogImportError(RuntimeError):
    """The SQLite source cannot be imported without losing catalog semantics."""


class CatalogImportDestinationNotEmptyError(SQLiteCatalogImportError):
    """The destination contains catalog data and cannot accept a one-shot import."""


@dataclass(frozen=True)
class SQLiteCatalogImportReport:
    """Counts committed by :func:`import_sqlite_catalog`."""

    source_layout: _SOURCE_LAYOUT
    tiers: int
    pools: int
    objects: int
    placements: int
    move_jobs: int
    move_job_transitions: int
    audit_events: int = 0


def import_sqlite_catalog(
    source_path: str | Path,
    destination: SQLCatalog,
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> SQLiteCatalogImportReport:
    """Atomically copy a legacy or normalized SQLite catalog into ``destination``.

    The destination is normally PostgreSQL, although accepting an empty SQLite
    ``SQLCatalog`` keeps the data conversion independently testable. The source
    is opened read-only and never migrated in place. The destination must be at
    the current migration head and completely empty; merging catalogs has
    ambiguous object and move-job conflict semantics and is deliberately not
    supported.

    Scheduler reservations and run-recovery records are a separate state store
    and are not copied by this function.
    """

    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError("batch_size must be a positive integer")
    if destination.read_only:
        raise SQLiteCatalogImportError("the destination catalog is read-only")

    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise SQLiteCatalogImportError(f"SQLite catalog does not exist: {source}")
    _reject_same_sqlite_catalog(source, destination)

    uri = f"{source.as_uri()}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as source_connection:
        source_connection.row_factory = sqlite3.Row
        source_connection.execute("PRAGMA query_only = ON")
        source_connection.execute("PRAGMA busy_timeout = 5000")
        source_connection.execute("BEGIN")
        layout = _detect_layout(source_connection)

        with destination.engine.begin() as target_connection:
            _lock_and_require_empty_destination(target_connection)
            if layout == "legacy":
                report = _copy_legacy_catalog(
                    source_connection,
                    target_connection,
                    batch_size=batch_size,
                )
            else:
                report = _copy_normalized_catalog(
                    source_connection,
                    target_connection,
                    batch_size=batch_size,
                )

            move_count, transition_count = _copy_move_history(
                source_connection,
                target_connection,
                batch_size=batch_size,
            )
            source_tables = _source_tables(source_connection)
            audit_table_names = {
                "audit_events",
                "audit_move_heads",
                "audit_event_tombstones",
            }
            present_audit_tables = source_tables.intersection(audit_table_names)
            if present_audit_tables and present_audit_tables != audit_table_names:
                raise SQLiteCatalogImportError(
                    "SQLite source must contain audit_events, audit_move_heads, "
                    "and audit_event_tombstones together, or none of them"
                )
            source_has_audit = bool(present_audit_tables)
            audit_count = _copy_audit_history(
                source_connection,
                target_connection,
                batch_size=batch_size,
            )
            if source_has_audit:
                _copy_audit_move_heads(
                    source_connection,
                    target_connection,
                    batch_size=batch_size,
                )
                _copy_audit_event_tombstones(
                    source_connection,
                    target_connection,
                    batch_size=batch_size,
                )
            else:
                audit_count += _backfill_move_audit_history(
                    target_connection,
                    retention=destination.audit_retention,
                )
            _validate_imported_audit_heads(target_connection)
            report = SQLiteCatalogImportReport(
                source_layout=report.source_layout,
                tiers=report.tiers,
                pools=report.pools,
                objects=report.objects,
                placements=report.placements,
                move_jobs=move_count,
                move_job_transitions=transition_count,
                audit_events=audit_count,
            )

        source_connection.rollback()
        return report


def _reject_same_sqlite_catalog(source: Path, destination: SQLCatalog) -> None:
    if destination.backend != "sqlite":
        return
    database = destination.engine.url.database
    if database in (None, "", ":memory:"):
        return
    if Path(database).expanduser().resolve() == source:
        raise SQLiteCatalogImportError("source and destination must be different catalogs")


def _source_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _source_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[0]) for row in connection.execute("SELECT name FROM pragma_table_info(?)", (table,))
    }


def _require_columns(
    connection: sqlite3.Connection,
    table: str,
    required: set[str],
) -> set[str]:
    columns = _source_columns(connection, table)
    missing = required - columns
    if missing:
        raise SQLiteCatalogImportError(
            f"SQLite table {table!r} is missing required column(s): " + ", ".join(sorted(missing))
        )
    return columns


def _detect_layout(connection: sqlite3.Connection) -> _SOURCE_LAYOUT:
    tables = _source_tables(connection)
    if "objects" not in tables:
        raise SQLiteCatalogImportError("SQLite source is not a CogniStore catalog")

    columns = _source_columns(connection, "objects")
    legacy_columns = {"bucket", "key", "size", "tier", "metadata"}
    normalized_columns = {
        "object_id",
        "bucket",
        "object_key",
        "size",
        "metadata",
        "created_at",
        "updated_at",
    }
    if legacy_columns.issubset(columns) and "object_id" not in columns:
        return "legacy"
    if normalized_columns.issubset(columns):
        required_tables = {"tiers", "pools", "object_placements"}
        missing_tables = required_tables - tables
        if missing_tables:
            raise SQLiteCatalogImportError(
                "normalized SQLite catalog is missing table(s): "
                + ", ".join(sorted(missing_tables))
            )
        return "normalized"
    raise SQLiteCatalogImportError("SQLite objects table has an unsupported layout")


def _lock_and_require_empty_destination(connection: Connection) -> None:
    if connection.dialect.name == "postgresql":
        table_names = ", ".join(table.name for table in _DESTINATION_DATA_TABLES)
        connection.exec_driver_sql(f"LOCK TABLE {table_names} IN ACCESS EXCLUSIVE MODE")

    for table in _DESTINATION_DATA_TABLES:
        present = connection.execute(sa.select(sa.literal(1)).select_from(table).limit(1)).first()
        if present is not None:
            raise CatalogImportDestinationNotEmptyError(
                f"destination catalog is not empty: table {table.name!r} contains data"
            )


def _copy_rows(
    source: sqlite3.Connection,
    destination: Connection,
    table: sa.Table,
    query: str,
    transform: Callable[[sqlite3.Row], dict[str, Any]],
    *,
    batch_size: int,
) -> int:
    cursor = source.execute(query)
    count = 0
    while batch := cursor.fetchmany(batch_size):
        values = [transform(row) for row in batch]
        destination.execute(sa.insert(table), values)
        count += len(values)
    return count


def _legacy_uuid(kind: str, *values: str) -> UUID:
    identity = json.dumps([kind, *values], ensure_ascii=True, separators=(",", ":"))
    return uuid5(NAMESPACE_URL, identity)


def _uuid(raw: object, field: str) -> UUID:
    if isinstance(raw, UUID):
        return raw
    try:
        if isinstance(raw, bytes) and len(raw) == 16:
            return UUID(bytes=raw)
        if isinstance(raw, bytes):
            raw = raw.decode("ascii")
        return UUID(str(raw))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SQLiteCatalogImportError(f"{field} is not a valid UUID") from exc


def _json(raw: object, field: str, default: object) -> object:
    if raw is None:
        return default
    try:
        if isinstance(raw, (bytes, bytearray, memoryview)):
            raw = bytes(raw).decode("utf-8")
        if not isinstance(raw, str):
            return raw
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SQLiteCatalogImportError(f"{field} contains invalid JSON") from exc


def _json_mapping(raw: object, field: str) -> dict[str, Any]:
    value = _json(raw, field, {})
    if not isinstance(value, dict):
        raise SQLiteCatalogImportError(f"{field} must contain a JSON object")
    return value


def _json_list(raw: object, field: str) -> list[Any]:
    value = _json(raw, field, [])
    if not isinstance(value, list):
        raise SQLiteCatalogImportError(f"{field} must contain a JSON array")
    return value


def _redacted_json_list(raw: object, field: str) -> list[Any]:
    value = redact(_json_list(raw, field))
    if not isinstance(value, list):  # pragma: no cover - redaction preserves lists
        raise SQLiteCatalogImportError(f"{field} must contain a JSON array")
    return value


def _copy_legacy_catalog(
    source: sqlite3.Connection,
    destination: Connection,
    *,
    batch_size: int,
) -> SQLiteCatalogImportReport:
    _require_columns(
        source,
        "objects",
        {"bucket", "key", "size", "tier", "metadata"},
    )
    tier_names = {
        row["tier"] for row in source.execute("SELECT DISTINCT tier FROM objects ORDER BY tier")
    }
    if "move_jobs" in _source_tables(source):
        move_columns = _require_columns(source, "move_jobs", {"src_tier", "dst_tier"})
        assert {"src_tier", "dst_tier"}.issubset(move_columns)
        tier_names.update(
            row["tier"]
            for row in source.execute(
                "SELECT src_tier AS tier FROM move_jobs "
                "UNION SELECT dst_tier AS tier FROM move_jobs"
            )
        )
    tier_values = [
        {
            "name": name,
            "metadata": {},
            "created_at": _MIGRATED_AT,
            "updated_at": _MIGRATED_AT,
        }
        for name in sorted(tier_names)
    ]
    if tier_values:
        destination.execute(sa.insert(tiers), tier_values)
    tier_count = len(tier_values)

    def object_values(row: sqlite3.Row) -> dict[str, Any]:
        object_id = _legacy_uuid("object", row["bucket"], row["key"])
        return {
            "object_id": object_id,
            "bucket": row["bucket"],
            "object_key": row["key"],
            "size": row["size"],
            "metadata": _json_mapping(
                row["metadata"],
                f"metadata for object {row['bucket']!r}/{row['key']!r}",
            ),
            "created_at": _MIGRATED_AT,
            "updated_at": _MIGRATED_AT,
        }

    object_count = _copy_rows(
        source,
        destination,
        objects,
        "SELECT bucket, key, size, tier, metadata FROM objects ORDER BY bucket, key",
        object_values,
        batch_size=batch_size,
    )

    def placement_values(row: sqlite3.Row) -> dict[str, Any]:
        object_id = _legacy_uuid("object", row["bucket"], row["key"])
        return {
            "placement_id": _legacy_uuid("placement", str(object_id)),
            "object_id": object_id,
            "tier_name": row["tier"],
            "pool_id": None,
            "created_at": _MIGRATED_AT,
            "updated_at": _MIGRATED_AT,
        }

    placement_count = _copy_rows(
        source,
        destination,
        object_placements,
        "SELECT bucket, key, tier FROM objects ORDER BY bucket, key",
        placement_values,
        batch_size=batch_size,
    )
    _copy_rows(
        source,
        destination,
        object_mutation_fences,
        "SELECT bucket, key FROM objects ORDER BY bucket, key",
        lambda row: {
            "bucket": row["bucket"],
            "object_key": row["key"],
            "generation": 0,
        },
        batch_size=batch_size,
    )
    return SQLiteCatalogImportReport(
        source_layout="legacy",
        tiers=tier_count,
        pools=0,
        objects=object_count,
        placements=placement_count,
        move_jobs=0,
        move_job_transitions=0,
    )


def _copy_normalized_catalog(
    source: sqlite3.Connection,
    destination: Connection,
    *,
    batch_size: int,
) -> SQLiteCatalogImportReport:
    _require_columns(
        source,
        "tiers",
        {"name", "metadata", "created_at", "updated_at"},
    )
    tier_count = _copy_rows(
        source,
        destination,
        tiers,
        "SELECT name, metadata, created_at, updated_at FROM tiers ORDER BY name",
        lambda row: {
            "name": row["name"],
            "metadata": _json_mapping(row["metadata"], f"metadata for tier {row['name']!r}"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        },
        batch_size=batch_size,
    )

    _require_columns(
        source,
        "pools",
        {"pool_id", "tier_name", "metadata", "created_at", "updated_at"},
    )
    pool_count = _copy_rows(
        source,
        destination,
        pools,
        "SELECT pool_id, tier_name, metadata, created_at, updated_at FROM pools ORDER BY pool_id",
        lambda row: {
            "pool_id": row["pool_id"],
            "tier_name": row["tier_name"],
            "metadata": _json_mapping(row["metadata"], f"metadata for pool {row['pool_id']!r}"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        },
        batch_size=batch_size,
    )

    _require_columns(
        source,
        "objects",
        {
            "object_id",
            "bucket",
            "object_key",
            "size",
            "metadata",
            "created_at",
            "updated_at",
        },
    )
    object_count = _copy_rows(
        source,
        destination,
        objects,
        "SELECT object_id, bucket, object_key, size, metadata, created_at, updated_at "
        "FROM objects ORDER BY bucket, object_key",
        lambda row: {
            "object_id": _uuid(row["object_id"], "objects.object_id"),
            "bucket": row["bucket"],
            "object_key": row["object_key"],
            "size": row["size"],
            "metadata": _json_mapping(
                row["metadata"],
                f"metadata for object {row['bucket']!r}/{row['object_key']!r}",
            ),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        },
        batch_size=batch_size,
    )

    _require_columns(
        source,
        "object_placements",
        {
            "placement_id",
            "object_id",
            "tier_name",
            "pool_id",
            "created_at",
            "updated_at",
        },
    )
    placement_count = _copy_rows(
        source,
        destination,
        object_placements,
        "SELECT placement_id, object_id, tier_name, pool_id, created_at, updated_at "
        "FROM object_placements ORDER BY object_id",
        lambda row: {
            "placement_id": _uuid(row["placement_id"], "object_placements.placement_id"),
            "object_id": _uuid(row["object_id"], "object_placements.object_id"),
            "tier_name": row["tier_name"],
            "pool_id": row["pool_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        },
        batch_size=batch_size,
    )
    if placement_count != object_count:
        raise SQLiteCatalogImportError(
            "normalized SQLite catalog must have exactly one placement per object"
        )

    source_tables = _source_tables(source)
    if "object_mutation_fences" in source_tables:
        _require_columns(
            source,
            "object_mutation_fences",
            {"bucket", "object_key", "generation"},
        )
        _copy_rows(
            source,
            destination,
            object_mutation_fences,
            "SELECT bucket, object_key, generation FROM object_mutation_fences "
            "ORDER BY bucket, object_key",
            lambda row: {
                "bucket": row["bucket"],
                "object_key": row["object_key"],
                "generation": row["generation"],
            },
            batch_size=batch_size,
        )
    if "move_job_claim_fences" in source_tables:
        _require_columns(
            source,
            "move_job_claim_fences",
            {"idempotency_key"},
        )
        _copy_rows(
            source,
            destination,
            move_job_claim_fences,
            "SELECT idempotency_key FROM move_job_claim_fences ORDER BY idempotency_key",
            lambda row: {"idempotency_key": row["idempotency_key"]},
            batch_size=batch_size,
        )

    return SQLiteCatalogImportReport(
        source_layout="normalized",
        tiers=tier_count,
        pools=pool_count,
        objects=object_count,
        placements=placement_count,
        move_jobs=0,
        move_job_transitions=0,
    )


def _copy_move_history(
    source: sqlite3.Connection,
    destination: Connection,
    *,
    batch_size: int,
) -> tuple[int, int]:
    source_tables = _source_tables(source)
    if "move_jobs" not in source_tables:
        if "move_job_transitions" in source_tables:
            raise SQLiteCatalogImportError(
                "SQLite source contains move transitions without move jobs"
            )
        return 0, 0

    required_columns = {
        "idempotency_key",
        "src_tier",
        "dst_tier",
        "bucket",
        "object_key",
        "expected_size",
        "source_metadata",
        "state",
        "owner_id",
        "lease_expires_at",
        "transferred_size",
        "source_size",
        "source_checksum",
        "destination_size",
        "destination_checksum",
        "terminal_reason",
        "created_at",
        "updated_at",
    }
    columns = _require_columns(source, "move_jobs", required_columns)
    destination_generation = (
        "destination_generation"
        if "destination_generation" in columns
        else "NULL AS destination_generation"
    )
    verification_details = (
        "verification_details"
        if "verification_details" in columns
        else "'[]' AS verification_details"
    )
    query = (
        "SELECT idempotency_key, src_tier, dst_tier, bucket, object_key, "
        "expected_size, source_metadata, state, owner_id, lease_expires_at, "
        "transferred_size, source_size, source_checksum, destination_size, "
        f"destination_checksum, {destination_generation}, {verification_details}, "
        "terminal_reason, created_at, updated_at FROM move_jobs "
        "ORDER BY created_at, idempotency_key"
    )

    def move_values(row: sqlite3.Row) -> dict[str, Any]:
        identity = row["idempotency_key"]
        return {
            "idempotency_key": identity,
            "src_tier": row["src_tier"],
            "dst_tier": row["dst_tier"],
            "bucket": row["bucket"],
            "object_key": row["object_key"],
            "expected_size": row["expected_size"],
            "source_metadata": _json_mapping(
                row["source_metadata"],
                f"source metadata for move job {identity!r}",
            ),
            "state": row["state"],
            "owner_id": row["owner_id"],
            "lease_expires_at": row["lease_expires_at"],
            "transferred_size": row["transferred_size"],
            "source_size": row["source_size"],
            "source_checksum": row["source_checksum"],
            "destination_size": row["destination_size"],
            "destination_checksum": row["destination_checksum"],
            "destination_generation": row["destination_generation"],
            "verification_details": _redacted_json_list(
                row["verification_details"],
                f"verification details for move job {identity!r}",
            ),
            "terminal_reason": (
                None
                if row["terminal_reason"] is None
                else redact_text(str(row["terminal_reason"]))
            ),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    move_count = _copy_rows(
        source,
        destination,
        move_jobs,
        query,
        move_values,
        batch_size=batch_size,
    )

    if "move_job_transitions" not in source_tables:
        return move_count, 0
    _require_columns(
        source,
        "move_job_transitions",
        {
            "idempotency_key",
            "sequence",
            "from_state",
            "to_state",
            "reason",
            "created_at",
        },
    )
    transition_count = _copy_rows(
        source,
        destination,
        move_job_transitions,
        "SELECT idempotency_key, sequence, from_state, to_state, reason, created_at "
        "FROM move_job_transitions ORDER BY idempotency_key, sequence",
        lambda row: {
            "idempotency_key": row["idempotency_key"],
            "sequence": row["sequence"],
            "from_state": row["from_state"],
            "to_state": row["to_state"],
            "reason": redact_text(str(row["reason"])),
            "created_at": row["created_at"],
        },
        batch_size=batch_size,
    )
    return move_count, transition_count


def _copy_audit_history(
    source: sqlite3.Connection,
    destination: Connection,
    *,
    batch_size: int,
) -> int:
    """Copy versioned audit rows when the normalized source owns them.

    Older SQLite catalogs predate the audit schema and legitimately contribute
    no rows. Sources that do expose the table must satisfy the complete v1
    storage contract; accepting a partial table would silently discard the
    correlation dimensions operators rely on after cutover.
    """

    if "audit_events" not in _source_tables(source):
        return 0
    columns = {
        "event_id",
        "schema_version",
        "event_type",
        "outcome",
        "occurred_at",
        "recorded_at",
        "expires_at",
        "correlation_id",
        "causation_id",
        "actor_type",
        "actor_id",
        "bucket",
        "object_key",
        "job_id",
        "move_id",
        "move_sequence",
        "policy_name",
        "policy_version",
        "details",
    }
    _require_columns(source, "audit_events", columns)

    def event_values(row: sqlite3.Row) -> dict[str, Any]:
        event_id = _uuid(row["event_id"], "audit_events.event_id")
        causation_id = (
            None
            if row["causation_id"] is None
            else _uuid(row["causation_id"], "audit_events.causation_id")
        )
        try:
            event = AuditEvent(
                event_id=str(event_id),
                schema_version=row["schema_version"],
                event_type=row["event_type"],
                outcome=row["outcome"],
                occurred_at=row["occurred_at"],
                recorded_at=row["recorded_at"],
                expires_at=row["expires_at"],
                correlation_id=row["correlation_id"],
                causation_id=None if causation_id is None else str(causation_id),
                actor_type=row["actor_type"],
                actor_id=row["actor_id"],
                bucket=row["bucket"],
                object_key=row["object_key"],
                job_id=row["job_id"],
                move_id=row["move_id"],
                policy_name=row["policy_name"],
                policy_version=row["policy_version"],
                details=_json_mapping(
                    row["details"],
                    f"details for audit event {event_id}",
                ),
            )
            safe_event = redact_audit_event(event, _allow_pseudonyms=True)
            identity_fields = (
                "correlation_id",
                "actor_id",
                "bucket",
                "object_key",
                "job_id",
                "move_id",
                "policy_name",
                "policy_version",
            )
            if any(
                getattr(safe_event, field) != getattr(event, field)
                for field in identity_fields
            ):
                raise ValueError(
                    "persisted audit identifiers are not canonical and secret-safe"
                )
            event = safe_event
        except (TypeError, ValueError) as exc:
            raise SQLiteCatalogImportError(
                f"audit event {event_id} is invalid: {exc}"
            ) from exc
        return {
            "event_id": _uuid(event.event_id, "audit_events.event_id"),
            "schema_version": event.schema_version,
            "event_type": event.event_type,
            "outcome": event.outcome,
            "occurred_at": event.occurred_at,
            "recorded_at": event.recorded_at,
            "expires_at": event.expires_at,
            "correlation_id": event.correlation_id,
            "causation_id": (
                None
                if event.causation_id is None
                else _uuid(event.causation_id, "audit_events.causation_id")
            ),
            "actor_type": event.actor_type,
            "actor_id": event.actor_id,
            "bucket": event.bucket,
            "object_key": event.object_key,
            "job_id": event.job_id,
            "move_id": event.move_id,
            "move_sequence": row["move_sequence"],
            "policy_name": event.policy_name,
            "policy_version": event.policy_version,
            "details": dict(event.details),
        }

    return _copy_rows(
        source,
        destination,
        audit_events,
        "SELECT event_id, schema_version, event_type, outcome, occurred_at, "
        "recorded_at, expires_at, correlation_id, causation_id, actor_type, "
        "actor_id, bucket, object_key, job_id, move_id, policy_name, "
        "move_sequence, policy_version, details FROM audit_events "
        "ORDER BY occurred_at, event_id",
        event_values,
        batch_size=batch_size,
    )


def _copy_audit_move_heads(
    source: sqlite3.Connection,
    destination: Connection,
    *,
    batch_size: int,
) -> int:
    _require_columns(
        source,
        "audit_move_heads",
        {"move_id", "last_sequence", "last_event_id"},
    )

    def head_values(row: sqlite3.Row) -> dict[str, Any]:
        raw_move_id = row["move_id"]
        if not isinstance(raw_move_id, str):
            raise SQLiteCatalogImportError(
                "audit_move_heads.move_id must be a string"
            )
        safe_move_id = audit_text_identity(
            raw_move_id,
            _allow_pseudonym=True,
        )
        if safe_move_id != raw_move_id:
            raise SQLiteCatalogImportError(
                "audit_move_heads.move_id is not canonical and secret-safe"
            )
        return {
            "move_id": safe_move_id,
            "last_sequence": row["last_sequence"],
            "last_event_id": _uuid(
                row["last_event_id"],
                "audit_move_heads.last_event_id",
            ),
        }

    return _copy_rows(
        source,
        destination,
        audit_move_heads,
        "SELECT move_id, last_sequence, last_event_id FROM audit_move_heads "
        "ORDER BY move_id",
        head_values,
        batch_size=batch_size,
    )


def _copy_audit_event_tombstones(
    source: sqlite3.Connection,
    destination: Connection,
    *,
    batch_size: int,
) -> int:
    _require_columns(
        source,
        "audit_event_tombstones",
        {"event_id", "replay_digest", "causation_id", "expires_at"},
    )

    def tombstone_values(row: sqlite3.Row) -> dict[str, Any]:
        replay_digest = row["replay_digest"]
        if (
            not isinstance(replay_digest, str)
            or len(replay_digest) != 64
            or any(character not in "0123456789abcdef" for character in replay_digest)
        ):
            raise SQLiteCatalogImportError(
                "audit_event_tombstones.replay_digest must be lowercase SHA-256 hex"
            )
        expires_at = row["expires_at"]
        if expires_at is not None:
            try:
                canonical_expires_at = canonical_audit_timestamp(
                    expires_at,
                    "audit_event_tombstones.expires_at",
                )
            except ValueError as exc:
                raise SQLiteCatalogImportError(str(exc)) from exc
            if canonical_expires_at != expires_at:
                raise SQLiteCatalogImportError(
                    "audit_event_tombstones.expires_at must be a canonical UTC "
                    "audit timestamp"
                )
        return {
            "event_id": _uuid(
                row["event_id"],
                "audit_event_tombstones.event_id",
            ),
            "replay_digest": replay_digest,
            "causation_id": (
                None
                if row["causation_id"] is None
                else _uuid(
                    row["causation_id"],
                    "audit_event_tombstones.causation_id",
                )
            ),
            "expires_at": expires_at,
        }

    return _copy_rows(
        source,
        destination,
        audit_event_tombstones,
        "SELECT event_id, replay_digest, causation_id, expires_at "
        "FROM audit_event_tombstones ORDER BY event_id",
        tombstone_values,
        batch_size=batch_size,
    )


def _validate_imported_audit_heads(destination: Connection) -> None:
    heads = {
        row["move_id"]: (int(row["last_sequence"]), row["last_event_id"])
        for row in destination.execute(sa.select(audit_move_heads)).mappings()
    }
    active_event_ids = set(
        destination.execute(sa.select(audit_events.c.event_id)).scalars()
    )
    tombstoned_event_ids = set(
        destination.execute(
            sa.select(audit_event_tombstones.c.event_id)
        ).scalars()
    )
    if active_event_ids.intersection(tombstoned_event_ids):
        raise SQLiteCatalogImportError(
            "SQLite audit event cannot be both active and tombstoned"
        )
    known_event_ids = active_event_ids | tombstoned_event_ids
    for move_id, (_sequence, last_event_id) in heads.items():
        if last_event_id not in known_event_ids:
            raise SQLiteCatalogImportError(
                f"SQLite audit move head for {move_id!r} references an unknown event"
            )
    rows = destination.execute(
        sa.select(
            audit_events.c.event_id,
            audit_events.c.move_id,
            audit_events.c.move_sequence,
        ).where(
            audit_events.c.move_id.is_not(None)
            | audit_events.c.move_sequence.is_not(None)
        )
    ).mappings()
    for row in rows:
        move_id = row["move_id"]
        sequence = row["move_sequence"]
        if move_id is None or sequence is None or move_id not in heads:
            raise SQLiteCatalogImportError(
                "SQLite audit move event is missing its durable move head"
            )
        last_sequence, last_event_id = heads[move_id]
        if int(sequence) > last_sequence:
            raise SQLiteCatalogImportError(
                "SQLite audit move event is newer than its durable move head"
            )
        if int(sequence) == last_sequence and row["event_id"] != last_event_id:
            raise SQLiteCatalogImportError(
                "SQLite audit move head does not identify its latest retained event"
            )
    for raw_move_id in destination.execute(
        sa.select(move_jobs.c.idempotency_key)
    ).scalars():
        try:
            safe_move_id = audit_text_identity(raw_move_id)
        except ValueError as exc:
            raise SQLiteCatalogImportError(
                f"move job {raw_move_id!r} has an invalid audit identity: {exc}"
            ) from exc
        if safe_move_id not in heads:
            raise SQLiteCatalogImportError(
                f"move job {raw_move_id!r} is missing its durable audit head"
            )


def _backfill_move_audit_history(
    destination: Connection,
    *,
    retention: AuditRetentionPolicy,
) -> int:
    """Synthesize a linear audit prefix for a pre-audit SQLite source."""

    rows = destination.execute(
        sa.select(
            move_job_transitions.c.idempotency_key,
            move_job_transitions.c.sequence,
            move_job_transitions.c.from_state,
            move_job_transitions.c.to_state,
            move_job_transitions.c.reason,
            move_job_transitions.c.created_at,
            move_jobs.c.src_tier,
            move_jobs.c.dst_tier,
            move_jobs.c.bucket,
            move_jobs.c.object_key,
            move_jobs.c.expected_size,
        )
        .join(
            move_jobs,
            move_jobs.c.idempotency_key
            == move_job_transitions.c.idempotency_key,
        )
        .order_by(
            move_job_transitions.c.idempotency_key,
            move_job_transitions.c.sequence,
        )
    ).mappings()
    previous_by_move: dict[str, str] = {}
    last_by_move: dict[str, tuple[int, str]] = {}
    count = 0
    for row in rows:
        move_id = str(row["idempotency_key"])
        transition_sequence = int(row["sequence"])
        to_state = str(row["to_state"])
        event_type = AuditEventType.MOVE_TRANSITIONED
        outcome = AuditOutcome.SUCCEEDED
        if to_state == "prepared":
            event_type = AuditEventType.MOVE_PREPARED
            outcome = AuditOutcome.STARTED
        elif to_state == "completed":
            event_type = AuditEventType.MOVE_COMPLETED
        elif to_state == "failed":
            event_type = AuditEventType.MOVE_FAILED
            outcome = AuditOutcome.FAILED
        event = redact_audit_event(
            AuditEvent.create(
                event_type,
                outcome,
                AuditContext(
                    correlation_id=move_id,
                    actor_type="migration",
                    actor_id="sqlite-import",
                    causation_id=previous_by_move.get(move_id),
                ),
                event_id=stable_audit_event_id(
                    "move-transition",
                    move_id,
                    str(transition_sequence),
                ),
                occurred_at=str(row["created_at"]),
                recorded_at=str(row["created_at"]),
                retention=retention,
                bucket=str(row["bucket"]),
                object_key=str(row["object_key"]),
                move_id=move_id,
                details={
                    "transition_sequence": transition_sequence,
                    "from_state": row["from_state"],
                    "to_state": row["to_state"],
                    "reason": row["reason"],
                    "src_tier": row["src_tier"],
                    "dst_tier": row["dst_tier"],
                    "expected_size": row["expected_size"],
                    "backfilled": True,
                },
            )
        )
        destination.execute(
            sa.insert(audit_events).values(
                **SQLCatalog._audit_event_values(
                    event,
                    move_sequence=transition_sequence,
                )
            )
        )
        previous_by_move[move_id] = event.event_id
        assert event.move_id is not None
        last_by_move[event.move_id] = (transition_sequence, event.event_id)
        count += 1
    if last_by_move:
        destination.execute(
            sa.insert(audit_move_heads),
            [
                {
                    "move_id": move_id,
                    "last_sequence": sequence,
                    "last_event_id": UUID(event_id),
                }
                for move_id, (sequence, event_id) in sorted(last_by_move.items())
            ],
        )
    return count
