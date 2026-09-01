from __future__ import annotations

import json
import sqlite3
from io import BytesIO
from pathlib import Path

import pytest
import sqlalchemy as sa

from cognistore.core.audit import (
    AuditContext,
    AuditEvent,
    AuditEventType,
    AuditOutcome,
    AuditQuery,
    AuditRetentionPolicy,
)
from cognistore.core.content_identity import ContentIdentityBuilder
from cognistore.core.move_jobs import MoveJobState
from cognistore.db.catalog import SQLCatalog
from cognistore.db.migrations import MigrationManager
from cognistore.db.schema import (
    audit_event_tombstones,
    audit_events,
    audit_move_heads,
    content_blobs,
    content_manifest_chunks,
    content_manifests,
    move_job_claim_fences,
    move_job_transitions,
    move_jobs,
    object_contents,
    object_mutation_fences,
    object_placements,
    objects,
    pools,
    tiers,
)
from cognistore.db.sqlite_import import (
    CatalogImportDestinationNotEmptyError,
    SQLiteCatalogImportError,
    SQLiteCatalogImportReport,
    import_sqlite_catalog,
)
from tests.catalog_fixtures import create_prototype_sqlite_catalog


def _table_count(catalog: SQLCatalog, table: sa.Table) -> int:
    with catalog.engine.connect() as connection:
        return connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()


def _create_content_identity_source(path: Path) -> None:
    payload = b"abcdefghij"
    content = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(payload),
        expected_size=len(payload),
    )
    with SQLCatalog(path) as source:
        fence = source.capture_scan_fence("bucket", "object.bin")
        assert source.upsert_scan_observation(
            "bucket",
            "object.bin",
            size=len(payload),
            tier="hot",
            generation="hot:v1",
            metadata={},
            fence=fence,
            content=content,
        )


def test_imports_legacy_objects_move_jobs_and_transition_history(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    create_prototype_sqlite_catalog(source)
    source_bytes = source.read_bytes()
    destination = SQLCatalog(
        tmp_path / "destination.db",
        audit_retention=AuditRetentionPolicy(None),
    )

    report = import_sqlite_catalog(source, destination, batch_size=1)

    assert report == SQLiteCatalogImportReport(
        source_layout="legacy",
        tiers=2,
        pools=0,
        objects=1,
        placements=1,
        move_jobs=1,
        move_job_transitions=2,
        audit_events=2,
    )
    record = destination.get("bucket", "reports/annual.pdf")
    assert record is not None
    assert (record.size, record.tier, record.metadata) == (
        41,
        "hot",
        {"labels": ["finance"], "generation": "source:v1"},
    )

    job = destination.get_move_job("move:annual-report")
    assert job is not None
    assert job.state is MoveJobState.FAILED
    assert job.source_metadata == {"generation": "source:v1"}
    assert job.destination_generation == "destination:v1"
    assert job.verification_details == ("size matched", "checksum matched")
    assert job.terminal_reason == "source cleanup was fenced"
    transitions = destination.list_move_job_transitions("move:annual-report")
    assert [transition.sequence for transition in transitions] == [1, 2]
    assert [transition.to_state for transition in transitions] == [
        MoveJobState.PREPARED,
        MoveJobState.FAILED,
    ]
    assert [transition.reason for transition in transitions] == [
        "move prepared",
        "source cleanup was fenced",
    ]
    audit_chain = destination.list_audit_events(
        AuditQuery(move_id="move:annual-report")
    )
    assert [event.event_type for event in audit_chain] == [
        AuditEventType.MOVE_PREPARED.value,
        AuditEventType.MOVE_FAILED.value,
    ]
    assert audit_chain[1].causation_id == audit_chain[0].event_id
    assert all(event.expires_at is None for event in audit_chain)

    assert "scheduled_runs" not in sa.inspect(destination.engine).get_table_names()
    assert source.read_bytes() == source_bytes
    destination.close()


def test_import_redacts_legacy_move_diagnostics_before_destination_persistence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy-secrets.db"
    create_prototype_sqlite_catalog(source)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "UPDATE move_jobs SET verification_details = ?, terminal_reason = ?",
            (
                json.dumps(
                    [
                        "token=opaque-secret",
                        "Authorization: Bearer abc.def.ghi",
                    ]
                ),
                "password=hunter2",
            ),
        )
        connection.execute(
            "UPDATE move_job_transitions SET reason = ? WHERE sequence = 2",
            ("password=transition-secret",),
        )

    with SQLCatalog(tmp_path / "redacted-destination.db") as destination:
        import_sqlite_catalog(source, destination)
        job = destination.get_move_job("move:annual-report")
        assert job is not None
        transitions = destination.list_move_job_transitions("move:annual-report")
        events = destination.list_audit_events(
            AuditQuery(move_id="move:annual-report")
        )
        persisted = repr(
            (
                job.verification_details,
                job.terminal_reason,
                [transition.reason for transition in transitions],
                [event.details for event in events],
            )
        )

    for secret in (
        "opaque-secret",
        "abc.def.ghi",
        "hunter2",
        "transition-secret",
    ):
        assert secret not in persisted


