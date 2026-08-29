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

from .catalog import SQLCatalog
from .schema import (
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
            report = SQLiteCatalogImportReport(
                source_layout=report.source_layout,
                tiers=report.tiers,
                pools=report.pools,
                objects=report.objects,
                placements=report.placements,
                move_jobs=move_count,
                move_job_transitions=transition_count,
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
            "verification_details": _json_list(
                row["verification_details"],
                f"verification details for move job {identity!r}",
            ),
            "terminal_reason": row["terminal_reason"],
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
            "reason": row["reason"],
            "created_at": row["created_at"],
        },
        batch_size=batch_size,
    )
    return move_count, transition_count
