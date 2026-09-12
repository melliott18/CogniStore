"""Exercise GCS through the catalog mover without cloud credentials."""

from pathlib import Path

import pytest

from cognistore.core.catalog import Catalog
from cognistore.core.mover import Mover
from cognistore.drivers.gcs_driver import GCSDriver
from cognistore.drivers.posix_driver import PosixDriver
from tests.gcs_fake import GCSFake


def test_mover_streams_to_gcs_and_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = GCSFake()
    local = PosixDriver(str(tmp_path / "local"), chunk_size=256 * 1024)
    catalog = Catalog()
    key = "large/roundtrip.bin"
    payload = bytes(range(256)) * 2049
    local.put_object("bucket", key, payload)
    catalog.upsert("bucket", key, size=len(payload), tier="local", metadata={"label": "keep"})

    def forbid_buffered_transfer(*args: object, **kwargs: object) -> None:
        pytest.fail("mover must use streaming operations")

    with server.client() as client:
        cloud = GCSDriver(client=client, emulator_endpoint=server.endpoint, chunk_size=256 * 1024)
        mover = Mover({"local": local, "cloud": cloud}, catalog)
        with monkeypatch.context() as patch:
            for driver in (local, cloud):
                patch.setattr(driver, "get_object", forbid_buffered_transfer)
                patch.setattr(driver, "put_object", forbid_buffered_transfer)
            first = mover.move("local", "cloud", "bucket", key)
            assert first.verified
            assert list(local.list_objects("bucket")) == []
            record = catalog.get("bucket", key)
            assert record is not None and record.tier == "cloud"
            second = mover.move("cloud", "local", "bucket", key)
            assert second.verified
            assert second.source_checksum == first.source_checksum
            assert list(cloud.list_objects("bucket")) == []
        assert local.get_object("bucket", key) == payload
        record = catalog.get("bucket", key)
        assert record is not None and record.tier == "local"
        assert record.metadata["label"] == "keep"
