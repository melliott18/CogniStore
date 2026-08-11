from pathlib import Path

import pytest

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


def test_same_tier_move_is_rejected_without_deleting_source(tmp_path: Path):
	hot = PosixDriver(str(tmp_path / "hot"))
	catalog = Catalog()
	mover = Mover({"hot": hot}, catalog)
	bucket = "bk"
	key = "same-tier.txt"
	data = b"must survive"

	hot.put_object(bucket, key, data)
	catalog.upsert(bucket, key, size=len(data), tier="hot")

	with pytest.raises(ValueError, match="different"):
		mover.move("hot", "hot", bucket, key)

	assert hot.get_object(bucket, key) == data
	rec = catalog.get(bucket, key)
	assert rec is not None
	assert (rec.tier, rec.size) == ("hot", len(data))


@pytest.mark.parametrize(
	("src_tier", "dst_tier", "message"),
	[
		("missing", "warm", "Unknown source tier"),
		("hot", "missing", "Unknown destination tier"),
	],
)
def test_unknown_move_tiers_are_rejected_without_side_effects(
	tmp_path: Path, src_tier: str, dst_tier: str, message: str
):
	hot = PosixDriver(str(tmp_path / "hot"))
	warm = PosixDriver(str(tmp_path / "warm"))
	catalog = Catalog()
	mover = Mover({"hot": hot, "warm": warm}, catalog)
	bucket = "bk"
	key = "unknown-tier.txt"
	data = b"unchanged"

	hot.put_object(bucket, key, data)
	catalog.upsert(bucket, key, size=len(data), tier="hot")

	with pytest.raises(ValueError, match=message):
		mover.move(src_tier, dst_tier, bucket, key)

	assert hot.get_object(bucket, key) == data
	assert list(warm.list_objects(bucket)) == []
	rec = catalog.get(bucket, key)
	assert rec is not None
	assert (rec.tier, rec.size) == ("hot", len(data))


def test_backend_alias_move_is_rejected_without_deleting_source(tmp_path: Path):
	root = tmp_path / "shared"
	hot = PosixDriver(str(root))
	warm_alias = PosixDriver(str(root / "."))
	catalog = Catalog()
	mover = Mover({"hot": hot, "warm": warm_alias}, catalog)
	bucket = "bk"
	key = "aliased.txt"
	data = b"one physical object"

	hot.put_object(bucket, key, data)
	catalog.upsert(bucket, key, size=len(data), tier="hot")

	with pytest.raises(ValueError, match="same storage backend"):
		mover.move("hot", "warm", bucket, key)

	assert hot.get_object(bucket, key) == data
	rec = catalog.get(bucket, key)
	assert rec is not None
	assert rec.tier == "hot"


def test_destination_collision_preserves_both_objects_and_catalog(tmp_path: Path):
	hot = PosixDriver(str(tmp_path / "hot"))
	warm = PosixDriver(str(tmp_path / "warm"))
	catalog = Catalog()
	mover = Mover({"hot": hot, "warm": warm}, catalog)
	bucket = "bk"
	key = "collision.txt"
	source_data = b"source"
	destination_data = b"pre-existing destination"

	hot.put_object(bucket, key, source_data)
	warm.put_object(bucket, key, destination_data)
	catalog.upsert(bucket, key, size=len(source_data), tier="hot")

	with pytest.raises(FileExistsError, match="Destination object already exists"):
		mover.move("hot", "warm", bucket, key)

	assert hot.get_object(bucket, key) == source_data
	assert warm.get_object(bucket, key) == destination_data
	rec = catalog.get(bucket, key)
	assert rec is not None
	assert (rec.tier, rec.size) == ("hot", len(source_data))


def test_move_rejects_symlink_key_without_deleting_target(tmp_path: Path):
	hot = PosixDriver(str(tmp_path / "hot"))
	warm = PosixDriver(str(tmp_path / "warm"))
	catalog = Catalog()
	mover = Mover({"hot": hot, "warm": warm}, catalog)
	bucket = "bk"
	real_key = "real.txt"
	alias_key = "alias.txt"
	data = b"target must survive"

	hot.put_object(bucket, real_key, data)
	alias = hot.base / bucket / alias_key
	try:
		alias.symlink_to(real_key)
	except (NotImplementedError, OSError):
		pytest.skip("file symlinks are unavailable on this platform")
	catalog.upsert(bucket, alias_key, size=len(data), tier="hot")

	with pytest.raises(ValueError, match="symbolic links"):
		mover.move("hot", "warm", bucket, alias_key)

	assert hot.get_object(bucket, real_key) == data
	assert alias.is_symlink()
	assert list(warm.list_objects(bucket)) == []
	record = catalog.get(bucket, alias_key)
	assert record is not None
	assert record.tier == "hot"
