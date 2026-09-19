from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pytest
import sqlalchemy as sa

from cognistore.cli import cognistore_cli
from cognistore.core.audit import (
    AuditContext,
    AuditEvent,
    AuditEventType,
    AuditOutcome,
    AuditQuery,
)
from cognistore.core.catalog import Catalog, CatalogStore, ObjectRecord
from cognistore.core.content_identity import ContentIdentityBuilder
from cognistore.core.move_jobs import (
    MoveJob,
    MoveJobConflictError,
    MoveJobLeaseError,
    MoveJobState,
    MoveJobTransition,
)
from cognistore.db import (
    CatalogSchemaNotInstalledError,
    CatalogSchemaOutdatedError,
    MigrationManager,
    SQLCatalog,
)
from cognistore.db.engine import normalize_database_url
from cognistore.db.schema import (
    content_blobs,
    content_manifest_chunks,
    content_manifests,
    object_contents,
    object_mutation_fences,
    object_placements,
    objects,
    pools,
    tiers,
)
from cognistore.db.sqlite_import import import_sqlite_catalog
from cognistore.drivers.posix_driver import PosixDriver
from tests.catalog_fixtures import create_prototype_sqlite_catalog
from tests.conformance.catalog_store import CatalogStoreConformance

pytestmark = pytest.mark.integration

_CATALOG_TABLES = {
    "access_events",
    "alembic_version",
    "audit_events",
    "audit_move_heads",
    "audit_event_tombstones",
    "budget_definitions",
    "budget_reservations",
    "catalog_schema_features",
    "catalog_tenant",
    "legal_holds",
    "content_blobs",
    "content_manifest_chunks",
    "content_manifests",
    "embedding_document_spaces",
    "embedding_documents",
    "embedding_passages",
    "embedding_spaces",
    "embedding_vectors",
    "move_job_claim_fences",
    "move_job_transitions",
    "move_jobs",
    "object_contents",
    "object_embedding_documents",
    "object_mutation_fences",
    "object_placements",
    "objects",
    "pools",
    "tiers",
}

_CONCURRENT_INITIALIZER = """
import os
import sys
from pathlib import Path

from cognistore.db import MigrationManager, SQLCatalog

Path(os.environ["COGNISTORE_TEST_INITIALIZER_READY"]).touch()
if sys.stdin.buffer.read(1) != b"!":
    raise RuntimeError("initializer start signal was not received")

with SQLCatalog(
    os.environ["COGNISTORE_TEST_INITIALIZER_DSN"],
    tenant_id=os.environ["COGNISTORE_TEST_INITIALIZER_TENANT"],
) as catalog:
    if not MigrationManager().is_at_head(catalog.engine):
        raise RuntimeError("initializer did not observe the migration head")
"""


class TestPostgresCatalogConformance(CatalogStoreConformance):
    @pytest.fixture
    def catalog(self, postgres_dsn: str) -> Iterator[CatalogStore]:
        with SQLCatalog(postgres_dsn) as catalog:
            yield catalog


def test_postgres_clean_install_has_normalized_schema_and_pgvector(
    postgres_dsn: str,
) -> None:
    manager = MigrationManager()
    with SQLCatalog(postgres_dsn) as catalog:
        assert catalog.backend == "postgresql"
        assert manager.is_at_head(catalog.engine)

        inspector = sa.inspect(catalog.engine)
        assert _CATALOG_TABLES <= set(inspector.get_table_names())
        assert {"object_id", "bucket", "object_key", "size", "metadata"} <= {
            column["name"] for column in inspector.get_columns("objects")
        }
        assert "key" not in {column["name"] for column in inspector.get_columns("objects")}
        assert any(
            constraint["column_names"] == ["object_id"]
            for constraint in inspector.get_unique_constraints("object_placements")
        )

        with catalog.engine.connect() as connection:
            vector_version = connection.execute(
                sa.text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).scalar_one()
            vector_storage_type = connection.exec_driver_sql(
                "SELECT format_type(atttypid, atttypmod) "
                "FROM pg_attribute "
                "WHERE attrelid = 'embedding_vectors'::regclass "
                "AND attname = 'embedding' AND NOT attisdropped"
            ).scalar_one()
            vector_constraints = set(
                connection.exec_driver_sql(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'embedding_vectors'::regclass"
                ).scalars()
            )
            feature = connection.execute(
                sa.text(
                    "SELECT available, owned FROM catalog_schema_features "
                    "WHERE feature = 'pgvector'"
                )
            ).one()

        assert vector_version
        assert vector_storage_type == "vector"
        assert (
            "ck_embedding_vectors_embedding_vector_dimensions_match"
            in vector_constraints
        )
        assert "fk_embedding_vectors_space_dimensions" in vector_constraints
        assert "fk_embedding_vectors_passage_text" in vector_constraints
        assert tuple(feature) == (True, True)


