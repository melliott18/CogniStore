from __future__ import annotations

import json
import sqlite3
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
from cognistore.core.move_jobs import MoveJobState
from cognistore.db.catalog import SQLCatalog
from cognistore.db.schema import (
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
from cognistore.db.sqlite_import import (
    CatalogImportDestinationNotEmptyError,
    SQLiteCatalogImportError,
    SQLiteCatalogImportReport,
    import_sqlite_catalog,
)


def _create_legacy_catalog(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE objects (
            bucket TEXT NOT NULL,
            key TEXT NOT NULL,
            size INTEGER NOT NULL,
            tier TEXT NOT NULL,
            metadata TEXT,
            PRIMARY KEY(bucket, key)
        );
        CREATE TABLE move_jobs (
            idempotency_key TEXT PRIMARY KEY,
            src_tier TEXT NOT NULL,
            dst_tier TEXT NOT NULL,
            bucket TEXT NOT NULL,
            object_key TEXT NOT NULL,
            expected_size INTEGER NOT NULL,
            source_metadata TEXT NOT NULL,
            state TEXT NOT NULL,
            owner_id TEXT,
            lease_expires_at TEXT,
            transferred_size INTEGER,
            source_size INTEGER,
            source_checksum TEXT,
            destination_size INTEGER,
            destination_checksum TEXT,
            destination_generation TEXT,
            verification_details TEXT NOT NULL DEFAULT '[]',
            terminal_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE move_job_transitions (
            idempotency_key TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            from_state TEXT,
            to_state TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(idempotency_key, sequence),
            FOREIGN KEY(idempotency_key) REFERENCES move_jobs(idempotency_key)
        );
        CREATE TABLE scheduled_runs (
            job_id TEXT PRIMARY KEY,
            state TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO objects(bucket, key, size, tier, metadata) VALUES(?,?,?,?,?)",
        (
            "bucket",
            "reports/annual.pdf",
            41,
            "hot",
            json.dumps({"labels": ["finance"], "generation": "source:v1"}),
        ),
    )
    connection.execute(
        """
        INSERT INTO move_jobs(
            idempotency_key, src_tier, dst_tier, bucket, object_key,
            expected_size, source_metadata, state, owner_id,
            lease_expires_at, transferred_size, source_size,
            source_checksum, destination_size, destination_checksum,
            destination_generation, verification_details, terminal_reason,
            created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "move:annual-report",
            "hot",
            "warm",
            "bucket",
            "reports/annual.pdf",
            41,
            json.dumps({"generation": "source:v1"}),
            "failed",
            None,
            None,
            41,
            41,
            "abc123",
            41,
            "abc123",
            "destination:v1",
            json.dumps(["size matched", "checksum matched"]),
            "source cleanup was fenced",
            "2026-08-01T00:00:00.000000Z",
            "2026-08-01T00:00:01.000000Z",
        ),
    )
    connection.executemany(
        """
        INSERT INTO move_job_transitions(
            idempotency_key, sequence, from_state, to_state, reason, created_at
        ) VALUES(?,?,?,?,?,?)
        """,
        [
            (
                "move:annual-report",
                1,
                None,
                "prepared",
                "move prepared",
                "2026-08-01T00:00:00.000000Z",
            ),
            (
                "move:annual-report",
                2,
                "prepared",
                "failed",
                "source cleanup was fenced",
                "2026-08-01T00:00:01.000000Z",
            ),
        ],
    )
    connection.execute(
        "INSERT INTO scheduled_runs(job_id, state) VALUES(?, ?)",
        ("scheduled:one", "reserved"),
    )
    connection.commit()
    connection.close()


def _table_count(catalog: SQLCatalog, table: sa.Table) -> int:
    with catalog.engine.connect() as connection:
        return connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()


def test_imports_legacy_objects_move_jobs_and_transition_history(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    _create_legacy_catalog(source)
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
    _create_legacy_catalog(source)
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
    _create_legacy_catalog(source)
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
    _create_legacy_catalog(source)
    destination = SQLCatalog(tmp_path / "destination.db")
    destination.upsert("existing", "object", size=1, tier="hot")

    with pytest.raises(CatalogImportDestinationNotEmptyError, match="not empty"):
        import_sqlite_catalog(source, destination)

    assert destination.get("existing", "object") is not None
    assert destination.get("bucket", "reports/annual.pdf") is None
    destination.close()
