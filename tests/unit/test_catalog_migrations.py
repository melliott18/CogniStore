from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa

from cognistore.core.audit import (
    AuditEventType,
    AuditQuery,
    AuditRetentionPolicy,
)
from cognistore.core.content_identity import ContentIdentityBuilder, cas_key_for_sha256
from cognistore.core.move_jobs import MoveJobState
from cognistore.db import MigrationManager, SQLCatalog
from cognistore.db.engine import create_catalog_engine
from cognistore.db.schema import (
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
    object_contents,
    object_embedding_documents,
    object_placements,
    objects,
    pools,
    tiers,
)
from tests.catalog_fixtures import create_prototype_sqlite_catalog

_SQLiteRows = tuple[tuple[object, ...], ...]
_SQLiteTableSnapshots = tuple[tuple[str, _SQLiteRows], ...]
_SQLiteSnapshot = tuple[
    _SQLiteRows,
    _SQLiteTableSnapshots,
    _SQLiteTableSnapshots,
    _SQLiteTableSnapshots,
]


def test_concurrent_sqlite_initialization_serializes_migrations(
    tmp_path: Path,
) -> None:
    database = tmp_path / "concurrent.sqlite3"
    worker_count = 8
    barrier = threading.Barrier(worker_count)

    def initialize() -> None:
        barrier.wait(timeout=10)
        with SQLCatalog(database) as catalog:
            assert MigrationManager().is_at_head(catalog.engine)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(initialize) for _ in range(worker_count)]
        for future in futures:
            future.result(timeout=30)

    with SQLCatalog(database, migrate=False) as catalog:
        assert MigrationManager().is_at_head(catalog.engine)
        with catalog.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_failed_sqlite_migration_restores_prototype_schema_and_data(
    tmp_path: Path,
) -> None:
    database = tmp_path / "prototype.sqlite3"
    create_prototype_sqlite_catalog(database)
    source_tables = (
        "objects",
        "move_jobs",
        "move_job_transitions",
        "scheduled_runs",
    )

    def snapshot() -> _SQLiteSnapshot:
        connection = sqlite3.connect(database)
        try:
            schema = tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                ).fetchall()
            )
            columns = tuple(
                (
                    table,
                    tuple(
                        tuple(row)
                        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                    ),
                )
                for table in source_tables
            )
            rows = tuple(
                (
                    table,
                    tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}")),
                )
                for table in source_tables
            )
            source_state = tuple(
                (
                    pragma,
                    tuple(tuple(row) for row in connection.execute(f"PRAGMA {pragma}")),
                )
                for pragma in (
                    "application_id",
                    "user_version",
                    "integrity_check",
                    "foreign_key_check",
                )
            )
            return schema, columns, rows, source_state
        finally:
            connection.close()

    before_snapshot = snapshot()
    before_bytes = database.read_bytes()
    before_schema, before_columns, _before_rows, _before_source_state = before_snapshot
    schema_names = {row[1] for row in before_schema}
    assert {"move_jobs_state_idx", "move_jobs_object_idx"} <= schema_names
    assert "alembic_version" not in schema_names
    move_job_columns = dict(before_columns)["move_jobs"]
    idempotency_column = next(row for row in move_job_columns if row[1] == "idempotency_key")
    assert (idempotency_column[3], idempotency_column[5]) == (0, 1)

    manager = MigrationManager()
    engine, compatibility_connection = create_catalog_engine(database)
    injected = False
    statements: list[str] = []

    def fail_after_earlier_ddl(
        _connection: sa.Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal injected
        statements.append(statement)
        normalized_statement = " ".join(statement.split())
        if not injected and "CREATE TABLE object_placements" in normalized_statement:
            injected = True
            raise RuntimeError("injected SQLite migration failure")

    try:
        sa.event.listen(engine, "before_cursor_execute", fail_after_earlier_ddl)
        try:
            with pytest.raises(RuntimeError, match="injected SQLite migration failure"):
                manager.upgrade(engine)

            assert injected
            assert any("move_job_transitions_0002_backup" in sql for sql in statements)
            assert any("objects_legacy" in sql for sql in statements)
            assert manager.current(engine) is None
        finally:
            sa.event.remove(engine, "before_cursor_execute", fail_after_earlier_ddl)
    finally:
        engine.dispose()
        if compatibility_connection is not None:
            compatibility_connection.close()

    assert snapshot() == before_snapshot
    assert database.read_bytes() == before_bytes


def test_legacy_sqlite_upgrade_preserves_move_transition_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.sqlite3"
    manager = MigrationManager()
    catalog = SQLCatalog(database)
    manager.downgrade(catalog.engine, "0001_legacy_catalog")
    with catalog.engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO move_jobs(
                    idempotency_key, src_tier, dst_tier, bucket, object_key,
                    expected_size, source_metadata, state, owner_id,
                    lease_expires_at, verification_details, terminal_reason,
                    created_at, updated_at
                ) VALUES(
                    :idempotency_key, :src_tier, :dst_tier, :bucket, :object_key,
                    :expected_size, :source_metadata, :state, :owner_id,
                    :lease_expires_at, :verification_details, :terminal_reason,
                    :created_at, :updated_at
                )
                """
            ),
            {
                "idempotency_key": "legacy:move",
                "src_tier": "hot",
                "dst_tier": "warm",
                "bucket": "bucket",
                "object_key": "object",
                "expected_size": 1,
                "source_metadata": '{"generation":"source:v1"}',
                "state": MoveJobState.PREPARED.value,
                "owner_id": "worker",
                "lease_expires_at": "2000-01-01T00:01:00.000000Z",
                "verification_details": '["token=opaque-secret"]',
                "terminal_reason": "password=hunter2",
                "created_at": "2000-01-01T00:00:00.000000Z",
                "updated_at": "2000-01-01T00:00:00.000000Z",
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO move_job_transitions(
                    idempotency_key, sequence, from_state, to_state, reason,
                    created_at
                ) VALUES(
                    :idempotency_key, 1, NULL, :to_state, :reason, :created_at
                )
                """
            ),
            {
                "idempotency_key": "legacy:move",
                "to_state": MoveJobState.PREPARED.value,
                "reason": "Authorization: Bearer abc.def.ghi",
                "created_at": "2000-01-01T00:00:00.000000Z",
            },
        )
    catalog.close()

    with SQLCatalog(database) as upgraded:
        assert manager.is_at_head(upgraded.engine)
        assert [
            transition.to_state for transition in upgraded.list_move_job_transitions("legacy:move")
        ] == [MoveJobState.PREPARED]
        upgraded_job = upgraded.get_move_job("legacy:move")
        assert upgraded_job is not None
        persisted_diagnostics = repr(
            (
                upgraded_job.verification_details,
                upgraded_job.terminal_reason,
                upgraded.list_move_job_transitions("legacy:move")[0].reason,
            )
        )
        assert "opaque-secret" not in persisted_diagnostics
        assert "hunter2" not in persisted_diagnostics
        assert "abc.def.ghi" not in persisted_diagnostics
        audit_chain = upgraded.list_audit_events(AuditQuery(move_id="legacy:move"))
        assert [event.event_type for event in audit_chain] == [AuditEventType.MOVE_PREPARED.value]
        assert audit_chain[0].details["backfilled"] is True
        with upgraded.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

        claimed = upgraded.claim_move_job(
            "legacy:move",
            src_tier="hot",
            dst_tier="warm",
            bucket="bucket",
            key="object",
            expected_size=1,
            source_metadata={"generation": "source:v1"},
            owner_id="recovery-worker",
            now="2001-01-01T00:00:00.000000Z",
            lease_expires_at="2001-01-01T00:01:00.000000Z",
        )
        upgraded.transition_move_job(
            claimed.idempotency_key,
            owner_id="recovery-worker",
            expected_state=MoveJobState.PREPARED,
            to_state=MoveJobState.FAILED,
            reason="recovery failed",
            now="2001-01-01T00:00:01.000000Z",
            lease_expires_at="2001-01-01T00:01:01.000000Z",
            updates={"terminal_reason": "recovery failed"},
        )
        completed_chain = upgraded.list_audit_events(AuditQuery(move_id="legacy:move"))
        assert [event.event_type for event in completed_chain] == [
            AuditEventType.MOVE_PREPARED.value,
            AuditEventType.MOVE_RETRY.value,
            AuditEventType.MOVE_FAILED.value,
        ]
        assert [event.causation_id for event in completed_chain[1:]] == [
            event.event_id for event in completed_chain[:-1]
        ]


