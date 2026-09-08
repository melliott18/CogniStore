"""The access observer preserves the complete storage-driver contract."""

from pathlib import Path

import pytest

from cognistore.core.catalog import Catalog
from cognistore.drivers.observed import ObservedStorageDriver
from cognistore.drivers.posix_driver import PosixDriver
from tests.conformance.storage_driver import StorageDriverConformance


class TestObservedDriverConformance(StorageDriverConformance):
    @pytest.fixture
    def driver(self, tmp_path: Path) -> ObservedStorageDriver:
        return ObservedStorageDriver(PosixDriver(str(tmp_path / "objects")), Catalog())
