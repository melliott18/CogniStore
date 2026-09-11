from __future__ import annotations

import json
from dataclasses import replace
from decimal import ROUND_DOWN, Decimal, localcontext

import pytest

from cognistore.core.budgets import (
    BudgetDefinition,
    BudgetOverride,
    ObjectiveWeights,
    active_budget_definitions,
    estimate_budget_charge,
    evaluate_budget,
    score_candidates,
)
from cognistore.core.catalog import ObjectRecord
from cognistore.core.estimation import (
    RATE_UNITS,
    EstimationProfile,
    EstimationWorkload,
    RateAssumption,
    StorageImpactEstimator,
)
from cognistore.core.topology import AttributeValue, Pool

NOW = "2026-09-01T00:00:00Z"
END = "2026-10-01T10:00:00Z"  # Exactly 730 hours after NOW.
GIB = 1073741824


def profile(prices=(0, 0, 0, 0, 0), energies=(0, 0, 0, 0, 0), intensity=10, *, bounded=False):
    values = dict(zip((name for name in RATE_UNITS if name.endswith("price")), prices))
    values.update(zip((name for name in RATE_UNITS if name.endswith("energy")), energies))
    values["carbon_intensity"] = intensity
    return EstimationProfile(
        version="test-v1",
        backend="posix",
        rates={
            name: RateAssumption(
                value=value, unit=RATE_UNITS[name], source="test", source_version="v1",
                effective_from=NOW, effective_until="2027-01-01T00:00:00Z",
                lower=0 if bounded else None, upper=value * 2 if bounded else None,
            ) for name, value in values.items()
        },
    )


def pool(pool_id="hot-pool", tier="hot", **kwargs):
    return Pool(pool_id=pool_id, tier=tier, region=kwargs.pop("region", "us-west-2"),
                members=("member",), **kwargs)


def workload(**kwargs):
    return EstimationWorkload(**{
        "read_requests": 0, "write_requests": 0, "transfer_bytes": 0,
        "retrieval_bytes": 0, **kwargs,
    })


def budget(**kwargs):
    return BudgetDefinition(**{
        "budget_id": "september", "period_start": NOW, "period_end": END,
        "cost_limit_usd": "100", "carbon_limit_gco2e": "1000",
        "tier_pools": {"hot": "hot-pool", "cold": "cold-pool"},
        "profiles": {"hot-pool": profile(), "cold-pool": profile()},
        "workload": workload(), **kwargs,
    })


def charge(definition=None, record=None, *, pools=None, as_of=NOW, dst_tier="cold"):
    return estimate_budget_charge(
        definition or budget(),
        record or ObjectRecord("bucket", "prefix/file", GIB, "hot"), dst_tier,
        pools or {"hot-pool": pool(), "cold-pool": pool("cold-pool", "cold")},
        as_of=as_of,
    )


def test_definition_round_trip_detaches_assumptions_and_normalizes_utc():
    bindings = {"hot": "hot-pool"}
    definition = budget(tier_pools=bindings, period_start="2026-08-31T17:00:00-07:00")
    bindings["hot"] = "another-pool"
    assert definition.tier_pools["hot"] == "hot-pool"
    assert definition.period_start == "2026-09-01T00:00:00.000000Z"
    with pytest.raises(TypeError):
        definition.tier_pools["hot"] = "another-pool"
    restored = BudgetDefinition.from_mapping(json.loads(json.dumps(definition.to_dict())))
    assert restored == definition
    assert restored.to_dict() == definition.to_dict()


@pytest.mark.parametrize("changes", [
    {"cost_limit_usd": None, "carbon_limit_gco2e": None},
    {"cost_limit_usd": "NaN"}, {"carbon_limit_gco2e": -1}, {"opening_cost_usd": True},
    {"opening_cost_usd": None},
    {"period_end": NOW}, {"period_start": "2026-09-01"}, {"version": True},
    {"tier_pools": {}}, {"workload": EstimationWorkload()},
])
def test_definition_rejects_ambiguous_or_invalid_inputs(changes):
    with pytest.raises(ValueError):
        budget(**changes)


