from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa

from cognistore.core.move_jobs import MoveJobState
from cognistore.db.catalog import SQLCatalog
from cognistore.db.schema import (
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
    destination = SQLCatalog(tmp_path / "destination.db")

    report = import_sqlite_catalog(source, destination, batch_size=1)

    assert report == SQLiteCatalogImportReport(
        source_layout="legacy",
        tiers=2,
        pools=0,
        objects=1,
        placements=1,
        move_jobs=1,
        move_job_transitions=2,
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

    assert "scheduled_runs" not in sa.inspect(destination.engine).get_table_names()
    assert source.read_bytes() == source_bytes
    destination.close()


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

    destination.close()
    source.close()


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