@pytest.mark.parametrize("tenant_id", ["default", "alice"])
def test_concurrent_postgres_initializers_reach_one_complete_migration_head(
    postgres_dsn: str,
    tmp_path: Path,
    tenant_id: str,
) -> None:
    worker_count = 8
    worker_environment = os.environ.copy()
    worker_environment["COGNISTORE_TEST_INITIALIZER_DSN"] = postgres_dsn
    worker_environment["COGNISTORE_TEST_INITIALIZER_TENANT"] = tenant_id
    processes: list[subprocess.Popen[bytes]] = []
    results: list[tuple[int, bytes, bytes]] = []
    ready_paths = [tmp_path / f"initializer-{index}.ready" for index in range(worker_count)]

    try:
        for ready_path in ready_paths:
            process_environment = worker_environment.copy()
            process_environment["COGNISTORE_TEST_INITIALIZER_READY"] = str(ready_path)
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", _CONCURRENT_INITIALIZER],
                    env=process_environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )

        ready_deadline = time.monotonic() + 30
        while not all(path.exists() for path in ready_paths):
            early_exits = [
                index
                for index, process in enumerate(processes)
                if process.poll() is not None
            ]
            if early_exits:
                raise AssertionError(
                    f"initializers exited before the start barrier: {early_exits}"
                )
            if time.monotonic() >= ready_deadline:
                raise TimeoutError("initializers did not reach the start barrier")
            time.sleep(0.01)

        # Every child has imported the application and now blocks on this byte,
        # so all SQLCatalog constructors contend on the PostgreSQL advisory lock.
        for process in processes:
            assert process.stdin is not None
            process.stdin.write(b"!")
            process.stdin.flush()

        for process in processes:
            stdout, stderr = process.communicate(timeout=45)
            results.append((process.returncode, stdout, stderr))
    finally:
        for process in processes:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.kill()
        for process in processes:
            process.wait(timeout=5)

    failures = [
        f"initializer {index} exited {returncode}:\n"
        f"stdout:\n{stdout.decode(errors='replace')}\n"
        f"stderr:\n{stderr.decode(errors='replace')}"
        for index, (returncode, stdout, stderr) in enumerate(results)
        if returncode != 0
    ]
    assert failures == []

    manager = MigrationManager()
    with SQLCatalog(postgres_dsn, tenant_id=tenant_id, migrate=False) as catalog:
        heads = manager.heads()
        assert len(heads) == 1
        assert manager.current(catalog.engine) == heads[0]
        assert set(sa.inspect(catalog.engine).get_table_names()) == _CATALOG_TABLES
        with catalog.engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalars().all() == [heads[0]]
            assert connection.exec_driver_sql(
                "SELECT feature, available, owned "
                "FROM catalog_schema_features ORDER BY feature"
            ).all() == [("pgvector", True, tenant_id == "default")]


def test_postgres_read_only_refuses_an_absent_schema(postgres_dsn: str) -> None:
    engine = sa.create_engine(normalize_database_url(postgres_dsn))
    try:
        with pytest.raises(CatalogSchemaNotInstalledError, match="migration head"):
            SQLCatalog(postgres_dsn, read_only=True)

        assert MigrationManager().current(engine) is None
        assert sa.inspect(engine).get_table_names() == []
    finally:
        engine.dispose()


def test_postgres_read_only_refuses_an_outdated_schema(postgres_dsn: str) -> None:
    manager = MigrationManager()
    engine = sa.create_engine(normalize_database_url(postgres_dsn))
    try:
        manager.upgrade(engine, "0002_normalized_catalog")
        tables_before_open = set(sa.inspect(engine).get_table_names())

        with pytest.raises(CatalogSchemaOutdatedError, match="migration head"):
            SQLCatalog(postgres_dsn, read_only=True)

        assert manager.current(engine) == "0002_normalized_catalog"
        assert set(sa.inspect(engine).get_table_names()) == tables_before_open
    finally:
        engine.dispose()


def test_postgres_read_only_reads_at_head_and_rejects_writes(
    postgres_dsn: str,
) -> None:
    with SQLCatalog(postgres_dsn) as writer:
        writer.upsert(
            "read-only-bucket",
            "object",
            size=7,
            tier="hot",
            metadata={"generation": "source:v1"},
        )

    with SQLCatalog(postgres_dsn, read_only=True) as reader:
        record = reader.get("read-only-bucket", "object")
        assert record is not None
        assert record == ObjectRecord(
            placement_started_at=record.placement_started_at,
            bucket="read-only-bucket",
            key="object",
            size=7,
            tier="hot",
            metadata={"generation": "source:v1"},
        )
        with reader.engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SHOW default_transaction_read_only"
            ).scalar_one() == "on"

        with pytest.raises(sa.exc.DBAPIError) as write_error:
            reader.update_placement("read-only-bucket", "object", "warm")

        assert getattr(write_error.value.orig, "sqlstate", None) == "25006"
        unchanged = reader.get("read-only-bucket", "object")
        assert unchanged is not None
        assert unchanged.tier == "hot"


def test_cognistore_owned_pgvector_is_removed_on_downgrade(
    postgres_dsn: str,
) -> None:
    manager = MigrationManager()
    with SQLCatalog(postgres_dsn) as catalog:
        with catalog.engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT available, owned FROM catalog_schema_features "
                "WHERE feature = 'pgvector'"
            ).one() == (True, True)
            assert connection.exec_driver_sql(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            ).scalar_one()

        manager.downgrade(catalog.engine, "0001_legacy_catalog")

        assert manager.current(catalog.engine) == "0001_legacy_catalog"
        with catalog.engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
            ).scalar_one_or_none() is None


def test_platform_provided_pgvector_is_not_owned_and_survives_downgrade(
    postgres_dsn: str,
) -> None:
    manager = MigrationManager()
    engine = sa.create_engine(normalize_database_url(postgres_dsn))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE EXTENSION vector")
            platform_version = connection.exec_driver_sql(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            ).scalar_one()

        with SQLCatalog(postgres_dsn) as catalog:
            with catalog.engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT available, owned FROM catalog_schema_features "
                    "WHERE feature = 'pgvector'"
                ).one() == (True, False)

            manager.downgrade(catalog.engine, "0001_legacy_catalog")

            assert manager.current(catalog.engine) == "0001_legacy_catalog"
            with catalog.engine.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
                ).scalar_one() == platform_version
    finally:
        engine.dispose()