def test_scope_is_literal_and_period_half_open():
    definition = budget(bucket="bucket", prefix="prefix/")
    assert definition.matches("bucket", "prefix/file")
    assert not definition.matches("bucket", "prefix-other/file")
    assert not definition.matches("another", "prefix/file")
    assert definition.active_at(NOW)
    assert not definition.active_at(END)
    assert budget().matches("any", "anything")


def test_exact_scope_renewal_does_not_mask_expired_narrower_scope():
    old = budget(budget_id="old", period_start="2026-08-01T00:00:00Z", period_end=NOW)
    new = budget(budget_id="new")
    narrow = replace(old, budget_id="narrow-old", bucket="bucket", prefix="prefix/")
    definitions = active_budget_definitions([old, new, narrow], "bucket", "prefix/file", as_of=NOW)
    assert [item.budget_id for item in definitions] == ["narrow-old", "new"]
    renewed = replace(new, budget_id="narrow-new", bucket="bucket", prefix="prefix/")
    definitions = active_budget_definitions(
        [old, new, narrow, renewed], "bucket", "prefix/file", as_of=NOW,
    )
    assert [item.budget_id for item in definitions] == ["narrow-new", "new"]


@pytest.mark.parametrize("bounded,cost,carbon", [(False, "98", "1060"), (True, "196", "4240")])
def test_charge_accounts_for_full_destination_and_both_sides_of_migration(bounded, cost, carbon):
    definition = budget(
        profiles={
            "hot-pool": profile((5, 2, 3, 4, 5), (1, 2, 3, 4, 5), bounded=bounded),
            "cold-pool": profile((10, 1, 2, 3, 4), (1, 2, 3, 4, 5), bounded=bounded),
        },
        workload=workload(read_requests=2, write_requests=3, transfer_bytes=GIB,
                          retrieval_bytes=GIB, stored_bytes=1, duration_hours=1),
    )
    result = charge(definition)
    assert result["cost_usd"] == cost
    assert result["carbon_gco2e"] == carbon
    assumptions = result["assumptions"]
    assert assumptions["remaining_hours"] == "730"
    assert assumptions["source_savings_credited"] is False
    for role, reads in (("source_migration", 4), ("destination_migration", 3)):
        migration = assumptions["estimates"][role]["workload"]
        assert migration["read_requests"] == str(reads)
        assert migration["write_requests"] == "1"
        assert migration["transfer_bytes"] == str(reads * GIB)
        assert migration["retrieval_bytes"] == str(reads * GIB)
    holding = assumptions["estimates"]["destination_holding"]
    assert holding["workload"]["stored_bytes"] == str(GIB)
    assert holding["workload"]["duration_hours"] == "730"
    assert assumptions["amount_basis"]["cost_usd"]["destination_holding"] == (
        "upper_bound" if bounded else "nominal_unquantified"
    )
    with localcontext() as ctx:
        ctx.prec = 6
        ctx.rounding = ROUND_DOWN
        assert charge(definition) == result


def test_staying_put_has_no_migration_charge_and_remaining_horizon_decreases():
    definition = budget(profiles={"hot-pool": profile((730, 10, 10, 10, 10))})
    full = charge(definition, dst_tier="hot")
    later = charge(definition, dst_tier="hot", as_of="2026-09-02T00:00:00Z")
    assert full["cost_usd"] == "730"
    assert later["cost_usd"] == "706"
    assert list(full["assumptions"]["estimates"]) == ["destination_holding"]