def test_imports_normalized_tiers_pools_placements_and_fences(tmp_path: Path) -> None:
    source_path = tmp_path / "normalized.db"
    source = SQLCatalog(source_path)
    source.register_tier("hot", {"region": "us\0west", "nested\0key": ["tier\0value"]})
    source.register_pool("pool-a", "hot", {"device": "nvme0\0serial"})
    source.upsert(
        "bucket",
        "object",
        size=7,
        tier="hot",
        metadata={"content-type": "text/plain\0legacy"},
    )
    with source.engine.begin() as connection:
        connection.execute(sa.update(object_placements).values(pool_id="pool-a"))
    source.claim_move_job(
        "move:normalized",
        src_tier="hot",
        dst_tier="warm",
        bucket="bucket",
        key="object",
        expected_size=7,
        source_metadata={"generation": "source:v2"},
        owner_id="worker-one",
        now="2026-08-02T00:00:00.000000Z",
        lease_expires_at="2026-08-02T00:01:00.000000Z",
    )
    source_audit_events = source.list_audit_events()

    with source.engine.connect() as connection:
        source_object = connection.execute(sa.select(objects)).mappings().one()
        source_placement = connection.execute(sa.select(object_placements)).mappings().one()

    destination = SQLCatalog(tmp_path / "destination.db")
    report = import_sqlite_catalog(source_path, destination)

    assert report == SQLiteCatalogImportReport(
        source_layout="normalized",
        tiers=1,
        pools=1,
        objects=1,
        placements=1,
        move_jobs=1,
        move_job_transitions=1,
        audit_events=1,
    )
    with destination.engine.connect() as connection:
        imported_tier = connection.execute(sa.select(tiers)).mappings().one()
        imported_pool = connection.execute(sa.select(pools)).mappings().one()
        imported_object = connection.execute(sa.select(objects)).mappings().one()
        imported_placement = connection.execute(sa.select(object_placements)).mappings().one()
    assert imported_tier["metadata"] == {
        "region": "us\0west",
        "nested\0key": ["tier\0value"],
    }
    assert imported_pool["metadata"] == {"device": "nvme0\0serial"}
    assert imported_object["metadata"] == {"content-type": "text/plain\0legacy"}
    assert imported_placement["pool_id"] == "pool-a"
    assert imported_object["object_id"] == source_object["object_id"]
    assert imported_placement["placement_id"] == source_placement["placement_id"]
    assert _table_count(destination, object_mutation_fences) == 1
    assert _table_count(destination, move_job_claim_fences) == 1
    assert _table_count(destination, audit_move_heads) == 1
    assert destination.list_audit_events() == source_audit_events

    destination.close()
    source.close()


