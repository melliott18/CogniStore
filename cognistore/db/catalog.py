from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List
from uuid import NAMESPACE_URL, UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Connection, Engine, RowMapping

from cognistore.core.audit import (
    AuditContext,
    AuditEvent,
    AuditEventType,
    AuditOutcome,
    AuditQuery,
    AuditRetentionPolicy,
    audit_event_replay_digest,
    audit_text_identity,
    redact_audit_event,
    stable_audit_event_id,
)
from cognistore.core.catalog import (
    Catalog,
    ObjectRecord,
    ScanFence,
    validate_catalog_size,
)
from cognistore.core.move_jobs import (
    MoveJob,
    MoveJobLeaseError,
    MoveJobState,
    MoveJobTransition,
    validate_move_job_transition,
)
from cognistore.utils.redaction import redact, redact_text

from .engine import create_catalog_engine
from .migrations import MigrationManager, catalog_schema_exists
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

_MOVE_JOB_COLUMNS = tuple(move_jobs.c)
_ALLOWED_MOVE_UPDATES = frozenset(
    {
        "transferred_size",
        "source_size",
        "source_checksum",
        "destination_size",
        "destination_checksum",
        "destination_generation",
        "verification_details",
        "terminal_reason",
    }
)


class CatalogSchemaError(RuntimeError):
    """The configured database cannot be used by this catalog version."""


class CatalogSchemaNotInstalledError(CatalogSchemaError):
    """The database has no CogniStore catalog schema."""