def test_audit_event_migration_is_reversible_without_changing_catalog_data(
    tmp_path: Path,
) -> None:
    manager = MigrationManager()
    database = tmp_path / "audit-migration.sqlite3"
    with SQLCatalog(database) as catalog:
        catalog.upsert("bucket", "object", size=7, tier="hot")
        assert manager.current(catalog.engine) == "0012_tenant_ownership"
        assert sa.inspect(catalog.engine).has_table(audit_events.name)
        assert sa.inspect(catalog.engine).has_table(audit_move_heads.name)
        assert sa.inspect(catalog.engine).has_table(audit_event_tombstones.name)

        manager.downgrade(catalog.engine, "0002_normalized_catalog")
        assert manager.current(catalog.engine) == "0002_normalized_catalog"
        assert not sa.inspect(catalog.engine).has_table(audit_events.name)
        assert not sa.inspect(catalog.engine).has_table(audit_move_heads.name)
        assert not sa.inspect(catalog.engine).has_table(audit_event_tombstones.name)
        with catalog.engine.connect() as connection:
            assert connection.execute(sa.select(objects.c.object_id)).first() is not None

        manager.upgrade(catalog.engine)
        assert manager.is_at_head(catalog.engine)
        assert sa.inspect(catalog.engine).has_table(audit_events.name)
        assert sa.inspect(catalog.engine).has_table(audit_move_heads.name)
        assert sa.inspect(catalog.engine).has_table(audit_event_tombstones.name)
        assert catalog.get("bucket", "object") is not None


