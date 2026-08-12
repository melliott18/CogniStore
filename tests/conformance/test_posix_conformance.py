"""Run the shared storage-driver contract against the POSIX backend."""

from pathlib import Path

import pytest

from cognistore.drivers.posix_driver import PosixDriver
from tests.conformance.storage_driver import StorageDriverConformance


class TestPosixDriverConformance(StorageDriverConformance):
    @pytest.fixture
    def driver(self, tmp_path: Path) -> PosixDriver:
        return PosixDriver(base_path=str(tmp_path / "objects"))