def test_postgres_audit_round_trip_query_and_retention(postgres_dsn: str) -> None:
    occurred_at = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)
    event = AuditEvent.create(
        AuditEventType.JOB_RETRY,
        AuditOutcome.RETRYING,
        AuditContext(
            correlation_id="request\0postgres-31",
            actor_type="worker",
            actor_id="worker\0postgres",
            job_id="job\0postgres-31",
        ),
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        details={"attempt": 2, "category": "transient"},
    )

    with SQLCatalog(postgres_dsn) as catalog:
        stored = catalog.append_audit_event(event)
        assert catalog.get_audit_event(event.event_id) == stored
        assert catalog.list_audit_events(
            AuditQuery(
                correlation_id=event.correlation_id,
                job_id=event.job_id,
                event_types=frozenset({AuditEventType.JOB_RETRY}),
            )
        ) == [stored]
        assert catalog.prune_expired_audit_events(datetime(2026, 10, 1, tzinfo=timezone.utc)) == 1
        assert catalog.get_audit_event(event.event_id) is None


def test_postgres_failed_migration_rolls_back_schema_and_extension(
    postgres_dsn: str,
) -> None:
    manager = MigrationManager()
    engine = sa.create_engine(normalize_database_url(postgres_dsn))
    injected = False

    def fail_after_earlier_ddl(
        _connection: sa.Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal injected
        if not injected and "CREATE TABLE object_placements" in statement:
            injected = True
            raise RuntimeError("injected migration failure")

    sa.event.listen(engine, "before_cursor_execute", fail_after_earlier_ddl)
    try:
        with pytest.raises(RuntimeError, match="injected migration failure"):
            manager.upgrade(engine)
    finally:
        sa.event.remove(engine, "before_cursor_execute", fail_after_earlier_ddl)

    try:
        assert injected
        assert manager.current(engine) is None
        assert not {
            "alembic_version",
            "catalog_schema_features",
            "object_placements",
            "objects",
            "pools",
            "tiers",
        } & set(sa.inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                ).scalar_one_or_none()
                is None
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "prefix",
    ["", "a_", "a%", "a\\", "Foo", "foo", "nul\0"],
)
def test_postgres_object_prefix_matches_in_memory_including_nul(
    postgres_dsn: str,
    prefix: str,
) -> None:
    memory = Catalog()
    sqlite = SQLCatalog(":memory:")
    postgres = SQLCatalog(postgres_dsn)
    catalogs: tuple[CatalogStore, ...] = (memory, sqlite, postgres)
    keys = [
        "a_one",
        "abone",
        "a%literal",
        "a\\literal",
        "Foo",
        "foo",
        "nul\0key",
    ]

    try:
        for catalog in catalogs:
            for key in keys:
                catalog.upsert("bucket\0name", key, size=1, tier="hot\0tier")
            catalog.upsert("other", "a_one", size=1, tier="hot")

        expected = sorted(record.key for record in memory.list("bucket\0name", prefix))
        for catalog in (sqlite, postgres):
            actual = sorted(record.key for record in catalog.list("bucket\0name", prefix))
            assert actual == expected
            persisted = catalog.get("bucket\0name", "nul\0key")
            in_memory = memory.get("bucket\0name", "nul\0key")
            assert persisted is not None and in_memory is not None
            assert persisted.placement_started_at is not None
            assert in_memory.placement_started_at is not None
            # Independent inserts have independent clocks; every other field
            # still follows the same NUL-safe catalog contract.
            assert persisted == replace(
                in_memory, placement_started_at=persisted.placement_started_at,
            )
    finally:
        sqlite.close()
        postgres.close()


def test_postgres_list_page_matches_memory_order_filters_and_keyset(
    postgres_dsn: str,
) -> None:
    memory = Catalog()
    postgres = SQLCatalog(postgres_dsn)
    records = [
        ("page\0a", "hot\0tier"),
        ("page%literal", "warm"),
        ("pageA", "hot\0tier"),
        ("page_", "warm"),
        ("pagea", "hot\0tier"),
        ("page\u00e9", "hot\0tier"),
    ]

    try:
        for catalog in (memory, postgres):
            for key, tier in reversed(records):
                catalog.upsert("bucket\0name", key, size=len(key), tier=tier)
            catalog.upsert("other", "page-before", size=1, tier="hot\0tier")

        queries = [
            {"prefix": "page", "limit": 3},
            {"prefix": "page", "after_key": "pageA", "limit": 2},
            {"prefix": "page", "limit": 10, "tier": "hot\0tier"},
            {
                "prefix": "page",
                "after_key": "pageA",
                "limit": 10,
                "tier": "hot\0tier",
            },
        ]
        for query in queries:
            expected = memory.list_page("bucket\0name", **query)  # type: ignore[arg-type]
            actual = postgres.list_page("bucket\0name", **query)  # type: ignore[arg-type]
            assert [(record.key, record.tier) for record in actual] == [
                (record.key, record.tier) for record in expected
            ]
    finally:
        postgres.close()


