from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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
from cognistore.core.move_jobs import (
    MoveJob,
    MoveJobConflictError,
    MoveJobLeaseError,
    MoveJobState,
    MoveJobTransition,
)
from cognistore.db import MigrationManager, SQLCatalog
from cognistore.db.engine import normalize_database_url
from cognistore.db.schema import object_placements, objects, pools, tiers
from cognistore.db.sqlite_import import import_sqlite_catalog
from cognistore.drivers.posix_driver import PosixDriver
from tests.conformance.catalog_store import CatalogStoreConformance

pytestmark = pytest.mark.integration


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
        assert {
            "alembic_version",
            "audit_events",
            "audit_move_heads",
            "audit_event_tombstones",
            "catalog_schema_features",
            "move_job_claim_fences",
            "move_job_transitions",
            "move_jobs",
            "object_mutation_fences",
            "object_placements",
            "objects",
            "pools",
            "tiers",
        } <= set(inspector.get_table_names())
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
            feature = connection.execute(
                sa.text(
                    "SELECT available, owned FROM catalog_schema_features "
                    "WHERE feature = 'pgvector'"
                )
            ).one()

        assert vector_version
        assert tuple(feature) == (True, True)


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
            assert catalog.get("bucket\0name", "nul\0key") == memory.get("bucket\0name", "nul\0key")
    finally:
        sqlite.close()
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
            assert catalog.get("bucket", "object") == ObjectRecord(
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
            if statement.startswith("UPDATE move_jobs SET state"):
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
        source.register_pool("pool-a", "hot", {"device": "nvme0\0serial"})
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
        assert destination.get("bucket\0name", "object\0key") == ObjectRecord(
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


def test_concurrent_postgres_pool_registration_is_atomic(postgres_dsn: str) -> None:
    worker_count = 16
    barrier = threading.Barrier(worker_count)

    with SQLCatalog(postgres_dsn) as catalog:

        def register(index: int) -> None:
            barrier.wait(timeout=10)
            catalog.register_pool(
                "shared-pool",
                f"tier-{index % 2}",
                {"writer": index},
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
        assert record == ObjectRecord(
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
        assert (
            catalog.get_move_job("move\0job"),
            catalog.get("bucket\0name", "objects/item\0.bin"),
            catalog.list_move_job_transitions("move\0job"),
            [job.idempotency_key for job in catalog.list_move_jobs(idempotency_prefix="move\0")],
        ) == move_snapshot


def test_postgres_downgrade_refuses_unrepresentable_normalized_state(
    postgres_dsn: str,
) -> None:
    manager = MigrationManager()
    with SQLCatalog(postgres_dsn) as catalog:
        catalog.upsert("bucket", "object", size=1, tier="hot")
        catalog.register_tier("hot", {"region": "west"})
        catalog.register_pool("pool-a", "hot", {"device": "nvme0"})
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
