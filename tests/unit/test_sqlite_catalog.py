import sqlite3
from pathlib import Path

import pytest

from cognistore.core.sqlite_catalog import SQLiteCatalog


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
