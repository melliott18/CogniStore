import os
import stat
import threading
from io import BytesIO
from pathlib import Path

import pytest

from cognistore.drivers.posix_driver import PosixDriver


class _TrackingStream(BytesIO):
	def __init__(self, payload: bytes) -> None:
		super().__init__(payload)
		self.request_sizes: list[int] = []

	def read(self, size: int = -1) -> bytes:
		self.request_sizes.append(size)
		return super().read(size)


class _InterruptedStream(BytesIO):
	def __init__(self, payload: bytes) -> None:
		super().__init__(payload)
		self.reads = 0

	def read(self, size: int = -1) -> bytes:
		self.reads += 1
		if self.reads > 1:
			raise KeyboardInterrupt("injected interruption")
		return super().read(size)


class _BlockingAfterPayloadStream:
	def __init__(self, payload: bytes) -> None:
		self.payload = payload
		self.reads = 0
		self.waiting = threading.Event()
		self.release = threading.Event()

	def read(self, size: int = -1) -> bytes:
		self.reads += 1
		if self.reads == 1:
			return self.payload[:size]
		self.waiting.set()
		if not self.release.wait(timeout=5):
			raise TimeoutError("test did not release blocked stream")
		return b""


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


@pytest.mark.parametrize("chunk_size", [0, -1, True, 1.5, "8", None])
def test_chunk_size_must_be_a_positive_integer(
	tmp_path: Path, chunk_size: object
) -> None:
	with pytest.raises(ValueError, match="chunk_size.*positive integer"):
		PosixDriver(str(tmp_path / "tier"), chunk_size=chunk_size)  # type: ignore[arg-type]


def test_streaming_put_never_requests_more_than_configured_chunk_size(
	tmp_path: Path,
) -> None:
	d = PosixDriver(str(tmp_path / "tier"), chunk_size=3)
	payload = b"0123456789"
	source = _TrackingStream(payload)

	written = d.put_object_stream("bucket", "key", source, size=len(payload))

	assert written == len(payload)
	assert d.get_object("bucket", "key") == payload
	assert source.request_sizes == [3, 3, 3, 1, 1]


@pytest.mark.parametrize(
	("payload", "declared_size"),
	[(b"short", 6), (b"too-long", 7)],
)
def test_stream_size_mismatch_cleans_temporary_file_and_preserves_existing_object(
	tmp_path: Path,
	payload: bytes,
	declared_size: int,
) -> None:
	tier = tmp_path / "tier"
	d = PosixDriver(str(tier), chunk_size=2)
	d.put_object("bucket", "key", b"original")

	with pytest.raises(ValueError):
		d.put_object_stream(
			"bucket",
			"key",
			BytesIO(payload),
			size=declared_size,
			overwrite=True,
		)

	assert d.get_object("bucket", "key") == b"original"
	assert sorted(path.name for path in (tier / "bucket").iterdir()) == ["key"]


def test_stream_interruption_cleans_temporary_file_without_publishing(
	tmp_path: Path,
) -> None:
	tier = tmp_path / "tier"
	d = PosixDriver(str(tier), chunk_size=3)

	with pytest.raises(KeyboardInterrupt, match="injected interruption"):
		d.put_object_stream(
			"bucket",
			"key",
			_InterruptedStream(b"012345"),
			size=6,
		)

	assert list((tier / "bucket").iterdir()) == []


def test_in_progress_stream_is_not_visible_in_object_listing(tmp_path: Path) -> None:
	tier = tmp_path / "tier"
	d = PosixDriver(str(tier), chunk_size=4)
	source = _BlockingAfterPayloadStream(b"data")
	errors: list[BaseException] = []

	def write_object() -> None:
		try:
			d.put_object_stream("bucket", "target.bin", source, size=4)
		except BaseException as error:
			errors.append(error)

	writer = threading.Thread(target=write_object)
	writer.start()
	try:
		assert source.waiting.wait(timeout=5)
		assert list(d.list_objects("bucket")) == []
		staging_entries = list((tier / ".cognistore-staging").iterdir())
		assert len(staging_entries) == 1
	finally:
		source.release.set()
		writer.join(timeout=5)

	assert not writer.is_alive()
	assert errors == []
	assert list(d.list_objects("bucket")) == ["target.bin"]
	assert list((tier / ".cognistore-staging").iterdir()) == []


def test_staging_directory_symlink_is_rejected(tmp_path: Path) -> None:
	tier = tmp_path / "tier"
	outside = tmp_path / "outside"
	tier.mkdir()
	outside.mkdir()
	try:
		(tier / ".cognistore-staging").symlink_to(
			outside,
			target_is_directory=True,
		)
	except (NotImplementedError, OSError):
		pytest.skip("directory symlinks are unavailable on this platform")

	d = PosixDriver(str(tier))
	with pytest.raises(ValueError, match="staging directory.*symbolic link"):
		d.put_object("bucket", "key", b"payload")

	assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
	"bucket",
	[".cognistore-staging", ".cognistore-staging/nested"],
)
def test_internal_staging_namespace_cannot_be_used_as_a_bucket(
	tmp_path: Path,
	bucket: str,
) -> None:
	d = PosixDriver(str(tmp_path / "tier"))

	with pytest.raises(ValueError, match="reserved namespace"):
		d.put_object(bucket, "key", b"payload")


def test_new_object_mode_honors_current_umask(tmp_path: Path) -> None:
	d = PosixDriver(str(tmp_path / "tier"))
	previous_umask = os.umask(0o027)
	try:
		d.put_object("bucket", "key", b"payload")
	finally:
		os.umask(previous_umask)

	mode = stat.S_IMODE((d.base / "bucket" / "key").stat().st_mode)
	assert mode == 0o640


def test_overwrite_preserves_existing_destination_mode(tmp_path: Path) -> None:
	d = PosixDriver(str(tmp_path / "tier"))
	target = d.base / "bucket" / "key"
	target.parent.mkdir(parents=True)
	target.write_bytes(b"original")
	target.chmod(0o604)

	d.put_object("bucket", "key", b"replacement")

	assert target.read_bytes() == b"replacement"
	assert stat.S_IMODE(target.stat().st_mode) == 0o604


def test_object_reader_context_closes_the_underlying_file(tmp_path: Path) -> None:
	d = PosixDriver(str(tmp_path / "tier"))
	d.put_object("bucket", "key", b"payload")

	with d.open_object_reader("bucket", "key") as source:
		assert source.read(3) == b"pay"
		assert source.closed is False  # type: ignore[attr-defined]

	assert source.closed is True  # type: ignore[attr-defined]


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