class CatalogSchemaOutdatedError(CatalogSchemaError):
    """The database has a catalog schema that requires a writable migration."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _stable_uuid(kind: str, *values: str) -> UUID:
    # Keep newly written identities identical to the frozen normalization
    # migration and the legacy SQLite importer so downgrade/re-upgrade cycles
    # do not rewrite internal object and placement identities.
    identity = json.dumps([kind, *values], ensure_ascii=True, separators=(",", ":"))
    return uuid5(NAMESPACE_URL, identity)


class SQLCatalog(Catalog):
    """Transactional SQL catalog shared by SQLite and PostgreSQL.

    Each method owns one short transaction and obtains a connection from the
    engine. PostgreSQL callers therefore never share mutable sessions between
    worker and heartbeat threads. Durable fence rows serialize mutations for a
    logical object and close the insertion gap that row-locking move jobs alone
    would leave during scan publication.
    """

    def __init__(
        self,
        locator: str | Path,
        *,
        read_only: bool = False,
        migrate: bool = True,
        audit_retention: AuditRetentionPolicy | None = None,
    ) -> None:
        self.db_path = str(locator)
        self.read_only = read_only
        self.audit_retention = audit_retention or AuditRetentionPolicy()
        self._engine, self._conn = create_catalog_engine(locator, read_only=read_only)
        self._sqlite_lock = threading.RLock()
        self._closed = False
        migrations = MigrationManager(audit_retention=self.audit_retention)
        try:
            if read_only:
                schema_exists = catalog_schema_exists(self._engine)
                if not schema_exists:
                    inspector = sa.inspect(self._engine)
                    schema_present = any(
                        inspector.has_table(table)
                        for table in (
                            "alembic_version",
                            "objects",
                            "move_jobs",
                            "object_placements",
                            "tiers",
                        )
                    )
                    error_type = (
                        CatalogSchemaOutdatedError
                        if schema_present
                        else CatalogSchemaNotInstalledError
                    )
                    raise error_type(
                        "read-only catalog schema is not at the current migration "
                        "head; open it writable and migrate it first"
                    )
                if not migrations.is_at_head(self._engine):
                    raise CatalogSchemaOutdatedError(
                        "read-only catalog schema is not at the current migration "
                        "head; open it writable and migrate it first"
                    )
            elif migrate:
                migrations.upgrade(self._engine)
                if not catalog_schema_exists(self._engine):
                    raise CatalogSchemaError(
                        "catalog migration head is missing one or more owned tables"
                    )
            elif not catalog_schema_exists(self._engine):
                raise CatalogSchemaNotInstalledError("catalog schema is not installed")
            elif not migrations.is_at_head(self._engine):
                raise CatalogSchemaOutdatedError(
                    "catalog schema is not at the current migration head"
                )
        except BaseException:
            self.close()
            raise

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def backend(self) -> str:
        return self._engine.dialect.name

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._engine.dispose()
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:
                pass

    def __enter__(self) -> SQLCatalog:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[Connection]:
        if self._closed:
            raise RuntimeError("catalog is closed")
        lock = self._sqlite_lock if self.backend == "sqlite" else contextlib.nullcontext()
        with lock:
            try:
                with self._engine.begin() as connection:
                    yield connection
            except sa.exc.DBAPIError as exc:
                if self.backend == "sqlite" and isinstance(exc.orig, sqlite3.Error):
                    raise exc.orig from exc
                raise

    @contextlib.contextmanager
    def _connection(self) -> Iterator[Connection]:
        if self._closed:
            raise RuntimeError("catalog is closed")
        lock = self._sqlite_lock if self.backend == "sqlite" else contextlib.nullcontext()
        with lock:
            with self._engine.connect() as connection:
                yield connection

    @staticmethod
    def _do_nothing_insert(connection: Connection, table: sa.Table, values: dict[str, Any]) -> None:
        statement: Any
        if connection.dialect.name == "postgresql":
            statement = postgresql.insert(table).values(**values).on_conflict_do_nothing()
        elif connection.dialect.name == "sqlite":
            statement = sqlite.insert(table).values(**values).on_conflict_do_nothing()
        else:  # pragma: no cover - factory rejects unsupported dialects
            raise RuntimeError(f"unsupported catalog dialect: {connection.dialect.name}")
        connection.execute(statement)

    def _lock_move_key(self, connection: Connection, idempotency_key: str) -> None:
        self._do_nothing_insert(
            connection,
            move_job_claim_fences,
            {"idempotency_key": idempotency_key},
        )
        statement = sa.select(move_job_claim_fences.c.idempotency_key).where(
            move_job_claim_fences.c.idempotency_key == idempotency_key
        )
        if connection.dialect.name == "postgresql":
            statement = statement.with_for_update()
        connection.execute(statement).one()

    def _lock_object(self, connection: Connection, bucket: str, key: str) -> None:
        self._do_nothing_insert(
            connection,
            object_mutation_fences,
            {"bucket": bucket, "object_key": key, "generation": 0},
        )
        statement = sa.select(object_mutation_fences.c.generation).where(
            object_mutation_fences.c.bucket == bucket,
            object_mutation_fences.c.object_key == key,
        )
        if connection.dialect.name == "postgresql":
            statement = statement.with_for_update()
        connection.execute(statement).one()

    @staticmethod
    def _ensure_tier(connection: Connection, tier: str, now: str) -> None:
        SQLCatalog._do_nothing_insert(
            connection,
            tiers,
            {"name": tier, "metadata": {}, "created_at": now, "updated_at": now},
        )

    @staticmethod
    def _object_id(bucket: str, key: str) -> UUID:
        return _stable_uuid("object", bucket, key)

    @staticmethod
    def _placement_id(object_id: UUID) -> UUID:
        return _stable_uuid("placement", str(object_id))

    def _write_object(
        self,
        connection: Connection,
        bucket: str,
        key: str,
        *,
        size: int,
        tier: str,
        metadata: Mapping[str, object],
        now: str,
    ) -> None:
        self._ensure_tier(connection, tier, now)
        object_id = self._object_id(bucket, key)
        current = connection.execute(
            sa.select(objects.c.object_id).where(
                objects.c.bucket == bucket,
                objects.c.object_key == key,
            )
        ).scalar_one_or_none()
        if current is None:
            connection.execute(
                sa.insert(objects).values(
                    object_id=object_id,
                    bucket=bucket,
                    object_key=key,
                    size=size,
                    metadata=dict(metadata),
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            object_id = current
            connection.execute(
                sa.update(objects)
                .where(objects.c.object_id == object_id)
                .values(size=size, metadata=dict(metadata), updated_at=now)
            )

        placement = (
            connection.execute(
                sa.select(
                    object_placements.c.placement_id,
                    object_placements.c.tier_name,
                    object_placements.c.pool_id,
                ).where(object_placements.c.object_id == object_id)
            )
            .mappings()
            .first()
        )
        if placement is None:
            connection.execute(
                sa.insert(object_placements).values(
                    placement_id=self._placement_id(object_id),
                    object_id=object_id,
                    tier_name=tier,
                    pool_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            pool_id = placement["pool_id"] if placement["tier_name"] == tier else None
            connection.execute(
                sa.update(object_placements)
                .where(object_placements.c.object_id == object_id)
                .values(tier_name=tier, pool_id=pool_id, updated_at=now)
            )

    def upsert(
        self,
        bucket: str,
        key: str,
        size: int,
        tier: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        validate_catalog_size(size)
        now = _timestamp()
        with self._transaction() as connection:
            self._lock_object(connection, bucket, key)
            self._write_object(
                connection,
                bucket,
                key,
                size=size,
                tier=tier,
                metadata=metadata or {},
                now=now,
            )

    def capture_scan_fence(self, bucket: str, key: str) -> ScanFence:
        with self._connection() as connection:
            jobs = self._select_scan_move_jobs(connection, bucket, key)
        return ScanFence(
            bucket=bucket,
            key=key,
            move_jobs=self._scan_move_job_fingerprints(jobs),
        )

    def upsert_scan_observation(
        self,
        bucket: str,
        key: str,
        *,
        size: int,
        tier: str,
        generation: str,
        metadata: dict[str, object] | None,
        fence: ScanFence,
    ) -> bool:
        if (fence.bucket, fence.key) != (bucket, key):
            raise ValueError("scan fence does not identify the observed object")
        if not isinstance(generation, str) or not generation:
            raise ValueError("scan observation requires a non-empty generation")
        validate_catalog_size(size)
        now = _timestamp()
        with self._transaction() as connection:
            self._lock_object(connection, bucket, key)
            jobs = self._select_scan_move_jobs(connection, bucket, key)
            if self._scan_move_job_fingerprints(
                jobs
            ) != fence.move_jobs or not self._scan_observation_is_authoritative(
                jobs, tier=tier, generation=generation
            ):
                return False
            self._write_object(
                connection,
                bucket,
                key,
                size=size,
                tier=tier,
                metadata=metadata or {},
                now=now,
            )
            return True

    @staticmethod
    def _object_select() -> sa.Select[Any]:
        return sa.select(
            objects.c.bucket,
            objects.c.object_key,
            objects.c.size,
            object_placements.c.tier_name,
            objects.c.metadata,
        ).select_from(
            objects.join(
                object_placements,
                object_placements.c.object_id == objects.c.object_id,
            )
        )

    @staticmethod
    def _record(row: RowMapping) -> ObjectRecord:
        return ObjectRecord(
            bucket=row["bucket"],
            key=row["object_key"],
            size=row["size"],
            tier=row["tier_name"],
            metadata=dict(row["metadata"] or {}),
        )

    def get(self, bucket: str, key: str) -> ObjectRecord | None:
        statement = self._object_select().where(
            objects.c.bucket == bucket,
            objects.c.object_key == key,
        )
        with self._connection() as connection:
            row = connection.execute(statement).mappings().first()
        return None if row is None else self._record(row)

    def update_placement(self, bucket: str, key: str, tier: str) -> None:
        now = _timestamp()
        with self._transaction() as connection:
            self._lock_object(connection, bucket, key)
            object_id = connection.execute(
                sa.select(objects.c.object_id).where(
                    objects.c.bucket == bucket,
                    objects.c.object_key == key,
                )
            ).scalar_one_or_none()
            if object_id is None:
                raise KeyError(f"Object not found: {bucket}/{key}")
            self._ensure_tier(connection, tier, now)
            placement = (
                connection.execute(
                    sa.select(
                        object_placements.c.tier_name,
                        object_placements.c.pool_id,
                    ).where(object_placements.c.object_id == object_id)
                )
                .mappings()
                .first()
            )
            if placement is None:
                raise RuntimeError(f"Object placement is missing: {bucket}/{key}")
            pool_id = placement["pool_id"] if placement["tier_name"] == tier else None
            connection.execute(
                sa.update(object_placements)
                .where(object_placements.c.object_id == object_id)
                .values(tier_name=tier, pool_id=pool_id, updated_at=now)
            )

    def upsert_placement(
        self,
        bucket: str,
        key: str,
        *,
        size: int,
        tier: str,
        checksum: str | None = None,
    ) -> None:
        validate_catalog_size(size)
        now = _timestamp()
        with self._transaction() as connection:
            self._lock_object(connection, bucket, key)
            existing = connection.execute(
                sa.select(objects.c.metadata).where(
                    objects.c.bucket == bucket,
                    objects.c.object_key == key,
                )
            ).scalar_one_or_none()
            metadata = dict(existing or {})
            if checksum is not None:
                metadata["sha256"] = checksum
            self._write_object(
                connection,
                bucket,
                key,
                size=size,
                tier=tier,
                metadata=metadata,
                now=now,
            )

    def delete(self, bucket: str, key: str) -> None:
        with self._transaction() as connection:
            self._lock_object(connection, bucket, key)
            connection.execute(
                sa.delete(objects).where(
                    objects.c.bucket == bucket,
                    objects.c.object_key == key,
                )
            )

    @staticmethod
    def _literal_prefix(connection: Connection, column: Any, value: str) -> Any:
        parameter = sa.bindparam("literal_prefix", value, type_=column.type)
        if connection.dialect.name == "postgresql":
            return sa.func.substr(column, 1, sa.func.octet_length(parameter)) == parameter
        binary_column = sa.cast(column, sa.LargeBinary())
        binary_parameter = sa.cast(parameter, sa.LargeBinary())
        return (
            sa.func.substr(binary_column, 1, sa.func.length(binary_parameter)) == binary_parameter
        )

    def list(self, bucket: str, prefix: str = "") -> list[ObjectRecord]:
        with self._connection() as connection:
            statement = (
                self._object_select()
                .where(
                    objects.c.bucket == bucket,
                    self._literal_prefix(connection, objects.c.object_key, prefix),
                )
                .order_by(objects.c.object_key)
            )
            rows = connection.execute(statement).mappings().all()
        return [self._record(row) for row in rows]

    def append_audit_event(self, event: AuditEvent) -> AuditEvent:
        """Append one redacted event, idempotently by event UUID."""

        if self.read_only:
            raise PermissionError("cannot append audit events to a read-only catalog")
        if not isinstance(event, AuditEvent):
            raise ValueError("event must be an AuditEvent")
        with self._transaction() as connection:
            direct_replay = self._select_audit_event(connection, event.event_id)
            if direct_replay == event:
                return direct_replay
            tombstone = self._select_audit_tombstone(connection, event.event_id)
            safe = self._prepare_audit_event(
                event,
                allow_pseudonyms=tombstone is not None,
            )
            return self._insert_audit_event(connection, safe)

    def get_audit_event(self, event_id: str) -> AuditEvent | None:
        try:
            identifier = UUID(str(event_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("event_id must be a UUID") from exc
        with self._connection() as connection:
            row = (
                connection.execute(
                    sa.select(audit_events).where(audit_events.c.event_id == identifier)
                )
                .mappings()
                .first()
            )
        return None if row is None else self._audit_event_from_row(row)

    def list_audit_events(self, query: AuditQuery | None = None) -> List[AuditEvent]:
        criteria = query or AuditQuery()
        if not isinstance(criteria, AuditQuery):
            raise ValueError("query must be an AuditQuery")
        statement = sa.select(audit_events)
        for column, value in (
            (audit_events.c.correlation_id, criteria.correlation_id),
            (audit_events.c.job_id, criteria.job_id),
            (audit_events.c.move_id, criteria.move_id),
            (audit_events.c.bucket, criteria.bucket),
            (audit_events.c.object_key, criteria.object_key),
            (audit_events.c.policy_name, criteria.policy_name),
            (audit_events.c.policy_version, criteria.policy_version),
            (audit_events.c.actor_type, criteria.actor_type),
            (audit_events.c.actor_id, criteria.actor_id),
        ):
            if value is not None:
                statement = statement.where(column == value)
        if criteria.event_types is not None:
            statement = statement.where(audit_events.c.event_type.in_(sorted(criteria.event_types)))
        if criteria.outcomes is not None:
            statement = statement.where(audit_events.c.outcome.in_(sorted(criteria.outcomes)))
        if criteria.occurred_after is not None:
            statement = statement.where(audit_events.c.occurred_at >= criteria.occurred_after)
        if criteria.occurred_before is not None:
            statement = statement.where(audit_events.c.occurred_at < criteria.occurred_before)
        order = (
            (audit_events.c.occurred_at.asc(), audit_events.c.event_id.asc())
            if criteria.ascending
            else (audit_events.c.occurred_at.desc(), audit_events.c.event_id.desc())
        )
        statement = statement.order_by(*order).limit(criteria.limit)
        with self._connection() as connection:
            rows = connection.execute(statement).mappings().all()
        return [self._audit_event_from_row(row) for row in rows]

    def prune_audit_events(
        self,
        occurred_before: str | datetime,
        *,
        limit: int = 1000,
    ) -> int:
        query_value = (
            occurred_before.isoformat()
            if isinstance(occurred_before, datetime)
            else occurred_before
        )
        cutoff = AuditQuery(occurred_before=query_value, limit=limit).occurred_before
        assert cutoff is not None
        return self._prune_audit_events(
            audit_events.c.occurred_at < cutoff,
            limit=limit,
        )

    def prune_expired_audit_events(
        self,
        now: str | datetime,
        *,
        limit: int = 1000,
    ) -> int:
        query_value = now.isoformat() if isinstance(now, datetime) else now
        cutoff = AuditQuery(occurred_before=query_value, limit=limit).occurred_before
        assert cutoff is not None
        return self._prune_audit_events(
            audit_events.c.expires_at.is_not(None) & (audit_events.c.expires_at <= cutoff),
            limit=limit,
        )

    def _prune_audit_events(self, predicate: Any, *, limit: int) -> int:
        if self.read_only:
            raise PermissionError("cannot prune audit events from a read-only catalog")
        # AuditQuery supplies the shared bounded-limit validation.
        AuditQuery(limit=limit)
        with self._transaction() as connection:
            rows = connection.execute(
                sa.select(audit_events)
                .where(predicate)
                .order_by(audit_events.c.occurred_at, audit_events.c.event_id)
                .limit(limit)
            ).mappings().all()
            events = [self._audit_event_from_row(row) for row in rows]
            identifiers = [UUID(event.event_id) for event in events]
            for event in events:
                self._do_nothing_insert(
                    connection,
                    audit_event_tombstones,
                    {
                        "event_id": UUID(event.event_id),
                        "replay_digest": audit_event_replay_digest(event),
                        "causation_id": (
                            None
                            if event.causation_id is None
                            else UUID(event.causation_id)
                        ),
                        "expires_at": event.expires_at,
                    },
                )
            if identifiers:
                result = connection.execute(
                    sa.delete(audit_events).where(audit_events.c.event_id.in_(identifiers))
                )
                return max(0, int(result.rowcount or 0))
            return 0

    def _prepare_audit_event(
        self,
        event: AuditEvent,
        *,
        allow_pseudonyms: bool = False,
    ) -> AuditEvent:
        if not isinstance(event, AuditEvent):
            raise ValueError("event must be an AuditEvent")
        return redact_audit_event(
            replace(
                event,
                expires_at=self.audit_retention.expires_at(event.occurred_at),
            ),
            _allow_pseudonyms=allow_pseudonyms,
        )

    def _insert_audit_event(
        self,
        connection: Connection,
        event: AuditEvent,
    ) -> AuditEvent:
        existing = self._select_audit_event(connection, event.event_id)
        if existing is not None:
            self._assert_audit_replay_matches(existing, event)
            return existing
        tombstone = self._select_audit_tombstone(connection, event.event_id)
        if tombstone is not None:
            if audit_event_replay_digest(event) != tombstone["replay_digest"]:
                raise ValueError(
                    f"audit event {event.event_id} was pruned with different data"
                )
            return replace(
                event,
                causation_id=(
                    None
                    if tombstone["causation_id"] is None
                    else str(tombstone["causation_id"])
                ),
                expires_at=tombstone["expires_at"],
            )

        move_sequence: int | None = None
        head: RowMapping | None = None
        if event.move_id is not None:
            self._lock_move_key(connection, event.move_id)
            existing = self._select_audit_event(connection, event.event_id)
            if existing is not None:
                self._assert_audit_replay_matches(existing, event)
                return existing
            head = (
                connection.execute(
                    sa.select(
                        audit_move_heads.c.last_event_id,
                        audit_move_heads.c.last_sequence,
                    )
                    .where(
                        audit_move_heads.c.move_id == event.move_id,
                    )
                )
                .mappings()
                .first()
            )
            if head is None:
                move_sequence = 1
            else:
                last_event_id = str(head["last_event_id"])
                move_sequence = int(head["last_sequence"]) + 1
                event = replace(event, causation_id=last_event_id)

        self._do_nothing_insert(
            connection,
            audit_events,
            self._audit_event_values(event, move_sequence=move_sequence),
        )
        persisted = self._select_audit_event(connection, event.event_id)
        assert persisted is not None
        self._assert_audit_replay_matches(persisted, event)
        if event.move_id is not None:
            assert move_sequence is not None
            head_values = {
                "move_id": event.move_id,
                "last_sequence": move_sequence,
                "last_event_id": UUID(event.event_id),
            }
            if head is None:
                connection.execute(sa.insert(audit_move_heads).values(**head_values))
            else:
                connection.execute(
                    sa.update(audit_move_heads)
                    .where(audit_move_heads.c.move_id == event.move_id)
                    .values(
                        last_sequence=move_sequence,
                        last_event_id=UUID(event.event_id),
                    )
                )
        return persisted

    @staticmethod
    def _audit_event_values(
        event: AuditEvent,
        *,
        move_sequence: int | None = None,
    ) -> dict[str, Any]:
        return {
            "event_id": UUID(event.event_id),
            "schema_version": event.schema_version,
            "event_type": event.event_type,
            "outcome": event.outcome,
            "occurred_at": event.occurred_at,
            "recorded_at": event.recorded_at,
            "expires_at": event.expires_at,
            "correlation_id": event.correlation_id,
            "causation_id": (None if event.causation_id is None else UUID(event.causation_id)),
            "actor_type": event.actor_type,
            "actor_id": event.actor_id,
            "bucket": event.bucket,
            "object_key": event.object_key,
            "job_id": event.job_id,
            "move_id": event.move_id,
            "move_sequence": move_sequence,
            "policy_name": event.policy_name,
            "policy_version": event.policy_version,
            "details": dict(event.details),
        }

    @staticmethod
    def _select_audit_event(
        connection: Connection,
        event_id: str,
    ) -> AuditEvent | None:
        row = (
            connection.execute(
                sa.select(audit_events).where(
                    audit_events.c.event_id == UUID(event_id)
                )
            )
            .mappings()
            .first()
        )
        return None if row is None else SQLCatalog._audit_event_from_row(row)

    @staticmethod
    def _select_audit_tombstone(
        connection: Connection,
        event_id: str,
    ) -> RowMapping | None:
        return (
            connection.execute(
                sa.select(audit_event_tombstones).where(
                    audit_event_tombstones.c.event_id == UUID(event_id)
                )
            )
            .mappings()
            .first()
        )

    @staticmethod
    def _assert_audit_replay_matches(
        persisted: AuditEvent,
        event: AuditEvent,
    ) -> None:
        expected = (
            replace(event, causation_id=persisted.causation_id)
            if event.move_id is not None
            else event
        )
        if replace(persisted, expires_at=event.expires_at) != expected:
            raise ValueError(
                f"audit event {event.event_id} already exists with different data"
            )

    @staticmethod
    def _audit_event_from_row(row: RowMapping) -> AuditEvent:
        return AuditEvent(
            event_id=str(row["event_id"]),
            schema_version=row["schema_version"],
            event_type=row["event_type"],
            outcome=row["outcome"],
            occurred_at=row["occurred_at"],
            recorded_at=row["recorded_at"],
            expires_at=row["expires_at"],
            correlation_id=row["correlation_id"],
            causation_id=(None if row["causation_id"] is None else str(row["causation_id"])),
            actor_type=row["actor_type"],
            actor_id=row["actor_id"],
            bucket=row["bucket"],
            object_key=row["object_key"],
            job_id=row["job_id"],
            move_id=row["move_id"],
            policy_name=row["policy_name"],
            policy_version=row["policy_version"],
            details=dict(row["details"] or {}),
        )

    def claim_move_job(
        self,
        idempotency_key: str,
        *,
        src_tier: str,
        dst_tier: str,
        bucket: str,
        key: str,
        expected_size: int,
        source_metadata: Mapping[str, Any],
        owner_id: str,
        now: str,
        lease_expires_at: str,
        audit_context: AuditContext | None = None,
    ) -> MoveJob:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must be a non-empty string")
        validate_catalog_size(expected_size, field="expected_size")
        with self._transaction() as connection:
            self._lock_move_key(connection, idempotency_key)
            self._lock_object(connection, bucket, key)
            existing = self._select_move_job(connection, idempotency_key, for_update=True)
            transition: MoveJobTransition | None = None
            retry_from: MoveJob | None = None
            if existing is None:
                connection.execute(
                    sa.insert(move_jobs).values(
                        idempotency_key=idempotency_key,
                        src_tier=src_tier,
                        dst_tier=dst_tier,
                        bucket=bucket,
                        object_key=key,
                        expected_size=expected_size,
                        source_metadata=dict(source_metadata),
                        state=MoveJobState.PREPARED.value,
                        owner_id=owner_id,
                        lease_expires_at=lease_expires_at,
                        verification_details=[],
                        created_at=now,
                        updated_at=now,
                    )
                )
                transition = self._insert_move_transition(
                    connection,
                    idempotency_key,
                    None,
                    MoveJobState.PREPARED,
                    "move prepared",
                    now,
                )
            else:
                Catalog._assert_same_move(
                    existing,
                    src_tier=src_tier,
                    dst_tier=dst_tier,
                    bucket=bucket,
                    key=key,
                )
                if existing.state.terminal:
                    return existing
                if (
                    existing.owner_id not in (None, owner_id)
                    and existing.lease_expires_at is not None
                    and existing.lease_expires_at > now
                ):
                    raise MoveJobLeaseError(
                        f"Move job {idempotency_key!r} is leased by "
                        f"{existing.owner_id!r} until {existing.lease_expires_at}"
                    )
                connection.execute(
                    sa.update(move_jobs)
                    .where(move_jobs.c.idempotency_key == idempotency_key)
                    .values(
                        owner_id=owner_id,
                        lease_expires_at=lease_expires_at,
                        updated_at=now,
                    )
                )
                retry_from = existing
            claimed = self._select_move_job(connection, idempotency_key)
            assert claimed is not None
            if transition is not None:
                self._insert_move_audit_event(
                    connection,
                    claimed,
                    transition,
                    owner_id=owner_id,
                    audit_context=audit_context,
                )
            elif retry_from is not None:
                self._insert_move_retry_audit_event(
                    connection,
                    retry_from,
                    claimed,
                    owner_id=owner_id,
                    audit_context=audit_context,
                )
            return claimed

    def get_move_job(self, idempotency_key: str) -> MoveJob | None:
        with self._connection() as connection:
            return self._select_move_job(connection, idempotency_key)

    def list_move_jobs(
        self,
        *,
        states: set[MoveJobState] | None = None,
        idempotency_prefix: str | None = None,
    ) -> List[MoveJob]:
        if states is not None and not states:
            return []
        with self._connection() as connection:
            statement = sa.select(*_MOVE_JOB_COLUMNS)
            if states is not None:
                statement = statement.where(
                    move_jobs.c.state.in_(sorted(state.value for state in states))
                )
            if idempotency_prefix is not None:
                statement = statement.where(
                    self._literal_prefix(
                        connection,
                        move_jobs.c.idempotency_key,
                        idempotency_prefix,
                    )
                )
            rows = connection.execute(
                statement.order_by(move_jobs.c.created_at, move_jobs.c.idempotency_key)
            ).mappings()
            return [self._move_job_from_row(row) for row in rows]

    def list_move_job_transitions(self, idempotency_key: str) -> List[MoveJobTransition]:
        statement = (
            sa.select(move_job_transitions)
            .where(move_job_transitions.c.idempotency_key == idempotency_key)
            .order_by(move_job_transitions.c.sequence)
        )
        with self._connection() as connection:
            rows = connection.execute(statement).mappings()
            return [
                MoveJobTransition(
                    sequence=row["sequence"],
                    idempotency_key=row["idempotency_key"],
                    from_state=(
                        None if row["from_state"] is None else MoveJobState(row["from_state"])
                    ),
                    to_state=MoveJobState(row["to_state"]),
                    reason=row["reason"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    def renew_move_job_lease(
        self,
        idempotency_key: str,
        *,
        owner_id: str,
        expected_state: MoveJobState | None,
        now: str,
        lease_expires_at: str,
    ) -> MoveJob:
        with self._transaction() as connection:
            self._lock_move_key(connection, idempotency_key)
            initial = self._select_move_job(connection, idempotency_key)
            if initial is None:
                raise KeyError(f"Move job not found: {idempotency_key}")
            self._lock_object(connection, initial.bucket, initial.key)
            job = self._select_move_job(connection, idempotency_key, for_update=True)
            assert job is not None
            if expected_state is None and job.state.terminal:
                return job
            if job.owner_id != owner_id:
                raise MoveJobLeaseError(
                    f"Move job {idempotency_key!r} is not owned by {owner_id!r}"
                )
            if expected_state is not None and job.state != expected_state:
                raise RuntimeError(
                    f"Move job {idempotency_key!r} is {job.state.value}, "
                    f"expected {expected_state.value}"
                )
            connection.execute(
                sa.update(move_jobs)
                .where(
                    move_jobs.c.idempotency_key == idempotency_key,
                    move_jobs.c.owner_id == owner_id,
                )
                .values(lease_expires_at=lease_expires_at, updated_at=now)
            )
            renewed = self._select_move_job(connection, idempotency_key)
            assert renewed is not None
            return renewed

    def transition_move_job(
        self,
        idempotency_key: str,
        *,
        owner_id: str,
        expected_state: MoveJobState,
        to_state: MoveJobState,
        reason: str,
        now: str,
        lease_expires_at: str,
        updates: Mapping[str, Any] | None = None,
        audit_context: AuditContext | None = None,
    ) -> MoveJob:
        validate_move_job_transition(expected_state, to_state)
        changes = dict(redact(dict(updates or {})))
        reason = redact_text(reason)
        unknown = changes.keys() - _ALLOWED_MOVE_UPDATES
        if unknown:
            raise ValueError(f"Unsupported move-job updates: {', '.join(sorted(unknown))}")
        with self._transaction() as connection:
            self._lock_move_key(connection, idempotency_key)
            initial = self._select_move_job(connection, idempotency_key)
            if initial is None:
                raise KeyError(f"Move job not found: {idempotency_key}")
            self._lock_object(connection, initial.bucket, initial.key)
            job = self._select_owned_move_job(connection, idempotency_key, owner_id, expected_state)
            values = {
                "state": to_state.value,
                "owner_id": None if to_state.terminal else owner_id,
                "lease_expires_at": None if to_state.terminal else lease_expires_at,
                "updated_at": now,
                "transferred_size": changes.get("transferred_size", job.transferred_size),
                "source_size": changes.get("source_size", job.source_size),
                "source_checksum": changes.get("source_checksum", job.source_checksum),
                "destination_size": changes.get("destination_size", job.destination_size),
                "destination_checksum": changes.get(
                    "destination_checksum", job.destination_checksum
                ),
                "destination_generation": changes.get(
                    "destination_generation", job.destination_generation
                ),
                "verification_details": list(
                    changes.get("verification_details", job.verification_details)
                ),
                "terminal_reason": changes.get("terminal_reason", job.terminal_reason),
            }
            connection.execute(
                sa.update(move_jobs)
                .where(move_jobs.c.idempotency_key == idempotency_key)
                .values(**values)
            )
            transition = self._insert_move_transition(
                connection,
                idempotency_key,
                expected_state,
                to_state,
                reason,
                now,
            )
            updated = self._select_move_job(connection, idempotency_key)
            assert updated is not None
            self._insert_move_audit_event(
                connection,
                updated,
                transition,
                owner_id=owner_id,
                audit_context=audit_context,
                updates=changes,
            )
            return updated

    def commit_move_job_placement(
        self,
        idempotency_key: str,
        *,
        owner_id: str,
        size: int,
        tier: str,
        checksum: str,
        now: str,
        lease_expires_at: str,
        audit_context: AuditContext | None = None,
    ) -> MoveJob:
        validate_move_job_transition(MoveJobState.VERIFIED, MoveJobState.COMMITTED)
        validate_catalog_size(size)
        with self._transaction() as connection:
            self._lock_move_key(connection, idempotency_key)
            initial = self._select_move_job(connection, idempotency_key)
            if initial is None:
                raise KeyError(f"Move job not found: {idempotency_key}")
            self._lock_object(connection, initial.bucket, initial.key)
            job = self._select_owned_move_job(
                connection,
                idempotency_key,
                owner_id,
                MoveJobState.VERIFIED,
            )
            existing = connection.execute(
                sa.select(objects.c.metadata).where(
                    objects.c.bucket == job.bucket,
                    objects.c.object_key == job.key,
                )
            ).scalar_one_or_none()
            metadata = dict(existing or {})
            metadata["sha256"] = checksum
            self._write_object(
                connection,
                job.bucket,
                job.key,
                size=size,
                tier=tier,
                metadata=metadata,
                now=now,
            )
            connection.execute(
                sa.update(move_jobs)
                .where(move_jobs.c.idempotency_key == idempotency_key)
                .values(
                    state=MoveJobState.COMMITTED.value,
                    lease_expires_at=lease_expires_at,
                    updated_at=now,
                )
            )
            transition = self._insert_move_transition(
                connection,
                idempotency_key,
                MoveJobState.VERIFIED,
                MoveJobState.COMMITTED,
                "catalog placement committed",
                now,
            )
            updated = self._select_move_job(connection, idempotency_key)
            assert updated is not None
            self._insert_move_audit_event(
                connection,
                updated,
                transition,
                owner_id=owner_id,
                audit_context=audit_context,
            )
            return updated

    def register_tier(self, name: str, metadata: Mapping[str, object] | None = None) -> None:
        now = _timestamp()
        with self._transaction() as connection:
            self._ensure_tier(connection, name, now)
            connection.execute(
                sa.update(tiers)
                .where(tiers.c.name == name)
                .values(metadata=dict(metadata or {}), updated_at=now)
            )

    def register_pool(
        self,
        pool_id: str,
        tier: str,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        now = _timestamp()
        with self._transaction() as connection:
            self._ensure_tier(connection, tier, now)
            values = {
                "pool_id": pool_id,
                "tier_name": tier,
                "metadata": dict(metadata or {}),
                "created_at": now,
                "updated_at": now,
            }
            statement: Any
            if connection.dialect.name == "postgresql":
                pg_insert = postgresql.insert(pools).values(**values)
                statement = pg_insert.on_conflict_do_update(
                    index_elements=[pools.c.pool_id],
                    set_={
                        "tier_name": pg_insert.excluded.tier_name,
                        "metadata": pg_insert.excluded.metadata,
                        "updated_at": pg_insert.excluded.updated_at,
                    },
                )
            else:
                sqlite_insert = sqlite.insert(pools).values(**values)
                statement = sqlite_insert.on_conflict_do_update(
                    index_elements=[pools.c.pool_id],
                    set_={
                        "tier_name": sqlite_insert.excluded.tier_name,
                        "metadata": sqlite_insert.excluded.metadata,
                        "updated_at": sqlite_insert.excluded.updated_at,
                    },
                )
            connection.execute(statement)

    @staticmethod
    def _select_move_job(
        connection: Connection,
        idempotency_key: str,
        *,
        for_update: bool = False,
    ) -> MoveJob | None:
        statement = sa.select(*_MOVE_JOB_COLUMNS).where(
            move_jobs.c.idempotency_key == idempotency_key
        )
        if for_update and connection.dialect.name == "postgresql":
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().first()
        return None if row is None else SQLCatalog._move_job_from_row(row)

    @staticmethod
    def _select_owned_move_job(
        connection: Connection,
        idempotency_key: str,
        owner_id: str,
        expected_state: MoveJobState,
    ) -> MoveJob:
        job = SQLCatalog._select_move_job(connection, idempotency_key, for_update=True)
        if job is None:
            raise KeyError(f"Move job not found: {idempotency_key}")
        if job.owner_id != owner_id:
            raise MoveJobLeaseError(f"Move job {idempotency_key!r} is not owned by {owner_id!r}")
        if job.state != expected_state:
            raise RuntimeError(
                f"Move job {idempotency_key!r} is {job.state.value}, "
                f"expected {expected_state.value}"
            )
        return job

    @staticmethod
    def _select_scan_move_jobs(connection: Connection, bucket: str, key: str) -> List[MoveJob]:
        statement = (
            sa.select(*_MOVE_JOB_COLUMNS)
            .where(move_jobs.c.bucket == bucket, move_jobs.c.object_key == key)
            .order_by(
                move_jobs.c.created_at,
                move_jobs.c.updated_at,
                move_jobs.c.idempotency_key,
            )
        )
        return [
            SQLCatalog._move_job_from_row(row) for row in connection.execute(statement).mappings()
        ]

    def _insert_move_audit_event(
        self,
        connection: Connection,
        job: MoveJob,
        transition: MoveJobTransition,
        *,
        owner_id: str,
        audit_context: AuditContext | None,
        updates: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        previous_event_id = self._latest_sql_move_audit_event_id(
            connection,
            job.idempotency_key,
        )
        context = Catalog._move_audit_context(
            job.idempotency_key,
            owner_id=owner_id,
            audit_context=audit_context,
            causation_id=(
                previous_event_id
                if previous_event_id is not None
                else None
                if audit_context is None
                else audit_context.causation_id
            ),
        )
        event_type = AuditEventType.MOVE_TRANSITIONED
        outcome = AuditOutcome.SUCCEEDED
        if transition.to_state == MoveJobState.PREPARED:
            event_type = AuditEventType.MOVE_PREPARED
            outcome = AuditOutcome.STARTED
        elif transition.to_state == MoveJobState.COMPLETED:
            event_type = AuditEventType.MOVE_COMPLETED
        elif transition.to_state == MoveJobState.FAILED:
            event_type = AuditEventType.MOVE_FAILED
            outcome = AuditOutcome.FAILED

        details: dict[str, Any] = {
            "transition_sequence": transition.sequence,
            "from_state": (None if transition.from_state is None else transition.from_state.value),
            "to_state": transition.to_state.value,
            "reason": transition.reason,
            "src_tier": job.src_tier,
            "dst_tier": job.dst_tier,
            "expected_size": job.expected_size,
        }
        for name, value in (updates or {}).items():
            if name in _ALLOWED_MOVE_UPDATES:
                details[name] = list(value) if isinstance(value, tuple) else value
        event = AuditEvent.create(
            event_type,
            outcome,
            context,
            event_id=stable_audit_event_id(
                "move-transition",
                job.idempotency_key,
                str(transition.sequence),
            ),
            occurred_at=transition.created_at,
            recorded_at=transition.created_at,
            retention=self.audit_retention,
            bucket=job.bucket,
            object_key=job.key,
            move_id=job.idempotency_key,
            details=details,
        )
        return self._insert_audit_event(connection, self._prepare_audit_event(event))

    def _insert_move_retry_audit_event(
        self,
        connection: Connection,
        previous_job: MoveJob,
        claimed_job: MoveJob,
        *,
        owner_id: str,
        audit_context: AuditContext | None,
    ) -> AuditEvent:
        previous_event_id = self._latest_sql_move_audit_event_id(
            connection,
            claimed_job.idempotency_key,
        )
        context = Catalog._move_audit_context(
            claimed_job.idempotency_key,
            owner_id=owner_id,
            audit_context=audit_context,
            causation_id=(
                previous_event_id
                if previous_event_id is not None
                else None
                if audit_context is None
                else audit_context.causation_id
            ),
        )
        event = AuditEvent.create(
            AuditEventType.MOVE_RETRY,
            AuditOutcome.RETRYING,
            context,
            event_id=stable_audit_event_id(
                "move-retry",
                claimed_job.idempotency_key,
                previous_event_id or "",
                claimed_job.updated_at,
                owner_id,
            ),
            occurred_at=claimed_job.updated_at,
            recorded_at=claimed_job.updated_at,
            retention=self.audit_retention,
            bucket=claimed_job.bucket,
            object_key=claimed_job.key,
            move_id=claimed_job.idempotency_key,
            details={
                "state": claimed_job.state.value,
                "previous_owner": previous_job.owner_id,
                "lease_expires_at": previous_job.lease_expires_at,
            },
        )
        return self._insert_audit_event(connection, self._prepare_audit_event(event))

    @staticmethod
    def _latest_sql_move_audit_event_id(
        connection: Connection,
        move_id: str,
    ) -> str | None:
        event_id = connection.execute(
            sa.select(audit_move_heads.c.last_event_id).where(
                audit_move_heads.c.move_id == audit_text_identity(move_id)
            )
        ).scalar_one_or_none()
        return None if event_id is None else str(event_id)

    @staticmethod
    def _insert_move_transition(
        connection: Connection,
        idempotency_key: str,
        from_state: MoveJobState | None,
        to_state: MoveJobState,
        reason: str,
        now: str,
    ) -> MoveJobTransition:
        next_sequence = connection.execute(
            sa.select(sa.func.coalesce(sa.func.max(move_job_transitions.c.sequence), 0) + 1).where(
                move_job_transitions.c.idempotency_key == idempotency_key
            )
        ).scalar_one()
        connection.execute(
            sa.insert(move_job_transitions).values(
                idempotency_key=idempotency_key,
                sequence=next_sequence,
                from_state=None if from_state is None else from_state.value,
                to_state=to_state.value,
                reason=reason,
                created_at=now,
            )
        )
        return MoveJobTransition(
            sequence=next_sequence,
            idempotency_key=idempotency_key,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            created_at=now,
        )

    @staticmethod
    def _move_job_from_row(row: RowMapping) -> MoveJob:
        return MoveJob(
            idempotency_key=row["idempotency_key"],
            src_tier=row["src_tier"],
            dst_tier=row["dst_tier"],
            bucket=row["bucket"],
            key=row["object_key"],
            expected_size=row["expected_size"],
            source_metadata=dict(row["source_metadata"] or {}),
            state=MoveJobState(row["state"]),
            owner_id=row["owner_id"],
            lease_expires_at=row["lease_expires_at"],
            transferred_size=row["transferred_size"],
            source_size=row["source_size"],
            source_checksum=row["source_checksum"],
            destination_size=row["destination_size"],
            destination_checksum=row["destination_checksum"],
            destination_generation=row["destination_generation"],
            verification_details=tuple(row["verification_details"] or ()),
            terminal_reason=row["terminal_reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
