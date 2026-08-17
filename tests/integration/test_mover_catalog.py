import hashlib
import tracemalloc
from pathlib import Path

import pytest

from cognistore.core.catalog import Catalog
from cognistore.core.move_jobs import MoveJobState
from cognistore.core.mover import Mover, MoveVerificationError
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver


def _forbid_buffered_transfer(*_args, **_kwargs):
	raise AssertionError("mover attempted a whole-object transfer")


def test_mover_updates_catalog_and_storage(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
	hot = PosixDriver(str(tmp_path / "hot"))
	warm = PosixDriver(str(tmp_path / "warm"))
	drivers = {"hot": hot, "warm": warm}

	catalog = Catalog()
	mover = Mover(drivers, catalog)

	bucket = "bk"
	key = "dir/file.txt"
	data = b"hello mover"
	catalog_metadata = {
		"mime": "text/plain",
		"sample_len": len(data),
		"labels": ["important", "reviewed"],
	}

	# Put in hot tier and register in catalog
	hot.put_object(bucket, key, data)
	catalog.upsert(
		bucket=bucket,
		key=key,
		size=1,
		tier="hot",
		metadata=catalog_metadata,
	)

	original_stat = hot.stat_object

	def stat_with_portable_metadata(bucket: str, key: str):
		metadata = original_stat(bucket, key)
		metadata.update(
			{
				"content_type": "text/plain",
				"metadata": {"origin": "integration-test"},
			}
		)
		return metadata

	stream_calls = []
	original_put_stream = warm.put_object_stream

	def recording_put_stream(
		bucket: str,
		key: str,
		source,
		*,
		size: int,
		overwrite: bool = True,
		metadata=None,
	):
		stream_calls.append(
			{
				"size": size,
				"overwrite": overwrite,
				"metadata": dict(metadata or {}),
			}
		)
		return original_put_stream(
			bucket,
			key,
			source,
			size=size,
			overwrite=overwrite,
			metadata=metadata,
		)

	monkeypatch.setattr(hot, "stat_object", stat_with_portable_metadata)
	monkeypatch.setattr(hot, "get_object", _forbid_buffered_transfer)
	monkeypatch.setattr(warm, "put_object", _forbid_buffered_transfer)
	monkeypatch.setattr(warm, "put_object_stream", recording_put_stream)

	# Move to warm
	verification = mover.move("hot", "warm", bucket, key)

	# Verify storage: not in hot, present in warm
	assert list(hot.list_objects(bucket)) == []
	assert key in list(warm.list_objects(bucket))

	# Verify catalog updated
	rec = catalog.get(bucket, key)
	assert rec is not None
	assert rec.tier == "warm"
	assert rec.size == len(data)
	assert rec.metadata == {
		**catalog_metadata,
		"sha256": hashlib.sha256(data).hexdigest(),
	}
	assert rec.metadata is not catalog_metadata
	assert verification.verified is True
	assert verification.status == "verified"
	assert verification.algorithm == "sha256"
	assert verification.source_size == len(data)
	assert verification.destination_size == len(data)
	assert verification.source_checksum == hashlib.sha256(data).hexdigest()
	assert verification.destination_checksum == verification.source_checksum
	assert stream_calls[0]["size"] == len(data)
	assert stream_calls[0]["overwrite"] is False
	assert stream_calls[0]["metadata"]["content_type"] == "text/plain"
	assert stream_calls[0]["metadata"]["metadata"] == {
		"origin": "integration-test"
	}


def test_mover_streams_multiple_chunks_with_bounded_memory(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
	chunk_size = 64 * 1024
	hot = PosixDriver(str(tmp_path / "hot"), chunk_size=chunk_size)
	warm = PosixDriver(str(tmp_path / "warm"), chunk_size=chunk_size)
	catalog = Catalog()
	mover = Mover({"hot": hot, "warm": warm}, catalog)
	bucket = "bk"
	key = "large/many-chunks.bin"
	block = bytes(range(256)) * (chunk_size // 256)
	block_count = 128
	size = len(block) * block_count

	source_path = hot.base / bucket / key
	source_path.parent.mkdir(parents=True)
	expected_digest = hashlib.sha256()
	with source_path.open("wb") as source:
		for _ in range(block_count):
			source.write(block)
			expected_digest.update(block)
	catalog.upsert(bucket, key, size=size, tier="hot", metadata={"mime": "application/octet-stream"})

	monkeypatch.setattr(hot, "get_object", _forbid_buffered_transfer)
	monkeypatch.setattr(warm, "put_object", _forbid_buffered_transfer)

	tracemalloc.start()
	try:
		mover.move("hot", "warm", bucket, key)
		_, peak = tracemalloc.get_traced_memory()
	finally:
		tracemalloc.stop()

	# The payload is 128 chunks. Allow implementation overhead and a handful of
	# simultaneous buffers while ensuring memory does not scale with object size.
	assert peak < chunk_size * 16
	with pytest.raises(FileNotFoundError):
		hot.stat_object(bucket, key)
	assert warm.stat_object(bucket, key)["size"] == size

	actual_digest = hashlib.sha256()
	with warm.open_object_reader(bucket, key) as destination:
		while chunk := destination.read(chunk_size):
			actual_digest.update(chunk)
	assert actual_digest.digest() == expected_digest.digest()


def test_mover_preserves_sqlite_catalog_metadata_and_corrects_size(tmp_path: Path):
	hot = PosixDriver(str(tmp_path / "hot"))
	warm = PosixDriver(str(tmp_path / "warm"))
	data = b"sqlite catalog streaming move"
	metadata = {
		"mime": "text/plain",
		"sample_len": len(data),
		"classification": {"retention": "standard"},
	}
	hot.put_object("bucket", "nested/object.txt", data)
	catalog = SQLiteCatalog(tmp_path / "catalog.sqlite")
	catalog.upsert(
		"bucket",
		"nested/object.txt",
		size=1,
		tier="hot",
		metadata=metadata,
	)

	try:
		Mover({"hot": hot, "warm": warm}, catalog).move(
			"hot", "warm", "bucket", "nested/object.txt"
		)

		record = catalog.get("bucket", "nested/object.txt")
		assert record is not None
		assert record.tier == "warm"
		assert record.size == len(data)
		assert record.metadata == {
			**metadata,
			"sha256": hashlib.sha256(data).hexdigest(),
		}
		assert warm.get_object("bucket", "nested/object.txt") == data
		with pytest.raises(FileNotFoundError):
			hot.stat_object("bucket", "nested/object.txt")
	finally:
		catalog.close()


@pytest.mark.parametrize("catalog_kind", ["memory", "sqlite"])
def test_mover_preserves_metadata_changed_during_transfer(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	catalog_kind: str,
) -> None:
	hot = PosixDriver(str(tmp_path / f"{catalog_kind}-hot"))
	warm = PosixDriver(str(tmp_path / f"{catalog_kind}-warm"))
	catalog = (
		SQLiteCatalog(tmp_path / "concurrent-catalog.sqlite")
		if catalog_kind == "sqlite"
		else Catalog()
	)
	bucket = "bucket"
	key = "metadata-update.bin"
	data = b"streamed data"
	hot.put_object(bucket, key, data)
	catalog.upsert(
		bucket,
		key,
		size=1,
		tier="hot",
		metadata={"revision": "before-transfer"},
	)
	original_put_stream = warm.put_object_stream

	def update_metadata_then_transfer(*args, **kwargs):
		catalog.upsert(
			bucket,
			key,
			size=2,
			tier="hot",
			metadata={"revision": "during-transfer", "scanner": "latest"},
		)
		return original_put_stream(*args, **kwargs)

	monkeypatch.setattr(warm, "put_object_stream", update_metadata_then_transfer)

	try:
		Mover({"hot": hot, "warm": warm}, catalog).move(
			"hot", "warm", bucket, key
		)

		record = catalog.get(bucket, key)
		assert record is not None
		assert (record.tier, record.size, record.metadata) == (
			"warm",
			len(data),
			{
				"revision": "during-transfer",
				"scanner": "latest",
				"sha256": hashlib.sha256(data).hexdigest(),
			},
		)
	finally:
		if isinstance(catalog, SQLiteCatalog):
			catalog.close()


@pytest.mark.parametrize("catalog_kind", ["memory", "sqlite"])
def test_mover_creates_catalog_record_when_missing_at_commit(
	tmp_path: Path,
	catalog_kind: str,
) -> None:
	hot = PosixDriver(str(tmp_path / f"{catalog_kind}-hot"))
	warm = PosixDriver(str(tmp_path / f"{catalog_kind}-warm"))
	catalog = (
		SQLiteCatalog(tmp_path / "missing-record.sqlite")
		if catalog_kind == "sqlite"
		else Catalog()
	)
	bucket = "bucket"
	key = "uncataloged.bin"
	data = b"catalog after move"
	hot.put_object(bucket, key, data)

	try:
		Mover({"hot": hot, "warm": warm}, catalog).move(
			"hot", "warm", bucket, key
		)

		record = catalog.get(bucket, key)
		assert record is not None
		assert (record.tier, record.size, record.metadata) == (
			"warm",
			len(data),
			{"sha256": hashlib.sha256(data).hexdigest()},
		)
	finally:
		if isinstance(catalog, SQLiteCatalog):
			catalog.close()


def test_stream_failure_preserves_source_and_catalog(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
	hot = PosixDriver(str(tmp_path / "hot"))
	warm = PosixDriver(str(tmp_path / "warm"))
	bucket = "bk"
	key = "failed-transfer.bin"
	data = b"source must survive"
	metadata = {"mime": "application/octet-stream"}
	hot.put_object(bucket, key, data)
	catalog = Catalog()
	catalog.upsert(bucket, key, size=len(data), tier="hot", metadata=metadata)

	def fail_stream(*_args, **_kwargs):
		raise OSError("injected destination failure")

	monkeypatch.setattr(warm, "put_object_stream", fail_stream)

	with pytest.raises(OSError, match="injected destination failure"):
		Mover({"hot": hot, "warm": warm}, catalog).move(
			"hot", "warm", bucket, key
		)

	assert hot.get_object(bucket, key) == data
	with pytest.raises(FileNotFoundError):
		warm.stat_object(bucket, key)
	record = catalog.get(bucket, key)
	assert record is not None
	assert (record.tier, record.size, record.metadata) == (
		"hot",
		len(data),
		metadata,
	)


@pytest.mark.parametrize("damage", ["truncate", "corrupt"])
def test_destination_verification_failure_preserves_posix_source_and_catalog(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	damage: str,
) -> None:
	hot = PosixDriver(str(tmp_path / f"{damage}-hot"))
	warm = PosixDriver(str(tmp_path / f"{damage}-warm"))
	bucket = "bk"
	key = f"{damage}.bin"
	data = b"source bytes must survive verification failure"
	original_metadata = {"sha256": "stale-scan-digest", "revision": 7}
	hot.put_object(bucket, key, data)
	catalog = Catalog()
	catalog.upsert(
		bucket,
		key,
		size=len(data),
		tier="hot",
		metadata=original_metadata,
	)
	original_put_stream = warm.put_object_stream

	def damage_committed_destination(*args, **kwargs):
		written = original_put_stream(*args, **kwargs)
		path = warm.base / bucket / key
		if damage == "truncate":
			with path.open("r+b") as destination:
				destination.truncate(len(data) - 1)
		else:
			with path.open("r+b") as destination:
				destination.seek(len(data) // 2)
				original = destination.read(1)
				destination.seek(len(data) // 2)
				destination.write(bytes([original[0] ^ 0xFF]))
		return written

	monkeypatch.setattr(warm, "put_object_stream", damage_committed_destination)

	with pytest.raises(MoveVerificationError) as captured:
		Mover({"hot": hot, "warm": warm}, catalog).move(
			"hot", "warm", bucket, key
		)

	result = captured.value.result
	assert result.status == "failed"
	assert result.verified is False
	assert result.source_checksum == hashlib.sha256(data).hexdigest()
	assert result.destination_checksum is not None
	assert result.destination_checksum != result.source_checksum
	assert result.source_checksum in str(captured.value)
	assert result.destination_checksum in str(captured.value)
	assert hot.get_object(bucket, key) == data
	record = catalog.get(bucket, key)
	assert record is not None
	assert (record.tier, record.size, record.metadata) == (
		"hot",
		len(data),
		original_metadata,
	)


def test_transient_destination_verification_is_resumable_without_retransfer(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	hot = PosixDriver(str(tmp_path / "hot"))
	warm = PosixDriver(str(tmp_path / "warm"))
	bucket = "bk"
	key = "unreadable-destination.bin"
	data = b"retain source when destination cannot be read"
	hot.put_object(bucket, key, data)
	catalog = Catalog()
	catalog.upsert(bucket, key, len(data), "hot", metadata={"revision": 3})

	original_reader = warm.open_object_reader
	original_put = warm.put_object_stream
	verification_reads = 0
	transfers = 0

	def fail_first_verification_read(*args, **kwargs):
		nonlocal verification_reads
		verification_reads += 1
		if verification_reads == 1:
			raise TimeoutError("injected destination read timeout")
		return original_reader(*args, **kwargs)

	def count_transfer(*args, **kwargs):
		nonlocal transfers
		transfers += 1
		return original_put(*args, **kwargs)

	monkeypatch.setattr(warm, "open_object_reader", fail_first_verification_read)
	monkeypatch.setattr(warm, "put_object_stream", count_transfer)
	mover = Mover({"hot": hot, "warm": warm}, catalog)
	move_id = "test-job"

	with pytest.raises(TimeoutError, match="injected destination read timeout"):
		mover.move(
			"hot", "warm", bucket, key, idempotency_key=move_id  # gitleaks:allow
		)

	interrupted = mover.get_job(move_id)
	assert interrupted is not None
	assert interrupted.state == MoveJobState.TRANSFERRED
	assert hot.get_object(bucket, key) == data
	record = catalog.get(bucket, key)
	assert record is not None
	assert (record.tier, record.metadata) == ("hot", {"revision": 3})

	result = mover.move(
		"hot", "warm", bucket, key, idempotency_key=move_id  # gitleaks:allow
	)
	assert result.verified is True
	assert transfers == 1
	assert verification_reads >= 2
	assert warm.get_object(bucket, key) == data
	with pytest.raises(FileNotFoundError):
		hot.stat_object(bucket, key)
	assert catalog.get(bucket, key).tier == "warm"  # type: ignore[union-attr]


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
