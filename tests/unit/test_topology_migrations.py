from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from cognistore.core.topology import AttributeValue
from cognistore.db import MigrationManager, SQLCatalog
from cognistore.db.catalog import CatalogSchemaOutdatedError
from cognistore.db.schema import object_placements, objects
from cognistore.db.sqlite_import import SQLiteCatalogImportError, import_sqlite_catalog


def _legacy_pool_catalog(path: Path) -> tuple[object, object]:
    """Create a real revision-0006 catalog, including an assigned legacy pool."""
    with SQLCatalog(path) as catalog:
        catalog.upsert("bucket", "object", size=3, tier="hot", metadata={"owner": "alice"})
        catalog.register_pool(
            "pool-a", "hot", {"device": "nvme0"}, region="west", members=("nvme0",)
        )
        catalog.assign_pool("bucket", "object", "pool-a")
        catalog.claim_move_job(
            "historical-move",
            src_tier="retired-source",
            dst_tier="retired-destination",
            bucket="bucket",
            key="other",
            expected_size=3,
            source_metadata={},
            owner_id="worker",
            now="2026-09-01T00:00:00Z",
            lease_expires_at="2026-09-01T00:01:00Z",
        )
        with catalog.engine.connect() as connection:
            object_id = connection.execute(sa.select(objects.c.object_id)).scalar_one()
            placement_id = connection.execute(
                sa.select(object_placements.c.placement_id)
            ).scalar_one()
        MigrationManager().downgrade(catalog.engine, "0006_embeddings")
        assert "active" not in {
            column["name"] for column in sa.inspect(catalog.engine).get_columns("tiers")
        }
        assert "region" not in {
            column["name"] for column in sa.inspect(catalog.engine).get_columns("pools")
        }
    return object_id, placement_id