def test_content_identity_migration_is_reversible_without_unsafe_backfill(
    tmp_path: Path,
) -> None:
    manager = MigrationManager()
    database = tmp_path / "content-identity-migration.sqlite3"
    content_tables = (
        content_blobs,
        content_manifests,
        content_manifest_chunks,
        object_contents,
    )

    with SQLCatalog(database) as catalog:
        catalog.upsert(
            "bucket",
            "legacy-scan",
            size=2 * 1024 * 1024,
            tier="hot",
            metadata={"sha256": "untrusted-first-mebibyte-sample"},
        )
        assert manager.current(catalog.engine) == "0012_tenant_ownership"
        assert all(
            sa.inspect(catalog.engine).has_table(table.name)
            for table in content_tables
        )
        assert catalog.get_object_content("bucket", "legacy-scan") is None

        manager.downgrade(catalog.engine, "0003_audit_events")
        assert manager.current(catalog.engine) == "0003_audit_events"
        assert all(not sa.inspect(catalog.engine).has_table(table.name) for table in content_tables)
        with catalog.engine.connect() as connection:
            legacy_metadata = connection.execute(sa.select(objects.c.metadata)).scalar_one()
        assert legacy_metadata == {"sha256": "untrusted-first-mebibyte-sample"}

        manager.upgrade(catalog.engine)
        assert manager.current(catalog.engine) == "0012_tenant_ownership"
        assert all(sa.inspect(catalog.engine).has_table(table.name) for table in content_tables)
        assert catalog.get_object_content("bucket", "legacy-scan") is None


def test_embedding_migration_is_reversible_and_uses_portable_vector_storage(
    tmp_path: Path,
) -> None:
    manager = MigrationManager()
    embedding_tables = (
        embedding_spaces,
        embedding_documents,
        object_embedding_documents,
        embedding_passages,
        embedding_vectors,
        embedding_document_spaces,
    )

    with SQLCatalog(tmp_path / "embedding-migration.sqlite3") as catalog:
        inspector = sa.inspect(catalog.engine)
        assert manager.current(catalog.engine) == "0012_tenant_ownership"
        assert all(inspector.has_table(table.name) for table in embedding_tables)
        mapping_primary_key = inspector.get_pk_constraint(object_embedding_documents.name)[
            "constrained_columns"
        ]
        assert mapping_primary_key == ["object_id", "space_id"]
        vector_column = next(
            column
            for column in inspector.get_columns(embedding_vectors.name)
            if column["name"] == "embedding"
        )
        assert isinstance(vector_column["type"], sa.Text)

        manager.downgrade(catalog.engine, "0005_content_references")
        assert manager.current(catalog.engine) == "0005_content_references"
        assert all(
            not sa.inspect(catalog.engine).has_table(table.name) for table in embedding_tables
        )

        manager.upgrade(catalog.engine)
        inspector = sa.inspect(catalog.engine)
        assert manager.current(catalog.engine) == "0012_tenant_ownership"
        assert all(inspector.has_table(table.name) for table in embedding_tables)
        vector_column = next(
            column
            for column in inspector.get_columns(embedding_vectors.name)
            if column["name"] == "embedding"
        )
        assert isinstance(vector_column["type"], sa.Text)


