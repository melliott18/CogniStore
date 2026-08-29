import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa

from cognistore.core.catalog import Catalog
from cognistore.core.move_jobs import MoveJobState
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.db.schema import object_placements


def _claim_move(catalog: SQLiteCatalog, idempotency_key: str) -> None:
    catalog.claim_move_job(
        idempotency_key,
        src_tier="hot",
        dst_tier="warm",
        bucket="bucket",
        key=f"objects/{idempotency_key}",
        expected_size=1,
        source_metadata={"generation": f"generation:{idempotency_key}"},
        owner_id="test-owner",
        now="2000-01-01T00:00:00.000000Z",
        lease_expires_at="2000-01-01T00:01:00.000000Z",
    )


def test_sqlite_catalog_crud(tmp_path: Path):
    db = tmp_path / "catalog.db"
    cat = SQLiteCatalog(db)

    bucket = "b"
    key = "k/x.txt"
    cat.upsert(bucket, key, size=11, tier="hot", metadata={"a": 1})

    rec = cat.get(bucket, key)
    assert rec is not None
    assert rec.tier == "hot"
    assert rec.size == 11
    assert rec.metadata.get("a") == 1

    # list with prefix
    lst = cat.list(bucket, prefix="k/")
    assert len(lst) == 1

    # update placement
    cat.update_placement(bucket, key, "warm")
    rec2 = cat.get(bucket, key)
    assert rec2 and rec2.tier == "warm"

    # delete
    cat.delete(bucket, key)
    assert cat.get(bucket, key) is None

    cat.close()


def test_same_tier_writes_preserve_pool_assignment(tmp_path: Path) -> None:
    with SQLiteCatalog(tmp_path / "catalog.db") as catalog:
        catalog.upsert("bucket", "object", size=1, tier="hot")
        catalog.register_pool("pool-a", "hot", {"device": "nvme0"})
        with catalog.engine.begin() as connection:
            connection.execute(sa.update(object_placements).values(pool_id="pool-a"))

        catalog.upsert("bucket", "object", size=2, tier="hot")
        catalog.update_placement("bucket", "object", "hot")
        catalog.upsert_placement("bucket", "object", size=3, tier="hot")
        fence = catalog.capture_scan_fence("bucket", "object")
        assert catalog.upsert_scan_observation(
            "bucket",
            "object",
            size=4,
            tier="hot",
            generation="hot:v2",
            metadata={},
            fence=fence,
        )
        with catalog.engine.connect() as connection:
            assert (
                connection.execute(sa.select(object_placements.c.pool_id)).scalar_one() == "pool-a"
            )

        catalog.update_placement("bucket", "object", "warm")
        with catalog.engine.connect() as connection:
            assert connection.execute(sa.select(object_placements.c.pool_id)).scalar_one() is None


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        (
            "",
            ["Foo", "a%literal", "a\\literal", "a_one", "abone", "foo", "nul\0key"],
        ),
        ("a_", ["a_one"]),
        ("a%", ["a%literal"]),
        ("a\\", ["a\\literal"]),
        ("Foo", ["Foo"]),
        ("foo", ["foo"]),
        ("nul\0", ["nul\0key"]),
    ],
)
def test_list_prefix_matches_in_memory_literal_case_sensitive_semantics(
    tmp_path: Path, prefix: str, expected: list[str]
) -> None:
    catalogs = [Catalog(), SQLiteCatalog(tmp_path / "catalog.db")]
    keys = [
        "a_one",
        "abone",
        "a%literal",
        "a\\literal",
        "Foo",
        "foo",
        "nul\0key",
    ]
    for catalog in catalogs:
        for key in keys:
            catalog.upsert("bucket", key, size=1, tier="hot")
        catalog.upsert("other", "a_one", size=1, tier="hot")

    for catalog in catalogs:
        assert sorted(record.key for record in catalog.list("bucket", prefix)) == expected

    catalogs[1].close()


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        (
            "",
            [
                "Batch_:one",
                "batch%:one",
                "batchX:one",
                "batch\\:one",
                "batch_:one",
                "nul\0job",
            ],
        ),
        ("batch_", ["batch_:one"]),
        ("batch%", ["batch%:one"]),
        ("batch\\", ["batch\\:one"]),
        ("Batch_", ["Batch_:one"]),
        ("nul\0", ["nul\0job"]),
    ],
)
def test_list_move_jobs_prefix_is_literal_and_case_sensitive(
    tmp_path: Path,
    prefix: str,
    expected: list[str],
) -> None:
    catalog = SQLiteCatalog(tmp_path / "catalog.db")
    keys = [
        "batch_:one",
        "batchX:one",
        "batch%:one",
        "batch\\:one",
        "Batch_:one",
        "nul\0job",
    ]
    for key in keys:
        _claim_move(catalog, key)

    assert [
        job.idempotency_key for job in catalog.list_move_jobs(idempotency_prefix=prefix)
    ] == expected
    catalog.close()