@pytest.mark.parametrize("kind", ["missing", "stale", "future"])
def test_unavailable_rate_is_never_a_zero_even_with_zero_quantity(kind):
    rates = dict(profile().rates)
    if kind == "missing":
        del rates["storage_price"]
    elif kind == "stale":
        rates["storage_price"] = replace(
            rates["storage_price"], effective_from="2026-08-01T00:00:00Z",
            effective_until="2026-08-31T00:00:00Z",
        )
    else:
        rates["storage_price"] = replace(
            rates["storage_price"], effective_from="2026-09-02T00:00:00Z",
        )
    result = charge(budget(profiles={
        "hot-pool": profile(), "cold-pool": EstimationProfile("test", "posix", rates),
    }), record=ObjectRecord("bucket", "key", 0, "hot"))
    assert result["cost_usd"] is None
    assert any("storage_price:" + kind in reason for reason in result["reasons"])


@pytest.mark.parametrize("kind", ["missing", "wrong-tier", "inactive", "record-mismatch"])
def test_pool_binding_ambiguity_fails_closed(kind):
    pools = {"hot-pool": pool(), "cold-pool": pool("cold-pool", "cold")}
    record = ObjectRecord("bucket", "key", GIB, "hot")
    if kind == "missing":
        del pools["cold-pool"]
    elif kind == "wrong-tier":
        pools["cold-pool"] = pool("cold-pool", "hot")
    elif kind == "inactive":
        pools["hot-pool"] = pool(active=False)
    else:
        record.pool_id = "another-pool"
    result = charge(pools=pools, record=record)
    assert result["cost_usd"] is None
    assert result["carbon_gco2e"] is None
    assert result["reasons"]


def test_budget_limits_include_opening_commitments_and_existing_reservations():
    definition = budget(opening_cost_usd=10, opening_carbon_gco2e=100)
    reservations = [{"cost_usd": "20", "carbon_gco2e": "200"}]
    result = evaluate_budget(definition, {"cost_usd": "70", "carbon_gco2e": "700"},
                             reservations, as_of=NOW)
    assert result["allowed"]
    assert result["before"] == {"cost_usd": "30", "carbon_gco2e": "300"}
    assert result["after"] == {"cost_usd": "100", "carbon_gco2e": "1000"}
    denied = evaluate_budget(definition, {"cost_usd": "70.01", "carbon_gco2e": "701"},
                             reservations, as_of=NOW)
    assert not denied["allowed"]
    assert denied["binding_constraints"] == ["cost_usd_limit", "carbon_gco2e_limit"]


def test_explicit_override_is_auditable_and_only_bypasses_numeric_limits():
    exception = BudgetOverride.from_mapping({"actor_id": "operator@example", "reason": "recovery"})
    definition = budget(cost_limit_usd=0)
    result = evaluate_budget(definition, {"cost_usd": "1", "carbon_gco2e": "0"}, [],
                             as_of=NOW, override=exception)
    assert result["allowed"] and result["override_applied"]
    assert result["override"] == exception.to_dict()
    assert result["binding_constraints"] == ["cost_usd_limit"]
    unavailable = evaluate_budget(definition, {"cost_usd": None, "carbon_gco2e": "0"}, [],
                                  as_of=NOW, override=exception)
    assert not unavailable["allowed"]
    assert not unavailable["override_applied"]
    assert unavailable["after"]["cost_usd"] is None
    with pytest.raises(ValueError):
        BudgetOverride("", "reason")
    with pytest.raises(ValueError):
        BudgetOverride("operator", " ")


@pytest.mark.parametrize("as_of,reason", [
    ("2026-08-31T23:59:59Z", "budget_not_started"), (END, "budget_expired"),
])
def test_invalid_budget_period_cannot_be_overridden(as_of, reason):
    result = evaluate_budget(budget(), {"cost_usd": "0", "carbon_gco2e": "0"}, [],
                             as_of=as_of, override=BudgetOverride("operator", "recovery"))
    assert not result["allowed"]
    assert reason in result["binding_constraints"]


