"""Run the shared catalog object-read contract against local backends."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from cognistore.core.catalog import Catalog, CatalogStore
from cognistore.core.sqlite_catalog import SQLiteCatalog
from tests.conformance.catalog_store import CatalogStoreConformance


class TestMemoryCatalogConformance(CatalogStoreConformance):
    @pytest.fixture
    def catalog(self) -> CatalogStore:
        return Catalog()


class TestSQLiteCatalogConformance(CatalogStoreConformance):
    @pytest.fixture
    def catalog(self, tmp_path: Path) -> Iterator[CatalogStore]:
        with SQLiteCatalog(tmp_path / "catalog.db") as catalog:
            yield catalog