def _exercise_move_catalog(
    catalog: CatalogStore,
) -> tuple[MoveJob, ObjectRecord | None, list[MoveJobTransition], list[str]]:
    idempotency_key = "move\0job"
    bucket = "bucket\0name"
    key = "objects/item\0.bin"
    owner = "worker\0one"
    source_tier = "hot\0tier"
    destination_tier = "warm\0tier"
    catalog.upsert(
        bucket,
        key,
        size=1,
        tier=source_tier,
        metadata={"classification": "keep\0exactly", "nested\0key": ["value\0one"]},
    )
    catalog.claim_move_job(
        idempotency_key,
        src_tier=source_tier,
        dst_tier=destination_tier,
        bucket=bucket,
        key=key,
        expected_size=11,
        source_metadata={"generation": "source-generation", "note": "source\0metadata"},
        owner_id=owner,
        now="2000-01-01T00:00:00.000000Z",
        lease_expires_at="2000-01-01T00:01:00.000000Z",
    )
    catalog.transition_move_job(
        idempotency_key,
        owner_id=owner,
        expected_state=MoveJobState.PREPARED,
        to_state=MoveJobState.TRANSFERRED,
        reason="copy\0complete",
        now="2000-01-01T00:00:01.000000Z",
        lease_expires_at="2000-01-01T00:01:01.000000Z",
        updates={"transferred_size": 11},
    )
    catalog.transition_move_job(
        idempotency_key,
        owner_id=owner,
        expected_state=MoveJobState.TRANSFERRED,
        to_state=MoveJobState.VERIFIED,
        reason="checksums\0match",
        now="2000-01-01T00:00:02.000000Z",
        lease_expires_at="2000-01-01T00:01:02.000000Z",
        updates={
            "source_size": 11,
            "source_checksum": "digest",
            "destination_size": 11,
            "destination_checksum": "digest",
            "destination_generation": "destination\0generation",
            "verification_details": ["size\0matched", "checksum"],
            "terminal_reason": "retained\0diagnostic",
        },
    )
    job = catalog.commit_move_job_placement(
        idempotency_key,
        owner_id=owner,
        size=11,
        tier=destination_tier,
        checksum="digest",
        now="2000-01-01T00:00:03.000000Z",
        lease_expires_at="2000-01-01T00:01:03.000000Z",
    )
    listed = [move.idempotency_key for move in catalog.list_move_jobs(idempotency_prefix="move\0")]
    return (
        job,
        catalog.get(bucket, key),
        catalog.list_move_job_transitions(idempotency_key),
        listed,
    )


def test_postgres_move_job_parity_including_nul(postgres_dsn: str) -> None:
    memory = Catalog()
    with SQLCatalog(":memory:") as sqlite, SQLCatalog(postgres_dsn) as postgres:
        expected = _exercise_move_catalog(memory)
        assert _exercise_move_catalog(sqlite) == expected
        assert _exercise_move_catalog(postgres) == expected


def test_postgres_catalog_guard_and_scan_fence_parity(postgres_dsn: str) -> None:
    memory = Catalog()
    sqlite = SQLCatalog(":memory:")
    postgres = SQLCatalog(postgres_dsn)
    catalogs: tuple[CatalogStore, ...] = (memory, sqlite, postgres)

    try:
        for catalog in catalogs:
            catalog.upsert(
                "bucket",
                "object",
                size=3,
                tier="hot",
                metadata={"classification": "retain"},
            )
            catalog.update_placement("bucket", "object", "warm")
            catalog.upsert_placement(
                "bucket",
                "object",
                size=4,
                tier="hot",
                checksum="digest",
            )
            record = catalog.get("bucket", "object")
            assert record is not None
            assert record.last_tier_move_at is not None
            assert record == ObjectRecord(
                placement_started_at=record.placement_started_at,
                last_tier_move_at=record.last_tier_move_at,
                bucket="bucket",
                key="object",
                size=4,
                tier="hot",
                metadata={"classification": "retain", "sha256": "digest"},
            )

            fence = catalog.capture_scan_fence("bucket", "object")
            catalog.claim_move_job(
                "guard:move",
                src_tier="hot",
                dst_tier="warm",
                bucket="bucket",
                key="object",
                expected_size=4,
                source_metadata={"generation": "source:v1"},
                owner_id="worker-one",
                now="2000-01-01T00:00:00.000000Z",
                lease_expires_at="2000-01-01T00:01:00.000000Z",
            )
            assert not catalog.upsert_scan_observation(
                "bucket",
                "object",
                size=99,
                tier="hot",
                generation="source:v1",
                metadata={"stale": True},
                fence=fence,
            )

            with pytest.raises(MoveJobLeaseError):
                catalog.claim_move_job(
                    "guard:move",
                    src_tier="hot",
                    dst_tier="warm",
                    bucket="bucket",
                    key="object",
                    expected_size=4,
                    source_metadata={"generation": "source:v1"},
                    owner_id="worker-two",
                    now="2000-01-01T00:00:01.000000Z",
                    lease_expires_at="2000-01-01T00:01:01.000000Z",
                )
            with pytest.raises(MoveJobConflictError):
                catalog.claim_move_job(
                    "guard:move",
                    src_tier="hot",
                    dst_tier="cold",
                    bucket="bucket",
                    key="other",
                    expected_size=4,
                    source_metadata={"generation": "source:v1"},
                    owner_id="worker-one",
                    now="2000-01-01T00:00:01.000000Z",
                    lease_expires_at="2000-01-01T00:01:01.000000Z",
                )
            renewed = catalog.renew_move_job_lease(
                "guard:move",
                owner_id="worker-one",
                expected_state=MoveJobState.PREPARED,
                now="2000-01-01T00:00:02.000000Z",
                lease_expires_at="2000-01-01T00:02:00.000000Z",
            )
            assert renewed.lease_expires_at == "2000-01-01T00:02:00.000000Z"
            assert [
                job.idempotency_key
                for job in catalog.list_move_jobs(
                    states={MoveJobState.PREPARED},
                    idempotency_prefix="guard:",
                )
            ] == ["guard:move"]

            with pytest.raises(ValueError, match="non-negative integer"):
                catalog.upsert("bucket", "negative", size=-1, tier="hot")
            with pytest.raises(ValueError, match="non-negative integer"):
                catalog.upsert("bucket", "oversized", size=2**63, tier="hot")
            with pytest.raises(ValueError, match="non-negative integer"):
                catalog.claim_move_job(
                    "guard:negative",
                    src_tier="hot",
                    dst_tier="warm",
                    bucket="bucket",
                    key="negative",
                    expected_size=-1,
                    source_metadata={},
                    owner_id="worker-one",
                    now="2000-01-01T00:00:00.000000Z",
                    lease_expires_at="2000-01-01T00:01:00.000000Z",
                )

            catalog.delete("bucket", "object")
            assert catalog.get("bucket", "object") is None
            with pytest.raises(KeyError):
                catalog.update_placement("bucket", "object", "cold")
    finally:
        sqlite.close()
        postgres.close()


