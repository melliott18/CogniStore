"""Run the shared tier/pool contract against the local catalog backends."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from cognistore.core.catalog import Catalog, CatalogStore
from cognistore.core.sqlite_catalog import SQLiteCatalog
from tests.conformance.topology_store import TopologyStoreConformance, race, register_pool


class TestMemoryTopologyConformance(TopologyStoreConformance):
    @pytest.fixture
    def catalog(self) -> CatalogStore:
        return Catalog()


class TestSQLiteTopologyConformance(TopologyStoreConformance):
    @pytest.fixture
    def catalog(self, tmp_path: Path) -> Iterator[CatalogStore]:
        with SQLiteCatalog(tmp_path / "catalog.db") as catalog:
            yield catalog


def test_sqlite_separate_handles_serialize_lifecycle_and_assignment(tmp_path: Path) -> None:
    path = tmp_path / "catalog.db"
    with SQLiteCatalog(path) as first, SQLiteCatalog(path) as second:
        register_pool(first)
        first.register_tier("warm")
        first.upsert("bucket", "object", size=1, tier="hot")
        outcomes = race(
            lambda: first.assign_pool("bucket", "object", "hot-west"),
            lambda: second.register_pool("hot-west", "warm", region="us", members=("a",)),
        )
        assert outcomes[0] is None
        record = first.get("bucket", "object")
        pool = second.get_pool("hot-west")
        assert record is not None and pool is not None
        assert (record.tier, record.pool_id) == (pool.tier, pool.pool_id)
