from pathlib import Path

from cognistore.core.catalog import Catalog
from cognistore.core.mover import Mover
from cognistore.drivers.posix_driver import PosixDriver


def test_mover_updates_catalog_and_storage(tmp_path: Path):
	hot = PosixDriver(str(tmp_path / "hot"))
	warm = PosixDriver(str(tmp_path / "warm"))
	drivers = {"hot": hot, "warm": warm}

	catalog = Catalog()
	mover = Mover(drivers, catalog)

	bucket = "bk"
	key = "dir/file.txt"
	data = b"hello mover"

	# Put in hot tier and register in catalog
	hot.put_object(bucket, key, data)
	catalog.upsert(bucket=bucket, key=key, size=len(data), tier="hot")

	# Move to warm
	mover.move("hot", "warm", bucket, key)

	# Verify storage: not in hot, present in warm
	assert list(hot.list_objects(bucket)) == []
	assert key in list(warm.list_objects(bucket))

	# Verify catalog updated
	rec = catalog.get(bucket, key)
	assert rec is not None
	assert rec.tier == "warm"
	assert rec.size == len(data)

