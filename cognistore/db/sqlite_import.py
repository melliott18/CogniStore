"""Offline import of prototype SQLite catalogs into the SQL catalog schema."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from cognistore.core.access import AccessEvent
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
from cognistore.core.content_identity import (
    CHUNKING_ALGORITHM,
    CHUNKING_VERSION,
    CONTENT_IDENTITY_SCHEMA_VERSION,
    CONTENT_REPRESENTATION,
    DIGEST_ALGORITHM,
    cas_key_for_sha256,
)
from cognistore.core.placement_controls import ImportanceTag
from cognistore.core.topology import Pool, Tier
from cognistore.utils.redaction import redact, redact_text

from .catalog import SQLCatalog
from .schema import (
    access_events,
    audit_event_tombstones,
    audit_events,
    audit_move_heads,
    content_blobs,
    content_manifest_chunks,
    content_manifests,
    embedding_document_spaces,
    embedding_documents,
    embedding_passages,
    embedding_spaces,
    embedding_vectors,
    move_job_claim_fences,
    move_job_transitions,
    move_jobs,
    object_contents,
    object_embedding_documents,
    object_mutation_fences,
    object_placements,
    objects,
    pools,
    tiers,
)

_MIGRATED_AT = "1970-01-01T00:00:00.000000Z"
_DEFAULT_BATCH_SIZE = 1000
_SOURCE_LAYOUT = Literal["legacy", "normalized"]


def _content_reference_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )

_DESTINATION_DATA_TABLES = (
    access_events,
    embedding_vectors,
    embedding_document_spaces,
    object_embedding_documents,
    embedding_passages,
    embedding_documents,
    embedding_spaces,
    audit_event_tombstones,
    audit_events,
    audit_move_heads,
    content_manifest_chunks,
    object_contents,
    content_manifests,
    content_blobs,
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
    content_blobs: int = 0
    content_manifests: int = 0
    content_manifest_chunks: int = 0
    object_contents: int = 0
    access_events: int = 0


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
        _reject_source_embedding_state(source_connection)

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

            content_counts = _copy_content_identity(
                source_connection,
                target_connection,
                batch_size=batch_size,
                imported_at=_content_reference_timestamp(),
            )
            _strip_unmapped_content_identity_metadata(
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
            access_count = _copy_access_history(
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
                audit_events=audit_count,
                content_blobs=content_counts[0],
                content_manifests=content_counts[1],
                content_manifest_chunks=content_counts[2],
                object_contents=content_counts[3],
                access_events=access_count,
            )

        source_connection.rollback()
        return report


def _reject_same_sqlite_catalog(source: Path, destination: SQLCatalog) -> None:
    if destination.backend != "sqlite":
        return
    database = destination.engine.url.database
    if database is None or database in ("", ":memory:"):
        return
    if Path(database).expanduser().resolve() == source:
        raise SQLiteCatalogImportError("source and destination must be different catalogs")


def _source_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _reject_source_embedding_state(connection: sqlite3.Connection) -> None:
    """Do not silently discard derived vectors from a manually edited source.

    Similarity search is PostgreSQL-only, so ordinary SQLite catalogs never
    contain embedding rows.  If a source was populated outside the supported
    API, require an explicit rebuild on the PostgreSQL destination instead of
    making the one-shot importer appear to preserve that unsupported state.
    """

    embedding_table_probes = {
        "embedding_spaces": "SELECT 1 FROM embedding_spaces LIMIT 1",
        "embedding_documents": "SELECT 1 FROM embedding_documents LIMIT 1",
        "embedding_passages": "SELECT 1 FROM embedding_passages LIMIT 1",
        "embedding_vectors": "SELECT 1 FROM embedding_vectors LIMIT 1",
        "embedding_document_spaces": "SELECT 1 FROM embedding_document_spaces LIMIT 1",
        "object_embedding_documents": "SELECT 1 FROM object_embedding_documents LIMIT 1",
    }
    source_tables = _source_tables(connection)
    for table_name, probe in embedding_table_probes.items():
        if table_name not in source_tables:
            continue
        if connection.execute(probe).fetchone() is not None:
            raise SQLiteCatalogImportError(
                "SQLite embedding state cannot be imported; rebuild embeddings "
                "from canonical extraction text after the catalog import"
            )


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


def _topology_active(value: object) -> bool:
    if not isinstance(value, int) or value not in (0, 1):
        raise ValueError("active must be a SQLite boolean (0 or 1)")
    return bool(value)


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


def _import_importance(row: sqlite3.Row) -> dict[str, Any] | None:
    if "importance" not in row.keys() or row["importance"] in (None, "null"):
        return None
    try:
        return ImportanceTag.from_mapping(
            _json_mapping(row["importance"], "object importance")
        ).to_dict()
    except ValueError as exc:
        raise SQLiteCatalogImportError(f"invalid source importance: {exc}") from exc


def _import_importance_revision(row: sqlite3.Row) -> int:
    value = row["importance_revision"] if "importance_revision" in row.keys() else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 2**63 - 1:
        raise SQLiteCatalogImportError("invalid source importance revision")
    return value


def _import_placement_start(row: sqlite3.Row) -> str | None:
    value = (
        row["placement_started_at"] if "placement_started_at" in row.keys()
        else (None if row["updated_at"] == _MIGRATED_AT else row["updated_at"])
    )
    if value is None:
        return None
    try:
        canonical = canonical_audit_timestamp(value, "placement_started_at")
    except ValueError as exc:
        raise SQLiteCatalogImportError(f"invalid source placement start: {exc}") from exc
    if canonical != value:
        raise SQLiteCatalogImportError("source placement start must use canonical UTC")
    return canonical


def _copy_normalized_catalog(
    source: sqlite3.Connection,
    destination: Connection,
    *,
    batch_size: int,
) -> SQLiteCatalogImportReport:
    tier_columns = _require_columns(
        source,
        "tiers",
        {"name", "metadata", "created_at", "updated_at"},
    )
    def tier_values(row: sqlite3.Row) -> dict[str, Any]:
        try:
            definition = Tier(
                name=row["name"],
                metadata=_json_mapping(row["metadata"], "tier metadata"),
                active=_topology_active(row["active"]) if "active" in tier_columns else True,
            )
        except ValueError as exc:
            raise SQLiteCatalogImportError(f"invalid source tier topology: {exc}") from exc
        return {
            **definition.to_mapping(),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    tier_count = _copy_rows(
        source,
        destination,
        tiers,
        "SELECT * FROM tiers ORDER BY name",
        tier_values,
        batch_size=batch_size,
    )

    pool_columns = _require_columns(
        source,
        "pools",
        {"pool_id", "tier_name", "metadata", "created_at", "updated_at"},
    )
    topology_columns = {"region", "members", "localities", "attributes", "active"}
    if pool_columns & topology_columns and not topology_columns <= pool_columns:
        raise SQLiteCatalogImportError("source pool topology schema is incomplete")
    has_topology = topology_columns <= pool_columns

    def pool_values(row: sqlite3.Row) -> dict[str, Any]:
        try:
            definition = Pool.from_mapping({
                "pool_id": row["pool_id"],
                "tier": row["tier_name"],
                "metadata": _json_mapping(row["metadata"], "pool metadata"),
                "region": row["region"] if has_topology else None,
                "members": _json_list(row["members"], "pool members") if has_topology else [],
                "localities": (
                    _json_list(row["localities"], "pool localities") if has_topology else []
                ),
                "attributes": (
                    _json_mapping(row["attributes"], "pool attributes") if has_topology else {}
                ),
                "active": _topology_active(row["active"]) if has_topology else False,
            })
        except ValueError as exc:
            raise SQLiteCatalogImportError(f"invalid source pool topology: {exc}") from exc
        values = definition.to_mapping()
        values["tier_name"] = values.pop("tier")
        return {**values, "created_at": row["created_at"], "updated_at": row["updated_at"]}

    pool_count = _copy_rows(
        source,
        destination,
        pools,
        "SELECT * FROM pools ORDER BY pool_id",
        pool_values,
        batch_size=batch_size,
    )
    invalid_parent = destination.execute(
        sa.select(pools.c.pool_id)
        .select_from(pools.join(tiers, pools.c.tier_name == tiers.c.name))
        .where(pools.c.active.is_(True), tiers.c.active.is_(False))
        .limit(1)
    ).first()
    if invalid_parent is not None:
        raise SQLiteCatalogImportError("active source pool references an inactive tier")

    object_columns = _require_columns(
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
    importance_columns = {"importance", "importance_revision"}
    has_controls = bool(object_columns & importance_columns)
    if has_controls and not importance_columns <= object_columns:
        raise SQLiteCatalogImportError("source has partial importance columns")
    object_count = _copy_rows(
        source,
        destination,
        objects,
        "SELECT * FROM objects ORDER BY bucket, object_key",
        lambda row: {
            "object_id": _uuid(row["object_id"], "objects.object_id"),
            "importance": _import_importance(row),
            "importance_revision": _import_importance_revision(row),
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

    placement_columns = _require_columns(
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
    if has_controls != ("placement_started_at" in placement_columns):
        raise SQLiteCatalogImportError("source has partial placement controls columns")
    placement_count = _copy_rows(
        source,
        destination,
        object_placements,
        "SELECT * FROM object_placements ORDER BY object_id",
        lambda row: {
            "placement_id": _uuid(row["placement_id"], "object_placements.placement_id"),
            "placement_started_at": _import_placement_start(row),
            "object_id": _uuid(row["object_id"], "object_placements.object_id"),
            "tier_name": row["tier_name"],
            "pool_id": row["pool_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        },
        batch_size=batch_size,
    )
    inactive_placement = destination.execute(
        sa.select(object_placements.c.placement_id)
        .select_from(object_placements.join(tiers, object_placements.c.tier_name == tiers.c.name))
        .where(tiers.c.active.is_(False))
        .limit(1)
    ).first()
    if inactive_placement is not None:
        raise SQLiteCatalogImportError("source placement references an inactive tier")
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


def _copy_content_identity(
    source: sqlite3.Connection,
    destination: Connection,
    *,
    batch_size: int,
    imported_at: str,
) -> tuple[int, int, int, int]:
    """Copy the optional revision-0004 identity topology in FK order."""

    table_names = {
        "content_blobs",
        "content_manifests",
        "content_manifest_chunks",
        "object_contents",
    }
    present = _source_tables(source).intersection(table_names)
    if not present:
        return 0, 0, 0, 0
    if present != table_names:
        raise SQLiteCatalogImportError(
            "SQLite source must contain content_blobs, content_manifests, "
            "content_manifest_chunks, and object_contents together, or none of them"
        )

    blob_columns = _source_columns(source, "content_blobs")
    _require_columns(
        source,
        "content_blobs",
        {"sha256", "digest_algorithm", "size", "cas_key", "created_at"},
    )
    reference_columns = {"reference_count", "unreferenced_at"}
    present_reference_columns = blob_columns.intersection(reference_columns)
    if present_reference_columns and present_reference_columns != reference_columns:
        raise SQLiteCatalogImportError(
            "SQLite source content_blobs must contain reference_count and "
            "unreferenced_at together, or neither"
        )
    source_has_references = present_reference_columns == reference_columns
    blob_select = (
        "SELECT sha256, digest_algorithm, size, cas_key, created_at, "
        "reference_count, unreferenced_at FROM content_blobs ORDER BY sha256"
        if source_has_references
        else "SELECT sha256, digest_algorithm, size, cas_key, created_at "
        "FROM content_blobs ORDER BY sha256"
    )
    blob_count = _copy_rows(
        source,
        destination,
        content_blobs,
        blob_select,
        lambda row: {
            "sha256": row["sha256"],
            "digest_algorithm": row["digest_algorithm"],
            "size": row["size"],
            "cas_key": row["cas_key"],
            "created_at": row["created_at"],
            "reference_count": (
                row["reference_count"] if source_has_references else 0
            ),
            "unreferenced_at": (
                row["unreferenced_at"] if source_has_references else imported_at
            ),
        },
        batch_size=batch_size,
    )

    _require_columns(
        source,
        "content_manifests",
        {
            "manifest_id",
            "content_sha256",
            "schema_version",
            "representation",
            "chunking_algorithm",
            "chunking_version",
            "chunk_size",
            "chunk_count",
            "created_at",
        },
    )
    manifest_count = _copy_rows(
        source,
        destination,
        content_manifests,
        "SELECT manifest_id, content_sha256, schema_version, representation, "
        "chunking_algorithm, chunking_version, chunk_size, chunk_count, created_at "
        "FROM content_manifests ORDER BY manifest_id",
        lambda row: {
            "manifest_id": _uuid(row["manifest_id"], "content_manifests.manifest_id"),
            "content_sha256": row["content_sha256"],
            "schema_version": row["schema_version"],
            "representation": row["representation"],
            "chunking_algorithm": row["chunking_algorithm"],
            "chunking_version": row["chunking_version"],
            "chunk_size": row["chunk_size"],
            "chunk_count": row["chunk_count"],
            "created_at": row["created_at"],
        },
        batch_size=batch_size,
    )

    _require_columns(
        source,
        "content_manifest_chunks",
        {
            "manifest_id",
            "chunk_index",
            "chunk_sha256",
            "byte_offset",
            "byte_length",
        },
    )
    chunk_count = _copy_rows(
        source,
        destination,
        content_manifest_chunks,
        "SELECT manifest_id, chunk_index, chunk_sha256, byte_offset, byte_length "
        "FROM content_manifest_chunks ORDER BY manifest_id, chunk_index",
        lambda row: {
            "manifest_id": _uuid(
                row["manifest_id"],
                "content_manifest_chunks.manifest_id",
            ),
            "chunk_index": row["chunk_index"],
            "chunk_sha256": row["chunk_sha256"],
            "byte_offset": row["byte_offset"],
            "byte_length": row["byte_length"],
        },
        batch_size=batch_size,
    )

    _require_columns(
        source,
        "object_contents",
        {"object_id", "manifest_id", "created_at", "updated_at"},
    )
    object_content_count = _copy_rows(
        source,
        destination,
        object_contents,
        "SELECT object_id, manifest_id, created_at, updated_at "
        "FROM object_contents ORDER BY object_id",
        lambda row: {
            "object_id": _uuid(row["object_id"], "object_contents.object_id"),
            "manifest_id": _uuid(row["manifest_id"], "object_contents.manifest_id"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        },
        batch_size=batch_size,
    )
    if not source_has_references:
        _backfill_imported_content_references(
            destination,
            unreferenced_at=imported_at,
        )
    _validate_imported_content_identity(destination)
    return blob_count, manifest_count, chunk_count, object_content_count


def _content_integer(raw: object, field: str, *, minimum: int) -> int:
    if (
        isinstance(raw, bool)
        or not isinstance(raw, int)
        or raw < minimum
        or raw > 2**63 - 1
    ):
        qualifier = "positive" if minimum == 1 else "non-negative"
        raise SQLiteCatalogImportError(
            f"{field} must be a {qualifier} integer no greater than {2**63 - 1}"
        )
    return raw


def _validate_imported_content_blob(row: Any) -> None:
    digest = row["sha256"]
    try:
        expected_cas_key = cas_key_for_sha256(digest)
    except ValueError as exc:
        raise SQLiteCatalogImportError(
            "content_blobs.sha256 must be lowercase SHA-256 hex"
        ) from exc
    if row["digest_algorithm"] != DIGEST_ALGORITHM:
        raise SQLiteCatalogImportError(
            f"content blob {digest!r} uses unsupported digest algorithm "
            f"{row['digest_algorithm']!r}"
        )
    _content_integer(row["size"], f"content blob {digest!r} size", minimum=0)
    if row["cas_key"] != expected_cas_key:
        raise SQLiteCatalogImportError(
            f"content blob {digest!r} does not use its canonical CAS key"
        )


def _validate_imported_manifest_header(row: Any) -> tuple[int, int, int]:
    manifest_id = row["manifest_id"]
    schema_version = _content_integer(
        row["schema_version"],
        f"content manifest {manifest_id} schema_version",
        minimum=1,
    )
    if schema_version != CONTENT_IDENTITY_SCHEMA_VERSION:
        raise SQLiteCatalogImportError(
            f"content manifest {manifest_id} uses unsupported schema version "
            f"{schema_version}"
        )
    if row["representation"] != CONTENT_REPRESENTATION:
        raise SQLiteCatalogImportError(
            f"content manifest {manifest_id} uses unsupported representation "
            f"{row['representation']!r}"
        )
    if row["chunking_algorithm"] != CHUNKING_ALGORITHM:
        raise SQLiteCatalogImportError(
            f"content manifest {manifest_id} uses unsupported chunking algorithm "
            f"{row['chunking_algorithm']!r}"
        )
    chunking_version = _content_integer(
        row["chunking_version"],
        f"content manifest {manifest_id} chunking_version",
        minimum=1,
    )
    if chunking_version != CHUNKING_VERSION:
        raise SQLiteCatalogImportError(
            f"content manifest {manifest_id} uses unsupported chunking version "
            f"{chunking_version}"
        )
    chunk_size = _content_integer(
        row["chunk_size"],
        f"content manifest {manifest_id} chunk_size",
        minimum=1,
    )
    chunk_count = _content_integer(
        row["chunk_count"],
        f"content manifest {manifest_id} chunk_count",
        minimum=0,
    )
    content_size = _content_integer(
        row["content_size"],
        f"content manifest {manifest_id} content size",
        minimum=0,
    )
    return chunk_size, chunk_count, content_size


def _validate_imported_manifest_extent(
    manifest_id: object,
    *,
    declared_chunk_count: int,
    observed_chunk_count: int,
    content_size: int,
    observed_size: int,
) -> None:
    if observed_chunk_count != declared_chunk_count:
        raise SQLiteCatalogImportError(
            f"content manifest {manifest_id} declares {declared_chunk_count} chunks "
            f"but contains {observed_chunk_count}"
        )
    if observed_size != content_size:
        raise SQLiteCatalogImportError(
            f"content manifest {manifest_id} chunks cover {observed_size} bytes "
            f"but its content blob has size {content_size}"
        )


def _validate_imported_content_manifests(destination: Connection) -> None:
    full_blobs = content_blobs.alias("import_manifest_full_blobs")
    chunk_blobs = content_blobs.alias("import_manifest_chunk_blobs")
    rows = destination.execute(
        sa.select(
            content_manifests.c.manifest_id,
            content_manifests.c.schema_version,
            content_manifests.c.representation,
            content_manifests.c.chunking_algorithm,
            content_manifests.c.chunking_version,
            content_manifests.c.chunk_size,
            content_manifests.c.chunk_count,
            full_blobs.c.size.label("content_size"),
            content_manifest_chunks.c.chunk_index,
            content_manifest_chunks.c.chunk_sha256,
            content_manifest_chunks.c.byte_offset,
            content_manifest_chunks.c.byte_length,
            chunk_blobs.c.size.label("chunk_blob_size"),
        )
        .select_from(
            content_manifests.join(
                full_blobs,
                full_blobs.c.sha256 == content_manifests.c.content_sha256,
            )
            .outerjoin(
                content_manifest_chunks,
                content_manifest_chunks.c.manifest_id == content_manifests.c.manifest_id,
            )
            .outerjoin(
                chunk_blobs,
                chunk_blobs.c.sha256 == content_manifest_chunks.c.chunk_sha256,
            )
        )
        .order_by(
            content_manifests.c.manifest_id,
            content_manifest_chunks.c.chunk_index,
        )
    ).mappings()

    current_manifest_id: object | None = None
    manifest_chunk_size = 0
    declared_chunk_count = 0
    content_size = 0
    observed_chunk_count = 0
    observed_size = 0
    previous_chunk_length: int | None = None
    for row in rows:
        manifest_id = row["manifest_id"]
        if manifest_id != current_manifest_id:
            if current_manifest_id is not None:
                _validate_imported_manifest_extent(
                    current_manifest_id,
                    declared_chunk_count=declared_chunk_count,
                    observed_chunk_count=observed_chunk_count,
                    content_size=content_size,
                    observed_size=observed_size,
                )
            current_manifest_id = manifest_id
            (
                manifest_chunk_size,
                declared_chunk_count,
                content_size,
            ) = _validate_imported_manifest_header(row)
            observed_chunk_count = 0
            observed_size = 0
            previous_chunk_length = None

        if row["chunk_index"] is None:
            continue
        chunk_index = _content_integer(
            row["chunk_index"],
            f"content manifest {manifest_id} chunk index",
            minimum=0,
        )
        if chunk_index != observed_chunk_count:
            raise SQLiteCatalogImportError(
                f"content manifest {manifest_id} chunk indexes must be contiguous "
                "from zero"
            )
        byte_offset = _content_integer(
            row["byte_offset"],
            f"content manifest {manifest_id} chunk {chunk_index} byte_offset",
            minimum=0,
        )
        if byte_offset != observed_size:
            raise SQLiteCatalogImportError(
                f"content manifest {manifest_id} chunk offsets must be contiguous "
                "from zero"
            )
        byte_length = _content_integer(
            row["byte_length"],
            f"content manifest {manifest_id} chunk {chunk_index} byte_length",
            minimum=1,
        )
        if byte_length > manifest_chunk_size:
            raise SQLiteCatalogImportError(
                f"content manifest {manifest_id} chunk {chunk_index} exceeds "
                "the canonical chunk size"
            )
        if previous_chunk_length is not None and previous_chunk_length != manifest_chunk_size:
            raise SQLiteCatalogImportError(
                f"content manifest {manifest_id} has a short non-final chunk"
            )
        chunk_blob_size = _content_integer(
            row["chunk_blob_size"],
            f"content blob {row['chunk_sha256']!r} size",
            minimum=0,
        )
        if chunk_blob_size != byte_length:
            raise SQLiteCatalogImportError(
                f"content manifest {manifest_id} chunk {chunk_index} length does not "
                "match its content blob"
            )
        observed_chunk_count += 1
        observed_size += byte_length
        previous_chunk_length = byte_length

    if current_manifest_id is not None:
        _validate_imported_manifest_extent(
            current_manifest_id,
            declared_chunk_count=declared_chunk_count,
            observed_chunk_count=observed_chunk_count,
            content_size=content_size,
            observed_size=observed_size,
        )


def _validate_imported_object_contents(destination: Connection) -> None:
    full_blobs = content_blobs.alias("import_object_content_blobs")
    rows = destination.execute(
        sa.select(
            objects.c.bucket,
            objects.c.object_key,
            objects.c.size.label("object_size"),
            objects.c.metadata,
            content_manifests.c.schema_version,
            content_manifests.c.representation,
            content_manifests.c.chunking_algorithm,
            content_manifests.c.chunking_version,
            content_manifests.c.chunk_size,
            content_manifests.c.chunk_count,
            full_blobs.c.digest_algorithm,
            full_blobs.c.sha256,
            full_blobs.c.size.label("content_size"),
            full_blobs.c.cas_key,
        )
        .select_from(
            object_contents.join(
                objects,
                objects.c.object_id == object_contents.c.object_id,
            )
            .join(
                content_manifests,
                content_manifests.c.manifest_id == object_contents.c.manifest_id,
            )
            .join(
                full_blobs,
                full_blobs.c.sha256 == content_manifests.c.content_sha256,
            )
        )
        .order_by(objects.c.bucket, objects.c.object_key)
    ).mappings()
    for row in rows:
        coordinates = f"{row['bucket']!r}/{row['object_key']!r}"
        object_size = _content_integer(
            row["object_size"],
            f"object {coordinates} size",
            minimum=0,
        )
        content_size = _content_integer(
            row["content_size"],
            f"object {coordinates} content size",
            minimum=0,
        )
        if object_size != content_size:
            raise SQLiteCatalogImportError(
                f"object {coordinates} size does not match its content manifest"
            )
        expected_summary = {
            "schema_version": row["schema_version"],
            "representation": row["representation"],
            "digest_algorithm": row["digest_algorithm"],
            "sha256": row["sha256"],
            "size": content_size,
            "cas_key": row["cas_key"],
            "chunking_algorithm": row["chunking_algorithm"],
            "chunking_version": row["chunking_version"],
            "chunk_size": row["chunk_size"],
            "chunk_count": row["chunk_count"],
        }
        metadata = dict(row["metadata"] or {})
        if metadata.get("sha256") != row["sha256"]:
            raise SQLiteCatalogImportError(
                f"object {coordinates} SHA-256 metadata does not match its content manifest"
            )
        if metadata.get("content_identity") != expected_summary:
            raise SQLiteCatalogImportError(
                f"object {coordinates} content_identity metadata does not match its manifest"
            )


def _content_reference_aggregate() -> Any:
    object_edges = (
        sa.select(
            content_manifests.c.content_sha256.label("sha256"),
            sa.literal(1).label("object_references"),
            sa.literal(0).label("chunk_references"),
        )
        .select_from(
            object_contents.join(
                content_manifests,
                content_manifests.c.manifest_id == object_contents.c.manifest_id,
            )
        )
    )
    chunk_edges = (
        sa.select(
            content_manifest_chunks.c.chunk_sha256.label("sha256"),
            sa.literal(0).label("object_references"),
            sa.literal(1).label("chunk_references"),
        )
        .select_from(
            object_contents.join(
                content_manifest_chunks,
                content_manifest_chunks.c.manifest_id == object_contents.c.manifest_id,
            )
        )
    )
    edges = sa.union_all(object_edges, chunk_edges).subquery(
        "import_content_reference_edges"
    )
    return (
        sa.select(
            edges.c.sha256,
            sa.func.sum(edges.c.object_references).label("object_references"),
            sa.func.sum(edges.c.chunk_references).label("chunk_references"),
            sa.func.count().label("reference_count"),
        )
        .group_by(edges.c.sha256)
        .subquery("import_content_reference_counts")
    )


def _backfill_imported_content_references(
    destination: Connection,
    *,
    unreferenced_at: str,
) -> None:
    """Initialize ticket-35 state when importing a revision-0004 source."""

    destination.execute(
        sa.update(content_blobs).values(
            reference_count=0,
            unreferenced_at=unreferenced_at,
        )
    )
    counts = _content_reference_aggregate()
    for row in destination.execute(
        sa.select(counts.c.sha256, counts.c.reference_count).order_by(counts.c.sha256)
    ).mappings():
        destination.execute(
            sa.update(content_blobs)
            .where(content_blobs.c.sha256 == row["sha256"])
            .values(reference_count=row["reference_count"], unreferenced_at=None)
        )


def _validate_imported_content_references(destination: Connection) -> None:
    counts = _content_reference_aggregate()
    rows = destination.execute(
        sa.select(
            content_blobs.c.sha256,
            content_blobs.c.reference_count,
            content_blobs.c.unreferenced_at,
            sa.func.coalesce(counts.c.reference_count, 0).label(
                "expected_reference_count"
            ),
        )
        .select_from(
            content_blobs.outerjoin(
                counts,
                counts.c.sha256 == content_blobs.c.sha256,
            )
        )
        .order_by(content_blobs.c.sha256)
    ).mappings()
    for row in rows:
        sha256 = row["sha256"]
        reference_count = _content_integer(
            row["reference_count"],
            f"content blob {sha256!r} reference_count",
            minimum=0,
        )
        expected = int(row["expected_reference_count"])
        if reference_count != expected:
            raise SQLiteCatalogImportError(
                f"content blob {sha256!r} reference_count {reference_count} "
                f"does not match its {expected} active reference edges"
            )
        unreferenced_at = row["unreferenced_at"]
        if reference_count == 0:
            if unreferenced_at is None:
                raise SQLiteCatalogImportError(
                    f"content blob {sha256!r} has zero references without "
                    "unreferenced_at"
                )
            try:
                canonical_audit_timestamp(
                    unreferenced_at,
                    "content_blobs.unreferenced_at",
                )
            except ValueError as exc:
                raise SQLiteCatalogImportError(str(exc)) from exc
        elif unreferenced_at is not None:
            raise SQLiteCatalogImportError(
                f"content blob {sha256!r} is referenced but has unreferenced_at"
            )


def _validate_imported_content_identity(destination: Connection) -> None:
    for row in destination.execute(
        sa.select(
            content_blobs.c.sha256,
            content_blobs.c.digest_algorithm,
            content_blobs.c.size,
            content_blobs.c.cas_key,
        ).order_by(content_blobs.c.sha256)
    ).mappings():
        _validate_imported_content_blob(row)
    _validate_imported_content_manifests(destination)
    _validate_imported_object_contents(destination)
    _validate_imported_content_references(destination)


def _strip_unmapped_content_identity_metadata(
    destination: Connection,
    *,
    batch_size: int,
) -> None:
    """Remove the reserved projection from objects without a manifest mapping."""

    rows = destination.execute(
        sa.select(objects.c.object_id, objects.c.metadata)
        .select_from(
            objects.outerjoin(
                object_contents,
                object_contents.c.object_id == objects.c.object_id,
            )
        )
        .where(object_contents.c.object_id.is_(None))
        .order_by(objects.c.object_id)
    ).mappings()
    update_statement = (
        sa.update(objects)
        .where(objects.c.object_id == sa.bindparam("import_object_id"))
        .values(metadata=sa.bindparam("import_object_metadata"))
    )
    while batch := rows.fetchmany(batch_size):
        updates = []
        for row in batch:
            metadata = dict(row["metadata"] or {})
            if "content_identity" not in metadata:
                continue
            metadata.pop("content_identity")
            updates.append(
                {
                    "import_object_id": row["object_id"],
                    "import_object_metadata": metadata,
                }
            )
        if updates:
            destination.execute(update_statement, updates)


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


def _copy_access_history(
    source: sqlite3.Connection,
    destination: Connection,
    *,
    batch_size: int,
) -> int:
    """Preserve observations and expired retry identities without replaying them.

    Catalogs predating access telemetry have no table. When the table exists,
    every row must satisfy the complete current contract; partial or malformed
    history must abort the entire import. In particular, appending events via
    the public API would reset expired tombstones to active observations.
    """
    if "access_events" not in _source_tables(source):
        return 0
    columns = (
        "event_id",
        "operation_id",
        "correlation_id",
        "occurred_at",
        "kind",
        "bucket",
        "object_key",
        "tier",
        "source",
        "sample_rate",
        "schema_version",
        "expired",
    )
    _require_columns(source, "access_events", set(columns))

    def access_values(row: sqlite3.Row) -> dict[str, Any]:
        try:
            event = AccessEvent(
                event_id=row["event_id"],
                operation_id=row["operation_id"],
                correlation_id=row["correlation_id"],
                occurred_at=row["occurred_at"],
                kind=row["kind"],
                bucket=row["bucket"],
                key=row["object_key"],
                tier=row["tier"],
                source=row["source"],
                sample_rate=row["sample_rate"],
                schema_version=row["schema_version"],
            )
            if event.occurred_at != row["occurred_at"]:
                raise ValueError("occurred_at must be a canonical UTC access timestamp")
            expired = row["expired"]
            if not isinstance(expired, int) or expired not in (0, 1):
                raise ValueError("expired must be a SQLite boolean (0 or 1)")
        except (TypeError, ValueError, OverflowError) as exc:
            raise SQLiteCatalogImportError(f"invalid source access event: {exc}") from exc
        # Use the original values, preserving identities, provenance, event
        # time, sampling rates, and tombstones across the database cutover.
        return {**dict(row), "expired": bool(expired)}

    return _copy_rows(
        source,
        destination,
        access_events,
        "SELECT " + ", ".join(columns) + " FROM access_events ORDER BY occurred_at, event_id",
        access_values,
        batch_size=batch_size,
    )


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