def test_unknown_prior_usage_blocks_only_its_constrained_dimension():
    usage = {"charge": {"cost_usd": "0", "carbon_gco2e": None}}
    result = evaluate_budget(budget(), {"cost_usd": "0", "carbon_gco2e": "0"}, [usage],
                             as_of=NOW)
    assert not result["allowed"]
    assert result["binding_constraints"] == ["carbon_gco2e_unavailable"]
    cost_only = evaluate_budget(budget(carbon_limit_gco2e=None),
                                {"cost_usd": "0", "carbon_gco2e": None}, [usage], as_of=NOW)
    assert cost_only["allowed"]


def candidates():
    pools = {
        "cheap": pool("cheap", "cold", region="us-east-1", attributes={
            "latency": AttributeValue(100, "ms", "test", "measured", NOW, 86400),
        }),
        "clean": pool("clean", "hot", localities=("near",), attributes={
            "latency": AttributeValue(1, "ms", "test", "measured", NOW, 86400),
        }),
    }
    estimator = StorageImpactEstimator({
        "cheap": profile((1, 0, 0, 0, 0), (10, 0, 0, 0, 0)),
        "clean": profile((10, 0, 0, 0, 0), (1, 0, 0, 0, 0)),
    })
    estimates = [estimator.estimate(item, workload(stored_bytes=GIB, duration_hours=730), as_of=NOW)
                 for item in pools.values()]
    return estimates, pools


@pytest.mark.parametrize("weights,winner", [
    ({"cost": 1}, "cheap"),
    ({"cost": 0, "carbon": 1}, "clean"),
    ({"cost": 0, "latency": 1}, "clean"),
    ({"cost": 0, "locality": 1}, "clean"),
    ({"cost": 4, "carbon": 1, "latency": 1, "locality": 1}, "cheap"),
    ({"cost": 1, "carbon": 1, "latency": 1, "locality": 1}, "clean"),
])
def test_competing_objectives_change_placement_with_replayable_evidence(weights, winner):
    estimates, pools = candidates()
    result = score_candidates(estimates, pools, ObjectiveWeights(**weights), as_of=NOW,
                              preferred_region="us-west-2", preferred_localities=("near",))
    assert result[0]["pool_id"] == winner
    assert all(row["score"] is not None for row in result)
    assert result[0]["objectives"].keys() == {"cost", "carbon", "latency", "locality"}
    assert result[0]["normalized"].keys() == result[0]["objectives"].keys()


def test_missing_soft_objective_blocks_only_when_weighted():
    estimates, pools = candidates()
    no_locality = score_candidates(estimates, pools, ObjectiveWeights(cost=0, locality=1), as_of=NOW)
    assert all(row["score"] is None for row in no_locality)
    assert no_locality[0]["reasons"] == ["objective_locality_unavailable"]
    stale_latency = score_candidates(estimates, pools, ObjectiveWeights(cost=0, latency=1),
                                     as_of="2026-09-03T00:00:00Z")
    assert all(row["score"] is None for row in stale_latency)
    unweighted = score_candidates(estimates, pools, ObjectiveWeights(),
                                  as_of="2026-09-03T00:00:00Z")
    assert all(row["score"] is not None for row in unweighted)


def test_weights_are_validated_serializable_and_ties_are_deterministic():
    weights = ObjectiveWeights.from_mapping({"cost": "1", "carbon": "1"})
    assert weights.to_dict() == {"cost": "1", "carbon": "1", "latency": "0", "locality": "0"}
    for values in ({"cost": 0}, {"cost": "Infinity"}, {"latency": -1}, {"cost": True}):
        with pytest.raises(ValueError):
            ObjectiveWeights(**values)
    estimates, pools = candidates()
    forward = score_candidates(estimates, pools, weights, as_of=NOW)
    backward = score_candidates(list(reversed(estimates)), pools, weights, as_of=NOW)
    assert forward == backward
    assert [row["score"] for row in forward] == ["1", "1"]
    assert [row["pool_id"] for row in forward] == ["cheap", "clean"]
    assert Decimal(forward[0]["objectives"]["cost"]) == 1
