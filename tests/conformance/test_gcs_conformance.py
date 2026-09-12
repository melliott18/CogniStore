"""Run the shared storage contract against a deterministic GCS HTTP emulator."""

from collections.abc import Iterator

import pytest

from cognistore.drivers.gcs_driver import GCSDriver
from tests.conformance.storage_driver import StorageDriverConformance
from tests.gcs_fake import GCSFake


class TestGCSDriverConformance(StorageDriverConformance):
    @pytest.fixture
    def driver(self) -> Iterator[GCSDriver]:
        server = GCSFake()
        with server.client() as client:
            yield GCSDriver(
                client=client,
                emulator_endpoint=server.endpoint,
                project="test-project",
                auto_create_bucket=True,
                list_page_size=2,
                chunk_size=256 * 1024,
            )