def test_imports_normalized_content_identity_and_object_mapping(tmp_path: Path) -> None:
    source_path = tmp_path / "content-source.db"
    payload = b"abcdefghij"
    content = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(payload),
        expected_size=len(payload),
    )
    source = SQLCatalog(source_path)
    fence = source.capture_scan_fence("bucket", "object.bin")
    assert source.upsert_scan_observation(
        "bucket",
        "object.bin",
        size=len(payload),
        tier="hot",
        generation="hot:v1",
        metadata={"content_identity": content.to_metadata()},
        fence=fence,
        content=content,
    )
    identity_tables = (
        content_blobs,
        content_manifests,
        content_manifest_chunks,
        object_contents,
    )
    with source.engine.connect() as connection:
        source_identity_rows = {
            table.name: [
                dict(row)
                for row in connection.execute(sa.select(table).order_by(*table.primary_key.columns))
                .mappings()
                .all()
            ]
            for table in identity_tables
        }
    source.close()

    with SQLCatalog(tmp_path / "content-destination.db") as destination:
        report = import_sqlite_catalog(source_path, destination, batch_size=1)

        assert report == SQLiteCatalogImportReport(
            source_layout="normalized",
            tiers=1,
            pools=0,
            objects=1,
            placements=1,
            move_jobs=0,
            move_job_transitions=0,
            audit_events=0,
            content_blobs=4,
            content_manifests=1,
            content_manifest_chunks=3,
            object_contents=1,
        )
        assert destination.get_object_content("bucket", "object.bin") == content
        with destination.engine.connect() as connection:
            imported_identity_rows = {
                table.name: [
                    dict(row)
                    for row in connection.execute(
                        sa.select(table).order_by(*table.primary_key.columns)
                    )
                    .mappings()
                    .all()
                ]
                for table in identity_tables
            }
        assert imported_identity_rows == source_identity_rows


def test_import_backfills_reference_state_from_revision_0004_source(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "revision-0004-content-source.db"
    live = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(b"live-content"),
        expected_size=len(b"live-content"),
    )
    orphan = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(b"orphaned"),
        expected_size=len(b"orphaned"),
    )
    with SQLCatalog(source_path) as source:
        for key, content in (("live", live), ("orphan", orphan)):
            fence = source.capture_scan_fence("bucket", key)
            assert source.upsert_scan_observation(
                "bucket",
                key,
                size=content.size,
                tier="hot",
                generation=f"hot:{key}",
                metadata={},
                fence=fence,
                content=content,
            )
        source.delete("bucket", "orphan")
        MigrationManager().downgrade(source.engine, "0004_content_identity")

    with SQLCatalog(tmp_path / "revision-0004-content-destination.db") as destination:
        report = import_sqlite_catalog(source_path, destination, batch_size=1)
        references = destination.reconcile_content_references(
            grace_period_seconds=0,
            now="2100-01-01T00:00:00.000000Z",
        )

    assert report.content_blobs == len(
        {
            live.sha256,
            orphan.sha256,
            *(chunk.sha256 for chunk in live.chunks),
            *(chunk.sha256 for chunk in orphan.chunks),
        }
    )
    assert references.consistent
    entries = {entry.sha256: entry for entry in references.entries}
    assert entries[live.sha256].expected_object_reference_count == 1
    assert entries[live.sha256].unreferenced_at is None
    assert entries[orphan.sha256].expected_reference_count == 0
    assert entries[orphan.sha256].unreferenced_at is not None
    assert entries[orphan.sha256].reclamation_eligible