def test_content_identity_migration_strips_unbacked_reserved_metadata(
    tmp_path: Path,
) -> None:
    manager = MigrationManager()
    with SQLCatalog(tmp_path / "unbacked-content-identity.sqlite3") as catalog:
        catalog.upsert(
            "bucket",
            "legacy-object",
            size=7,
            tier="hot",
            metadata={"classification": "retain", "sha256": "legacy-sample"},
        )
        manager.downgrade(catalog.engine, "0003_audit_events")
        with catalog.engine.begin() as connection:
            connection.execute(
                sa.update(objects)
                .where(
                    objects.c.bucket == "bucket",
                    objects.c.object_key == "legacy-object",
                )
                .values(
                    metadata={
                        "classification": "retain",
                        "sha256": "legacy-sample",
                        "content_identity": {"forged": True},
                    }
                )
            )

        manager.upgrade(catalog.engine)

        record = catalog.get("bucket", "legacy-object")
        assert record is not None
        assert record.metadata == {
            "classification": "retain",
            "sha256": "legacy-sample",
        }
        assert catalog.get_object_content("bucket", "legacy-object") is None


def test_content_identity_downgrade_removes_the_mapping_projection(
    tmp_path: Path,
) -> None:
    manager = MigrationManager()
    payload = b"abcdefghij"
    content = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(payload),
        expected_size=len(payload),
    )
    with SQLCatalog(tmp_path / "content-identity-downgrade.sqlite3") as catalog:
        fence = catalog.capture_scan_fence("bucket", "object")
        assert catalog.upsert_scan_observation(
            "bucket",
            "object",
            size=len(payload),
            tier="hot",
            generation="hot:v1",
            metadata={"classification": "retain"},
            fence=fence,
            content=content,
        )
        before = catalog.get("bucket", "object")
        assert before is not None
        assert before.metadata["content_identity"] == content.to_metadata()

        manager.downgrade(catalog.engine, "0003_audit_events")
        with catalog.engine.connect() as connection:
            downgraded_metadata = connection.execute(sa.select(objects.c.metadata)).scalar_one()
        assert downgraded_metadata == {
            "classification": "retain",
            "sha256": content.sha256,
        }

        manager.upgrade(catalog.engine)
        reupgraded = catalog.get("bucket", "object")
        assert reupgraded is not None
        assert reupgraded.metadata == downgraded_metadata
        assert catalog.get_object_content("bucket", "object") is None


def test_content_identity_migration_uses_declared_check_constraint_names(
    tmp_path: Path,
) -> None:
    with SQLCatalog(tmp_path / "content-constraint-names.sqlite3") as catalog:
        inspector = sa.inspect(catalog.engine)
        for table in (
            content_blobs,
            content_manifests,
            content_manifest_chunks,
        ):
            expected = {
                constraint.name
                for constraint in table.constraints
                if isinstance(constraint, sa.CheckConstraint)
            }
            observed = {
                constraint["name"] for constraint in inspector.get_check_constraints(table.name)
            }
            assert observed == expected


def test_content_reference_migration_backfills_active_edges_and_fresh_orphans(
    tmp_path: Path,
) -> None:
    manager = MigrationManager()
    payload = b"abcdabcd"
    content = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(payload),
        expected_size=len(payload),
    )
    orphan_digest = "f" * 64

    with SQLCatalog(tmp_path / "content-reference-backfill.sqlite3") as catalog:
        for key in ("first", "second"):
            fence = catalog.capture_scan_fence("bucket", key)
            assert catalog.upsert_scan_observation(
                "bucket",
                key,
                size=len(payload),
                tier="hot",
                generation=f"hot:{key}",
                metadata={},
                fence=fence,
                content=content,
            )

        manager.downgrade(catalog.engine, "0004_content_identity")
        assert {
            column["name"] for column in sa.inspect(catalog.engine).get_columns("content_blobs")
        }.isdisjoint({"reference_count", "unreferenced_at"})
        with catalog.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO content_blobs "
                    "(sha256, digest_algorithm, size, cas_key, created_at) "
                    "VALUES (:sha256, 'sha256', 7, :cas_key, :created_at)"
                ),
                {
                    "sha256": orphan_digest,
                    "cas_key": cas_key_for_sha256(orphan_digest),
                    "created_at": "2000-01-01T00:00:00.000000Z",
                },
            )

        manager.upgrade(catalog.engine)

        with catalog.engine.connect() as connection:
            rows = {
                row["sha256"]: row
                for row in connection.execute(
                    sa.select(
                        content_blobs.c.sha256,
                        content_blobs.c.reference_count,
                        content_blobs.c.unreferenced_at,
                    )
                ).mappings()
            }
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

        repeated_chunk_digest = content.chunks[0].sha256
        assert rows[content.sha256]["reference_count"] == 2
        assert rows[content.sha256]["unreferenced_at"] is None
        assert rows[repeated_chunk_digest]["reference_count"] == 4
        assert rows[repeated_chunk_digest]["unreferenced_at"] is None
        assert rows[orphan_digest]["reference_count"] == 0
        assert rows[orphan_digest]["unreferenced_at"] > "2000-01-01T00:00:00.000000Z"

        inspector = sa.inspect(catalog.engine)
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints(content_blobs.name)
        } >= {
            "ck_content_blobs_content_blob_reference_count_nonnegative",
        }
        assert any(
            index["name"] == "content_blobs_reclamation_idx"
            and index["column_names"] == ["reference_count", "unreferenced_at"]
            for index in inspector.get_indexes(content_blobs.name)
        )

        manager.downgrade(catalog.engine, "0004_content_identity")
        assert catalog.get_object_content("bucket", "first") == content
        with catalog.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_audit_migration_backfill_uses_configured_retention(tmp_path: Path) -> None:
    manager = MigrationManager()
    database = tmp_path / "audit-backfill-retention.sqlite3"
    with SQLCatalog(database) as catalog:
        catalog.claim_move_job(
            "retention:move",
            src_tier="hot",
            dst_tier="warm",
            bucket="bucket",
            key="object",
            expected_size=1,
            source_metadata={"generation": "source:v1"},
            owner_id="worker",
            now="2026-08-01T00:00:00.000000Z",
            lease_expires_at="2026-08-01T00:01:00.000000Z",
        )
        manager.downgrade(catalog.engine, "0002_normalized_catalog")

    with SQLCatalog(
        database,
        audit_retention=AuditRetentionPolicy(None),
    ) as upgraded:
        events = upgraded.list_audit_events(AuditQuery(move_id="retention:move"))
        assert len(events) == 1
        assert events[0].expires_at is None


