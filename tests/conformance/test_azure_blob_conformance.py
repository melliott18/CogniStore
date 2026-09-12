"""Run the provider-neutral contract against a stateful Azure SDK fake."""

import pytest

pytest.importorskip("azure.storage.blob")

from cognistore.drivers.azure_blob_driver import AzureBlobDriver
from tests.azure_blob_fixtures import FakeAzureService
from tests.conformance.storage_driver import StorageDriverConformance


class TestAzureBlobDriverConformance(StorageDriverConformance):
    @pytest.fixture
    def driver(self) -> AzureBlobDriver:
        return AzureBlobDriver(
            client=FakeAzureService(), auto_create_container=True, list_page_size=2,
        )