def test_list_move_jobs_applies_combined_filters_before_deserialization(
    tmp_path: Path,
) -> None:
    catalog = SQLiteCatalog(tmp_path / "catalog.db")
    for key in ("target:prepared", "target:failed", "unrelated:prepared"):
        _claim_move(catalog, key)
    catalog.transition_move_job(
        "target:failed",
        owner_id="test-owner",
        expected_state=MoveJobState.PREPARED,
        to_state=MoveJobState.FAILED,
        reason="expected test failure",
        now="2000-01-01T00:00:01.000000Z",
        lease_expires_at="2000-01-01T00:01:01.000000Z",
        updates={"terminal_reason": "expected test failure"},
    )

    # If either filter were still applied in Python, these excluded rows would
    # be deserialized first and their deliberately invalid JSON would fail the
    # query. SQL must discard them before MoveJob construction.
    catalog._conn.execute(
        """
        UPDATE move_jobs
        SET source_metadata='not-json'
        WHERE idempotency_key IN ('target:failed', 'unrelated:prepared')
        """
    )
    catalog._conn.commit()

    jobs = catalog.list_move_jobs(
        states={MoveJobState.PREPARED},
        idempotency_prefix="target:",
    )

    assert [job.idempotency_key for job in jobs] == ["target:prepared"]
    assert catalog.list_move_jobs(states=set()) == []
    catalog.close()


def test_upsert_placement_preserves_metadata_and_creates_missing_record(
    tmp_path: Path,
) -> None:
    cat = SQLiteCatalog(tmp_path / "catalog.db")
    cat.upsert("bucket", "existing", size=1, tier="hot", metadata={"version": 2})

    cat.upsert_placement(
        "bucket",
        "existing",
        size=42,
        tier="warm",
        checksum="verified-digest",
    )
    cat.upsert_placement("bucket", "missing", size=7, tier="cold")

    existing = cat.get("bucket", "existing")
    assert existing is not None
    assert (existing.size, existing.tier, existing.metadata) == (
        42,
        "warm",
        {"version": 2, "sha256": "verified-digest"},
    )
    missing = cat.get("bucket", "missing")
    assert missing is not None
    assert (missing.size, missing.tier, missing.metadata) == (7, "cold", {})
    cat.close()


def test_read_only_catalog_sees_committed_wal_records_and_rejects_writes(
    tmp_path: Path,
):
    db = tmp_path / "catalog.db"
    writer = SQLiteCatalog(db)
    writer._conn.execute("PRAGMA journal_mode = WAL")
    writer.upsert("bucket", "key", size=4, tier="hot")

    reader = SQLiteCatalog(db, read_only=True)
    record = reader.get("bucket", "key")

    assert record is not None
    assert record.tier == "hot"
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.update_placement("bucket", "key", "warm")
    assert writer.get("bucket", "key").tier == "hot"  # type: ignore[union-attr]

    reader.close()
    writer.close()


def test_existing_move_job_schema_is_migrated_for_destination_generations(
    tmp_path: Path,
) -> None:
    database = tmp_path / "catalog.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
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
            verification_details TEXT NOT NULL DEFAULT '[]',
            terminal_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE move_job_transitions (
            idempotency_key TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            from_state TEXT,
            to_state TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(idempotency_key, sequence),
            FOREIGN KEY(idempotency_key) REFERENCES move_jobs(idempotency_key)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO move_jobs(
            idempotency_key, src_tier, dst_tier, bucket, object_key,
            expected_size, source_metadata, state, owner_id,
            lease_expires_at, verification_details, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "legacy:move",
            "hot",
            "warm",
            "bucket",
            "object",
            1,
            "{}",
            MoveJobState.PREPARED.value,
            "worker",
            "2000-01-01T00:01:00.000000Z",
            "[]",
            "2000-01-01T00:00:00.000000Z",
            "2000-01-01T00:00:00.000000Z",
        ),
    )
    connection.execute(
        """
        INSERT INTO move_job_transitions(
            idempotency_key, sequence, from_state, to_state, reason, created_at
        ) VALUES(?,?,?,?,?,?)
        """,
        (
            "legacy:move",
            1,
            None,
            MoveJobState.PREPARED.value,
            "move prepared",
            "2000-01-01T00:00:00.000000Z",
        ),
    )
    connection.commit()
    connection.close()

    catalog = SQLiteCatalog(database)

    columns = {row[1] for row in catalog._conn.execute("PRAGMA table_info(move_jobs)")}
    assert "destination_generation" in columns
    idempotency_column = next(
        row
        for row in catalog._conn.execute("PRAGMA table_info(move_jobs)")
        if row[1] == "idempotency_key"
    )
    assert idempotency_column[3] == 1
    assert [
        transition.to_state for transition in catalog.list_move_job_transitions("legacy:move")
    ] == [MoveJobState.PREPARED]
    assert catalog._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    catalog.close()
