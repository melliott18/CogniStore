from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from cognistore.core.catalog import Catalog
from cognistore.core.topology import (
    ATTRIBUTE_UNITS,
    AttributeValue,
    PlacementConstraints,
    Pool,
    Tier,
    eligible_candidates,
    rank_placements,
)
from cognistore.core.topology_config import (
    TopologyConfig,
    load_topology_config,
    register_topology,
)

OBSERVED = "2026-09-08T12:00:00Z"


def attribute(**changes):
    values = dict(
        value=3.5,
        unit="ms",
        source="probe/p95-read",
        kind="measured",
        observed_at=OBSERVED,
        max_age_seconds=60,
    )
    values.update(changes)
    return AttributeValue(**values)


def pool(**changes):
    values = dict(
        pool_id="west",
        tier="hot",
        region="us-west-2",
        members=("ssd-1",),
        localities=("US", "tenant-a"),
        attributes={"latency": attribute()},
    )
    values.update(changes)
    return Pool(**values)


@pytest.mark.parametrize("field,value", [
    ("value", -1),
    ("value", True),
    ("value", float("nan")),
    ("value", float("inf")),
    ("value", 10**500),
    ("value", "3.5"),
    ("max_age_seconds", 0),
    ("max_age_seconds", -1),
    ("max_age_seconds", True),
    ("max_age_seconds", float("inf")),
    ("kind", "estimated"),
    ("source", ""),
    ("source", " "),
    ("unit", ""),
    ("observed_at", "2026-09-08T12:00:00"),
    ("observed_at", "yesterday"),
])
def test_invalid_attribute_values_are_explicit(field, value):
    with pytest.raises(ValueError):
        attribute(**{field: value})


@pytest.mark.parametrize("name,unit", ATTRIBUTE_UNITS.items())
def test_pool_enforces_each_attribute_unit(name, unit):
    valid = attribute(unit=unit)
    assert pool(attributes={name: valid}).attributes[name] == valid
    with pytest.raises(ValueError, match="unit must be"):
        pool(attributes={name: attribute(unit="seconds")})


@pytest.mark.parametrize("kind", ["configured", "measured"])
def test_freshness_has_explicit_inclusive_expiry_and_future_states(kind):
    value = attribute(kind=kind)
    assert value.freshness(now=OBSERVED) == "fresh"
    assert value.is_fresh(now="2026-09-08T12:01:00Z")
    assert value.freshness(now="2026-09-08T12:01:00.000001Z") == "stale"
    assert not value.is_fresh(now="2026-09-08T12:01:00.000001Z")
    assert value.freshness(now="2026-09-08T11:59:59Z") == "future"


def test_attribute_serialization_normalizes_utc_and_requires_provenance():
    value = attribute(observed_at="2026-09-08T05:00:00-07:00")
    assert value.observed_at == "2026-09-08T12:00:00.000000Z"
    assert AttributeValue.from_mapping(json.loads(json.dumps(value.to_mapping()))) == value
    incomplete = value.to_mapping()
    incomplete.pop("source")
    with pytest.raises(ValueError, match="missing required fields: source"):
        AttributeValue.from_mapping(incomplete)
    with pytest.raises(ValueError, match="unknown fields"):
        AttributeValue.from_mapping({**value.to_mapping(), "fresh": True})


@pytest.mark.parametrize("changes", [
    {"pool_id": ""},
    {"tier": " hot"},
    {"region": None},
    {"region": ""},
    {"members": ()},
    {"members": "ssd-1"},
    {"members": ("ssd-1", "ssd-1")},
    {"localities": ("US", "US")},
    {"active": 1},
    {"attributes": {"unknown": attribute()}},
    {"attributes": {"latency": {"value": 3}}},
])
def test_invalid_topology_cannot_be_active(changes):
    with pytest.raises(ValueError):
        pool(**changes)


def test_incomplete_inactive_legacy_pool_is_explicit_and_ineligible():
    legacy = Pool("old", "hot", active=False, metadata={"legacy": True})
    assert legacy.region is None
    assert not legacy.members
    assert eligible_candidates([Tier("hot")], [legacy], now=OBSERVED) == []
    with pytest.raises(ValueError, match="active pools require"):
        replace(legacy, active=True)


def test_pool_roundtrip_and_model_boundaries_detach_nested_values():
    metadata = {"owner": {"labels": ["tenant-a"]}}
    attributes = {"latency": attribute()}
    value = pool(metadata=metadata, attributes=attributes)
    metadata["owner"]["labels"].append("changed")
    attributes.clear()
    assert value.metadata == {"owner": {"labels": ["tenant-a"]}}
    assert value.attributes["latency"].value == 3.5
    serialized = value.to_mapping()
    assert Pool.from_mapping(json.loads(json.dumps(serialized))) == value
    serialized["metadata"]["owner"]["labels"].append("changed-again")
    assert value.metadata == {"owner": {"labels": ["tenant-a"]}}
    assert deepcopy(value) == value


