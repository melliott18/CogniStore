from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Connection, Engine, RowMapping, ScalarResult

from cognistore.auth.tenancy import TenantIsolationError, require_tenant, validate_tenant_id
from cognistore.core.access import (
    ACCESS_KINDS,
    AccessConfig,
    AccessEvent,
    AccessSnapshot,
    AccessWindow,
    access_cutoff,
    access_timestamp,
)
from cognistore.core.audit import (
    ORPHAN_CLEANUP_EVENT_TYPES,
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
from cognistore.core.audit_integrity import (
    AuditCheckpoint,
    AuditIntegrityResult,
    digest,
    event_digest,
    verify_snapshot,
)
from cognistore.core.budgets import BudgetConstraintError, BudgetDefinition
from cognistore.core.catalog import (
    Catalog,
    ObjectRecord,
    ScanFence,
    _assert_catalog_move_allowed,
    _budget_configuration_event,
    _budget_reservation_event,
    _importance_event,
    _prepare_budget_reservations,
    _validate_importance_actor,
    validate_catalog_size,
)
from cognistore.core.content_identity import (
    MAX_IN_MEMORY_CONTENT_CHUNKS,
    ContentChunk,
    ContentChunkSequence,
    ObjectContent,
)
from cognistore.core.content_references import (
    ContentReferenceReport,
    ContentReferenceSnapshot,
    build_content_reference_report,
)
from cognistore.core.legal_hold_lock import LegalHoldFence
from cognistore.core.legal_holds import (
    LEGAL_HOLD_EVENT_TYPES,
    LegalHold,
    _scope_identity,
    guard_legal_hold,
    legal_hold_actor,
    legal_hold_event,
    serialize_cleanup,
)
from cognistore.core.move_jobs import (
    MoveJob,
    MoveJobConflictError,
    MoveJobLeaseError,
    MoveJobState,
    MoveJobTransition,
    validate_move_job_transition,
)
from cognistore.core.object_mutation_lock import ObjectMutationConflictError, ObjectMutationFence
from cognistore.core.placement_controls import (
    ImportanceTag,
    validate_minimum_residency_seconds,
)
from cognistore.core.topology import (
    AttributeValue,
    PlacementCandidate,
    PlacementConstraints,
    Pool,
    Tier,
    eligible_candidates,
)
from cognistore.encryption import require_at_rest
from cognistore.observability import observe
from cognistore.utils.redaction import redact, redact_text

from .audit_integrity import append_entry, read_export, read_head, read_snapshot
from .engine import create_catalog_engine, normalize_database_url, tenant_catalog_locator
from .migrations import MigrationManager, catalog_schema_exists
from .schema import (
    access_events,
    audit_event_tombstones,
    audit_events,
    audit_integrity_head,
    audit_move_heads,
    budget_definitions,
    budget_reservations,
    catalog_tenant,
    content_blobs,
    content_manifest_chunks,
    content_manifests,
    legal_holds,
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


class _LegalHoldLockState:
    def __init__(self) -> None:
        self.fence = LegalHoldFence()
        self.mutations = ObjectMutationFence()
        self.local = threading.local()


_LEGAL_HOLD_LOCKS: dict[tuple[int, str], _LegalHoldLockState] = {}
_LEGAL_HOLD_LOCKS_GUARD = threading.Lock()


def _legal_hold_lock_state(identity: str) -> _LegalHoldLockState:
    # Include the PID so a fork never inherits the parent's reentrancy depth.
    with _LEGAL_HOLD_LOCKS_GUARD:
        return _LEGAL_HOLD_LOCKS.setdefault((os.getpid(), identity), _LegalHoldLockState())


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
        tenant_id: str = "default",
    ) -> None:
        self._tenant_id = validate_tenant_id(tenant_id)
        require_tenant(self._tenant_id)
        self._root_locator = locator
        self._migrate = migrate
        self._sql_tenant_catalogs: dict[str, SQLCatalog] = {}
        self._tenant_catalog_root = self
        self._tenant_catalog_lock = threading.RLock()
        partition_locator, self.schema_name = tenant_catalog_locator(locator, tenant_id)
        if isinstance(partition_locator, Path) and tenant_id != "default" and not read_only:
            partition_locator.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(partition_locator)
        self.read_only = read_only
        self.audit_retention = audit_retention or AuditRetentionPolicy()
        self._engine, self._conn = create_catalog_engine(
            partition_locator, read_only=read_only, schema_name=self.schema_name
        )
        database_url = sa.engine.make_url(normalize_database_url(partition_locator))
        self._legal_hold_lock_path: Path | None = None
        if self.backend == "sqlite":
            database = database_url.database
            if database and database != ":memory:":
                self._legal_hold_lock_path = Path(str(Path(database).resolve()) + ".legal-holds.lock")
                self._legal_hold_lock_identity = str(self._legal_hold_lock_path)
            else:
                self._legal_hold_lock_identity = f"sqlite-memory:{id(self._engine)}"
        else:
            self._legal_hold_lock_identity = (
                database_url.render_as_string(hide_password=True) + ":" + str(self.schema_name)
            )
        self._legal_hold_advisory_key = int.from_bytes(hashlib.sha256(
            ("cognistore-legal-holds:" + (self.schema_name or "public")).encode()
        ).digest()[:8], "big", signed=True)
        # Session advisory locks must not exhaust the ordinary transaction
        # pool: lock holders still need a connection to finish their writes.
        self._legal_hold_engine = (
            create_catalog_engine(partition_locator, read_only=read_only,
                                  schema_name=self.schema_name)[0]
            if self.backend == "postgresql" else self._engine
        )
        self._sqlite_lock = threading.RLock()
        if self._engine.dialect.name == "sqlite":
            @sa.event.listens_for(self._engine, "connect")
            def _retention_function(dbapi_connection: Any, record: Any) -> None:
                dbapi_connection.create_function("cognistore_audit_retention", 0,
                    lambda: int(record.info.get("audit_retention_authorized", False)))
            # Compatibility connection is intentionally unauthorized.
            if self._conn is not None:
                self._conn.create_function("cognistore_audit_retention", 0, lambda: 0)
        self._closed = False
        self._engine = self._engine.execution_options(cognistore_tenant_id=tenant_id)
        if self.schema_name is not None:
            self._engine = self._engine.execution_options(
                schema_translate_map={None: self.schema_name}
            )

        @sa.event.listens_for(self._engine, "before_cursor_execute")
        def _tenant_boundary(*_args: object) -> None:
            # Also guard engines/connections retained before a context switch.
            require_tenant(self._tenant_id)

        migrations = MigrationManager(audit_retention=self.audit_retention)
        try:
            if self.schema_name is not None:
                self._prepare_postgres_partition()
            self._validate_tenant_owner(allow_missing=True)
            if read_only:
                schema_exists = catalog_schema_exists(self._engine)
                if not schema_exists:
                    inspector = sa.inspect(self._engine)
                    schema_present = any(
                        inspector.has_table(table, schema=self.schema_name)
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
            self._validate_tenant_owner()
        except BaseException:
            self.close()
            raise

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    def for_tenant(self, tenant_id: str) -> SQLCatalog:
        tenant_id = validate_tenant_id(tenant_id)
        require_tenant(tenant_id)
        if self._closed:
            raise RuntimeError("catalog is closed")
        if tenant_id == self.tenant_id:
            return self
        if self._tenant_catalog_root is not self:
            return self._tenant_catalog_root.for_tenant(tenant_id)
        with self._tenant_catalog_lock:
            catalog = self._sql_tenant_catalogs.get(tenant_id)
            if catalog is None or catalog._closed:
                catalog = SQLCatalog(
                    self._root_locator, tenant_id=tenant_id,
                    read_only=self.read_only, migrate=self._migrate,
                    audit_retention=self.audit_retention,
                )
                catalog._tenant_catalog_root = self
                self._sql_tenant_catalogs[tenant_id] = catalog
            return catalog

    def _prepare_postgres_partition(self) -> None:
        schema = self.schema_name
        assert schema is not None
        with self._engine.begin() as connection:
            if not self.read_only:
                connection.exec_driver_sql("SELECT pg_advisory_xact_lock(1129270868)")
                quoted = connection.dialect.identifier_preparer.quote(schema)
                connection.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS {quoted}")
                # Extensions belong to the database, not a removable tenant
                # schema. This also prevents an individual tenant downgrade
                # from claiming ownership of and removing a shared extension.
                connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        # Each engine owns its dialect. Keep reflection's default namespace
        # consistent with its session; expression SQL is explicitly translated.
        self._engine.dialect.default_schema_name = schema

    def _validate_tenant_owner(self, *, allow_missing: bool = False) -> None:
        with self._engine.connect() as connection:
            if connection.dialect.name == "postgresql":
                # Ownership and schema presence must describe one migration
                # state. A concurrent initializer cannot commit between these
                # reads and make a fresh partition appear partially owned.
                connection.exec_driver_sql("SELECT pg_advisory_xact_lock(1129270868)")
            else:
                # SQLite SELECTs do not start a real read transaction under
                # sqlite3's legacy transaction mode; pin its snapshot explicitly.
                connection.exec_driver_sql("BEGIN")
            inspector = sa.inspect(connection)
            if not inspector.has_table("catalog_tenant", schema=self.schema_name):
                if allow_missing:
                    # Only the legacy default catalog may predate ownership.
                    # Never silently adopt an existing unowned tenant partition.
                    if self.tenant_id != "default" and inspector.get_table_names(
                        schema=self.schema_name
                    ):
                        raise TenantIsolationError()
                    return
                raise TenantIsolationError()
            owner: Sequence[str] = connection.execute(
                sa.select(catalog_tenant.c.tenant_id)
            ).scalars().all()
            if owner != [self.tenant_id]:
                raise TenantIsolationError()

    @property
    def engine(self) -> Engine:
        require_tenant(self.tenant_id)
        return self._engine

    @property
    def backend(self) -> str:
        return self._engine.dialect.name

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._tenant_catalog_lock:
            for catalog in self._sql_tenant_catalogs.values():
                catalog.close()
            self._sql_tenant_catalogs.clear()
        self._engine.dispose()
        if self.backend == "postgresql":
            self._legal_hold_engine.dispose()
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
    def _legal_hold_serialization(self, *, exclusive: bool = False) -> Iterator[None]:
        """Serialize lifecycle and storage effects across processes, without a DB transaction.

        A tenant-wide fence covers exact, prefix and whole-bucket holds. The
        separate session/file lock allows heartbeat and nested catalog writes
        to use their normal short transactions during slow storage operations.
        """
        require_tenant(self.tenant_id)
        if self._closed:
            raise RuntimeError("catalog is closed")
        state = _legal_hold_lock_state(self._legal_hold_lock_identity)
        with state.fence.hold(exclusive=exclusive):
            depth = getattr(state.local, "depth", 0)
            if depth:
                state.local.depth = depth + 1
                try:
                    yield
                finally:
                    state.local.depth = depth
                return
            with contextlib.ExitStack() as stack:
                if self.backend == "postgresql":
                    connection = stack.enter_context(self._legal_hold_engine.connect())
                    lock_function = "pg_advisory_lock" if exclusive else "pg_advisory_lock_shared"
                    unlock_function = "pg_advisory_unlock" if exclusive else "pg_advisory_unlock_shared"
                    try:
                        connection.execute(sa.text(f"SELECT {lock_function}(:key)"),
                                           {"key": self._legal_hold_advisory_key})
                        connection.commit()
                    except BaseException:
                        # An uncertain session-lock acquisition must never
                        # return a potentially locked connection to the pool.
                        connection.invalidate()
                        raise

                    def unlock() -> None:
                        if connection.invalidated:
                            # Invalidating closes the owning session and its
                            # locks; do not reconnect just to unlock them.
                            return
                        try:
                            connection.execute(sa.text(f"SELECT {unlock_function}(:key)"),
                                               {"key": self._legal_hold_advisory_key})
                            connection.commit()
                        except BaseException:
                            connection.invalidate()
                            raise
                    stack.callback(unlock)
                    # Object reservations reuse this session rather than
                    # exhausting a second pool slot while holding the first.
                    state.local.connection = connection
                elif self._legal_hold_lock_path is not None:
                    # OS locks disappear on process exit. No stale reservation
                    # or database write transaction survives a failed worker.
                    descriptor = os.open(self._legal_hold_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                    lockfile = stack.enter_context(os.fdopen(descriptor, "a+b"))
                    if os.name == "nt":  # pragma: no cover - Windows deployment
                        import importlib
                        msvcrt = importlib.import_module("msvcrt")
                        lockfile.write(b"\0")
                        lockfile.flush()
                        lockfile.seek(0)
                        # CRT byte-range locks are exclusive even for its
                        # read-lock constants. Preserve a conservative serial
                        # fallback on Windows SQLite.
                        msvcrt.locking(lockfile.fileno(), msvcrt.LK_LOCK, 1)
                    else:
                        import fcntl
                        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                        fcntl.flock(lockfile.fileno(), mode)
                state.local.depth = 1
                try:
                    yield
                finally:
                    state.local.depth = 0
                    if self.backend == "postgresql":
                        del state.local.connection

    @contextlib.contextmanager
    def object_mutation(self, bucket: str, key: str) -> Iterator[None]:
        """Exclude overlapping API mutations across SQL handles and workers.

        Acquire the legal-hold fence first, then a nonblocking object lock.
        No database write transaction spans storage I/O. File lock sidecars
        must remain in place while workers run; unlinking a live lock would
        split its identity. Process/session exit releases ownership.
        """
        with self._legal_hold_serialization():
            state = _legal_hold_lock_state(self._legal_hold_lock_identity)
            with state.mutations.hold(bucket, key), contextlib.ExitStack() as stack:
                identity = json.dumps(
                    ["cognistore-api-object", self.schema_name or "public",
                     self.tenant_id, bucket, key],
                    ensure_ascii=True, separators=(",", ":"),
                )
                digest = hashlib.sha256(identity.encode()).digest()
                if self.backend == "postgresql":
                    connection: Connection = state.local.connection
                    advisory_key = int.from_bytes(digest[:8], "big", signed=True)
                    try:
                        acquired: bool = connection.execute(
                            sa.text("SELECT pg_try_advisory_lock(:key)"),
                            {"key": advisory_key},
                        ).scalar_one()
                        connection.commit()
                    except BaseException:
                        connection.invalidate()
                        raise
                    if not acquired:
                        raise ObjectMutationConflictError(
                            "An object mutation is already in progress"
                        )

                    def unlock() -> None:
                        if connection.invalidated:
                            return
                        try:
                            connection.execute(
                                sa.text("SELECT pg_advisory_unlock(:key)"),
                                {"key": advisory_key},
                            )
                            connection.commit()
                        except BaseException:
                            connection.invalidate()
                            raise

                    stack.callback(unlock)
                elif self._legal_hold_lock_path is not None:
                    directory = self._legal_hold_lock_path.with_suffix(".objects")
                    directory.mkdir(mode=0o700, exist_ok=True)
                    descriptor = os.open(directory / digest.hex(), os.O_CREAT | os.O_RDWR, 0o600)
                    lockfile = stack.enter_context(os.fdopen(descriptor, "a+b"))
                    if os.name == "nt":  # pragma: no cover - Windows deployment
                        import importlib
                        msvcrt = importlib.import_module("msvcrt")
                        lockfile.write(b"\0")
                        lockfile.flush()
                        lockfile.seek(0)
                        try:
                            msvcrt.locking(lockfile.fileno(), msvcrt.LK_NBLCK, 1)
                        except OSError as exc:
                            if exc.errno not in (11, 13, 35, 36):
                                raise
                            raise ObjectMutationConflictError(
                                "An object mutation is already in progress"
                            ) from exc
                    else:
                        import fcntl
                        try:
                            fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError as exc:
                            raise ObjectMutationConflictError(
                                "An object mutation is already in progress"
                            ) from exc
                if self.backend == "postgresql":
                    keys: set[tuple[str, str]] = getattr(state.local, "mutation_keys", set())
                    state.local.mutation_keys = keys
                    keys.add((bucket, key))
                    try:
                        yield
                    finally:
                        keys.remove((bucket, key))
                else:
                    yield

    def place_legal_hold(
        self, bucket: str, *, key: str | None = None, prefix: str | None = None,
        reason: str, context: AuditContext,
    ) -> LegalHold:
        if self.read_only:
            raise PermissionError("cannot place legal holds in a read-only catalog")
        with self._legal_hold_serialization(exclusive=True), self._transaction() as connection:
            hold = LegalHold.create(
                self.tenant_id, bucket, key=key, prefix=prefix, reason=reason, context=context,
            )
            self._insert_audit_event(connection, self._prepare_audit_event(
                legal_hold_event(hold, context, released=False),
            ))
            connection.execute(sa.insert(legal_holds).values(
                hold_id=UUID(hold.hold_id), tenant_id=self.tenant_id, bucket=hold.bucket,
                object_key=hold.key, prefix=hold.prefix, evidence=hold.to_dict(),
                created_at=hold.created_at, released_at=None,
            ))
        return hold

    @staticmethod
    def _validated_legal_hold(row: RowMapping) -> LegalHold:
        """Reject inconsistent scope/state columns before they can hide a hold."""
        try:
            evidence = row["evidence"]
            if not isinstance(evidence, dict):
                raise ValueError("invalid hold evidence")
            hold = LegalHold.from_mapping(evidence)
            if (
                str(row["hold_id"]) != hold.hold_id
                or row["tenant_id"] != hold.tenant_id
                or row["bucket"] != hold.bucket
                or row["object_key"] != hold.key
                or row["prefix"] != hold.prefix
                or row["created_at"] != hold.created_at
                or row["released_at"] != hold.released_at
                or not isinstance(evidence.get("active"), bool)
                or evidence["active"] != hold.active
            ):
                raise ValueError("inconsistent hold evidence")
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise CatalogSchemaError("Stored legal hold evidence is inconsistent") from exc
        return hold

    def list_legal_holds(
        self, *, bucket: str | None = None, key: str | None = None,
        active_only: bool = False,
    ) -> list[LegalHold]:
        self._validate_hold_query(bucket, key)
        statement = sa.select(legal_holds).order_by(
            legal_holds.c.created_at, legal_holds.c.hold_id,
        )
        with self._connection() as connection:
            holds = [
                self._validated_legal_hold(row)
                for row in connection.execute(statement).mappings()
            ]
        if any(hold.tenant_id != self.tenant_id for hold in holds):
            raise CatalogSchemaError("Stored legal hold tenant is inconsistent")
        # Filtering decoded evidence preserves alias protection identically on
        # both databases. Validate every row first: denormalized scope or state
        # corruption must not make an active hold disappear from enforcement.
        return [
            hold for hold in holds
            if (not active_only or hold.active)
            and (bucket is None or hold.matches_bucket(bucket))
            and (key is None or hold.matches(hold.bucket, key))
        ]

    def release_legal_hold(
        self, hold_id: str, *, reason: str, context: AuditContext,
    ) -> LegalHold:
        if self.read_only:
            raise PermissionError("cannot release legal holds in a read-only catalog")
        legal_hold_actor(context)
        identifier = UUID(str(hold_id))
        with self._legal_hold_serialization(exclusive=True), self._transaction() as connection:
            row = connection.execute(sa.select(legal_holds).where(
                legal_holds.c.hold_id == identifier, legal_holds.c.tenant_id == self.tenant_id,
            )).mappings().first()
            if row is None:
                raise KeyError("Legal hold not found")
            released = self._validated_legal_hold(row).release(reason=reason, context=context)
            self._insert_audit_event(connection, self._prepare_audit_event(
                legal_hold_event(released, context, released=True),
            ))
            connection.execute(sa.update(legal_holds).where(
                legal_holds.c.hold_id == identifier,
            ).values(evidence=released.to_dict(), released_at=released.released_at))
        return released

    @contextlib.contextmanager
    def _transaction(self, *, audit: bool = True) -> Iterator[Connection]:
        require_tenant(self.tenant_id)
        if self._closed:
            raise RuntimeError("catalog is closed")
        lock: Any = (
            self._sqlite_lock
            if self.backend == "sqlite"
            else contextlib.nullcontext()
        )
        with observe("catalog", "transaction", backend=self.backend), lock:
            try:
                with self._engine.begin() as connection:
                    # Establish a writer order before any object/move row locks.
                    # Audit and business state must commit in the same transaction.
                    if self.backend == "sqlite":
                        connection.exec_driver_sql("BEGIN IMMEDIATE")
                    elif audit:
                        connection.execute(sa.select(audit_integrity_head).with_for_update()).first()
                    yield connection
            except sa.exc.DBAPIError as exc:
                if self.backend == "sqlite" and isinstance(exc.orig, sqlite3.Error):
                    raise exc.orig from exc
                raise

    @contextlib.contextmanager
    def _object_publication(self, bucket: str, key: str) -> Iterator[Connection]:
        """Finalize on the reserving session so lost ownership fails closed.

        PostgreSQL session loss releases advisory locks. A fresh transaction
        on another connection could then erase a replacement admitted after
        that loss. Use the owning connection for the short publication
        transaction; never reconnect it while assuming its locks still exist.
        """
        if self.backend == "postgresql":
            state = _legal_hold_lock_state(self._legal_hold_lock_identity)
            if (bucket, key) in getattr(state.local, "mutation_keys", ()):
                if self.read_only:
                    raise PermissionError("cannot publish objects through a read-only catalog")
                connection: Connection = state.local.connection
                if connection.invalidated:
                    raise sa.exc.DisconnectionError("Object mutation session was lost")
                with observe("catalog", "transaction", backend=self.backend), connection.begin():
                    yield connection
                return
        with self._transaction(audit=False) as connection:
            yield connection

    @contextlib.contextmanager
    def _connection(self) -> Iterator[Connection]:
        require_tenant(self.tenant_id)
        if self._closed:
            raise RuntimeError("catalog is closed")
        lock: Any = (
            self._sqlite_lock
            if self.backend == "sqlite"
            else contextlib.nullcontext()
        )
        with observe("catalog", "read", backend=self.backend), lock:
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

    def orphan_cleanup_revision(self, bucket: str | None = None, key: str | None = None) -> int:
        """Return a durable target revision including normalized name aliases.

        Fence rows survive logical deletion. Summing their monotonic counters
        detects reference creation followed by removal across process restarts.
        Streaming all coordinates makes alias matching independent of database
        collation without retaining unrelated rows or invalidating their targets.
        Omitting both coordinates returns the tenant-wide revision.
        """
        scope = self._cleanup_revision_scope(bucket, key)
        with self._connection() as connection:
            if scope is None:
                return int(connection.execute(sa.select(sa.func.coalesce(
                    sa.func.sum(object_mutation_fences.c.generation), 0,
                ))).scalar_one())
            rows = connection.execute(sa.select(
                object_mutation_fences.c.bucket, object_mutation_fences.c.object_key,
                object_mutation_fences.c.generation,
            ).execution_options(stream_results=True, max_row_buffer=1000)).mappings()
            try:
                return sum(int(row["generation"]) for row in rows if (
                    _scope_identity(row["bucket"]), _scope_identity(row["object_key"]),
                ) == scope)
            finally:
                rows.close()

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
        connection.execute(sa.update(object_mutation_fences).where(
            object_mutation_fences.c.bucket == bucket,
            object_mutation_fences.c.object_key == key,
        ).values(generation=object_mutation_fences.c.generation + 1))

    @staticmethod
    def _lock_budgets(connection: Connection) -> None:
        """Serialize scope lookup and reservation across different objects.

        A single admission lock also fences definition installation, so a
        concurrently registered scope cannot be skipped between lookup and
        admission. Always acquire this after move/object/topology locks.
        """
        if connection.dialect.name == "postgresql":
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(1129270870)")
        else:
            connection.execute(sa.update(budget_definitions).where(sa.false()).values(
                created_at=budget_definitions.c.created_at,
            ))

    def configure_budget(
        self, definition: BudgetDefinition, *, audit_context: AuditContext,
        occurred_at: str | datetime | None = None,
    ) -> BudgetDefinition:
        if not isinstance(definition, BudgetDefinition):
            raise ValueError("definition must be a BudgetDefinition")
        if not isinstance(audit_context, AuditContext) or audit_context.actor_type != "user":
            raise ValueError("budget configuration requires a user audit context")
        event = self._prepare_audit_event(
            _budget_configuration_event(definition, audit_context, occurred_at)
        )
        with self._transaction() as connection:
            self._lock_budgets(connection)
            existing = connection.execute(sa.select(budget_definitions.c.definition).where(
                budget_definitions.c.budget_id == definition.budget_id,
            )).scalar_one_or_none()
            if existing is not None:
                if existing != definition.to_dict():
                    raise BudgetConstraintError("budget definitions are immutable; use a new budget id")
                return BudgetDefinition.from_mapping(existing)
            active_jobs = connection.execute(sa.select(move_jobs.c.bucket, move_jobs.c.object_key).where(
                move_jobs.c.state.not_in([MoveJobState.COMPLETED.value, MoveJobState.FAILED.value]),
            ))
            if any(definition.matches(row.bucket, row.object_key) for row in active_jobs):
                raise BudgetConstraintError("cannot install a budget while matching moves are in flight")
            connection.execute(sa.insert(budget_definitions).values(
                budget_id=definition.budget_id, definition=definition.to_dict(),
                created_at=event.occurred_at,
            ))
            self._insert_audit_event(connection, event)
        return BudgetDefinition.from_mapping(definition.to_dict())

    def list_budgets(self) -> List[BudgetDefinition]:
        with self._connection() as connection:
            values: Sequence[dict[str, Any]] = connection.execute(
                sa.select(budget_definitions.c.definition)
            ).scalars().all()
        return sorted((BudgetDefinition.from_mapping(value) for value in values),
                      key=lambda item: item.budget_id)

    def list_budget_reservations(self, budget_id: str | None = None) -> List[dict[str, Any]]:
        statement = sa.select(budget_reservations.c.reservation)
        if budget_id is not None:
            statement = statement.where(budget_reservations.c.budget_id == budget_id)
        with self._connection() as connection:
            values: Sequence[dict[str, Any]] = connection.execute(statement).scalars().all()
        return sorted((deepcopy(value) for value in values),
                      key=lambda item: (item["budget_id"], item["move_id"], item["attempt"]))

    def read_budget_snapshot(self) -> tuple[List[BudgetDefinition], List[dict[str, Any]]]:
        """Read both ledger tables in one statement-consistent, read-only query.

        A single UNION ALL preserves one snapshot under PostgreSQL READ COMMITTED
        and SQLite, without taking the admission writer lock during scrapes.
        """
        statement = sa.union_all(
            sa.select(sa.literal("definition"), budget_definitions.c.definition),
            sa.select(sa.literal("reservation"), budget_reservations.c.reservation),
        )
        with self._connection() as connection:
            rows = connection.execute(statement).all()
        return (
            [BudgetDefinition.from_mapping(value) for kind, value in rows
             if kind == "definition"],
            [deepcopy(value) for kind, value in rows if kind == "reservation"],
        )

    def _reserve_move_budgets(
        self, connection: Connection, *, move_id: str, bucket: str, key: str,
        src_tier: str, dst_tier: str, size: int, source_metadata: Mapping[str, Any],
        now: str, owner_id: str, audit_context: AuditContext | None, trusted_override: bool,
    ) -> dict[str, Any]:
        self._lock_budgets(connection)
        definition_values: ScalarResult[dict[str, Any]] = connection.execute(
            sa.select(budget_definitions.c.definition)
        ).scalars()
        definitions = [BudgetDefinition.from_mapping(value) for value in definition_values]
        previous: list[dict[str, Any]] = list(
            connection.execute(sa.select(budget_reservations.c.reservation)).scalars()
        )
        pools_by_id = {row["pool_id"]: self._pool_record(row) for row in connection.execute(
            sa.select(pools)
        ).mappings()}
        record_row = connection.execute(self._object_select().where(
            objects.c.bucket == bucket, objects.c.object_key == key,
        )).mappings().first()
        prepared, metadata = _prepare_budget_reservations(
            definitions, previous, pools_by_id,
            None if record_row is None else self._record(record_row),
            move_id=move_id, bucket=bucket, key=key, src_tier=src_tier, dst_tier=dst_tier,
            size=size, source_metadata=source_metadata, now=now,
            audit_context=audit_context, trusted_override=trusted_override,
        )
        context = self._move_audit_context(
            move_id, owner_id=owner_id, audit_context=audit_context,
            causation_id=None if audit_context is None else audit_context.causation_id,
        )
        for reservation in prepared:
            connection.execute(sa.insert(budget_reservations).values(
                budget_id=reservation["budget_id"], move_id=move_id, attempt=reservation["attempt"],
                reservation=reservation, created_at=now,
            ))
            self._insert_audit_event(connection, self._prepare_audit_event(
                _budget_reservation_event(reservation, context)
            ))
        return metadata

    @staticmethod
    def _lock_topology(connection: Connection, *, exclusive: bool = False) -> None:
        """Serialize lifecycle validation against placement and membership writes.

        PostgreSQL placement writers share the lock and remain concurrent across
        objects. Lifecycle operations take it exclusively before checking active
        references, closing the race between a successful check and a new
        assignment. The lock is always acquired after any object mutation fence.
        SQLite needs its writer lock before a read/validate/write transaction.
        """
        if connection.dialect.name == "postgresql":
            function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
            connection.exec_driver_sql(f"SELECT {function}(1129270869)")
        else:
            connection.execute(sa.update(tiers).where(sa.false()).values(active=tiers.c.active))

    @staticmethod
    def _ensure_tier(connection: Connection, tier: str, now: str) -> None:
        Tier(name=tier)
        SQLCatalog._lock_topology(connection)
        SQLCatalog._do_nothing_insert(
            connection,
            tiers,
            {"name": tier, "metadata": {}, "active": True, "created_at": now, "updated_at": now},
        )
        active: bool = connection.execute(
            sa.select(tiers.c.active).where(tiers.c.name == tier)
        ).scalar_one()
        if not active:
            raise ValueError(f"Tier is inactive: {tier}")

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
    ) -> UUID:
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
                    object_placements.c.placement_started_at,
                    object_placements.c.last_tier_move_at,
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
                    placement_started_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            pool_id = placement["pool_id"] if placement["tier_name"] == tier else None
            connection.execute(
                sa.update(object_placements)
                .where(object_placements.c.object_id == object_id)
                .values(
                    tier_name=tier, pool_id=pool_id, updated_at=now,
                    placement_started_at=(
                        placement["placement_started_at"]
                        if placement["tier_name"] == tier else now
                    ),
                    last_tier_move_at=(
                        placement["last_tier_move_at"]
                        if placement["tier_name"] == tier else now
                    ),
                )
            )
        return object_id

    @staticmethod
    def _manifest_id(content: ObjectContent) -> UUID:
        return _stable_uuid(
            "content-manifest",
            content.sha256,
            str(content.schema_version),
            content.representation,
            content.chunking_algorithm,
            str(content.chunking_version),
            str(content.chunk_size),
        )

    @staticmethod
    def _validate_object_content(content: ObjectContent) -> None:
        if not isinstance(content, ObjectContent):
            raise ValueError("content must be an ObjectContent")
        expected_offset = 0
        for expected_index, chunk in enumerate(content.chunks):
            if chunk.index != expected_index:
                raise ValueError("content chunk indexes must be contiguous from zero")
            if chunk.offset != expected_offset:
                raise ValueError("content chunk offsets must be contiguous from zero")
            if chunk.size <= 0:
                raise ValueError("content chunks must be non-empty")
            expected_offset += chunk.size
        if expected_offset != content.size:
            raise ValueError("content chunk sizes must equal the full content size")

    @staticmethod
    def _insert_content_blob(
        connection: Connection,
        *,
        sha256: str,
        digest_algorithm: str,
        size: int,
        cas_key: str,
        now: str,
    ) -> None:
        expected = {
            "sha256": sha256,
            "digest_algorithm": digest_algorithm,
            "size": size,
            "cas_key": cas_key,
        }
        SQLCatalog._do_nothing_insert(
            connection,
            content_blobs,
            {
                **expected,
                "created_at": now,
                "reference_count": 0,
                "unreferenced_at": now,
            },
        )
        row = (
            connection.execute(
                sa.select(
                    content_blobs.c.sha256,
                    content_blobs.c.digest_algorithm,
                    content_blobs.c.size,
                    content_blobs.c.cas_key,
                ).where(content_blobs.c.sha256 == sha256)
            )
            .mappings()
            .first()
        )
        if row is None or dict(row) != expected:
            raise ValueError(f"content blob identity collision for sha256 {sha256}")

    @staticmethod
    def _manifest_layout_predicate(content: ObjectContent) -> Any:
        return sa.and_(
            content_manifests.c.content_sha256 == content.sha256,
            content_manifests.c.schema_version == content.schema_version,
            content_manifests.c.representation == content.representation,
            content_manifests.c.chunking_algorithm == content.chunking_algorithm,
            content_manifests.c.chunking_version == content.chunking_version,
            content_manifests.c.chunk_size == content.chunk_size,
        )

    @staticmethod
    @contextlib.contextmanager
    def _ordered_content_blob_descriptors(
        content: ObjectContent,
    ) -> Iterator[Iterator[tuple[str, str, int, str]]]:
        """Spool unique blob writes in one bounded global acquisition order."""

        require_at_rest("runtime")
        with tempfile.TemporaryDirectory(
            prefix="cognistore-content-blobs-"
        ) as spool_directory:
            spool = sqlite3.connect(Path(spool_directory) / "descriptors.db")
            try:
                # This database is disposable, private to one transaction, and
                # ordered by its WITHOUT ROWID primary key. Keep SQLite's page
                # cache fixed and force any auxiliary storage onto disk.
                spool.execute("PRAGMA journal_mode = OFF")
                spool.execute("PRAGMA synchronous = OFF")
                spool.execute("PRAGMA temp_store = FILE")
                spool.execute("PRAGMA cache_size = -256")
                spool.execute(
                    """
                    CREATE TABLE descriptors (
                        sha256 TEXT PRIMARY KEY COLLATE BINARY,
                        digest_algorithm TEXT NOT NULL,
                        size INTEGER NOT NULL,
                        cas_key TEXT NOT NULL
                    ) WITHOUT ROWID
                    """
                )

                def register(
                    sha256: str,
                    digest_algorithm: str,
                    size: int,
                    cas_key: str,
                ) -> None:
                    descriptor = (sha256, digest_algorithm, size, cas_key)
                    inserted = spool.execute(
                        "INSERT OR IGNORE INTO descriptors VALUES (?, ?, ?, ?)",
                        descriptor,
                    )
                    if inserted.rowcount == 1:
                        return
                    existing = spool.execute(
                        """
                        SELECT sha256, digest_algorithm, size, cas_key
                        FROM descriptors
                        WHERE sha256 = ?
                        """,
                        (sha256,),
                    ).fetchone()
                    if existing != descriptor:
                        raise ValueError(
                            f"content blob identity collision for sha256 {sha256}"
                        )

                register(
                    content.sha256,
                    content.digest_algorithm,
                    content.size,
                    content.cas_key,
                )
                for chunk in content.chunks:
                    register(
                        chunk.sha256,
                        content.digest_algorithm,
                        chunk.size,
                        chunk.cas_key,
                    )

                rows = spool.execute(
                    """
                    SELECT sha256, digest_algorithm, size, cas_key
                    FROM descriptors
                    ORDER BY sha256
                    """
                )

                def ordered() -> Iterator[tuple[str, str, int, str]]:
                    for sha256, digest_algorithm, size, cas_key in rows:
                        yield sha256, digest_algorithm, size, cas_key

                yield ordered()
            finally:
                spool.close()

    @staticmethod
    def _manifest_chunk_values(
        manifest_id: UUID,
        chunk: ContentChunk,
    ) -> dict[str, object]:
        return {
            "manifest_id": manifest_id,
            "chunk_index": chunk.index,
            "chunk_sha256": chunk.sha256,
            "byte_offset": chunk.offset,
            "byte_length": chunk.size,
        }

    @staticmethod
    def _content_reference_delta_statement(
        old_manifest_id: UUID | None,
        new_manifest_id: UUID | None,
    ) -> sa.Select[Any]:
        """Return sorted net blob-edge deltas for one mapping transition.

        A logical object owns one root edge plus every ordered chunk occurrence
        in its active manifest. ``UNION ALL`` deliberately preserves repeated
        chunks and the one-chunk case where the root and chunk digests match.
        """

        edge_statements: list[sa.Select[Any]] = []
        for manifest_id, direction in (
            (old_manifest_id, -1),
            (new_manifest_id, 1),
        ):
            if manifest_id is None:
                continue
            edge_statements.extend(
                (
                    sa.select(
                        content_manifests.c.content_sha256.label("sha256"),
                        sa.literal(direction, type_=sa.BigInteger()).label("delta"),
                    ).where(content_manifests.c.manifest_id == manifest_id),
                    sa.select(
                        content_manifest_chunks.c.chunk_sha256.label("sha256"),
                        sa.literal(direction, type_=sa.BigInteger()).label("delta"),
                    ).where(content_manifest_chunks.c.manifest_id == manifest_id),
                )
            )
        if not edge_statements:
            raise ValueError("a content-reference transition requires an old or new manifest")

        edges = sa.union_all(*edge_statements).subquery("content_reference_edge_deltas")
        delta = sa.func.sum(edges.c.delta).label("delta")
        return (
            sa.select(edges.c.sha256, delta)
            .group_by(edges.c.sha256)
            .having(sa.func.sum(edges.c.delta) != 0)
            .order_by(edges.c.sha256)
        )

    @staticmethod
    def _stream_content_reference_deltas(
        connection: Connection,
        old_manifest_id: UUID | None,
        new_manifest_id: UUID | None,
    ) -> Iterator[RowMapping]:
        rows = connection.execute(
            SQLCatalog._content_reference_delta_statement(
                old_manifest_id,
                new_manifest_id,
            ).execution_options(
                stream_results=True,
                max_row_buffer=MAX_IN_MEMORY_CONTENT_CHUNKS,
            )
        ).mappings()
        try:
            yield from rows
        finally:
            rows.close()

    @staticmethod
    def _lock_content_reference_deltas(
        connection: Connection,
        old_manifest_id: UUID | None,
        new_manifest_id: UUID | None,
    ) -> None:
        """Lock every affected digest in the shared global acquisition order."""

        for row in SQLCatalog._stream_content_reference_deltas(
            connection,
            old_manifest_id,
            new_manifest_id,
        ):
            statement = sa.select(content_blobs.c.reference_count).where(
                content_blobs.c.sha256 == row["sha256"]
            )
            if connection.dialect.name == "postgresql":
                # Manifest/chunk foreign-key checks retain KEY SHARE locks on
                # their referenced blobs.  NO KEY UPDATE still serializes these
                # count changes while remaining compatible with those locks, so
                # two different manifests that share a chunk do not deadlock
                # while upgrading their own FK locks.
                statement = statement.with_for_update(key_share=True)
                persisted = connection.execute(statement).scalar_one_or_none()
            else:
                # SQLite has no row-level FOR UPDATE. A no-op UPDATE takes its
                # database writer lock before the reclamation clock is sampled.
                persisted = connection.execute(statement).scalar_one_or_none()
                if persisted is not None:
                    connection.execute(
                        sa.update(content_blobs)
                        .where(content_blobs.c.sha256 == row["sha256"])
                        .values(reference_count=content_blobs.c.reference_count)
                    )
            if persisted is None:
                raise RuntimeError(
                    f"content reference points to missing blob {row['sha256']}"
                )

    @staticmethod
    def _apply_content_reference_deltas(
        connection: Connection,
        old_manifest_id: UUID | None,
        new_manifest_id: UUID | None,
        *,
        changed_at: str,
    ) -> None:
        for row in SQLCatalog._stream_content_reference_deltas(
            connection,
            old_manifest_id,
            new_manifest_id,
        ):
            delta = int(row["delta"])
            resulting_count = content_blobs.c.reference_count + delta
            updated = connection.execute(
                sa.update(content_blobs)
                .where(
                    content_blobs.c.sha256 == row["sha256"],
                    resulting_count >= 0,
                )
                .values(
                    reference_count=resulting_count,
                    unreferenced_at=sa.case(
                        (resulting_count == 0, changed_at),
                        else_=None,
                    ),
                )
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    f"content reference count underflow for blob {row['sha256']}"
                )

    def _set_object_content_manifest(
        self,
        connection: Connection,
        object_id: UUID,
        manifest_id: UUID | None,
        *,
        now: str,
    ) -> None:
        """Atomically replace one ownership edge and its transitive blob counts."""

        old_manifest_id = connection.execute(
            sa.select(object_contents.c.manifest_id).where(
                object_contents.c.object_id == object_id
            )
        ).scalar_one_or_none()
        if old_manifest_id == manifest_id:
            if manifest_id is not None:
                connection.execute(
                    sa.update(object_contents)
                    .where(object_contents.c.object_id == object_id)
                    .values(updated_at=now)
                )
            return

        self._lock_content_reference_deltas(
            connection,
            old_manifest_id,
            manifest_id,
        )
        # Sample the clock only after every shared digest lock is held. A writer
        # that waited behind another reference transition must receive a fresh
        # zero-reference grace boundary, not its pre-wait transaction time.
        reference_changed_at = _timestamp()
        self._apply_content_reference_deltas(
            connection,
            old_manifest_id,
            manifest_id,
            changed_at=reference_changed_at,
        )

        if manifest_id is None:
            connection.execute(
                sa.delete(object_contents).where(object_contents.c.object_id == object_id)
            )
        elif old_manifest_id is None:
            connection.execute(
                sa.insert(object_contents).values(
                    object_id=object_id,
                    manifest_id=manifest_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            connection.execute(
                sa.update(object_contents)
                .where(object_contents.c.object_id == object_id)
                .values(manifest_id=manifest_id, updated_at=now)
            )

    def _persist_object_content(
        self,
        connection: Connection,
        object_id: UUID,
        content: ObjectContent,
        *,
        now: str,
    ) -> None:
        self._validate_object_content(content)
        # Global blobs are shared by unrelated logical objects. PostgreSQL's
        # unique-index conflict checks wait on uncommitted inserts, so taking
        # these keys in manifest order could deadlock two scans whose shared
        # chunks appear in opposite order. The file-backed primary-key spool
        # deduplicates and establishes one lock-acquisition order without an
        # object-size-dependent Python collection.
        with self._ordered_content_blob_descriptors(content) as descriptors:
            for sha256, digest_algorithm, size, cas_key in descriptors:
                self._insert_content_blob(
                    connection,
                    sha256=sha256,
                    digest_algorithm=digest_algorithm,
                    size=size,
                    cas_key=cas_key,
                    now=now,
                )

        proposed_manifest_id = self._manifest_id(content)
        manifest_values = {
            "manifest_id": proposed_manifest_id,
            "content_sha256": content.sha256,
            "schema_version": content.schema_version,
            "representation": content.representation,
            "chunking_algorithm": content.chunking_algorithm,
            "chunking_version": content.chunking_version,
            "chunk_size": content.chunk_size,
            "chunk_count": len(content.chunks),
        }
        self._do_nothing_insert(
            connection,
            content_manifests,
            {**manifest_values, "created_at": now},
        )
        manifest_row = (
            connection.execute(
                sa.select(
                    content_manifests.c.manifest_id,
                    content_manifests.c.content_sha256,
                    content_manifests.c.schema_version,
                    content_manifests.c.representation,
                    content_manifests.c.chunking_algorithm,
                    content_manifests.c.chunking_version,
                    content_manifests.c.chunk_size,
                    content_manifests.c.chunk_count,
                ).where(self._manifest_layout_predicate(content))
            )
            .mappings()
            .first()
        )
        if manifest_row is None:
            raise ValueError("content manifest identity collision")
        manifest_id = manifest_row["manifest_id"]
        expected_manifest = {**manifest_values, "manifest_id": manifest_id}
        if dict(manifest_row) != expected_manifest:
            raise ValueError("content manifest layout collision")

        for chunk in content.chunks:
            self._do_nothing_insert(
                connection,
                content_manifest_chunks,
                self._manifest_chunk_values(manifest_id, chunk),
            )

        persisted_chunks = connection.execute(
            sa.select(
                content_manifest_chunks.c.manifest_id,
                content_manifest_chunks.c.chunk_index,
                content_manifest_chunks.c.chunk_sha256,
                content_manifest_chunks.c.byte_offset,
                content_manifest_chunks.c.byte_length,
            )
            .where(content_manifest_chunks.c.manifest_id == manifest_id)
            .order_by(content_manifest_chunks.c.chunk_index)
            .execution_options(
                stream_results=True,
                max_row_buffer=MAX_IN_MEMORY_CONTENT_CHUNKS,
            )
        ).mappings()
        try:
            for chunk in content.chunks:
                persisted = persisted_chunks.fetchone()
                expected = self._manifest_chunk_values(manifest_id, chunk)
                if persisted is None or dict(persisted) != expected:
                    raise ValueError(
                        "content manifest chunks conflict with the persisted layout"
                    )
            if persisted_chunks.fetchone() is not None:
                raise ValueError(
                    "content manifest chunks conflict with the persisted layout"
                )
        finally:
            persisted_chunks.close()

        self._set_object_content_manifest(
            connection,
            object_id,
            manifest_id,
            now=now,
        )

    def _invalidate_object_content_if_mismatched(
        self,
        connection: Connection,
        object_id: UUID,
        *,
        size: int,
        checksum: str | None,
    ) -> bool:
        row = (
            connection.execute(
                sa.select(
                    content_manifests.c.content_sha256,
                    content_blobs.c.size,
                )
                .select_from(
                    object_contents.join(
                        content_manifests,
                        content_manifests.c.manifest_id == object_contents.c.manifest_id,
                    ).join(
                        content_blobs,
                        content_blobs.c.sha256 == content_manifests.c.content_sha256,
                    )
                )
                .where(object_contents.c.object_id == object_id)
            )
            .mappings()
            .first()
        )
        if row is not None and (
            row["size"] != size
            or (checksum is not None and row["content_sha256"] != checksum)
        ):
            connection.execute(
                sa.delete(object_embedding_documents).where(
                    object_embedding_documents.c.object_id == object_id
                )
            )
            self._set_object_content_manifest(
                connection,
                object_id,
                None,
                now=_timestamp(),
            )
            return True
        return False

    @guard_legal_hold("catalog.upsert")
    def upsert(
        self,
        bucket: str,
        key: str,
        size: int,
        tier: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        validate_catalog_size(size)
        persisted_metadata = deepcopy(metadata) if metadata is not None else {}
        # This summary is a catalog-owned projection of ``object_contents``.
        # A generic write invalidates that mapping and therefore cannot retain
        # or accept a caller-forged reserved header.
        persisted_metadata.pop("content_identity", None)
        now = _timestamp()
        with self._object_publication(bucket, key) as connection:
            self._lock_object(connection, bucket, key)
            object_id = self._write_object(
                connection,
                bucket,
                key,
                size=size,
                tier=tier,
                metadata=persisted_metadata,
                now=now,
            )
            connection.execute(
                sa.delete(object_embedding_documents).where(
                    object_embedding_documents.c.object_id == object_id
                )
            )
            self._set_object_content_manifest(
                connection,
                object_id,
                None,
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

    @guard_legal_hold("catalog.upsert_scan_observation", scan=True)
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
        content: ObjectContent | None = None,
    ) -> bool:
        if (fence.bucket, fence.key) != (bucket, key):
            raise ValueError("scan fence does not identify the observed object")
        if not isinstance(generation, str) or not generation:
            raise ValueError("scan observation requires a non-empty generation")
        validate_catalog_size(size)
        if content is not None:
            self._validate_object_content(content)
            if content.size != size:
                raise ValueError("content size must match the scan observation size")
        now = _timestamp()
        with self._object_publication(bucket, key) as connection:
            self._lock_object(connection, bucket, key)
            jobs = self._select_scan_move_jobs(connection, bucket, key)
            if self._scan_move_job_fingerprints(
                jobs
            ) != fence.move_jobs or not self._scan_observation_is_authoritative(
                jobs, tier=tier, generation=generation
            ):
                return False
            existing = (
                connection.execute(
                    sa.select(
                        objects.c.metadata,
                        object_contents.c.manifest_id,
                    )
                    .select_from(
                        objects.outerjoin(
                            object_contents,
                            object_contents.c.object_id == objects.c.object_id,
                        )
                    )
                    .where(
                        objects.c.bucket == bucket,
                        objects.c.object_key == key,
                    )
                )
                .mappings()
                .first()
            )
            existing_metadata = dict(existing["metadata"] or {}) if existing else {}
            existing_manifest_id = existing["manifest_id"] if existing else None
            proposed_manifest_id = self._manifest_id(content) if content is not None else None
            content_changed = existing_manifest_id != proposed_manifest_id
            incoming_metadata = metadata or {}
            merged_metadata = dict(existing_metadata)
            merged_metadata.update(incoming_metadata)
            if content_changed and "document_extraction" not in incoming_metadata:
                # Extraction text is an observation of exact source bytes. A
                # partial-metadata caller cannot carry it across a replacement
                # manifest and thereby bind stale text to a new source digest.
                merged_metadata.pop("document_extraction", None)
            extraction_changed = existing_metadata.get(
                "document_extraction"
            ) != merged_metadata.get("document_extraction")
            if content_changed or extraction_changed:
                if "pii_detection" not in incoming_metadata:
                    merged_metadata.pop("pii_detection", None)
                # MIME selection and provenance are observations of the same
                # source bytes/extraction. Omitted evidence may be merged only
                # while that source identity remains unchanged.
                if "mime" not in incoming_metadata:
                    merged_metadata.pop("mime", None)
                if "mime_detection" not in incoming_metadata:
                    merged_metadata.pop("mime_detection", None)
            if content is None:
                merged_metadata.pop("content_identity", None)
            else:
                merged_metadata["sha256"] = content.sha256
                merged_metadata["content_identity"] = content.to_metadata()
            object_id = self._write_object(
                connection,
                bucket,
                key,
                size=size,
                tier=tier,
                metadata=merged_metadata,
                now=now,
            )
            if extraction_changed or content_changed:
                connection.execute(
                    sa.delete(object_embedding_documents).where(
                        object_embedding_documents.c.object_id == object_id
                    )
                )
            if content is None:
                self._set_object_content_manifest(
                    connection,
                    object_id,
                    None,
                    now=now,
                )
            else:
                self._persist_object_content(
                    connection,
                    object_id,
                    content,
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
            object_placements.c.pool_id,
            object_placements.c.placement_started_at,
            object_placements.c.last_tier_move_at,
            objects.c.importance,
            objects.c.importance_revision,
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
            metadata=deepcopy(dict(row["metadata"] or {})),
            pool_id=row["pool_id"],
            placement_started_at=row["placement_started_at"],
            last_tier_move_at=row["last_tier_move_at"],
            importance=(
                ImportanceTag.from_mapping(row["importance"])
                if row["importance"] is not None else None
            ),
            importance_revision=row["importance_revision"],
        )

    def get(self, bucket: str, key: str) -> ObjectRecord | None:
        statement = self._object_select().where(
            objects.c.bucket == bucket,
            objects.c.object_key == key,
        )
        with self._connection() as connection:
            row = connection.execute(statement).mappings().first()
        return None if row is None else self._record(row)

    def set_importance(
        self, bucket: str, key: str, tag: ImportanceTag | None, *,
        audit_context: AuditContext, occurred_at: str | datetime | None = None,
        provenance: str | None = None,
    ) -> ObjectRecord:
        """Store trusted importance and its audit in the same object transaction."""

        _validate_importance_actor(tag, audit_context, provenance)
        with self._transaction() as connection:
            self._lock_object(connection, bucket, key)
            row = connection.execute(self._object_select().where(
                objects.c.bucket == bucket, objects.c.object_key == key,
            )).mappings().first()
            if row is None:
                raise KeyError(f"Object not found: {bucket}/{key}")
            record = self._record(row)
            updated = replace(
                record, importance=deepcopy(tag),
                importance_revision=record.importance_revision + 1,
            )
            event = self._prepare_audit_event(
                _importance_event(record, updated, audit_context, occurred_at, provenance)
            )
            connection.execute(sa.update(objects).where(
                objects.c.bucket == bucket, objects.c.object_key == key,
            ).values(
                importance=tag.to_dict() if tag is not None else sa.null(),
                importance_revision=updated.importance_revision,
                updated_at=_timestamp(),
            ))
            self._insert_audit_event(connection, event)
            return updated

    def get_object_content(self, bucket: str, key: str) -> ObjectContent | None:
        statement = (
            sa.select(
                content_manifests.c.manifest_id,
                content_manifests.c.schema_version,
                content_manifests.c.representation,
                content_manifests.c.chunking_algorithm,
                content_manifests.c.chunking_version,
                content_manifests.c.chunk_size,
                content_manifests.c.chunk_count,
                content_blobs.c.digest_algorithm,
                content_blobs.c.sha256,
                content_blobs.c.size,
                content_blobs.c.cas_key,
            )
            .select_from(
                objects.join(
                    object_contents,
                    object_contents.c.object_id == objects.c.object_id,
                )
                .join(
                    content_manifests,
                    content_manifests.c.manifest_id == object_contents.c.manifest_id,
                )
                .join(
                    content_blobs,
                    content_blobs.c.sha256 == content_manifests.c.content_sha256,
                )
            )
            .where(
                objects.c.bucket == bucket,
                objects.c.object_key == key,
            )
        )
        with self._connection() as connection:
            row = connection.execute(statement).mappings().first()
            if row is None:
                return None
            chunk_rows = connection.execute(
                sa.select(
                    content_manifest_chunks.c.chunk_index,
                    content_manifest_chunks.c.byte_offset,
                    content_manifest_chunks.c.byte_length,
                    content_manifest_chunks.c.chunk_sha256,
                    content_blobs.c.size.label("blob_size"),
                    content_blobs.c.cas_key,
                )
                .select_from(
                    content_manifest_chunks.join(
                        content_blobs,
                        content_blobs.c.sha256
                        == content_manifest_chunks.c.chunk_sha256,
                    )
                )
                .where(content_manifest_chunks.c.manifest_id == row["manifest_id"])
                .order_by(content_manifest_chunks.c.chunk_index)
                # psycopg's ordinary cursor buffers the complete result in
                # libpq. Request server-side streaming so PostgreSQL retrieval
                # has the same fixed descriptor bound as SQLite iteration.
                .execution_options(
                    stream_results=True,
                    max_row_buffer=MAX_IN_MEMORY_CONTENT_CHUNKS,
                )
            ).mappings()
            try:

                def iter_chunks() -> Iterator[ContentChunk]:
                    for chunk_row in chunk_rows:
                        if chunk_row["byte_length"] != chunk_row["blob_size"]:
                            raise RuntimeError(
                                "persisted content chunk length does not match its blob"
                            )
                        yield ContentChunk(
                            index=chunk_row["chunk_index"],
                            offset=chunk_row["byte_offset"],
                            size=chunk_row["byte_length"],
                            sha256=chunk_row["chunk_sha256"],
                            cas_key=chunk_row["cas_key"],
                        )

                chunks = ContentChunkSequence.from_iterable(iter_chunks())
            finally:
                chunk_rows.close()
        if len(chunks) != row["chunk_count"]:
            raise RuntimeError("persisted content manifest has an invalid chunk count")
        return ObjectContent(
            schema_version=row["schema_version"],
            representation=row["representation"],
            digest_algorithm=row["digest_algorithm"],
            sha256=row["sha256"],
            size=row["size"],
            cas_key=row["cas_key"],
            chunking_algorithm=row["chunking_algorithm"],
            chunking_version=row["chunking_version"],
            chunk_size=row["chunk_size"],
            chunks=chunks,
        )

    def reconcile_content_references(
        self,
        *,
        grace_period_seconds: float,
        now: str | datetime | None = None,
    ) -> ContentReferenceReport:
        """Report stored counts against the active ownership graph without writes."""

        object_counts = (
            sa.select(
                content_manifests.c.content_sha256.label("sha256"),
                sa.func.count(object_contents.c.object_id).label("object_count"),
            )
            .select_from(
                object_contents.join(
                    content_manifests,
                    content_manifests.c.manifest_id == object_contents.c.manifest_id,
                )
            )
            .group_by(content_manifests.c.content_sha256)
            .subquery("expected_content_object_references")
        )
        chunk_counts = (
            sa.select(
                content_manifest_chunks.c.chunk_sha256.label("sha256"),
                sa.func.count().label("chunk_count"),
            )
            .select_from(
                object_contents.join(
                    content_manifest_chunks,
                    content_manifest_chunks.c.manifest_id
                    == object_contents.c.manifest_id,
                )
            )
            .group_by(content_manifest_chunks.c.chunk_sha256)
            .subquery("expected_content_chunk_references")
        )
        statement = (
            sa.select(
                content_blobs.c.sha256,
                content_blobs.c.cas_key,
                content_blobs.c.size,
                content_blobs.c.reference_count,
                content_blobs.c.unreferenced_at,
                sa.func.coalesce(object_counts.c.object_count, 0).label(
                    "expected_object_reference_count"
                ),
                sa.func.coalesce(chunk_counts.c.chunk_count, 0).label(
                    "expected_chunk_reference_count"
                ),
            )
            .select_from(
                content_blobs.outerjoin(
                    object_counts,
                    object_counts.c.sha256 == content_blobs.c.sha256,
                ).outerjoin(
                    chunk_counts,
                    chunk_counts.c.sha256 == content_blobs.c.sha256,
                )
            )
            .order_by(content_blobs.c.sha256)
            .execution_options(
                stream_results=True,
                max_row_buffer=MAX_IN_MEMORY_CONTENT_CHUNKS,
            )
        )
        with self._connection() as connection:
            held = False
            for row in connection.execute(sa.select(legal_holds)).mappings():
                hold = self._validated_legal_hold(row)
                if hold.tenant_id != self.tenant_id:
                    raise CatalogSchemaError("Stored legal hold tenant is inconsistent")
                held = held or hold.active
            rows = connection.execute(statement).mappings()
            try:

                def snapshots() -> Iterator[ContentReferenceSnapshot]:
                    for row in rows:
                        yield ContentReferenceSnapshot(
                            sha256=row["sha256"],
                            cas_key=row["cas_key"],
                            size=int(row["size"]),
                            stored_reference_count=int(row["reference_count"]),
                            expected_object_reference_count=int(
                                row["expected_object_reference_count"]
                            ),
                            expected_chunk_reference_count=int(
                                row["expected_chunk_reference_count"]
                            ),
                            unreferenced_at=row["unreferenced_at"],
                            legal_hold=held,
                        )

                return build_content_reference_report(
                    snapshots(),
                    grace_period_seconds=grace_period_seconds,
                    now=now,
                )
            finally:
                rows.close()

    @guard_legal_hold("catalog.update_placement")
    def update_placement(self, bucket: str, key: str, tier: str) -> None:
        now = _timestamp()
        with self._transaction(audit=False) as connection:
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
                        object_placements.c.placement_started_at,
                        object_placements.c.last_tier_move_at,
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
                .values(
                    tier_name=tier, pool_id=pool_id, updated_at=now,
                    placement_started_at=(
                        placement["placement_started_at"]
                        if placement["tier_name"] == tier else now
                    ),
                    last_tier_move_at=(
                        placement["last_tier_move_at"]
                        if placement["tier_name"] == tier else now
                    ),
                )
            )

    @guard_legal_hold("catalog.upsert_placement")
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
        with self._transaction(audit=False) as connection:
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
            object_id = self._write_object(
                connection,
                bucket,
                key,
                size=size,
                tier=tier,
                metadata=metadata,
                now=now,
            )
            invalidated = self._invalidate_object_content_if_mismatched(
                connection,
                object_id,
                size=size,
                checksum=checksum,
            )
            if invalidated:
                metadata.pop("content_identity", None)
                if checksum is None:
                    metadata.pop("sha256", None)
                connection.execute(
                    sa.update(objects)
                    .where(objects.c.object_id == object_id)
                    .values(metadata=metadata, updated_at=now)
                )

    @guard_legal_hold("catalog.delete")
    def delete(self, bucket: str, key: str) -> None:
        with self._object_publication(bucket, key) as connection:
            self._lock_object(connection, bucket, key)
            object_id = connection.execute(
                sa.select(objects.c.object_id).where(
                    objects.c.bucket == bucket,
                    objects.c.object_key == key,
                )
            ).scalar_one_or_none()
            if object_id is not None:
                self._set_object_content_manifest(
                    connection,
                    object_id,
                    None,
                    now=_timestamp(),
                )
            connection.execute(
                sa.delete(objects).where(
                    objects.c.bucket == bucket,
                    objects.c.object_key == key,
                )
            )

    @staticmethod
    def _literal_prefix(connection: Connection, column: Any, value: str) -> Any:
        parameter: sa.BindParameter[Any] = sa.bindparam(
            "literal_prefix",
            value,
            type_=column.type,
        )
        if connection.dialect.name == "postgresql":
            return sa.func.substr(column, 1, sa.func.octet_length(parameter)) == parameter
        binary_column = sa.cast(column, sa.LargeBinary())
        binary_parameter = sa.cast(parameter, sa.LargeBinary())
        return (
            sa.func.substr(binary_column, 1, sa.func.length(binary_parameter)) == binary_parameter
        )

    def list(self, bucket: str, prefix: str = "") -> list[ObjectRecord]:
        with self._connection() as connection:
            statement = self._object_select().where(
                objects.c.bucket == bucket,
                self._literal_prefix(connection, objects.c.object_key, prefix),
            )
            rows = connection.execute(statement).mappings().all()
        records = [self._record(row) for row in rows]
        # Normalize after fetching so database collation cannot change the
        # backend-neutral ordering contract.
        return sorted(records, key=lambda record: record.key)

    def list_page(
        self,
        bucket: str,
        prefix: str = "",
        *,
        after_key: str | None = None,
        limit: int = 100,
        tier: str | None = None,
    ) -> List[ObjectRecord]:
        """Return one bounded keyset page in backend-neutral key order."""

        if after_key is not None and not isinstance(after_key, str):
            raise ValueError("after_key must be a string or null")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if tier is not None and not isinstance(tier, str):
            raise ValueError("tier must be a string or null")
        with self._connection() as connection:
            # NulSafeText is BYTEA on PostgreSQL, whose native comparison is
            # already the UTF-8 byte order used by Python string ordering.
            # SQLite stores TEXT and needs an explicit binary collation so a
            # database's default collation cannot alter cursor boundaries.
            key: sa.ColumnElement[Any] = objects.c.object_key
            if connection.dialect.name == "sqlite":
                key = key.collate("BINARY")
            statement = self._object_select().where(
                objects.c.bucket == bucket,
                self._literal_prefix(connection, objects.c.object_key, prefix),
            )
            if after_key is not None:
                statement = statement.where(key > after_key)
            if tier is not None:
                statement = statement.where(object_placements.c.tier_name == tier)
            rows = (
                connection.execute(statement.order_by(key).limit(limit))
                .mappings()
                .all()
            )
        return [self._record(row) for row in rows]

    def iter_objects(self, *, batch_size: int = 1000) -> Iterator[ObjectRecord]:
        """Stream detached snapshots for a bounded full-catalog rebuild."""

        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        statement = self._object_select().order_by(
            objects.c.bucket,
            objects.c.object_key,
        )
        with self._connection() as connection:
            rows = connection.execution_options(
                stream_results=True,
                max_row_buffer=batch_size,
            ).execute(statement).mappings()
            try:
                while True:
                    require_tenant(self.tenant_id)
                    batch = rows.fetchmany(batch_size)
                    if not batch:
                        break
                    for row in batch:
                        require_tenant(self.tenant_id)
                        yield self._record(row)
            finally:
                rows.close()

    @staticmethod
    def _access_event_from_row(row: RowMapping) -> AccessEvent:
        return AccessEvent(
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

    def append_access_event(self, event: AccessEvent) -> AccessEvent:
        if self.read_only:
            raise PermissionError("cannot append access events to a read-only catalog")
        if not isinstance(event, AccessEvent):
            raise TypeError("event must be an AccessEvent")
        values = {
            "event_id": event.event_id,
            "operation_id": event.operation_id,
            "correlation_id": event.correlation_id,
            "occurred_at": event.occurred_at,
            "kind": event.kind,
            "bucket": event.bucket,
            "object_key": event.key,
            "tier": event.tier,
            "source": event.source,
            "sample_rate": event.sample_rate,
            "schema_version": event.schema_version,
            "expired": False,
        }
        with self._transaction(audit=False) as connection:
            if connection.dialect.name == "postgresql":
                # Return and lock the winning observation in the same statement.
                # DO NOTHING followed by SELECT leaves a gap where retention
                # can purge an expired retry ID before the SELECT sees it.
                # The no-op update preserves every first-observation field,
                # including the sampling rate and expired tombstone marker.
                statement = (
                    postgresql.insert(access_events)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=[access_events.c.event_id],
                        set_={"event_id": access_events.c.event_id},
                    )
                    .returning(*access_events.c)
                )
                row = connection.execute(statement).mappings().one()
            else:
                # SQLite's writer transaction already excludes concurrent purge.
                self._do_nothing_insert(connection, access_events, values)
                row = (
                    connection.execute(
                        sa.select(access_events).where(access_events.c.event_id == event.event_id)
                    )
                    .mappings()
                    .one()
                )
            persisted = self._access_event_from_row(row)
            if persisted.replay_identity() != event.replay_identity():
                raise ValueError("access event ID conflicts with persisted identity")
            return persisted

    def aggregate_access_events(
        self,
        bucket: str,
        key: str | None,
        *,
        config: AccessConfig,
        as_of: str | datetime,
    ) -> AccessSnapshot:
        instant = access_timestamp(as_of)
        lower = access_cutoff(instant, config.retention_seconds)
        fields: list[Any] = [
            sa.func.count().label("observed_events"),
            sa.func.min(access_events.c.occurred_at).label("observed_since"),
            sa.func.max(access_events.c.occurred_at).label("last_access_at"),
            sa.func.min(access_events.c.sample_rate).label("minimum_sample_rate"),
        ]
        for index, seconds in enumerate(config.windows_seconds):
            cutoff = access_cutoff(instant, seconds)
            for kind in ACCESS_KINDS:
                included = sa.and_(
                    access_events.c.kind == kind, access_events.c.occurred_at > cutoff
                )
                fields.append(
                    sa.func.coalesce(sa.func.sum(sa.case((included, 1), else_=0)), 0).label(
                        f"count_{index}_{kind}"
                    )
                )
                fields.append(
                    sa.func.coalesce(
                        sa.func.sum(
                            sa.case((included, 1.0 / access_events.c.sample_rate), else_=0.0)
                        ),
                        0.0,
                    ).label(f"estimate_{index}_{kind}")
                )
        # One aggregate query over one indexed object and a bounded time range.
        # The row count returned is constant regardless of raw event volume.
        query = (
            sa.select(*fields)
            .select_from(access_events)
            .where(
                access_events.c.bucket == bucket,
                access_events.c.object_key == key,
                access_events.c.expired.is_(False),
                access_events.c.occurred_at > lower,
                access_events.c.occurred_at <= instant,
            )
        )
        with self._connection() as connection:
            row = connection.execute(query).mappings().one()
        return AccessSnapshot(
            windows=tuple(
                AccessWindow(
                    seconds,
                    tuple(int(row[f"count_{index}_{kind}"]) for kind in ACCESS_KINDS),
                    tuple(float(row[f"estimate_{index}_{kind}"]) for kind in ACCESS_KINDS),
                )
                for index, seconds in enumerate(config.windows_seconds)
            ),
            observed_events=int(row["observed_events"]),
            observed_since=row["observed_since"],
            last_access_at=row["last_access_at"],
            minimum_sample_rate=row["minimum_sample_rate"],
        )

    def prune_access_events(
        self,
        occurred_before: str | datetime,
        *,
        limit: int = 1000,
        retention_seconds: int = 2592000,
    ) -> int:
        if self.read_only:
            raise PermissionError("cannot prune access events in a read-only catalog")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ValueError("limit must be an integer from 1 to 10000")
        config = AccessConfig(windows_seconds=(1,), retention_seconds=retention_seconds)
        cutoff = access_timestamp(occurred_before)
        dedup_cutoff = access_cutoff(cutoff, config.retention_seconds)
        with self._transaction(audit=False) as connection:
            purge = (
                sa.select(access_events.c.event_id)
                .where(
                    access_events.c.expired.is_(True), access_events.c.occurred_at < dedup_cutoff
                )
                .order_by(access_events.c.occurred_at, access_events.c.event_id)
                .limit(limit)
            )
            purged = connection.execute(
                sa.delete(access_events).where(access_events.c.event_id.in_(purge))
            )
            expire = (
                sa.select(access_events.c.event_id)
                .where(access_events.c.expired.is_(False), access_events.c.occurred_at < cutoff)
                .order_by(access_events.c.occurred_at, access_events.c.event_id)
                .limit(limit)
            )
            changed = connection.execute(
                sa.update(access_events)
                .where(access_events.c.expired.is_(False), access_events.c.event_id.in_(expire))
                .values(expired=True)
            )
            return int(changed.rowcount) + int(purged.rowcount)

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

    def audit_checkpoint(self) -> AuditCheckpoint:
        with self._connection() as connection:
            return read_head(connection, self.tenant_id)

    @contextlib.contextmanager
    def _audit_snapshot(self):
        with self._connection() as connection:
            if self.backend == "sqlite":
                connection.exec_driver_sql("BEGIN")
            else:
                connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            yield read_snapshot(connection, self.tenant_id)

    def verify_audit_integrity(self, checkpoint: AuditCheckpoint | Mapping[str, Any] | None = None) -> AuditIntegrityResult:
        with self._audit_snapshot() as (entries, events, tombstones, head, issues):
            return verify_snapshot(self.tenant_id, entries, events, tombstones, head, checkpoint, initial_issues=issues)

    def export_audit_evidence(self, *, after_sequence: int = 0, limit: int = 1000,
                             checkpoint: AuditCheckpoint | Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._connection() as connection:
            if self.backend == "sqlite":
                connection.exec_driver_sql("BEGIN")
            else:
                connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            return read_export(connection, self.tenant_id, after_sequence=after_sequence,
                               limit=limit, checkpoint=checkpoint)

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
        if criteria.before_event is not None:
            timestamp, event_id = criteria.before_event
            statement = statement.where(sa.or_(
                audit_events.c.occurred_at < timestamp,
                sa.and_(
                    audit_events.c.occurred_at == timestamp,
                    audit_events.c.event_id < UUID(event_id),
                ),
            ))
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
            entries, retained, tombstones, head, issues = read_snapshot(connection, self.tenant_id)
            integrity = verify_snapshot(self.tenant_id, entries, retained, tombstones, head, initial_issues=issues)
            if not integrity.valid:
                raise ValueError("cannot prune audit history that fails integrity verification")
            rows = connection.execute(
                sa.select(audit_events)
                .where(predicate, audit_events.c.event_type.not_in(LEGAL_HOLD_EVENT_TYPES | ORPHAN_CLEANUP_EVENT_TYPES),
                       audit_events.c.event_type != AuditEventType.AUDIT_RETENTION.value)
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
                append_entry(connection, self.tenant_id, kind="retention", event_id=event.event_id,
                    payload_digest=digest({"event_id": event.event_id, "replay_digest": audit_event_replay_digest(event),
                        "causation_id": event.causation_id, "expires_at": event.expires_at}))
            if identifiers:
                if self.backend == "sqlite":
                    connection.info["audit_retention_authorized"] = True
                    try:
                        result = connection.execute(
                            sa.delete(audit_events).where(audit_events.c.event_id.in_(identifiers)))
                        count = max(0, int(result.rowcount or 0))
                    finally:
                        connection.info.pop("audit_retention_authorized", None)
                else:
                    count = int(connection.execute(sa.text(
                        "SELECT cognistore_prune_audit_events(CAST(:identifiers AS uuid[]))"),
                        {"identifiers": identifiers}).scalar_one())
                self._insert_audit_event(connection, self._prepare_audit_event(AuditEvent.create(
                    AuditEventType.AUDIT_RETENTION, AuditOutcome.SUCCEEDED,
                    AuditContext(correlation_id=str(uuid4()), actor_type="service", actor_id="catalog-retention"),
                    details={"pruned_events": count}, retention=self.audit_retention)))
                return count
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
                expires_at=(None if event.event_type in LEGAL_HOLD_EVENT_TYPES | ORPHAN_CLEANUP_EVENT_TYPES
                            else self.audit_retention.expires_at(event.occurred_at)),
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
        append_entry(connection, self.tenant_id, kind="event", event_id=persisted.event_id,
                     payload_digest=event_digest(persisted), move_id=persisted.move_id, move_sequence=move_sequence)
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

    def _assert_move_controls(
        self, connection: Connection, bucket: str, key: str,
        src_tier: str, dst_tier: str, source_metadata: Mapping[str, Any], now: str,
    ) -> dict[str, Any]:
        # The caller holds the object fence; topology changes use this lock too.
        self._lock_topology(connection)
        row = connection.execute(self._object_select().where(
            objects.c.bucket == bucket, objects.c.object_key == key,
        )).mappings().first()
        tier_metadata = connection.execute(
            sa.select(tiers.c.metadata).where(tiers.c.name == src_tier)
        ).scalar_one_or_none()
        return _assert_catalog_move_allowed(
            None if row is None else self._record(row), src_tier, dst_tier,
            source_metadata, tier_metadata, now,
            tenant_id=self.tenant_id, bucket=bucket, key=key,
            tiers=[self._tier_record(item) for item in connection.execute(
                sa.select(tiers)
            ).mappings()],
            pools=[self._pool_record(item) for item in connection.execute(
                sa.select(pools)
            ).mappings()],
        )

    @serialize_cleanup
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
            if existing is not None:
                Catalog._assert_same_move(
                    existing, src_tier=src_tier, dst_tier=dst_tier,
                    bucket=bucket, key=key, source_metadata=source_metadata,
                )
            if existing is not None:
                if existing.state.terminal:
                    return existing
            source_metadata = existing.source_metadata if existing is not None else source_metadata
            if existing is None or existing.state in {
                MoveJobState.PREPARED, MoveJobState.TRANSFERRED, MoveJobState.VERIFIED,
            }:
                locality = self._assert_move_controls(
                    connection, bucket, key, src_tier, dst_tier,
                    source_metadata, now,
                )
                if locality["configured"] and (
                    existing is None or "cognistore_locality" not in source_metadata
                ):
                    source_metadata = {**source_metadata, "cognistore_locality": locality}
            if existing is not None and (existing.owner_id not in (None, owner_id)
                and existing.lease_expires_at is not None and existing.lease_expires_at > now):
                raise MoveJobLeaseError(f"Move job {idempotency_key!r} is leased by {existing.owner_id!r}")
            source_metadata = self._reserve_move_budgets(
                connection, move_id=idempotency_key, bucket=bucket, key=key,
                src_tier=src_tier, dst_tier=dst_tier,
                size=existing.expected_size if existing else expected_size,
                source_metadata=source_metadata,
                now=now, owner_id=owner_id, audit_context=audit_context,
                trusted_override=existing is not None,
            )
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
                        source_metadata=dict(source_metadata),
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

    def list_move_jobs_page(
        self,
        bucket: str,
        prefix: str = "",
        *,
        after_id: str | None = None,
        limit: int = 100,
    ) -> List[MoveJob]:
        """Read a bounded job page for one bucket and literal object-key prefix."""

        if not isinstance(bucket, str) or not bucket:
            raise ValueError("bucket must be a non-empty string")
        if not isinstance(prefix, str):
            raise ValueError("prefix must be a string")
        if after_id is not None and not isinstance(after_id, str):
            raise ValueError("after_id must be a string or null")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        with self._connection() as connection:
            identity: sa.ColumnElement[Any] = move_jobs.c.idempotency_key
            if connection.dialect.name == "sqlite":
                identity = identity.collate("BINARY")
            # NulSafeText is BYTEA on PostgreSQL, so its native comparison
            # already follows the same UTF-8 order as Python strings.
            statement = sa.select(*_MOVE_JOB_COLUMNS).where(
                move_jobs.c.bucket == bucket,
                self._literal_prefix(connection, move_jobs.c.object_key, prefix),
            )
            if after_id is not None:
                statement = statement.where(identity > after_id)
            rows = connection.execute(
                statement.order_by(identity).limit(limit)
            ).mappings().all()
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
        with self._transaction(audit=False) as connection:
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

    @serialize_cleanup
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

    @guard_legal_hold("catalog.commit_move_job_placement", move=True)
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
        expected_record: ObjectRecord | None = None,
    ) -> MoveJob:
        validate_move_job_transition(MoveJobState.VERIFIED, MoveJobState.COMMITTED)
        validate_catalog_size(size)
        with self._transaction() as connection:
            self._lock_move_key(connection, idempotency_key)
            initial = self._select_move_job(connection, idempotency_key)
            if initial is None:
                raise KeyError(f"Move job not found: {idempotency_key}")
            self._lock_object(connection, initial.bucket, initial.key)
            if expected_record is not None:
                row = connection.execute(self._object_select().where(
                    objects.c.bucket == initial.bucket, objects.c.object_key == initial.key,
                )).mappings().first()
                if row is None or self._record(row) != expected_record:
                    raise MoveJobConflictError("Catalog record changed since repair verification")
            job = self._select_owned_move_job(
                connection,
                idempotency_key,
                owner_id,
                MoveJobState.VERIFIED,
            )
            if tier != job.dst_tier:
                raise MoveJobConflictError("Committed tier must match move destination")
            locality = self._assert_move_controls(
                connection, job.bucket, job.key, job.src_tier, tier, job.source_metadata, now,
            )
            committed_metadata = dict(job.source_metadata)
            if locality["configured"] and "cognistore_locality" not in committed_metadata:
                committed_metadata["cognistore_locality"] = locality
            pool_id = job.source_metadata.get("cognistore_destination_pool_id")
            if pool_id is not None:
                bound_pool = connection.execute(sa.select(pools.c.tier_name, pools.c.active).where(
                    pools.c.pool_id == pool_id,
                )).mappings().first()
                if bound_pool is None or not bound_pool["active"] or bound_pool["tier_name"] != tier:
                    raise BudgetConstraintError("budget destination pool is no longer available")
            existing = connection.execute(
                sa.select(objects.c.metadata).where(
                    objects.c.bucket == job.bucket,
                    objects.c.object_key == job.key,
                )
            ).scalar_one_or_none()
            metadata = dict(existing or {})
            metadata["sha256"] = checksum
            object_id = self._write_object(
                connection,
                job.bucket,
                job.key,
                size=size,
                tier=tier,
                metadata=metadata,
                now=now,
            )
            if pool_id is not None:
                connection.execute(sa.update(object_placements).where(
                    object_placements.c.object_id == object_id,
                ).values(pool_id=pool_id))
            if job.src_tier != tier:
                # A direct catalog checkpoint may retain the same tier; only
                # actual movement starts a cooldown, including unscanned sources.
                connection.execute(
                    sa.update(object_placements)
                    .where(object_placements.c.object_id == object_id)
                    .values(last_tier_move_at=now)
                )
            invalidated = self._invalidate_object_content_if_mismatched(
                connection,
                object_id,
                size=size,
                checksum=checksum,
            )
            if invalidated:
                metadata.pop("content_identity", None)
                connection.execute(
                    sa.update(objects)
                    .where(objects.c.object_id == object_id)
                    .values(metadata=metadata, updated_at=now)
                )
            connection.execute(
                sa.update(move_jobs)
                .where(move_jobs.c.idempotency_key == idempotency_key)
                .values(
                    state=MoveJobState.COMMITTED.value,
                    source_metadata=committed_metadata,
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

    @staticmethod
    def _tier_record(row: RowMapping) -> Tier:
        return Tier(
            name=row["name"],
            metadata=deepcopy(dict(row["metadata"] or {})),
            active=bool(row["active"]),
        )

    @staticmethod
    def _pool_record(row: RowMapping) -> Pool:
        return Pool(
            pool_id=row["pool_id"],
            tier=row["tier_name"],
            region=row["region"],
            members=tuple(row["members"]),
            localities=tuple(row["localities"]),
            attributes={
                name: AttributeValue.from_mapping(value)
                for name, value in row["attributes"].items()
            },
            metadata=deepcopy(dict(row["metadata"] or {})),
            active=bool(row["active"]),
        )

    def register_tier(
        self,
        name: str,
        metadata: Mapping[str, object] | None = None,
        *,
        active: bool = True,
    ) -> None:
        if metadata is not None and "minimum_residency_seconds" in metadata:
            validate_minimum_residency_seconds(metadata["minimum_residency_seconds"])
        definition = Tier(name=name, metadata=dict(metadata or {}), active=active)
        now = _timestamp()
        with self._transaction(audit=False) as connection:
            self._lock_topology(connection, exclusive=True)
            if not definition.active:
                placed = connection.execute(
                    sa.select(object_placements.c.placement_id)
                    .where(object_placements.c.tier_name == name)
                    .limit(1)
                ).first()
                active_pool = connection.execute(
                    sa.select(pools.c.pool_id)
                    .where(pools.c.tier_name == name, pools.c.active.is_(True))
                    .limit(1)
                ).first()
                if placed is not None or active_pool is not None:
                    raise ValueError(f"Tier has active references: {name}")
            self._do_nothing_insert(
                connection,
                tiers,
                {
                    "name": name,
                    "metadata": definition.metadata,
                    "active": active,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            changes: dict[str, object] = {"active": active, "updated_at": now}
            if metadata is not None:
                changes["metadata"] = definition.metadata
            connection.execute(sa.update(tiers).where(tiers.c.name == name).values(**changes))

    def get_tier(self, name: str) -> Tier | None:
        with self._connection() as connection:
            row = connection.execute(
                sa.select(tiers).where(tiers.c.name == name)
            ).mappings().first()
        return None if row is None else self._tier_record(row)

    def list_tiers(self) -> List[Tier]:
        with self._connection() as connection:
            rows = connection.execute(sa.select(tiers)).mappings().all()
        # Sorting decoded identifiers gives the same result for both backends,
        # including strings that require PostgreSQL's NUL-safe representation.
        return sorted((self._tier_record(row) for row in rows), key=lambda item: item.name)

    def delete_tier(self, name: str) -> None:
        with self._transaction(audit=False) as connection:
            self._lock_topology(connection, exclusive=True)
            placed = connection.execute(
                sa.select(object_placements.c.placement_id)
                .where(object_placements.c.tier_name == name)
                .limit(1)
            ).first()
            pool = connection.execute(
                sa.select(pools.c.pool_id).where(pools.c.tier_name == name).limit(1)
            ).first()
            if placed is not None or pool is not None:
                raise ValueError(f"Tier has references: {name}")
            connection.execute(sa.delete(tiers).where(tiers.c.name == name))

    def register_pool(
        self,
        pool_id: str,
        tier: str,
        metadata: Mapping[str, object] | None = None,
        *,
        region: str | None = None,
        members: tuple[str, ...] = (),
        localities: tuple[str, ...] = (),
        attributes: Mapping[str, AttributeValue] | None = None,
        active: bool = True,
    ) -> None:
        definition = Pool(
            pool_id=pool_id,
            tier=tier,
            region=region,
            members=members,
            localities=localities,
            attributes=dict(attributes or {}),
            metadata=dict(metadata or {}),
            active=active,
        )
        now = _timestamp()
        with self._transaction(audit=False) as connection:
            self._lock_topology(connection, exclusive=True)
            tier_active = connection.execute(
                sa.select(tiers.c.active).where(tiers.c.name == tier)
            ).scalar_one_or_none()
            if tier_active is None:
                raise KeyError(f"Tier not found: {tier}")
            if not tier_active:
                raise ValueError(f"Tier is inactive: {tier}")
            previous = connection.execute(
                sa.select(pools.c.tier_name).where(pools.c.pool_id == pool_id)
            ).scalar_one_or_none()
            if previous is not None and (previous != tier or not active):
                placed = connection.execute(
                    sa.select(object_placements.c.placement_id)
                    .where(object_placements.c.pool_id == pool_id)
                    .limit(1)
                ).first()
                if placed is not None:
                    raise ValueError(f"Pool has active placements: {pool_id}")
            values = {
                "pool_id": pool_id,
                "tier_name": tier,
                "metadata": definition.metadata,
                "region": definition.region,
                "members": list(definition.members),
                "localities": list(definition.localities),
                "attributes": {
                    name: value.to_mapping() for name, value in definition.attributes.items()
                },
                "active": definition.active,
                "created_at": now,
                "updated_at": now,
            }
            if previous is None:
                connection.execute(sa.insert(pools).values(**values))
            else:
                values.pop("created_at")
                connection.execute(
                    sa.update(pools).where(pools.c.pool_id == pool_id).values(**values)
                )

    def get_pool(self, pool_id: str) -> Pool | None:
        with self._connection() as connection:
            row = connection.execute(
                sa.select(pools).where(pools.c.pool_id == pool_id)
            ).mappings().first()
        return None if row is None else self._pool_record(row)

    def list_pools(self, *, tier: str | None = None) -> List[Pool]:
        statement = sa.select(pools)
        if tier is not None:
            statement = statement.where(pools.c.tier_name == tier)
        with self._connection() as connection:
            rows = connection.execute(statement).mappings().all()
        return sorted((self._pool_record(row) for row in rows), key=lambda item: item.pool_id)

    def delete_pool(self, pool_id: str) -> None:
        with self._transaction(audit=False) as connection:
            self._lock_topology(connection, exclusive=True)
            placed = connection.execute(
                sa.select(object_placements.c.placement_id)
                .where(object_placements.c.pool_id == pool_id)
                .limit(1)
            ).first()
            if placed is not None:
                raise ValueError(f"Pool has active placements: {pool_id}")
            connection.execute(sa.delete(pools).where(pools.c.pool_id == pool_id))

    def eligible_placements(
        self,
        constraints: PlacementConstraints | None = None,
        *,
        now: str | datetime | None = None,
    ) -> List[PlacementCandidate]:
        # One statement gives both definitions a consistent database snapshot.
        statement = sa.select(
            pools,
            tiers.c.metadata.label("tier_metadata"),
            tiers.c.active.label("tier_active"),
        ).select_from(pools.join(tiers, pools.c.tier_name == tiers.c.name))
        with self._connection() as connection:
            rows = connection.execute(statement).mappings().all()
        definitions = [self._pool_record(row) for row in rows]
        tier_definitions = {
            row["tier_name"]: Tier(
                name=row["tier_name"],
                metadata=dict(row["tier_metadata"] or {}),
                active=bool(row["tier_active"]),
            )
            for row in rows
        }
        return eligible_candidates(tier_definitions.values(), definitions, constraints, now=now)

    @guard_legal_hold("catalog.assign_pool")
    def assign_pool(self, bucket: str, key: str, pool_id: str | None) -> None:
        now = _timestamp()
        with self._transaction(audit=False) as connection:
            self._lock_object(connection, bucket, key)
            self._lock_topology(connection)
            row = connection.execute(
                sa.select(
                    objects.c.object_id, object_placements.c.tier_name,
                    object_placements.c.placement_started_at,
                    object_placements.c.last_tier_move_at,
                )
                .select_from(objects.join(object_placements))
                .where(objects.c.bucket == bucket, objects.c.object_key == key)
            ).mappings().first()
            if row is None:
                raise KeyError(f"Object not found: {bucket}/{key}")
            tier = row["tier_name"]
            if pool_id is not None:
                target = connection.execute(
                    sa.select(pools.c.tier_name, pools.c.active, tiers.c.active.label("tier_active"))
                    .select_from(pools.join(tiers, pools.c.tier_name == tiers.c.name))
                    .where(pools.c.pool_id == pool_id)
                ).mappings().first()
                if target is None:
                    raise KeyError(f"Pool not found: {pool_id}")
                if not target["active"] or not target["tier_active"]:
                    raise ValueError(f"Pool or its tier is inactive: {pool_id}")
                tier = target["tier_name"]
            connection.execute(
                sa.update(object_placements)
                .where(object_placements.c.object_id == row["object_id"])
                .values(
                    tier_name=tier, pool_id=pool_id, updated_at=now,
                    placement_started_at=(
                        row["placement_started_at"] if row["tier_name"] == tier else now
                    ),
                    last_tier_move_at=(
                        row["last_tier_move_at"] if row["tier_name"] == tier else now
                    ),
                )
            )

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
        controls = job.source_metadata.get("cognistore_movement_constraints")
        locality = job.source_metadata.get("cognistore_locality")
        if isinstance(locality, Mapping):
            details["locality"] = deepcopy(dict(locality))
        if isinstance(controls, Mapping):
            details["movement_constraints"] = deepcopy(dict(controls))
            override = controls.get("stability_override")
            if override is not None:
                details["stability_override"] = deepcopy(override)
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
        next_sequence: int = connection.execute(
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
