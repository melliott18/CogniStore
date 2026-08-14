"""Provider-neutral conformance tests for :class:`StorageDriver` implementations.

Concrete test classes inherit :class:`StorageDriverConformance` and provide a
``driver`` fixture.  Keeping the contract here ensures that local and remote
backends are held to exactly the same observable behavior.
"""

from __future__ import annotations

import uuid
from io import BytesIO

import pytest

from cognistore.drivers.storage_driver import StorageDriver


class StorageDriverConformance:
    """Tests that every storage backend must pass.

    The class name intentionally does not start with ``Test``.  Pytest should
    collect these methods only from a concrete subclass that supplies the
    backend-specific ``driver`` fixture.
    """

    @pytest.fixture
    def driver(self) -> StorageDriver:
        raise NotImplementedError("concrete conformance tests must provide a driver")

    @pytest.fixture
    def bucket(self) -> str:
        # A distinct namespace keeps conformance cases independent even when a
        # remote backend retains state from a previous interrupted run.
        return f"cognistore-conformance-{uuid.uuid4().hex}"

    def test_declares_boolean_capabilities(self, driver: StorageDriver) -> None:
        capabilities = driver.capabilities

        assert isinstance(capabilities.range_reads, bool)
        assert isinstance(capabilities.range_writes, bool)
        assert isinstance(capabilities.atomic_no_overwrite, bool)

    def test_put_get_and_stat_roundtrip(
        self, driver: StorageDriver, bucket: str
    ) -> None:
        key = "nested/roundtrip.bin"
        payload = b"\x00CogniStore conformance\xff\n"

        driver.put_object(bucket, key, payload)

        assert driver.get_object(bucket, key) == payload
        metadata = driver.stat_object(bucket, key)
        assert metadata["size"] == len(payload)
        assert isinstance(metadata["mtime"], float)
        assert metadata["mtime"] >= 0

    def test_streaming_put_and_reader_roundtrip(
        self, driver: StorageDriver, bucket: str
    ) -> None:
        key = "nested/streaming-roundtrip.bin"
        payload = b"streamed-in-several-small-reads"

        written = driver.put_object_stream(
            bucket,
            key,
            BytesIO(payload),
            size=len(payload),
            metadata={"content_type": "application/octet-stream"},
        )

        chunks: list[bytes] = []
        with driver.open_object_reader(bucket, key) as source:
            while chunk := source.read(3):
                chunks.append(chunk)
        assert written == len(payload)
        assert b"".join(chunks) == payload

    def test_streaming_zero_byte_object(
        self, driver: StorageDriver, bucket: str
    ) -> None:
        key = "empty-stream.bin"

        written = driver.put_object_stream(bucket, key, BytesIO(), size=0)

        assert written == 0
        with driver.open_object_reader(bucket, key) as source:
            assert source.read(1) == b""
        assert driver.stat_object(bucket, key)["size"] == 0

    @pytest.mark.parametrize(
        ("payload", "declared_size"),
        [(b"short", 6), (b"too-long", 7)],
    )
    def test_stream_size_mismatch_does_not_publish_an_object(
        self,
        driver: StorageDriver,
        bucket: str,
        payload: bytes,
        declared_size: int,
    ) -> None:
        key = f"mismatched-{declared_size}.bin"

        with pytest.raises(ValueError):
            driver.put_object_stream(
                bucket,
                key,
                BytesIO(payload),
                size=declared_size,
            )

        with pytest.raises(FileNotFoundError):
            driver.stat_object(bucket, key)

    def test_list_objects_filters_prefix_and_returns_every_page(
        self, driver: StorageDriver, bucket: str
    ) -> None:
        objects = {
            "alpha/one.txt": b"one",
            "alpha/three.txt": b"three",
            "alpha/two.txt": b"two",
            "beta/four.txt": b"four",
            "root.txt": b"root",
        }
        for key, payload in objects.items():
            driver.put_object(bucket, key, payload)

        assert sorted(driver.list_objects(bucket)) == sorted(objects)
        assert sorted(driver.list_objects(bucket, prefix="alpha/")) == [
            "alpha/one.txt",
            "alpha/three.txt",
            "alpha/two.txt",
        ]
        assert list(driver.list_objects(bucket, prefix="missing/")) == []

    @pytest.mark.parametrize("operation", ["get", "stat"])
    def test_missing_object_raises_file_not_found(
        self, driver: StorageDriver, bucket: str, operation: str
    ) -> None:
        with pytest.raises(FileNotFoundError):
            if operation == "get":
                driver.get_object(bucket, "does/not/exist")
            else:
                driver.stat_object(bucket, "does/not/exist")

    def test_delete_is_idempotent(self, driver: StorageDriver, bucket: str) -> None:
        key = "delete-me.txt"

        # Deleting a missing object is part of the contract as well as deleting
        # an object that existed before the first call.
        driver.delete_object(bucket, "never-created.txt")
        driver.delete_object(bucket, "never-created.txt")
        driver.put_object(bucket, key, b"temporary")
        driver.delete_object(bucket, key)
        driver.delete_object(bucket, key)

        with pytest.raises(FileNotFoundError):
            driver.get_object(bucket, key)

    def test_put_without_overwrite_preserves_existing_object(
        self, driver: StorageDriver, bucket: str
    ) -> None:
        key = "exclusive.txt"
        driver.put_object(bucket, key, b"original")

        with pytest.raises(FileExistsError):
            driver.put_object(bucket, key, b"replacement", overwrite=False)

        assert driver.get_object(bucket, key) == b"original"

    def test_put_overwrites_existing_object_by_default(
        self, driver: StorageDriver, bucket: str
    ) -> None:
        key = "replace.txt"
        driver.put_object(bucket, key, b"original")

        driver.put_object(bucket, key, b"replacement")

        assert driver.get_object(bucket, key) == b"replacement"

    def test_range_read_when_supported(
        self, driver: StorageDriver, bucket: str
    ) -> None:
        if not driver.capabilities.range_reads:
            pytest.skip("backend does not advertise range reads")

        driver.put_object(bucket, "range-read.bin", b"0123456789")

        assert (
            driver.get_object(bucket, "range-read.bin", range="bytes=2-6")
            == b"23456"
        )

    def test_range_write_when_supported(
        self, driver: StorageDriver, bucket: str
    ) -> None:
        if not driver.capabilities.range_writes:
            pytest.skip("backend does not advertise range writes")

        key = "range-write.bin"
        driver.put_object(bucket, key, b"0123456789")
        driver.put_object(bucket, key, b"ABCD", range="bytes=3-6")

        assert driver.get_object(bucket, key) == b"012ABCD789"