def test_names_preserve_existing_catalog_nul_identifiers():
    tier = Tier("hot\x00archive")
    value = pool(tier=tier.name, pool_id="pool\x00one")
    assert Pool.from_mapping(value.to_mapping()) == value


@pytest.mark.parametrize("constraints", [
    PlacementConstraints(allowed_regions=()),
    PlacementConstraints(allowed_tiers=()),
    PlacementConstraints(allowed_pools=()),
    PlacementConstraints(required_localities=("US", "tenant-b")),
])
def test_empty_allowlist_and_all_required_localities_fail_closed(constraints):
    assert eligible_candidates([Tier("hot")], [pool()], constraints, now=OBSERVED) == []


def test_eligibility_keeps_stale_evidence_explicit_and_returns_detached_candidates():
    tier = Tier("hot", {"nested": {"items": ["original"]}})
    west = pool(metadata={"nested": {"items": ["original"]}})
    candidates = eligible_candidates([tier], [west], now="2026-09-08T12:02:00Z")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.attribute_states == {
        "latency": "stale",
        "capacity": "missing",
        "price": "missing",
        "carbon_intensity": "missing",
    }
    candidate.tier.metadata["nested"]["items"].append("changed")
    candidate.pool.metadata["nested"]["items"].append("changed")
    assert tier.metadata["nested"]["items"] == ["original"]
    assert west.metadata["nested"]["items"] == ["original"]


def test_ranking_filters_hard_constraints_before_invoking_score():
    catalog = Catalog()
    register_topology(catalog, TopologyConfig(
        tiers=(Tier("hot"), Tier("warm")),
        pools=(
            pool(pool_id="hot-europe", region="eu-west-1", localities=("EU",)),
            pool(pool_id="hot-west"),
            pool(pool_id="warm-west", tier="warm"),
        ),
    ))
    scored = []

    def score(candidate):
        scored.append(candidate.pool.pool_id)
        return -10 if candidate.tier.name == "warm" else 3.5

    candidates = rank_placements(
        catalog,
        PlacementConstraints(allowed_regions=("us-west-2",), required_localities=("US",)),
        score,
        now=OBSERVED,
    )
    assert scored == ["hot-west", "warm-west"]
    assert [candidate.pool.pool_id for candidate in candidates] == ["warm-west", "hot-west"]


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "3", 10**500])
def test_ranking_requires_finite_numeric_scores(value):
    catalog = Catalog()
    register_topology(catalog, TopologyConfig((Tier("hot"),), (pool(),)))
    with pytest.raises(ValueError, match="placement score must be a finite number"):
        rank_placements(catalog, None, lambda candidate: value, now=OBSERVED)


def test_configuration_examples_are_equivalent_and_register_multiple_pools_per_tier():
    examples = Path(__file__).resolve().parents[2] / "examples"
    yaml_config = load_topology_config(examples / "tier_pools.yaml")
    json_config = load_topology_config(examples / "tier_pools.json")
    assert yaml_config == json_config
    catalog = Catalog()
    register_topology(catalog, yaml_config)
    assert [value.pool_id for value in catalog.list_pools(tier="hot")] == [
        "hot-europe", "hot-west",
    ]
    assert len(catalog.list_pools()) == 3


@pytest.mark.parametrize("mutation", [
    lambda config: config["pools"].append(pool(pool_id="second", tier="unknown").to_mapping()),
    lambda config: config["pools"].append(pool().to_mapping()),
    lambda config: config["tiers"].append(Tier("hot").to_mapping()),
    lambda config: config["tiers"][0].update(active=False),
    lambda config: config["pools"][0]["attributes"]["latency"].update(unit="seconds"),
    lambda config: config.update(typo=True),
])
def test_all_configuration_validation_finishes_before_first_registration(mutation):
    config = TopologyConfig((Tier("hot"),), (pool(),)).to_mapping()
    mutation(config)
    catalog = Catalog()
    with patch.object(catalog, "register_tier", wraps=catalog.register_tier) as register:
        with pytest.raises(ValueError):
            register_topology(catalog, config)
        register.assert_not_called()
    assert catalog.list_tiers() == []


def test_inactive_pool_under_inactive_tier_is_rejected_before_registration():
    with pytest.raises(ValueError, match="requires an active tier for registration"):
        TopologyConfig((Tier("hot", active=False),), (Pool("old", "hot", active=False),))


def test_configuration_revalidates_mutated_model_attributes_before_writes():
    topology = TopologyConfig((Tier("hot"),), (pool(),))
    topology.pools[0].attributes["latency"] = attribute(unit="seconds")
    catalog = Catalog()
    with pytest.raises(ValueError, match="latency unit must be"):
        register_topology(catalog, topology)
    assert catalog.list_tiers() == []