def test_failed_postgres_placement_commit_rolls_back_object_and_job(
    postgres_dsn: str,
) -> None:
    with SQLCatalog(postgres_dsn) as catalog:
        catalog.upsert(
            "bucket",
            "object",
            size=4,
            tier="hot",
            metadata={"classification": "retain"},
        )
        catalog.claim_move_job(
            "atomic:move",
            src_tier="hot",
            dst_tier="warm",
            bucket="bucket",
            key="object",
            expected_size=4,
            source_metadata={"generation": "source:v1"},
            owner_id="worker-one",
            now="2000-01-01T00:00:00.000000Z",
            lease_expires_at="2000-01-01T00:01:00.000000Z",
        )
        catalog.transition_move_job(
            "atomic:move",
            owner_id="worker-one",
            expected_state=MoveJobState.PREPARED,
            to_state=MoveJobState.TRANSFERRED,
            reason="copied",
            now="2000-01-01T00:00:01.000000Z",
            lease_expires_at="2000-01-01T00:01:01.000000Z",
        )
        catalog.transition_move_job(
            "atomic:move",
            owner_id="worker-one",
            expected_state=MoveJobState.TRANSFERRED,
            to_state=MoveJobState.VERIFIED,
            reason="verified",
            now="2000-01-01T00:00:02.000000Z",
            lease_expires_at="2000-01-01T00:01:02.000000Z",
        )
        before = catalog.get("bucket", "object")

        def fail_job_update(
            _connection: sa.Connection,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            # Locality evidence may be the first updated column; inject at
            # the job checkpoint write independently of SQL column order.
            if statement.startswith("UPDATE move_jobs SET "):
                raise RuntimeError("injected placement commit failure")

        sa.event.listen(catalog.engine, "before_cursor_execute", fail_job_update)
        try:
            with pytest.raises(RuntimeError, match="injected placement commit failure"):
                catalog.commit_move_job_placement(
                    "atomic:move",
                    owner_id="worker-one",
                    size=4,
                    tier="warm",
                    checksum="digest",
                    now="2000-01-01T00:00:03.000000Z",
                    lease_expires_at="2000-01-01T00:01:03.000000Z",
                )
        finally:
            sa.event.remove(catalog.engine, "before_cursor_execute", fail_job_update)

        assert catalog.get("bucket", "object") == before
        job = catalog.get_move_job("atomic:move")
        assert job is not None
        assert job.state is MoveJobState.VERIFIED
        assert [
            transition.to_state for transition in catalog.list_move_job_transitions("atomic:move")
        ] == [
            MoveJobState.PREPARED,
            MoveJobState.TRANSFERRED,
            MoveJobState.VERIFIED,
        ]


def test_imports_normalized_sqlite_catalog_into_postgres(
    postgres_dsn: str,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "catalog.sqlite3"
    with SQLCatalog(source_path) as source:
        source.register_tier("hot", {"region": "us\0west"})
        source.register_pool(
            "pool-a", "hot", {"device": "nvme0\0serial"}, region="west", members=("nvme0",)
        )
        source.upsert(
            "bucket\0name",
            "object\0key",
            size=17,
            tier="hot",
            metadata={"source": "sqlite\0catalog", "nested\0key": ["value\0one"]},
        )
        source.claim_move_job(
            "import\0move",
            src_tier="hot",
            dst_tier="warm",
            bucket="bucket\0name",
            key="object\0key",
            expected_size=17,
            source_metadata={"generation": "source:v1", "note": "import\0metadata"},
            owner_id="worker",
            now="2000-01-01T00:00:00.000000Z",
            lease_expires_at="2000-01-01T00:01:00.000000Z",
        )

    with SQLCatalog(postgres_dsn) as destination:
        report = import_sqlite_catalog(source_path, destination, batch_size=1)
        assert (report.objects, report.placements, report.move_jobs) == (1, 1, 1)
        record = destination.get("bucket\0name", "object\0key")
        assert record is not None
        assert record == ObjectRecord(
            placement_started_at=record.placement_started_at,
            bucket="bucket\0name",
            key="object\0key",
            size=17,
            tier="hot",
            metadata={"source": "sqlite\0catalog", "nested\0key": ["value\0one"]},
        )
        imported_job = destination.get_move_job("import\0move")
        assert imported_job is not None
        assert imported_job.state is MoveJobState.PREPARED
        assert imported_job.source_metadata == {
            "generation": "source:v1",
            "note": "import\0metadata",
        }
        with destination.engine.connect() as connection:
            assert connection.execute(
                sa.select(tiers.c.metadata).where(tiers.c.name == "hot")
            ).scalar_one() == {"region": "us\0west"}
            assert connection.execute(
                sa.select(pools.c.metadata).where(pools.c.pool_id == "pool-a")
            ).scalar_one() == {"device": "nvme0\0serial"}


def test_imports_the_prototype_sqlite_catalog_into_postgres(
    postgres_dsn: str,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "prototype-catalog.sqlite3"
    create_prototype_sqlite_catalog(source_path)
    source_before_import = source_path.read_bytes()

    with SQLCatalog(postgres_dsn) as destination:
        report = import_sqlite_catalog(source_path, destination, batch_size=1)

        assert MigrationManager().is_at_head(destination.engine)
        assert (
            report.source_layout,
            report.tiers,
            report.pools,
            report.objects,
            report.placements,
            report.move_jobs,
            report.move_job_transitions,
            report.audit_events,
        ) == ("legacy", 2, 0, 1, 1, 1, 2, 2)
        imported_record = destination.get("bucket", "reports/annual.pdf")
        assert imported_record is not None and imported_record.last_tier_move_at is not None
        assert imported_record == ObjectRecord(
            last_tier_move_at=imported_record.last_tier_move_at,
            bucket="bucket",
            key="reports/annual.pdf",
            size=41,
            tier="hot",
            metadata={"labels": ["finance"], "generation": "source:v1"},
        )

        assert destination.get_move_job("move:annual-report") == MoveJob(
            idempotency_key="move:annual-report",
            src_tier="hot",
            dst_tier="warm",
            bucket="bucket",
            key="reports/annual.pdf",
            expected_size=41,
            source_metadata={"generation": "source:v1"},
            state=MoveJobState.FAILED,
            owner_id=None,
            lease_expires_at=None,
            transferred_size=41,
            source_size=41,
            source_checksum="abc123",
            destination_size=41,
            destination_checksum="abc123",
            destination_generation="destination:v1",
            verification_details=("size matched", "checksum matched"),
            terminal_reason="source cleanup was fenced",
            created_at="2026-08-01T00:00:00.000000Z",
            updated_at="2026-08-01T00:00:01.000000Z",
        )

        transitions = destination.list_move_job_transitions("move:annual-report")
        assert [
            (
                transition.sequence,
                transition.from_state,
                transition.to_state,
                transition.reason,
            )
            for transition in transitions
        ] == [
            (1, None, MoveJobState.PREPARED, "move prepared"),
            (
                2,
                MoveJobState.PREPARED,
                MoveJobState.FAILED,
                "source cleanup was fenced",
            ),
        ]

        with destination.engine.connect() as connection:
            assert connection.execute(
                sa.select(
                    object_placements.c.tier_name,
                    object_placements.c.pool_id,
                )
            ).one() == ("hot", None)
            assert connection.execute(
                sa.select(
                    object_mutation_fences.c.bucket,
                    object_mutation_fences.c.object_key,
                    object_mutation_fences.c.generation,
                )
            ).one() == ("bucket", "reports/annual.pdf", 0)
        assert "scheduled_runs" not in sa.inspect(destination.engine).get_table_names()

    assert source_path.read_bytes() == source_before_import


def test_keyed_cli_move_bootstraps_a_clean_postgres_catalog(
    postgres_dsn: str,
    tmp_path: Path,
) -> None:
    hot_path = tmp_path / "hot"
    warm_path = tmp_path / "warm"
    drivers_path = tmp_path / "drivers.yaml"
    drivers_path.write_text(
        "\n".join(
            [
                "tiers:",
                "  hot:",
                "    driver: posix",
                f"    path: {hot_path}",
                "  warm:",
                "    driver: posix",
                f"    path: {warm_path}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    PosixDriver(hot_path).put_object("bucket", "object", b"postgres bootstrap")

    assert (
        cognistore_cli.main(
            [
                "--no-config",
                "--drivers",
                str(drivers_path),
                "--catalog-url",
                postgres_dsn,
                "move",
                "hot",
                "warm",
                "bucket",
                "object",
                "--idempotency-key",
                "bootstrap:move",
                "--json",
            ]
        )
        == 0
    )

    with SQLCatalog(postgres_dsn, migrate=False) as catalog:
        job = catalog.get_move_job("bootstrap:move")
        assert job is not None
        assert job.state is MoveJobState.COMPLETED
        record = catalog.get("bucket", "object")
        assert record is not None
        assert record.tier == "warm"


def test_concurrent_postgres_placement_updates_preserve_uniqueness(
    postgres_dsn: str,
) -> None:
    update_count = 16
    barrier = threading.Barrier(update_count)
    bucket = "concurrent\0bucket"
    key = "shared\0object"

    with SQLCatalog(postgres_dsn) as catalog:

        def update(index: int) -> None:
            barrier.wait(timeout=10)
            catalog.upsert_placement(
                bucket,
                key,
                size=index,
                tier=f"tier-{index % 4}",
                checksum=f"checksum-{index}",
            )

        with ThreadPoolExecutor(max_workers=update_count) as executor:
            futures = [executor.submit(update, index) for index in range(update_count)]
            for future in futures:
                future.result(timeout=20)

        record = catalog.get(bucket, key)
        assert record is not None
        assert record.tier == f"tier-{record.size % 4}"
        assert record.metadata == {"sha256": f"checksum-{record.size}"}

        with catalog.engine.connect() as connection:
            object_ids = (
                connection.execute(
                    sa.select(objects.c.object_id).where(
                        objects.c.bucket == bucket,
                        objects.c.object_key == key,
                    )
                )
                .scalars()
                .all()
            )
            placement_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(object_placements)
                .where(object_placements.c.object_id.in_(object_ids))
            ).scalar_one()

        assert len(object_ids) == 1
        assert placement_count == 1


def test_concurrent_postgres_shared_content_create_delete_preserves_invariants(
    postgres_dsn: str,
) -> None:
    reference_count = 8
    content = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(b"abcdefgh"),
        expected_size=8,
    )
    barrier = threading.Barrier(reference_count * 2)

    with SQLCatalog(postgres_dsn) as catalog:
        for index in range(reference_count):
            key = f"old-{index}"
            fence = catalog.capture_scan_fence("bucket", key)
            assert catalog.upsert_scan_observation(
                "bucket",
                key,
                size=content.size,
                tier="hot",
                generation=f"hot:{key}",
                metadata={},
                fence=fence,
                content=content,
            )
        new_fences = {
            f"new-{index}": catalog.capture_scan_fence("bucket", f"new-{index}")
            for index in range(reference_count)
        }

        def delete_old(index: int) -> None:
            barrier.wait(timeout=10)
            catalog.delete("bucket", f"old-{index}")

        def create_new(index: int) -> None:
            key = f"new-{index}"
            barrier.wait(timeout=10)
            assert catalog.upsert_scan_observation(
                "bucket",
                key,
                size=content.size,
                tier="hot",
                generation=f"hot:{key}",
                metadata={},
                fence=new_fences[key],
                content=content,
            )

        with ThreadPoolExecutor(max_workers=reference_count * 2) as executor:
            futures = [
                executor.submit(operation, index)
                for index in range(reference_count)
                for operation in (delete_old, create_new)
            ]
            for future in futures:
                future.result(timeout=30)

        for index in range(reference_count):
            assert catalog.get("bucket", f"old-{index}") is None
            assert catalog.get_object_content("bucket", f"new-{index}") == content

        report = catalog.reconcile_content_references(
            grace_period_seconds=0,
            now="2100-01-01T00:00:00.000000Z",
        )
        with catalog.engine.connect() as connection:
            topology_counts = tuple(
                connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
                for table in (
                    content_blobs,
                    content_manifests,
                    content_manifest_chunks,
                    object_contents,
                )
            )

    unique_digests = {content.sha256, *(chunk.sha256 for chunk in content.chunks)}
    assert topology_counts == (
        len(unique_digests),
        1,
        len(content.chunks),
        reference_count,
    )
    assert report.consistent
    assert report.total_blobs == len(unique_digests)
    assert report.referenced_blobs == len(unique_digests)
    assert report.unreferenced_blobs == 0
    assert report.eligible_blobs == 0
    assert all(entry.stored_reference_count == reference_count for entry in report.entries)
    assert all(entry.expected_reference_count == reference_count for entry in report.entries)
    assert all(entry.unreferenced_at is None for entry in report.entries)
    assert all(not entry.reclamation_eligible for entry in report.entries)


def test_concurrent_postgres_distinct_manifests_sharing_a_chunk_do_not_deadlock(
    postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(b"abcd0000"),
        expected_size=8,
    )
    left = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(b"abcd1111"),
        expected_size=8,
    )
    right = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(b"abcd2222"),
        expected_size=8,
    )
    shared_digest = seed.chunks[0].sha256
    assert left.chunks[0].sha256 == shared_digest
    assert right.chunks[0].sha256 == shared_digest

    with SQLCatalog(postgres_dsn) as catalog:
        seed_fence = catalog.capture_scan_fence("bucket", "seed")
        assert catalog.upsert_scan_observation(
            "bucket",
            "seed",
            size=seed.size,
            tier="hot",
            generation="hot:seed",
            metadata={},
            fence=seed_fence,
            content=seed,
        )
        catalog.delete("bucket", "seed")

        # Both distinct manifest transactions must reach the blob-lock phase
        # after inserting their chunk foreign keys. This reproduces the lock
        # upgrade that deadlocks with FOR UPDATE but serializes safely with
        # FOR NO KEY UPDATE.
        lock_barrier = threading.Barrier(2)
        original_lock = SQLCatalog._lock_content_reference_deltas

        def synchronized_lock(connection, old_manifest_id, new_manifest_id) -> None:
            lock_barrier.wait(timeout=10)
            original_lock(connection, old_manifest_id, new_manifest_id)

        monkeypatch.setattr(
            SQLCatalog,
            "_lock_content_reference_deltas",
            staticmethod(synchronized_lock),
        )
        contents = {"left": left, "right": right}
        fences = {
            key: catalog.capture_scan_fence("bucket", key)
            for key in contents
        }

        def publish(key: str) -> None:
            content = contents[key]
            assert catalog.upsert_scan_observation(
                "bucket",
                key,
                size=content.size,
                tier="hot",
                generation=f"hot:{key}",
                metadata={},
                fence=fences[key],
                content=content,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(publish, key) for key in contents]
            for future in futures:
                future.result(timeout=30)

        report = catalog.reconcile_content_references(
            grace_period_seconds=0,
            now="2100-01-01T00:00:00.000000Z",
        )
        entries = {entry.sha256: entry for entry in report.entries}

        assert catalog.get_object_content("bucket", "left") == left
        assert catalog.get_object_content("bucket", "right") == right

    assert report.consistent
    assert entries[shared_digest].stored_reference_count == 2
    assert entries[shared_digest].expected_chunk_reference_count == 2
    active_unique_digests = {
        left.sha256,
        left.chunks[1].sha256,
        right.sha256,
        right.chunks[1].sha256,
    }
    assert all(entries[digest].stored_reference_count == 1 for digest in active_unique_digests)
    assert all(entries[digest].expected_reference_count == 1 for digest in active_unique_digests)


def test_concurrent_postgres_pool_registration_is_atomic(postgres_dsn: str) -> None:
    worker_count = 16
    barrier = threading.Barrier(worker_count)

    with SQLCatalog(postgres_dsn) as catalog:
        catalog.register_tier("tier-0")
        catalog.register_tier("tier-1")

        def register(index: int) -> None:
            barrier.wait(timeout=10)
            catalog.register_pool(
                "shared-pool",
                f"tier-{index % 2}",
                {"writer": index},
                region="west",
                members=(f"member-{index}",),
            )

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(register, index) for index in range(worker_count)]
            for future in futures:
                future.result(timeout=20)

        with catalog.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(pools.c.tier_name, pools.c.metadata).where(
                        pools.c.pool_id == "shared-pool"
                    )
                )
                .mappings()
                .one()
            )

        writer = row["metadata"]["writer"]
        assert row["tier_name"] == f"tier-{writer % 2}"