def test_downgrade_refuses_to_drop_an_object_without_a_placement(
    tmp_path: Path,
) -> None:
    manager = MigrationManager()
    with SQLCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.upsert("bucket", "object", size=1, tier="hot")
        with catalog.engine.begin() as connection:
            object_id = connection.execute(sa.select(objects.c.object_id)).scalar_one()
            connection.execute(
                sa.delete(object_placements).where(object_placements.c.object_id == object_id)
            )

        with pytest.raises(RuntimeError, match="objects without placements"):
            manager.downgrade(catalog.engine, "0001_legacy_catalog")

        assert manager.is_at_head(catalog.engine)
        assert "objects_legacy" not in sa.inspect(catalog.engine).get_table_names()
        with catalog.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(objects.c.object_id).where(objects.c.object_id == object_id)
                ).scalar_one()
                == object_id
            )


@pytest.mark.parametrize(
    "incompatible_state",
    ["pool", "tier-metadata", "unused-tier", "noncanonical-id"],
)
def test_downgrade_refuses_normalized_state_the_legacy_schema_cannot_represent(
    tmp_path: Path,
    incompatible_state: str,
) -> None:
    manager = MigrationManager()
    with SQLCatalog(tmp_path / f"{incompatible_state}.sqlite3") as catalog:
        catalog.upsert("bucket", "object", size=1, tier="hot")
        if incompatible_state == "pool":
            catalog.register_pool(
                "pool-a", "hot", {"device": "nvme0"}, region="west", members=("nvme0",)
            )
            with catalog.engine.begin() as connection:
                connection.execute(sa.update(object_placements).values(pool_id="pool-a"))
        elif incompatible_state == "tier-metadata":
            catalog.register_tier("hot", {"region": "west"})
        elif incompatible_state == "unused-tier":
            catalog.register_tier("archive")
        else:
            noncanonical_id = uuid4()
            with catalog.engine.begin() as connection:
                connection.execute(
                    sa.update(object_placements).values(placement_id=noncanonical_id)
                )

        with pytest.raises(RuntimeError, match="cannot downgrade.*legacy schema"):
            manager.downgrade(catalog.engine, "0001_legacy_catalog")

        assert manager.is_at_head(catalog.engine)
        assert catalog.get("bucket", "object") is not None
        with catalog.engine.connect() as connection:
            if incompatible_state == "pool":
                assert connection.execute(sa.select(pools.c.pool_id)).scalar_one() == "pool-a"
            elif incompatible_state == "tier-metadata":
                assert connection.execute(
                    sa.select(tiers.c.metadata).where(tiers.c.name == "hot")
                ).scalar_one() == {"region": "west"}
            elif incompatible_state == "unused-tier":
                assert (
                    connection.execute(
                        sa.select(tiers.c.name).where(tiers.c.name == "archive")
                    ).scalar_one()
                    == "archive"
                )
            else:
                assert (
                    connection.execute(sa.select(object_placements.c.placement_id)).scalar_one()
                    == noncanonical_id
                )
