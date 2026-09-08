"""One tier/pool contract shared by every catalog implementation."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Barrier

import pytest

from cognistore.core.catalog import CatalogStore
from cognistore.core.content_identity import ContentIdentityBuilder
from cognistore.core.move_jobs import MoveJobState
from cognistore.core.topology import AttributeValue, PlacementConstraints

NOW = "2026-09-08T12:00:00Z"
LEASE = "2026-09-08T12:01:00Z"


def register_pool(
    catalog: CatalogStore,
    pool_id: str = "hot-west",
    tier: str = "hot",
    *,
    region: str = "us-west-2",
    localities: tuple[str, ...] = ("us", "west"),
    active: bool = True,
) -> None:
    catalog.register_tier(tier)
    catalog.register_pool(
        pool_id,
        tier,
        region=region,
        members=(f"device:{pool_id}",),
        localities=localities,
        active=active,
    )


def race(*operations: Callable[[], None]) -> list[type[Exception] | None]:
    """Start lifecycle operations together and expose only contract failures."""
    barrier = Barrier(len(operations))

    def run(operation: Callable[[], None]) -> type[Exception] | None:
        barrier.wait(timeout=10)
        try:
            operation()
        except (ValueError, KeyError) as error:
            return type(error)
        return None

    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        futures = [executor.submit(run, operation) for operation in operations]
        return [future.result(timeout=30) for future in futures]


class TopologyStoreConformance:
    @pytest.fixture
    def catalog(self) -> CatalogStore:
        raise NotImplementedError("concrete tests must supply a fresh catalog")

    def test_registration_lookup_listing_and_membership(
        self, catalog: CatalogStore
    ) -> None:
        assert catalog.get_tier("missing") is None
        assert catalog.get_pool("missing") is None
        assert catalog.list_tiers() == []
        assert catalog.list_pools() == []
        for pool_id, tier in (("z-pool", "hot"), ("a-pool", "warm"), ("m-pool", "hot")):
            register_pool(catalog, pool_id, tier)
        assert [tier.name for tier in catalog.list_tiers()] == ["hot", "warm"]
        assert [pool.pool_id for pool in catalog.list_pools()] == ["a-pool", "m-pool", "z-pool"]
        assert [pool.pool_id for pool in catalog.list_pools(tier="hot")] == ["m-pool", "z-pool"]
        assert catalog.list_pools(tier="missing") == []
        pool = catalog.get_pool("m-pool")
        assert pool is not None
        assert pool.members == ("device:m-pool",)
        assert pool.region == "us-west-2"
        assert set(pool.localities) == {"us", "west"}
        catalog.register_pool(
            "m-pool", "hot", region="us-west-1", members=("device:new", "device:other")
        )
        updated = catalog.get_pool("m-pool")
        assert updated is not None
        assert updated.region == "us-west-1"
        assert set(updated.members) == {"device:new", "device:other"}

    def test_active_pools_require_complete_topology_and_an_active_tier(
        self, catalog: CatalogStore
    ) -> None:
        with pytest.raises(KeyError):
            catalog.register_pool("missing-parent", "missing", region="us", members=("a",))
        assert catalog.get_tier("missing") is None
        catalog.register_tier("inactive", active=False)
        with pytest.raises(ValueError):
            catalog.register_pool("inactive-parent", "inactive", region="us", members=("a",))
        catalog.register_tier("hot")
        for region, members in ((None, ()), ("us", ()), (None, ("a",))):
            with pytest.raises(ValueError):
                catalog.register_pool("incomplete", "hot", region=region, members=members)
        assert catalog.get_pool("incomplete") is None
        catalog.register_pool("legacy", "hot", active=False)
        legacy = catalog.get_pool("legacy")
        assert legacy is not None
        assert not legacy.active
        assert catalog.eligible_placements(now=NOW) == []
        catalog.register_pool("legacy", "hot", region="us", members=("a",))
        assert [candidate.pool.pool_id for candidate in catalog.eligible_placements(now=NOW)] == [
            "legacy"
        ]

    def test_tier_registration_preserves_omitted_metadata_and_clears_explicit_empty_metadata(
        self, catalog: CatalogStore
    ) -> None:
        catalog.register_tier("hot", {"owner": "storage", "nested": {"labels": ["retained"]}})
        expected = catalog.get_tier("hot")
        catalog.register_tier("hot")
        assert catalog.get_tier("hot") == expected
        catalog.register_tier("hot", None, active=False)
        inactive = catalog.get_tier("hot")
        assert inactive is not None and expected is not None
        assert inactive.metadata == expected.metadata
        assert not inactive.active
        catalog.register_tier("hot", {})
        cleared = catalog.get_tier("hot")
        assert cleared is not None
        assert cleared.metadata == {}
        assert cleared.active

    def test_registration_and_reads_have_independent_nested_metadata(
        self, catalog: CatalogStore
    ) -> None:
        metadata = {"nested": {"labels": ["original"]}}
        members = ["device:a"]
        catalog.register_tier("hot", metadata)
        catalog.register_pool("pool", "hot", metadata, region="us", members=members)
        metadata["nested"]["labels"].append("input-mutated")
        members.append("device:input-mutated")
        tier = catalog.get_tier("hot")
        pool = catalog.get_pool("pool")
        assert tier is not None and pool is not None
        expected = {"nested": {"labels": ["original"]}}
        assert tier.metadata == pool.metadata == expected
        assert pool.members == ("device:a",)
        tier.metadata["nested"]["labels"].append("snapshot-mutated")
        pool.metadata["nested"]["labels"].append("snapshot-mutated")
        assert catalog.get_tier("hot").metadata == expected
        assert catalog.get_pool("pool").metadata == expected
        first = catalog.eligible_placements(now=NOW)
        second = catalog.eligible_placements(now=NOW)
        first[0].pool.metadata["nested"]["labels"].append("candidate-mutated")
        first[0].tier.metadata["nested"]["labels"].append("candidate-mutated")
        assert second[0].pool.metadata == second[0].tier.metadata == expected
        assert catalog.list_tiers()[0].metadata == expected
        assert catalog.list_pools()[0].metadata == expected

    def test_pool_assignment_reassignment_and_clear_preserve_object_content(
        self, catalog: CatalogStore
    ) -> None:
        register_pool(catalog)
        register_pool(catalog, "hot-east", region="us-east-1")
        register_pool(catalog, "warm-east", "warm", region="us-east-1")
        content = ContentIdentityBuilder(chunk_size=4).build(BytesIO(b"abcdefgh"), expected_size=8)
        fence = catalog.capture_scan_fence("bucket", "object")
        assert catalog.upsert_scan_observation(
            "bucket", "object", size=8, tier="hot", generation="v1",
            metadata={"business": "retained"}, fence=fence, content=content,
        )
        original = catalog.get("bucket", "object")
        assert original is not None and original.pool_id is None
        for target, tier in (
            ("hot-west", "hot"), ("hot-east", "hot"), ("warm-east", "warm"), (None, "warm")
        ):
            catalog.assign_pool("bucket", "object", target)
            record = catalog.get("bucket", "object")
            assert record is not None
            assert (record.tier, record.pool_id, record.size) == (tier, target, 8)
            assert record.metadata == original.metadata
            assert catalog.get_object_content("bucket", "object") == content
            assert catalog.list("bucket")[0].pool_id == target
            assert catalog.list_page("bucket")[0].pool_id == target
            assert next(catalog.iter_objects()).pool_id == target

    def test_assignment_failures_are_atomic(self, catalog: CatalogStore) -> None:
        register_pool(catalog)
        register_pool(catalog, "disabled", active=False)
        catalog.upsert("bucket", "object", size=1, tier="hot", metadata={"keep": True})
        catalog.assign_pool("bucket", "object", "hot-west")
        before = catalog.get("bucket", "object")
        with pytest.raises(KeyError):
            catalog.assign_pool("bucket", "missing", "hot-west")
        with pytest.raises(KeyError):
            catalog.assign_pool("bucket", "missing", None)
        with pytest.raises(KeyError):
            catalog.assign_pool("bucket", "object", "unknown")
        with pytest.raises(ValueError):
            catalog.assign_pool("bucket", "object", "disabled")
        assert catalog.get("bucket", "object") == before

    @pytest.mark.parametrize("operation", ["upsert", "update", "placement", "scan", "move"])
    @pytest.mark.parametrize("target", ["hot", "warm"])
    def test_existing_object_writes_preserve_same_tier_pool_and_clear_on_tier_change(
        self, catalog: CatalogStore, operation: str, target: str
    ) -> None:
        register_pool(catalog)
        catalog.upsert("bucket", "object", size=1, tier="hot")
        catalog.assign_pool("bucket", "object", "hot-west")
        if operation == "upsert":
            catalog.upsert("bucket", "object", size=2, tier=target)
        elif operation == "update":
            catalog.update_placement("bucket", "object", target)
        elif operation == "placement":
            catalog.upsert_placement("bucket", "object", size=2, tier=target, checksum="digest")
        elif operation == "scan":
            fence = catalog.capture_scan_fence("bucket", "object")
            assert catalog.upsert_scan_observation(
                "bucket", "object", size=2, tier=target, generation="v2", metadata={}, fence=fence
            )
        else:
            catalog.claim_move_job(
                "move", src_tier="hot", dst_tier=target, bucket="bucket", key="object",
                expected_size=1, source_metadata={}, owner_id="owner", now=NOW,
                lease_expires_at=LEASE,
            )
            for previous, following in (
                (MoveJobState.PREPARED, MoveJobState.TRANSFERRED),
                (MoveJobState.TRANSFERRED, MoveJobState.VERIFIED),
            ):
                catalog.transition_move_job(
                    "move", owner_id="owner", expected_state=previous, to_state=following,
                    reason="test transfer", now=NOW, lease_expires_at=LEASE,
                )
            catalog.commit_move_job_placement(
                "move", owner_id="owner", size=1, tier=target, checksum="digest", now=NOW,
                lease_expires_at=LEASE,
            )
        record = catalog.get("bucket", "object")
        assert record is not None
        assert record.tier == target
        assert record.pool_id == ("hot-west" if target == "hot" else None)

    def test_lifecycle_protects_live_references_and_allows_ordered_cleanup(
        self, catalog: CatalogStore
    ) -> None:
        register_pool(catalog)
        catalog.register_tier("warm")
        catalog.upsert("bucket", "object", size=1, tier="hot")
        catalog.assign_pool("bucket", "object", "hot-west")
        before = catalog.get_pool("hot-west")
        operations = (
            lambda: catalog.delete_pool("hot-west"),
            lambda: catalog.delete_tier("hot"),
            lambda: catalog.register_tier("hot", active=False),
            lambda: catalog.register_pool(
                "hot-west", "hot", region="us", members=("a",), active=False
            ),
            lambda: catalog.register_pool("hot-west", "warm", region="us", members=("a",)),
        )
        for operation in operations:
            with pytest.raises(ValueError):
                operation()
        assert catalog.get_pool("hot-west") == before
        catalog.assign_pool("bucket", "object", None)
        catalog.register_pool("hot-west", "warm", region="us", members=("a",))
        with pytest.raises(ValueError):
            catalog.register_tier("hot", active=False)
        catalog.delete("bucket", "object")
        catalog.delete_tier("hot")
        with pytest.raises(ValueError):
            catalog.register_tier("warm", active=False)
        catalog.register_pool("hot-west", "warm", active=False)
        catalog.register_tier("warm", active=False)
        with pytest.raises(ValueError):
            catalog.delete_tier("warm")
        catalog.delete_pool("hot-west")
        catalog.delete_pool("hot-west")
        catalog.delete_tier("warm")
        catalog.delete_tier("warm")
        assert catalog.list_pools() == catalog.list_tiers() == []

    @pytest.mark.parametrize("operation", ["upsert", "update", "placement", "scan"])
    def test_object_writes_reject_inactive_tiers(self, catalog: CatalogStore, operation: str) -> None:
        catalog.upsert("bucket", "object", size=1, tier="hot")
        catalog.register_tier("retired", active=False)
        before = catalog.get("bucket", "object")
        with pytest.raises(ValueError):
            if operation == "upsert":
                catalog.upsert("bucket", "object", size=2, tier="retired")
            elif operation == "update":
                catalog.update_placement("bucket", "object", "retired")
            elif operation == "placement":
                catalog.upsert_placement("bucket", "object", size=2, tier="retired")
            else:
                fence = catalog.capture_scan_fence("bucket", "object")
                catalog.upsert_scan_observation(
                    "bucket", "object", size=2, tier="retired", generation="v2",
                    metadata={}, fence=fence,
                )
        assert catalog.get("bucket", "object") == before

    @pytest.mark.parametrize("tier", ["", " ", " hot "])
    def test_implicit_tier_registration_rejects_invalid_names_atomically(
        self, catalog: CatalogStore, tier: str
    ) -> None:
        catalog.upsert("bucket", "object", size=1, tier="hot")
        before = catalog.get("bucket", "object")
        with pytest.raises(ValueError):
            catalog.upsert("bucket", "object", size=2, tier=tier)
        assert catalog.get("bucket", "object") == before
        assert [entry.name for entry in catalog.list_tiers()] == ["hot"]

    def test_historical_move_tier_names_survive_topology_deletion(
        self, catalog: CatalogStore
    ) -> None:
        catalog.register_tier("historical-source")
        catalog.register_tier("historical-destination", active=False)
        job = catalog.claim_move_job(
            "history", src_tier="historical-source", dst_tier="historical-destination",
            bucket="bucket", key="object", expected_size=1, source_metadata={},
            owner_id="owner", now=NOW, lease_expires_at=LEASE,
        )
        catalog.delete_tier("historical-source")
        catalog.delete_tier("historical-destination")
        assert catalog.list_tiers() == []
        assert catalog.get_move_job("history") == job
        assert catalog.list_move_jobs() == [job]
        assert len(catalog.list_move_job_transitions("history")) == 1

    def test_hard_constraints_intersect_before_attributes_or_scoring(
        self, catalog: CatalogStore
    ) -> None:
        register_pool(catalog, "z-west", "hot")
        register_pool(catalog, "a-east", "hot", region="us-east-1", localities=("us", "east"))
        register_pool(catalog, "a-west", "warm")
        register_pool(catalog, "b-west", "warm", localities=("us",))
        register_pool(catalog, "inactive", "hot", active=False)
        assert [(candidate.tier.name, candidate.pool.pool_id)
                for candidate in catalog.eligible_placements(now=NOW)] == [
            ("hot", "a-east"), ("hot", "z-west"), ("warm", "a-west"), ("warm", "b-west")
        ]
        constraints = PlacementConstraints(
            allowed_regions=("us-west-2",), required_localities=("us", "west"),
            allowed_tiers=("warm",), allowed_pools=("a-west", "b-west", "a-east"),
        )
        assert [candidate.pool.pool_id for candidate in catalog.eligible_placements(
            constraints, now=NOW
        )] == ["a-west"]
        for constraints in (
            PlacementConstraints(allowed_regions=()),
            PlacementConstraints(allowed_tiers=()),
            PlacementConstraints(allowed_pools=()),
            PlacementConstraints(required_localities=("missing",)),
        ):
            assert catalog.eligible_placements(constraints, now=NOW) == []

    def test_missing_stale_and_future_attributes_are_explicit(
        self, catalog: CatalogStore
    ) -> None:
        catalog.register_tier("hot")
        catalog.register_pool(
            "pool", "hot", region="us", members=("a",), attributes={
                "latency": AttributeValue(
                    2.5, "ms", "probe", "measured", "2026-09-08T11:59:00Z", 60
                ),
                "capacity": AttributeValue(
                    1024, "bytes", "device", "measured", "2026-09-08T11:58:59Z", 60
                ),
                "price": AttributeValue(
                    0.02, "USD/GiB-month", "config", "configured", "2026-09-08T12:00:01Z", 60
                ),
            },
        )
        candidates = catalog.eligible_placements(now=NOW)
        assert len(candidates) == 1
        assert candidates[0].attribute_states == {
            "latency": "fresh", "capacity": "stale", "price": "future",
            "carbon_intensity": "missing",
        }
        persisted = catalog.get_pool("pool")
        assert persisted is not None
        assert persisted.attributes["latency"].unit == "ms"
        assert persisted.attributes["latency"].source == "probe"
        assert persisted.attributes["capacity"].max_age_seconds == 60

    def test_concurrent_pool_assignment_and_reparent_preserve_integrity(
        self, catalog: CatalogStore
    ) -> None:
        register_pool(catalog)
        catalog.register_tier("warm")
        catalog.upsert("bucket", "object", size=1, tier="hot")
        outcomes = race(
            lambda: catalog.assign_pool("bucket", "object", "hot-west"),
            lambda: catalog.register_pool("hot-west", "warm", region="us", members=("a",)),
        )
        assert outcomes[0] is None
        record = catalog.get("bucket", "object")
        pool = catalog.get_pool("hot-west")
        assert record is not None and pool is not None
        assert record.pool_id == pool.pool_id
        assert record.tier == pool.tier

    def test_concurrent_pool_assignment_and_deletion_preserve_integrity(
        self, catalog: CatalogStore
    ) -> None:
        register_pool(catalog)
        catalog.upsert("bucket", "object", size=1, tier="hot")
        outcomes = race(
            lambda: catalog.assign_pool("bucket", "object", "hot-west"),
            lambda: catalog.delete_pool("hot-west"),
        )
        assert outcomes in ([None, ValueError], [KeyError, None])
        record = catalog.get("bucket", "object")
        pool = catalog.get_pool("hot-west")
        assert record is not None
        assert (record.pool_id is None) == (pool is None)
        if pool is not None:
            assert record.tier == pool.tier

    def test_concurrent_tier_deactivation_and_new_placement_preserve_integrity(
        self, catalog: CatalogStore
    ) -> None:
        catalog.register_tier("hot")
        outcomes = race(
            lambda: catalog.upsert("bucket", "object", size=1, tier="hot"),
            lambda: catalog.register_tier("hot", active=False),
        )
        assert outcomes in ([None, ValueError], [ValueError, None])
        tier = catalog.get_tier("hot")
        assert tier is not None
        assert tier.active == (catalog.get("bucket", "object") is not None)