def test_postgres_migration_downgrade_and_reupgrade_preserves_catalog_data(
    postgres_dsn: str,
) -> None:
    manager = MigrationManager()
    bucket = "migration\0bucket"
    key = "legacy\0object"

    with SQLCatalog(postgres_dsn) as catalog:
        catalog.upsert(
            bucket,
            key,
            size=23,
            tier="archive\0tier",
            metadata={"source": "migration-test"},
        )
        move_snapshot = _exercise_move_catalog(catalog)
        with catalog.engine.connect() as connection:
            object_id_before_downgrade = connection.execute(
                sa.select(objects.c.object_id).where(
                    objects.c.bucket == bucket,
                    objects.c.object_key == key,
                )
            ).scalar_one()

    engine = sa.create_engine(normalize_database_url(postgres_dsn))
    try:
        manager.downgrade(engine, "0001_legacy_catalog")
        assert manager.current(engine) == "0001_legacy_catalog"
        assert {column["name"] for column in sa.inspect(engine).get_columns("objects")} == {
            "bucket",
            "key",
            "size",
            "tier",
            "metadata",
        }
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                ).scalar_one_or_none()
                is None
            )

        manager.upgrade(engine)
        assert manager.is_at_head(engine)
    finally:
        engine.dispose()

    with SQLCatalog(postgres_dsn, migrate=False) as catalog:
        record = catalog.get(bucket, key)
        assert record is not None and record.last_tier_move_at is not None
        assert record == ObjectRecord(
            last_tier_move_at=record.last_tier_move_at,
            bucket=bucket,
            key=key,
            size=23,
            tier="archive\0tier",
            metadata={"source": "migration-test"},
        )
        with catalog.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(objects.c.object_id).where(
                        objects.c.bucket == bucket,
                        objects.c.object_key == key,
                    )
                ).scalar_one()
                == object_id_before_downgrade
            )
            assert connection.execute(
                sa.text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).scalar_one()
        # The legacy schema has no placement clock. Re-upgrading represents
        # that missing history explicitly instead of inventing a new start.
        old_job, old_record, old_transitions, old_keys = move_snapshot
        assert old_record is not None
        upgraded_move_record = catalog.get("bucket\0name", "objects/item\0.bin")
        assert upgraded_move_record is not None
        assert upgraded_move_record.last_tier_move_at is not None
        expected_after_upgrade = (
            old_job, replace(old_record, placement_started_at=None,
                             last_tier_move_at=upgraded_move_record.last_tier_move_at),
            old_transitions, old_keys,
        )
        assert (
            catalog.get_move_job("move\0job"),
            catalog.get("bucket\0name", "objects/item\0.bin"),
            catalog.list_move_job_transitions("move\0job"),
            [job.idempotency_key for job in catalog.list_move_jobs(idempotency_prefix="move\0")],
        ) == expected_after_upgrade


def test_postgres_downgrade_refuses_unrepresentable_normalized_state(
    postgres_dsn: str,
) -> None:
    manager = MigrationManager()
    with SQLCatalog(postgres_dsn) as catalog:
        catalog.upsert("bucket", "object", size=1, tier="hot")
        catalog.register_tier("hot", {"region": "west"})
        catalog.register_pool(
            "pool-a", "hot", {"device": "nvme0"}, region="west", members=("nvme0",)
        )
        with catalog.engine.begin() as connection:
            connection.execute(sa.update(object_placements).values(pool_id="pool-a"))

        with pytest.raises(RuntimeError, match="cannot downgrade.*legacy schema"):
            manager.downgrade(catalog.engine, "0001_legacy_catalog")

        assert manager.is_at_head(catalog.engine)
        with catalog.engine.connect() as connection:
            assert connection.execute(sa.select(pools.c.pool_id)).scalar_one() == "pool-a"
            assert connection.execute(
                sa.select(tiers.c.metadata).where(tiers.c.name == "hot")
            ).scalar_one() == {"region": "west"}
            assert (
                connection.execute(sa.select(object_placements.c.pool_id)).scalar_one() == "pool-a"
            )
