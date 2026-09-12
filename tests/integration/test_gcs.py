"""Opt-in real HTTP emulator conformance and existing-bucket cloud validation.

The emulator path is anonymous and creates disposable buckets. The live path
requires COGNISTORE_GCS_LIVE_BUCKET and uses approved ADC credentials; it only
creates/deletes objects within one random test prefix in the supplied bucket.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from io import BytesIO

import httpx
import pytest

from cognistore.drivers.gcs_driver import GCSDriver
from cognistore.drivers.storage_driver import ObjectGenerationMismatchError
from tests.conformance.storage_driver import StorageDriverConformance

pytestmark = pytest.mark.integration

_EMULATOR_ENDPOINT = os.environ.get("COGNISTORE_GCS_EMULATOR_ENDPOINT")
_LIVE_BUCKET = os.environ.get("COGNISTORE_GCS_LIVE_BUCKET")
_CHUNK = 256 * 1024


class _EmulatorMissingMediaGenerationCheck(AssertionError):
    """The pinned emulator ignores ifGenerationMatch on media downloads."""


@pytest.mark.skipif(not _EMULATOR_ENDPOINT, reason="COGNISTORE_GCS_EMULATOR_ENDPOINT is not set")
class TestEmulatorGCSConformance(StorageDriverConformance):
    @pytest.fixture
    def driver(self) -> Iterator[GCSDriver]:
        driver = GCSDriver(
            emulator_endpoint=_EMULATOR_ENDPOINT,
            project="cognistore-test",
            auto_create_bucket=True,
            list_page_size=2,
            chunk_size=_CHUNK,
        )
        yield driver
        driver.close()

    @pytest.fixture
    def bucket(self, driver: GCSDriver) -> Iterator[str]:
        bucket = f"cognistore-gcs-test-{uuid.uuid4().hex}"
        yield bucket
        try:
            for key in driver.list_objects(bucket):
                driver.delete_object(bucket, key)
        except FileNotFoundError:
            return
        # Bucket cleanup is intentionally limited to the anonymous emulator.
        response = httpx.delete(f"{_EMULATOR_ENDPOINT}/storage/v1/b/{bucket}")
        assert response.status_code in {200, 204, 404}

    @pytest.mark.xfail(
        strict=True,
        raises=_EmulatorMissingMediaGenerationCheck,
        reason="fake-gcs-server 3c29d207 ignores ifGenerationMatch on media GET; live path checks it",
    )
    def test_generation_bound_reader_never_returns_a_replacement(
        self, driver: GCSDriver, bucket: str
    ) -> None:
        # Preserve the shared case unchanged, expecting only the exact missing
        # precondition assertion. Other failures remain failures, and an emulator
        # fix becomes a strict XPASS requiring this workaround to be removed.
        try:
            super().test_generation_bound_reader_never_returns_a_replacement(driver, bucket)
        except pytest.fail.Exception as error:
            if str(error) == "a stale generation-bound reader was yielded":
                raise _EmulatorMissingMediaGenerationCheck(str(error)) from None
            raise

    def test_large_resumable_upload(self, driver: GCSDriver, bucket: str) -> None:
        payload = b"part-" * _CHUNK + b"unaligned-tail"
        assert driver.put_object_stream(
            bucket, "large/data.bin", BytesIO(payload), size=len(payload),
            metadata={"content_type": "application/octet-stream", "metadata": {"test": "gcs"}},
        ) == len(payload)
        assert driver.get_object(bucket, "large/data.bin") == payload
        assert driver.stat_object(bucket, "large/data.bin")["size"] == len(payload)


@pytest.mark.skipif(not _LIVE_BUCKET, reason="COGNISTORE_GCS_LIVE_BUCKET is not set")
def test_live_gcs_existing_disposable_bucket() -> None:
    """Validate provider behavior without provisioning infrastructure."""
    assert _LIVE_BUCKET is not None
    driver = GCSDriver(
        project=os.environ.get("COGNISTORE_GCS_LIVE_PROJECT"),
        chunk_size=_CHUNK,
        list_page_size=2,
    )
    prefix = f"cognistore-validation/{uuid.uuid4().hex}/"
    key = prefix + "large.bin"
    payload = b"x" * (2 * _CHUNK) + b"tail"
    try:
        assert driver.put_object_stream(
            _LIVE_BUCKET, key, BytesIO(payload), size=len(payload), overwrite=False,
            metadata={"content_type": "application/octet-stream", "metadata": {"purpose": "validation"}},
        ) == len(payload)
        generation = driver.object_generation(_LIVE_BUCKET, key)
        with driver.open_object_reader_if_generation(_LIVE_BUCKET, key, generation) as source:
            assert source.read() == payload
        assert driver.get_object(_LIVE_BUCKET, key, range="bytes=2-6") == payload[2:7]
        with pytest.raises(FileExistsError):
            driver.put_object(_LIVE_BUCKET, key, b"competing", overwrite=False)
        assert driver.get_object(_LIVE_BUCKET, key) == payload
        for suffix in ["one", "two", "three"]:
            driver.put_object(_LIVE_BUCKET, prefix + suffix, b"")
        assert sorted(driver.list_objects(_LIVE_BUCKET, prefix)) == sorted(
            prefix + suffix for suffix in ["large.bin", "one", "two", "three"]
        )
        driver.put_object(_LIVE_BUCKET, key, b"replacement")
        with pytest.raises(ObjectGenerationMismatchError):
            driver.delete_object_if_generation(_LIVE_BUCKET, key, generation)
        assert driver.get_object(_LIVE_BUCKET, key) == b"replacement"
        with pytest.raises(ObjectGenerationMismatchError):
            with driver.open_object_reader_if_generation(_LIVE_BUCKET, key, generation):
                pytest.fail("stale generation opened")
        current = driver.object_generation(_LIVE_BUCKET, key)
        assert driver.delete_object_if_generation(_LIVE_BUCKET, key, current)
        assert not driver.delete_object_if_generation(_LIVE_BUCKET, key, current)
        with pytest.raises(FileNotFoundError):
            driver.get_object(_LIVE_BUCKET, key)
    finally:
        try:
            for own_key in driver.list_objects(_LIVE_BUCKET, prefix):
                driver.delete_object(_LIVE_BUCKET, own_key)
        finally:
            driver.close()
