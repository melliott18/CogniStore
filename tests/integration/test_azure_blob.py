"""Run the real Azure SDK against explicitly configured Azurite or Azure Blob.

Live runs require COGNISTORE_AZURE_LIVE=1 as well as an account URL or connection
string. Every case owns a random container; ambient credentials alone never
enable cloud writes. See docs/azure_blob_driver.md for commands.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("azure.storage.blob")

from azure.core.exceptions import ResourceNotFoundError

from cognistore.core.catalog import Catalog
from cognistore.core.mover import Mover
from cognistore.drivers.azure_blob_driver import AzureBlobDriver
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.drivers.storage_driver import ObjectGenerationMismatchError
from tests.conformance.storage_driver import StorageDriverConformance

pytestmark = pytest.mark.integration
_CHUNK_SIZE = 64 * 1024


@pytest.fixture(params=["azurite", "live"])
def azure_driver(request: pytest.FixtureRequest) -> Iterator[AzureBlobDriver]:
    options = {"chunk_size": _CHUNK_SIZE, "list_page_size": 2, "auto_create_container": True}
    if request.param == "azurite":
        if not os.environ.get("COGNISTORE_AZURITE_CONNECTION_STRING"):
            pytest.skip("COGNISTORE_AZURITE_CONNECTION_STRING is not configured")
        driver = AzureBlobDriver(
            connection_string=os.environ["COGNISTORE_AZURITE_CONNECTION_STRING"], **options
        )
    else:
        if os.environ.get("COGNISTORE_AZURE_LIVE") != "1":
            pytest.skip("Live Azure validation requires COGNISTORE_AZURE_LIVE=1")
        has_url = bool(os.environ.get("COGNISTORE_AZURE_ACCOUNT_URL"))
        has_connection = bool(os.environ.get("COGNISTORE_AZURE_CONNECTION_STRING"))
        if has_url == has_connection:
            pytest.fail(
                "Set exactly one of COGNISTORE_AZURE_ACCOUNT_URL or "
                "COGNISTORE_AZURE_CONNECTION_STRING for live validation"
            )
        driver = AzureBlobDriver(
            account_url=os.environ.get("COGNISTORE_AZURE_ACCOUNT_URL"),
            connection_string=os.environ.get("COGNISTORE_AZURE_CONNECTION_STRING"),
            **options,
        )
    try:
        yield driver
    finally:
        driver.close()


@pytest.fixture
def azure_bucket(azure_driver: AzureBlobDriver) -> Iterator[str]:
    bucket = f"cognistore-azure-test-{uuid4().hex}"
    try:
        yield bucket
    finally:
        try:
            azure_driver._client.delete_container(bucket, logging_enable=False)
        except ResourceNotFoundError:
            pass


class TestAzureBlobConformance(StorageDriverConformance):
    @pytest.fixture
    def driver(self, azure_driver: AzureBlobDriver) -> AzureBlobDriver:
        return azure_driver

    @pytest.fixture
    def bucket(self, azure_bucket: str) -> str:
        return azure_bucket


def test_large_upload_reads_only_bounded_chunks(
    azure_driver: AzureBlobDriver, azure_bucket: str
) -> None:
    size = 16 * 1024 * 1024 + 17

    class GeneratedSource:
        remaining = size

        def read(self, requested: int = -1) -> bytes:
            assert 0 < requested <= _CHUNK_SIZE
            # Short reads must be reassembled into complete blocks.
            count = min(requested, self.remaining, 16381)
            self.remaining -= count
            return b"x" * count

    assert azure_driver.put_object_stream(
        azure_bucket, "large.bin", GeneratedSource(), size=size,
        metadata={"content_type": "application/x-cognistore", "metadata": {"owner": "test"}},
    ) == size
    properties = azure_driver.stat_object(azure_bucket, "large.bin")
    assert properties["size"] == size
    assert properties["content_type"] == "application/x-cognistore"
    assert properties["metadata"] == {"owner": "test"}
    received = 0
    with azure_driver.open_object_reader(azure_bucket, "large.bin") as reader:
        while chunk := reader.read(_CHUNK_SIZE):
            assert chunk == b"x" * len(chunk)
            received += len(chunk)
    assert received == size


@pytest.mark.parametrize("existing", [False, True])
def test_failed_upload_is_invisible_and_retry_discards_abandoned_blocks(
    azure_driver: AzureBlobDriver, azure_bucket: str, existing: bool
) -> None:
    key = "failed-upload.bin"
    if existing:
        azure_driver.put_object(azure_bucket, key, b"original")
        generation = azure_driver.object_generation(azure_bucket, key)

    class BrokenSource:
        calls = 0

        def read(self, requested: int = -1) -> bytes:
            self.calls += 1
            if self.calls > 1:
                raise OSError("source disconnected")
            return b"x" * requested

    with pytest.raises(OSError, match="source disconnected"):
        azure_driver.put_object_stream(
            azure_bucket, key, BrokenSource(), size=_CHUNK_SIZE * 2
        )
    if existing:
        assert azure_driver.get_object(azure_bucket, key) == b"original"
        assert azure_driver.object_generation(azure_bucket, key) == generation
    else:
        with pytest.raises(FileNotFoundError):
            azure_driver.stat_object(azure_bucket, key)
        assert key not in list(azure_driver.list_objects(azure_bucket))

    blob = azure_driver._client.get_blob_client(azure_bucket, key)
    assert blob.get_block_list(block_list_type="all")[1]
    azure_driver.put_object(azure_bucket, key, b"retry succeeded")
    assert azure_driver.get_object(azure_bucket, key) == b"retry succeeded"
    assert not blob.get_block_list(block_list_type="all")[1]


def test_atomic_commit_preserves_concurrent_writer(
    azure_driver: AzureBlobDriver, azure_bucket: str
) -> None:
    key = "racing-writers.bin"

    class RacingSource(BytesIO):
        def read(self, size: int = -1) -> bytes:
            chunk = super().read(size)
            if not chunk:
                azure_driver.put_object(azure_bucket, key, b"winner", overwrite=False)
            return chunk

    with pytest.raises(FileExistsError):
        azure_driver.put_object_stream(
            azure_bucket, key, RacingSource(b"loser"), size=5, overwrite=False
        )
    assert azure_driver.get_object(azure_bucket, key) == b"winner"


def test_multi_request_reader_rejects_changed_generation(
    azure_driver: AzureBlobDriver, azure_bucket: str
) -> None:
    key = "changing.bin"
    azure_driver.put_object(azure_bucket, key, b"x" * (_CHUNK_SIZE * 2))
    generation = azure_driver.object_generation(azure_bucket, key)
    with azure_driver.open_object_reader_if_generation(azure_bucket, key, generation) as reader:
        assert reader.read(_CHUNK_SIZE) == b"x" * _CHUNK_SIZE
        azure_driver.put_object(azure_bucket, key, b"y" * (_CHUNK_SIZE * 2))
        with pytest.raises(ObjectGenerationMismatchError):
            reader.read(_CHUNK_SIZE)


def test_mover_streams_posix_to_azure_and_back(
    tmp_path: Path, azure_driver: AzureBlobDriver, azure_bucket: str
) -> None:
    local = PosixDriver(str(tmp_path / "local"), chunk_size=_CHUNK_SIZE)
    catalog = Catalog()
    payload = b"mover payload" * 20000
    key = "nested/moved.bin"
    local.put_object(azure_bucket, key, payload)
    catalog.upsert(azure_bucket, key, len(payload), "local")
    mover = Mover({"local": local, "azure": azure_driver}, catalog)
    mover.move("local", "azure", azure_bucket, key)
    assert azure_driver.get_object(azure_bucket, key) == payload
    with pytest.raises(FileNotFoundError):
        local.stat_object(azure_bucket, key)
    mover.move("azure", "local", azure_bucket, key)
    assert local.get_object(azure_bucket, key) == payload
    with pytest.raises(FileNotFoundError):
        azure_driver.stat_object(azure_bucket, key)
    assert catalog.get(azure_bucket, key).tier == "local"