def test_topology_migration_preserves_legacy_identity_and_requires_pool_inventory(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-pools.db"
    object_id, placement_id = _legacy_pool_catalog(path)
    with pytest.raises(CatalogSchemaOutdatedError):
        SQLCatalog(path, read_only=True)

    with SQLCatalog(path) as catalog:
        assert MigrationManager().current(catalog.engine) == "0008_access_events"
        tier = catalog.get_tier("hot")
        assert tier is not None and tier.active
        pool = catalog.get_pool("pool-a")
        assert pool is not None
        assert (pool.active, pool.region, pool.members, pool.localities, pool.attributes) == (
            False, None, (), (), {},
        )
        assert pool.metadata == {"device": "nvme0"}
        record = catalog.get("bucket", "object")
        assert record is not None
        assert (record.tier, record.pool_id, record.metadata) == (
            "hot", "pool-a", {"owner": "alice"},
        )
        assert catalog.eligible_placements() == []
        job = catalog.get_move_job("historical-move")
        assert job is not None
        assert (job.src_tier, job.dst_tier) == ("retired-source", "retired-destination")
        assert len(catalog.list_move_job_transitions("historical-move")) == 1
        with catalog.engine.connect() as connection:
            assert connection.execute(sa.select(objects.c.object_id)).scalar_one() == object_id
            assert connection.execute(
                sa.select(object_placements.c.placement_id)
            ).scalar_one() == placement_id
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

        catalog.register_pool("pool-a", "hot", region="west", members=("nvme0",))
        assert len(catalog.eligible_placements()) == 1
        # The downgrade is reversible and does not rebuild placement rows or FKs.
        MigrationManager().downgrade(catalog.engine, "0006_embeddings")
        MigrationManager().upgrade(catalog.engine)
        restored = catalog.get("bucket", "object")
        assert restored == record
        assert catalog.get_pool("pool-a").active is False


def test_import_pre_topology_catalog_preserves_legacy_pool_as_inactive(tmp_path: Path) -> None:
    source_path = tmp_path / "legacy-pools.db"
    object_id, placement_id = _legacy_pool_catalog(source_path)
    with SQLCatalog(tmp_path / "imported.db") as catalog:
        report = import_sqlite_catalog(source_path, catalog)
        assert (report.tiers, report.pools, report.objects, report.placements) == (1, 1, 1, 1)
        pool = catalog.get_pool("pool-a")
        assert pool is not None and not pool.active
        assert pool.region is None and pool.members == ()
        assert catalog.get("bucket", "object").pool_id == "pool-a"
        assert catalog.eligible_placements() == []
        with catalog.engine.connect() as connection:
            assert connection.execute(sa.select(objects.c.object_id)).scalar_one() == object_id
            assert connection.execute(
                sa.select(object_placements.c.placement_id)
            ).scalar_one() == placement_id


def test_import_current_catalog_preserves_topology_and_observation_provenance(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "topology.db"
    latency = AttributeValue(
        value=4.5,
        unit="ms",
        source="probe-west",
        kind="measured",
        observed_at="2026-09-01T00:00:00Z",
        max_age_seconds=60,
    )
    with SQLCatalog(source_path) as source:
        source.register_tier("hot", {"purpose": "interactive"})
        source.register_tier("offline", active=False)
        source.register_pool(
            "pool-a", "hot", {"device": "nvme0"},
            region="west", members=("node-a", "node-b"), localities=("zone-a",),
            attributes={"latency": latency},
        )
        with SQLCatalog(tmp_path / "imported.db") as destination:
            import_sqlite_catalog(source_path, destination)
            assert destination.list_tiers() == source.list_tiers()
            assert destination.list_pools() == source.list_pools()
            assert destination.eligible_placements(now="2026-09-01T00:00:30Z") == (
                source.eligible_placements(now="2026-09-01T00:00:30Z")
            )


@pytest.mark.parametrize(
    "corruption, expected_error",
    [
        ("attribute-unit", "latency unit"),
        ("missing-region", "active pools require a region"),
        ("inactive-parent", "active source pool references an inactive tier"),
        ("inactive-placement", "source placement references an inactive tier"),
        ("invalid-active", "active must be a SQLite boolean"),
        ("partial-schema", "source pool topology schema is incomplete"),
    ],
)
def test_import_rejects_invalid_topology_and_rolls_back(
    tmp_path: Path, corruption: str, expected_error: str,
) -> None:
    source_path = tmp_path / "corrupt-topology.db"
    with SQLCatalog(source_path) as source:
        source.upsert("bucket", "object", size=3, tier="hot")
        source.register_pool("pool-a", "hot", region="west", members=("node-a",))
        source.assign_pool("bucket", "object", "pool-a")
        with source.engine.begin() as connection:
            if corruption == "attribute-unit":
                value = {
                    "latency": {
                        "value": 4.5, "unit": "seconds", "source": "probe-west",
                        "kind": "measured", "observed_at": "2026-09-01T00:00:00Z",
                        "max_age_seconds": 60,
                    }
                }
                connection.exec_driver_sql(
                    "UPDATE pools SET attributes = ?", (json.dumps(value),)
                )
            elif corruption == "missing-region":
                connection.exec_driver_sql("UPDATE pools SET region = NULL")
            elif corruption == "invalid-active":
                connection.exec_driver_sql("UPDATE tiers SET active = 7")
            elif corruption == "partial-schema":
                connection.exec_driver_sql("ALTER TABLE pools DROP COLUMN attributes")
            else:
                connection.exec_driver_sql("UPDATE tiers SET active = 0")
                if corruption == "inactive-placement":
                    connection.exec_driver_sql("UPDATE pools SET active = 0")

    with SQLCatalog(tmp_path / "imported.db") as destination:
        with pytest.raises(SQLiteCatalogImportError, match=expected_error):
            import_sqlite_catalog(source_path, destination)
        assert destination.list_tiers() == []
        assert destination.list_pools() == []
        assert destination.get("bucket", "object") is None
