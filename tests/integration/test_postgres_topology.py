"""PostgreSQL conformance and transactions spanning independent catalog handles."""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from cognistore.core.catalog import CatalogStore
from cognistore.core.topology import AttributeValue
from cognistore.db import MigrationManager, SQLCatalog
from cognistore.db.schema import object_placements, objects
from tests.conformance.topology_store import (
    LEASE,
    NOW,
    TopologyStoreConformance,
    race,
    register_pool,
)

pytestmark = pytest.mark.integration


class TestPostgresTopologyConformance(TopologyStoreConformance):
    @pytest.fixture
    def catalog(self, postgres_dsn: str) -> Iterator[CatalogStore]:
        with SQLCatalog(postgres_dsn) as catalog:
            yield catalog


def test_separate_connections_racing_pool_reparent_and_assignment(
    postgres_dsn: str,
) -> None:
    with SQLCatalog(postgres_dsn) as first, SQLCatalog(postgres_dsn) as second:
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


def test_separate_connections_racing_tier_deactivation_and_placement(
    postgres_dsn: str,
) -> None:
    with SQLCatalog(postgres_dsn) as first, SQLCatalog(postgres_dsn) as second:
        first.register_tier("hot")
        outcomes = race(
            lambda: first.upsert("bucket", "object", size=1, tier="hot"),
            lambda: second.register_tier("hot", active=False),
        )
        assert outcomes in ([None, ValueError], [ValueError, None])
        tier = first.get_tier("hot")
        assert tier is not None
        assert tier.active == (second.get("bucket", "object") is not None)


def test_separate_connections_racing_assignment_and_tier_change(
    postgres_dsn: str,
) -> None:
    with SQLCatalog(postgres_dsn) as first, SQLCatalog(postgres_dsn) as second:
        register_pool(first)
        first.upsert("bucket", "object", size=1, tier="hot")
        assert race(
            lambda: first.assign_pool("bucket", "object", "hot-west"),
            lambda: second.update_placement("bucket", "object", "warm"),
        ) == [None, None]
        record = first.get("bucket", "object")
        assert record is not None
        assert (record.tier, record.pool_id) in (("hot", "hot-west"), ("warm", None))


def test_postgres_topology_downgrade_and_reupgrade_preserve_placement_identity(
    postgres_dsn: str,
) -> None:
    manager = MigrationManager()
    with SQLCatalog(postgres_dsn) as catalog:
        catalog.register_tier("hot", {"purpose": "interactive"})
        catalog.register_tier("warm")
        catalog.register_pool(
            "pool-a", "hot", {"device": "nvme0"}, region="us-west-2",
            members=("node-a", "node-b"), localities=("us", "west"), attributes={
                "latency": AttributeValue(2.5, "ms", "probe", "measured", NOW, 60),
            },
        )
        catalog.upsert("bucket", "object", size=3, tier="hot", metadata={"owner": "storage"})
        catalog.assign_pool("bucket", "object", "pool-a")
        record = catalog.get("bucket", "object")
        job = catalog.claim_move_job(
            "historical-move", src_tier="retired\0source", dst_tier="retired\0destination",
            bucket="bucket", key="historical-object", expected_size=3, source_metadata={},
            owner_id="worker", now=NOW, lease_expires_at=LEASE,
        )
        transitions = catalog.list_move_job_transitions("historical-move")
        identities = sa.select(objects.c.object_id, object_placements.c.placement_id).select_from(
            objects.join(object_placements)
        )
        with catalog.engine.connect() as connection:
            original_ids = connection.execute(identities).one()

        manager.downgrade(catalog.engine, "0006_embeddings")
        assert manager.current(catalog.engine) == "0006_embeddings"
        assert "region" not in {
            column["name"] for column in sa.inspect(catalog.engine).get_columns("pools")
        }
        for upgraded in (False, True):
            if upgraded:
                manager.upgrade(catalog.engine)
                assert manager.current(catalog.engine) == "0007_tier_pools"
            with catalog.engine.connect() as connection:
                assert connection.execute(identities).one() == original_ids
                assert connection.execute(sa.select(
                    object_placements.c.tier_name, object_placements.c.pool_id
                )).one() == ("hot", "pool-a")
            # Both tiers exist, so only the composite pool/tier FK rejects this
            # mismatch. Verify that it still protects placements at each revision.
            with pytest.raises(sa.exc.IntegrityError) as error:
                with catalog.engine.begin() as connection:
                    connection.execute(sa.update(object_placements).values(tier_name="warm"))
            assert error.value.orig.diag.constraint_name == "fk_object_placements_pool_tier"

        assert catalog.get("bucket", "object") == record
        pool = catalog.get_pool("pool-a")
        assert pool is not None
        assert (pool.active, pool.region, pool.members, pool.localities, pool.attributes) == (
            False, None, (), (), {},
        )
        assert pool.metadata == {"device": "nvme0"}
        assert catalog.get_tier("hot").metadata == {"purpose": "interactive"}
        assert catalog.eligible_placements(now=NOW) == []
        assert catalog.get_move_job("historical-move") == job
        assert catalog.list_move_job_transitions("historical-move") == transitions
        assert catalog.get_tier(job.src_tier) is None
        assert catalog.get_tier(job.dst_tier) is None
