from pathlib import Path

import pytest

from cognistore.drivers.posix_driver import PosixDriver


def test_put_get_delete_roundtrip(tmp_path: Path):
	d = PosixDriver(base_path=str(tmp_path))
	bucket = "test"
	key = "a/b/c.txt"
	data = b"hello world"

	d.put_object(bucket, key, data)
	out = d.get_object(bucket, key)
	assert out == data

	st = d.stat_object(bucket, key)
	assert st["size"] == len(data)

	d.delete_object(bucket, key)
	with pytest.raises(FileNotFoundError):
		d.get_object(bucket, key)


def test_range_reads_and_writes(tmp_path: Path):
	d = PosixDriver(base_path=str(tmp_path))
	bucket = "bk"
	key = "file.bin"
	blob = b"0123456789"  # 10 bytes
	# Write bytes 3-6 (indexes inclusive) -> "3456"
	d.put_object(bucket, key, b"3456", range="bytes=3-6")
	# Fill beginning and end
	d.put_object(bucket, key, b"012", range="bytes=0-2")
	d.put_object(bucket, key, b"789", range="bytes=7-9")

	# Full read
	full = d.get_object(bucket, key)
	assert full == blob
	# Range read
	mid = d.get_object(bucket, key, range="bytes=2-5")
	assert mid == b"2345"


def test_list_objects_with_prefix(tmp_path: Path):
	d = PosixDriver(base_path=str(tmp_path))
	bucket = "buck"
	files = [
		("aa/one.txt", b"1"),
		("aa/two.txt", b"2"),
		("bb/three.txt", b"3"),
	]
	for k, v in files:
		d.put_object(bucket, k, v)

	all_keys = sorted(list(d.list_objects(bucket)))
	assert all_keys == ["aa/one.txt", "aa/two.txt", "bb/three.txt"]

	aa_keys = sorted(list(d.list_objects(bucket, prefix="aa/")))
	assert aa_keys == ["aa/one.txt", "aa/two.txt"]


def test_driver_root_is_created_lazily(tmp_path: Path):
	root = tmp_path / "not-created-yet"
	d = PosixDriver(base_path=str(root))

	assert not root.exists()
	assert list(d.list_objects("bucket")) == []
	d.delete_object("bucket", "missing.txt")
	assert not root.exists()

	d.put_object("bucket", "created.txt", b"created")
	assert root.is_dir()


def test_put_without_overwrite_preserves_existing_object(tmp_path: Path):
	d = PosixDriver(base_path=str(tmp_path / "tier"))
	d.put_object("bucket", "key", b"original")

	with pytest.raises(FileExistsError):
		d.put_object("bucket", "key", b"replacement", overwrite=False)

	assert d.get_object("bucket", "key") == b"original"


@pytest.mark.parametrize("operation", ["put", "get", "stat", "delete"])
def test_object_operations_reject_parent_traversal_to_prefix_sibling(
	tmp_path: Path, operation: str
):
	root = tmp_path / "tier"
	sibling = tmp_path / "tier-escape"
	sibling.mkdir()
	escaped_object = sibling / "secret.txt"
	escaped_object.write_bytes(b"outside")
	d = PosixDriver(base_path=str(root))

	with pytest.raises(ValueError, match="escapes configured tier root"):
		if operation == "put":
			d.put_object("../tier-escape", "secret.txt", b"replacement")
		elif operation == "get":
			d.get_object("../tier-escape", "secret.txt")
		elif operation == "stat":
			d.stat_object("../tier-escape", "secret.txt")
		else:
			d.delete_object("../tier-escape", "secret.txt")

	assert escaped_object.read_bytes() == b"outside"


@pytest.mark.parametrize(
	("bucket", "key"),
	[
		("ABSOLUTE_BUCKET", "secret.txt"),
		("bucket", "ABSOLUTE_KEY"),
	],
)
def test_absolute_object_components_are_rejected(
	tmp_path: Path, bucket: str, key: str
):
	root = tmp_path / "tier"
	outside = tmp_path / "outside"
	d = PosixDriver(base_path=str(root))
	resolved_bucket = str(outside) if bucket == "ABSOLUTE_BUCKET" else bucket
	resolved_key = str(outside / "secret.txt") if key == "ABSOLUTE_KEY" else key

	with pytest.raises(ValueError, match="relative paths"):
		d.put_object(resolved_bucket, resolved_key, b"must not escape")

	assert not outside.exists()


def test_symlinked_bucket_cannot_escape_tier_root(tmp_path: Path):
	root = tmp_path / "tier"
	outside = tmp_path / "outside"
	root.mkdir()
	outside.mkdir()
	(outside / "secret.txt").write_bytes(b"outside")
	try:
		(root / "linked-bucket").symlink_to(outside, target_is_directory=True)
	except (NotImplementedError, OSError):
		pytest.skip("directory symlinks are unavailable on this platform")
	d = PosixDriver(base_path=str(root))

	with pytest.raises(ValueError, match="symbolic links"):
		d.get_object("linked-bucket", "secret.txt")
	with pytest.raises(ValueError, match="symbolic links"):
		list(d.list_objects("linked-bucket"))

	assert (outside / "secret.txt").read_bytes() == b"outside"


def test_list_objects_rejects_bucket_traversal(tmp_path: Path):
	root = tmp_path / "tier"
	outside = tmp_path / "outside"
	outside.mkdir()
	(outside / "secret.txt").write_bytes(b"outside")
	d = PosixDriver(base_path=str(root))

	with pytest.raises(ValueError, match="escapes configured tier root"):
		list(d.list_objects("../outside"))


@pytest.mark.parametrize(
	("bucket", "key"),
	[
		("scope/../other", "object.txt"),
		("scope", "nested/../object.txt"),
	],
)
def test_contained_parent_components_are_rejected(
	tmp_path: Path,
	bucket: str,
	key: str,
):
	root = tmp_path / "tier"
	d = PosixDriver(base_path=str(root))

	with pytest.raises(ValueError, match="escapes configured tier root"):
		d.put_object(bucket, key, b"ambiguous alias")

	assert not root.exists()


def test_in_root_symlink_key_cannot_read_or_delete_its_target(tmp_path: Path):
	root = tmp_path / "tier"
	d = PosixDriver(base_path=str(root))
	d.put_object("bucket", "real.txt", b"target data")
	alias = root / "bucket" / "alias.txt"
	try:
		alias.symlink_to("real.txt")
	except (NotImplementedError, OSError):
		pytest.skip("file symlinks are unavailable on this platform")

	with pytest.raises(ValueError, match="symbolic links"):
		d.get_object("bucket", "alias.txt")
	with pytest.raises(ValueError, match="symbolic links"):
		d.delete_object("bucket", "alias.txt")

	assert d.get_object("bucket", "real.txt") == b"target data"
	assert alias.is_symlink()
	assert list(d.list_objects("bucket")) == ["real.txt"]


def test_lazily_configured_root_cannot_be_replaced_by_symlink(tmp_path: Path):
	root = tmp_path / "tier"
	outside = tmp_path / "outside"
	d = PosixDriver(base_path=str(root))
	outside.mkdir()
	try:
		root.symlink_to(outside, target_is_directory=True)
	except (NotImplementedError, OSError):
		pytest.skip("directory symlinks are unavailable on this platform")

	with pytest.raises(ValueError, match="tier root.*symbolic links"):
		d.put_object("bucket", "object.txt", b"must not escape")

	assert list(outside.iterdir()) == []