@pytest.mark.parametrize(
    ("case", "corruption", "expected_error"),
    [
        (
            "chunk-count",
            "UPDATE content_manifests SET chunk_count = 99",
            "declares 99 chunks but contains 3",
        ),
        (
            "cas-key",
            "UPDATE content_blobs SET cas_key = 'not-a-canonical-cas-key' "
            "WHERE sha256 = (SELECT content_sha256 FROM content_manifests LIMIT 1)",
            "does not use its canonical CAS key",
        ),
        (
            "chunk-offset",
            "UPDATE content_manifest_chunks SET byte_offset = 1 WHERE chunk_index = 0",
            "chunk offsets must be contiguous from zero",
        ),
        (
            "object-size",
            "UPDATE objects SET size = size + 1",
            "size does not match its content manifest",
        ),
        (
            "mapped-header",
            "UPDATE objects SET metadata = json_remove(metadata, '$.content_identity')",
            "content_identity metadata does not match its manifest",
        ),
        (
            "reference-count",
            "UPDATE content_blobs SET reference_count = reference_count + 1 "
            "WHERE sha256 = (SELECT content_sha256 FROM content_manifests LIMIT 1)",
            "does not match its",
        ),
    ],
)
def test_import_rejects_noncanonical_content_topology_and_rolls_back(
    tmp_path: Path,
    case: str,
    corruption: str,
    expected_error: str,
) -> None:
    source_path = tmp_path / f"{case}-content-source.db"
    _create_content_identity_source(source_path)
    with sqlite3.connect(source_path) as connection:
        connection.execute(corruption)

    with SQLCatalog(tmp_path / f"{case}-content-destination.db") as destination:
        with pytest.raises(SQLiteCatalogImportError, match=expected_error):
            import_sqlite_catalog(source_path, destination, batch_size=1)

        for table in (
            tiers,
            objects,
            object_placements,
            content_blobs,
            content_manifests,
            content_manifest_chunks,
            object_contents,
        ):
            assert _table_count(destination, table) == 0


@pytest.mark.parametrize(
    "content_topology",
    ("pre-0004", "current-unmapped"),
)
def test_import_strips_reserved_content_identity_from_unmapped_objects(
    tmp_path: Path,
    content_topology: str,
) -> None:
    source_path = tmp_path / f"{content_topology}-reserved-header-source.db"
    with SQLCatalog(source_path) as source:
        source.upsert(
            "bucket",
            "legacy-object",
            size=7,
            tier="hot",
            metadata={"independent": "retained"},
        )
    forged_metadata = {
        "independent": "retained",
        "content_identity": {"forged": True},
    }
    with sqlite3.connect(source_path) as connection:
        connection.execute(
            "UPDATE objects SET metadata = ?",
            (json.dumps(forged_metadata),),
        )
        if content_topology == "pre-0004":
            for table in (
                "object_contents",
                "content_manifest_chunks",
                "content_manifests",
                "content_blobs",
            ):
                connection.execute(f"DROP TABLE {table}")
            connection.execute(
                "UPDATE alembic_version SET version_num = '0003_audit_events'"
            )

    with SQLCatalog(
        tmp_path / f"{content_topology}-reserved-header-destination.db"
    ) as destination:
        report = import_sqlite_catalog(source_path, destination)
        record = destination.get("bucket", "legacy-object")

        assert report.content_blobs == 0
        assert report.content_manifests == 0
        assert report.content_manifest_chunks == 0
        assert report.object_contents == 0
        assert record is not None
        assert record.metadata == {"independent": "retained"}


