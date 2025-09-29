from pathlib import Path

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