def test_import_preserves_a_move_head_after_all_its_events_were_pruned(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "pruned-head-source.db"
    first = AuditEvent.create(
        AuditEventType.MANUAL_ACTION,
        AuditOutcome.REQUESTED,
        AuditContext(
            correlation_id="pruned-import-31",
            actor_type="operator",
            actor_id="test",
        ),
        event_id="00000000-0000-0000-0000-000000000101",
        occurred_at="2026-08-01T00:00:00.000000Z",
        recorded_at="2026-08-01T00:00:00.000000Z",
        retention=AuditRetentionPolicy(None),
        bucket="bucket",
        object_key="object",
        move_id="pruned-import-31",
    )
    with SQLCatalog(
        source_path,
        audit_retention=AuditRetentionPolicy(None),
    ) as source:
        stored_first = source.append_audit_event(first)
        assert source.prune_audit_events("2026-08-02T00:00:00.000000Z") == 1
        assert source.list_audit_events() == []

    with SQLCatalog(
        tmp_path / "pruned-head-destination.db",
        audit_retention=AuditRetentionPolicy(None),
    ) as destination:
        report = import_sqlite_catalog(source_path, destination)
        assert report.audit_events == 0
        second = AuditEvent.create(
            AuditEventType.MOVE_RETRY,
            AuditOutcome.RETRYING,
            AuditContext(
                correlation_id="pruned-import-31",
                actor_type="worker",
                actor_id="test",
            ),
            event_id="00000000-0000-0000-0000-000000000102",
            occurred_at="2026-08-03T00:00:00.000000Z",
            recorded_at="2026-08-03T00:00:00.000000Z",
            retention=AuditRetentionPolicy(None),
            bucket="bucket",
            object_key="object",
            move_id="pruned-import-31",
        )
        stored_second = destination.append_audit_event(second)
        assert stored_second.causation_id == stored_first.event_id
        assert destination.append_audit_event(first) == stored_first
        assert destination.list_audit_events() == [stored_second]
        with destination.engine.connect() as connection:
            head = connection.execute(sa.select(audit_move_heads)).mappings().one()
            tombstones = connection.execute(
                sa.select(sa.func.count()).select_from(audit_event_tombstones)
            ).scalar_one()
        assert head["last_sequence"] == 2
        assert tombstones == 1


def test_import_rejects_a_noncanonical_tombstone_expiry(tmp_path: Path) -> None:
    source_path = tmp_path / "invalid-tombstone-expiry-source.db"
    event = AuditEvent.create(
        AuditEventType.MANUAL_ACTION,
        AuditOutcome.REQUESTED,
        AuditContext(
            correlation_id="invalid-tombstone-expiry-31",
            actor_type="operator",
            actor_id="test",
        ),
        event_id="00000000-0000-0000-0000-000000000104",
        occurred_at="2026-08-01T00:00:00.000000Z",
        recorded_at="2026-08-01T00:00:00.000000Z",
        retention=AuditRetentionPolicy(None),
        bucket="bucket",
        object_key="object",
        move_id="invalid-tombstone-expiry-31",
    )
    with SQLCatalog(
        source_path,
        audit_retention=AuditRetentionPolicy(None),
    ) as source:
        source.append_audit_event(event)
        assert source.prune_audit_events("2026-08-02T00:00:00.000000Z") == 1
    with sqlite3.connect(source_path) as source:
        source.execute(
            "UPDATE audit_event_tombstones SET expires_at = ?",
            ("password=hunter2",),
        )

    with SQLCatalog(tmp_path / "invalid-tombstone-expiry-destination.db") as destination:
        with pytest.raises(
            SQLiteCatalogImportError,
            match="expires_at must be an ISO-8601 timestamp",
        ):
            import_sqlite_catalog(source_path, destination)
        assert _table_count(destination, audit_event_tombstones) == 0


def test_import_accepts_catalog_owned_identifier_pseudonyms(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "pseudonym-source.db"
    original = AuditEvent.create(
        AuditEventType.JOB_FAILURE,
        AuditOutcome.FAILED,
        AuditContext(
            correlation_id="password=correlation-secret",
            actor_type="worker",
            actor_id="token=actor-secret",
            job_id="secret=job-secret",
        ),
        event_id="00000000-0000-0000-0000-000000000103",
        occurred_at="2026-08-01T00:00:00.000000Z",
        recorded_at="2026-08-01T00:00:00.000000Z",
        retention=AuditRetentionPolicy(None),
    )
    with SQLCatalog(
        source_path,
        audit_retention=AuditRetentionPolicy(None),
    ) as source:
        stored_source = source.append_audit_event(original)

    with SQLCatalog(
        tmp_path / "pseudonym-destination.db",
        audit_retention=AuditRetentionPolicy(None),
    ) as destination:
        report = import_sqlite_catalog(source_path, destination)
        imported = destination.list_audit_events(
            AuditQuery(
                correlation_id=original.correlation_id,
                job_id=original.job_id,
            )
        )

    assert report.audit_events == 1
    assert imported == [stored_source]


def test_import_rejects_a_partial_current_audit_schema(tmp_path: Path) -> None:
    source_path = tmp_path / "partial-audit-source.db"
    with SQLCatalog(source_path):
        pass
    with sqlite3.connect(source_path) as source:
        source.execute("DROP TABLE audit_move_heads")

    with SQLCatalog(tmp_path / "partial-audit-destination.db") as destination:
        with pytest.raises(
            SQLiteCatalogImportError,
            match="audit_events, audit_move_heads, and audit_event_tombstones",
        ):
            import_sqlite_catalog(source_path, destination)
        assert _table_count(destination, audit_event_tombstones) == 0
        assert _table_count(destination, audit_events) == 0
        assert _table_count(destination, audit_move_heads) == 0


def test_import_rejects_partial_content_topology_and_rolls_back(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "partial-content-source.db"
    with SQLCatalog(source_path) as source:
        source.upsert("bucket", "object", size=1, tier="hot")
    with sqlite3.connect(source_path) as connection:
        connection.execute("DROP TABLE object_contents")

    with SQLCatalog(tmp_path / "partial-content-destination.db") as destination:
        with pytest.raises(
            SQLiteCatalogImportError,
            match="content_blobs, content_manifests, content_manifest_chunks, and object_contents",
        ):
            import_sqlite_catalog(source_path, destination)

        for table in (
            tiers,
            objects,
            object_placements,
            content_blobs,
            content_manifests,
            content_manifest_chunks,
            object_contents,
        ):
            assert _table_count(destination, table) == 0


def test_import_rejects_a_current_move_journal_without_its_audit_head(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "missing-current-head-source.db"
    with SQLCatalog(source_path) as source:
        source.claim_move_job(
            "missing-head-31",
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
        with source.engine.begin() as connection:
            connection.execute(sa.delete(audit_events))
            connection.execute(sa.delete(audit_move_heads))

    with SQLCatalog(tmp_path / "missing-current-head-destination.db") as destination:
        with pytest.raises(
            SQLiteCatalogImportError,
            match="missing its durable audit head",
        ):
            import_sqlite_catalog(source_path, destination)
        assert _table_count(destination, move_jobs) == 0
        assert _table_count(destination, audit_events) == 0


def test_failed_import_rolls_back_every_destination_table(tmp_path: Path) -> None:
    source = tmp_path / "invalid.db"
    create_prototype_sqlite_catalog(source)
    connection = sqlite3.connect(source)
    connection.execute("UPDATE objects SET metadata = '{invalid-json'")
    connection.commit()
    connection.close()
    destination = SQLCatalog(tmp_path / "destination.db")

    with pytest.raises(SQLiteCatalogImportError, match="contains invalid JSON"):
        import_sqlite_catalog(source, destination)

    for table in (
        tiers,
        pools,
        objects,
        object_placements,
        object_mutation_fences,
        move_job_claim_fences,
        move_jobs,
        move_job_transitions,
        audit_event_tombstones,
        audit_events,
        audit_move_heads,
    ):
        assert _table_count(destination, table) == 0
    destination.close()


def test_import_refuses_to_merge_into_a_nonempty_destination(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    create_prototype_sqlite_catalog(source)
    destination = SQLCatalog(tmp_path / "destination.db")
    destination.upsert("existing", "object", size=1, tier="hot")

    with pytest.raises(CatalogImportDestinationNotEmptyError, match="not empty"):
        import_sqlite_catalog(source, destination)

    assert destination.get("existing", "object") is not None
    assert destination.get("bucket", "reports/annual.pdf") is None
    destination.close()
