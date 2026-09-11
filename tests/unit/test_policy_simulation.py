from __future__ import annotations

import json
from contextlib import ExitStack
from dataclasses import replace
from decimal import Decimal, localcontext
from unittest.mock import patch

import pytest

from cognistore.core.audit import AuditContext
from cognistore.core.budgets import BudgetDefinition, ObjectiveWeights
from cognistore.core.catalog import Catalog
from cognistore.core.estimation import (
    RATE_UNITS,
    EstimationProfile,
    EstimationWorkload,
    RateAssumption,
    StorageImpactEstimator,
)
from cognistore.core.impact_policy import EstimatePolicy
from cognistore.core.mover import Mover
from cognistore.core.placement_controls import ImportanceTag
from cognistore.core.policy import SimplePolicy
from cognistore.core.policy_runner import PolicyRunner
from cognistore.core.policy_simulation import simulate_policy
from cognistore.core.topology import AttributeValue, PlacementConstraints

AS_OF = "2026-09-01T00:00:00Z"
END = "2026-10-01T10:00:00Z"  # Exactly 730 hours from AS_OF.
GIB = 1073741824


def _profile(cost: str, carbon: str) -> EstimationProfile:
    values = {name: "0" for name in RATE_UNITS}
    values.update(storage_price=cost, storage_energy=carbon, carbon_intensity="1")
    return EstimationProfile(
        "synthetic-v1", "synthetic",
        rates={name: RateAssumption(
            value, RATE_UNITS[name], "synthetic-fixture", "v1", AS_OF, END,
            lower=value, upper=value,
        ) for name, value in values.items()},
    )


def _setup(*, estimates: bool = False):
    catalog = Catalog()
    for tier, region, latency in (("hot", "west", 1), ("warm", "east", 100)):
        catalog.register_tier(tier)
        catalog.register_pool(
            tier, tier, region=region, members=(tier,), localities=(region,),
            attributes={"latency": AttributeValue(
                latency, "ms", "synthetic-fixture", "configured", AS_OF, 86400,
            )},
        )
    catalog.upsert("bucket", "a", GIB, "hot")
    catalog.assign_pool("bucket", "a", "hot")
    profiles = {"hot": _profile("10", "1"), "warm": _profile("2", "3")}
    estimator = StorageImpactEstimator(profiles)
    workload = EstimationWorkload(
        duration_hours=730, read_requests=0, write_requests=0, transfer_bytes=0, retrieval_bytes=0,
    )
    policy = (
        EstimatePolicy(catalog, estimator, workload, {"hot": "hot", "warm": "warm"})
        if estimates else SimplePolicy(size_threshold=2 * GIB)
    )
    runner = PolicyRunner(
        catalog, {}, Mover({}, catalog), policy,
        allowed_tiers=("hot", "warm"), simulation_only=True,
    )
    return catalog, runner, estimator, workload


def _budget(estimator, workload, **changes):
    return BudgetDefinition(**{
        "budget_id": "september", "period_start": AS_OF, "period_end": END,
        "bucket": "bucket", "cost_limit_usd": "2", "tier_pools": {"hot": "hot", "warm": "warm"},
        "profiles": estimator.profiles, "workload": workload, **changes,
    })


def test_what_if_compares_current_baseline_and_proposal_without_any_mutation():
    catalog, runner, estimator, workload = _setup()
    before = catalog.list("bucket")
    with ExitStack() as stack:
        for target, methods in (
            (catalog, ("upsert", "append_audit_event", "append_access_event", "claim_move_job", "configure_budget")),
            (runner.mover, ("plan", "move")),
        ):
            for method in methods:
                stack.enter_context(patch.object(target, method, side_effect=AssertionError(method)))
        result = simulate_policy(
            runner, "bucket", as_of=AS_OF, proposed_policy=SimplePolicy(size_threshold=1),
            estimator=estimator, workload=workload,
        )
    assert result["mode"] == "simulation"
    assert result["totals"]["current"]["cost"]["value"] == "10"
    assert result["totals"]["baseline"]["cost"]["value"] == "10"
    assert result["totals"]["proposed"]["cost"]["value"] == "2"
    assert result["deltas"]["proposed_minus_current"]["cost"]["value"] == "-8"
    assert result["deltas"]["proposed_minus_current"]["carbon"]["value"] == "2"
    assert result["affected_objects"] == [{"bucket": "bucket", "key": "a"}]
    assert result["objects"][0]["proposed"]["mode"] == "simulation"
    assert result["objects"][0]["proposed"]["estimate"]["rates"][0]["assumption"]["source"] == "synthetic-fixture"
    assert runner.policy.size_threshold == 2 * GIB
    assert runner.budget_definitions is None
    assert catalog.list("bucket") == before
    assert catalog.list_audit_events() == []
    assert catalog.list_move_jobs() == []
    assert catalog.list_budget_reservations() == []
    assert json.loads(json.dumps(result)) == result
    with localcontext() as context:
        context.prec = 3
        assert result == simulate_policy(
            runner, "bucket", as_of=AS_OF, proposed_policy=SimplePolicy(size_threshold=1),
            estimator=estimator, workload=workload,
        )


def test_budget_simulation_projects_batch_usage_and_replaces_configuration_without_writes():
    catalog, runner, estimator, workload = _setup()
    catalog.upsert("bucket", "b", GIB, "hot")
    catalog.assign_pool("bucket", "b", "hot")
    budget = _budget(estimator, workload)
    catalog.configure_budget(
        budget, audit_context=AuditContext("configure", actor_type="user", actor_id="operator"),
        occurred_at=AS_OF,
    )
    runner.policy = SimplePolicy(size_threshold=1)
    audit_before = catalog.list_audit_events()
    result = simulate_policy(
        runner, "bucket", as_of=AS_OF,
        proposed_budget_definitions=(replace(budget, cost_limit_usd="4"),),
    )
    first, second = result["objects"]
    assert first["baseline"]["tier"] == "warm"
    assert second["baseline"]["tier"] == "hot"
    assert second["baseline"]["binding_constraints"][0]["kind"] == "budget"
    check = second["baseline"]["constraints"]["budgets"][0]
    assert check["before"]["cost_usd"] == "2"
    assert check["after"]["cost_usd"] == "4"
    assert second["proposed"]["tier"] == "warm"
    assert result["totals"]["baseline"]["cost"]["value"] == "12"
    assert result["totals"]["proposed"]["cost"]["value"] == "4"
    assert catalog.list_budgets()[0].cost_limit_usd == Decimal("2")
    assert catalog.list_audit_events() == audit_before
    assert catalog.list_budget_reservations() == []
    assert catalog.list_move_jobs() == []


def test_missing_estimates_remain_unknown_and_empty_scope_totals_are_zero():
    _, runner, _, _ = _setup()
    result = simulate_policy(runner, "bucket", as_of=AS_OF)
    assert result["totals"]["current"]["cost"]["value"] is None
    assert result["totals"]["current"]["cost"]["unavailable_object_count"] == 1
    assert result["deltas"]["proposed_minus_current"]["carbon"]["value"] is None
    empty = simulate_policy(runner, "bucket", prefix="absent/", as_of=AS_OF)
    assert empty["object_count"] == 0
    assert empty["totals"]["current"]["cost"]["value"] == "0"


def test_ambiguous_tier_pool_does_not_invent_destination_estimate():
    catalog, runner, estimator, workload = _setup()
    catalog.register_pool("other-warm", "warm", region="west", members=("other",))
    result = simulate_policy(
        runner, "bucket", as_of=AS_OF, proposed_policy=SimplePolicy(size_threshold=1),
        estimator=estimator, workload=workload,
    )
    assert result["objects"][0]["proposed"]["tier"] == "warm"
    assert result["objects"][0]["proposed"]["pool_id"] is None
    assert result["totals"]["proposed"]["cost"]["value"] is None


@pytest.mark.parametrize("weights,preferred_region,expected", [
    (ObjectiveWeights(cost="1"), None, "warm"),
    (ObjectiveWeights(cost="0", carbon="1"), None, "hot"),
    (ObjectiveWeights(cost="0", latency="1"), None, "hot"),
    (ObjectiveWeights(cost="0", locality="1"), "east", "warm"),
])
def test_estimate_policy_can_optimize_each_supported_objective(weights, preferred_region, expected):
    catalog, runner, estimator, workload = _setup(estimates=True)
    proposal = EstimatePolicy(
        catalog, estimator, workload, {"hot": "hot", "warm": "warm"}, weights,
        preferred_region=preferred_region,
    )
    result = simulate_policy(runner, "bucket", as_of=AS_OF, proposed_policy=proposal)
    assert result["objects"][0]["proposed"]["tier"] == expected
    assert result["configurations"]["proposed"]["objectives"]["weights"] == weights.to_dict()
    assert result["objects"][0]["proposed"]["constraints"]["objectives"]["selected"]["tier"] == expected


def test_estimate_policy_applies_hard_eligibility_and_executable_pool_binding_before_scoring():
    catalog, runner, estimator, workload = _setup(estimates=True)
    catalog.register_pool("unbound", "warm", region="west", members=("unbound",))
    estimator = StorageImpactEstimator({**estimator.profiles, "unbound": _profile("0", "0")})
    runner.policy = EstimatePolicy(
        catalog, estimator, workload, {"hot": "hot", "warm": "warm"},
        placement_constraints=PlacementConstraints(allowed_regions=("west",)),
    )
    result = simulate_policy(runner, "bucket", as_of=AS_OF)
    objectives = result["objects"][0]["baseline"]["objectives"]
    assert [item["pool_id"] for item in objectives["candidates"]] == ["hot"]
    assert result["objects"][0]["baseline"]["tier"] == "hot"


def test_estimate_policy_unknown_objectives_fail_closed_and_equal_scores_stay():
    catalog, runner, estimator, workload = _setup(estimates=True)
    runner.policy = EstimatePolicy(
        catalog, StorageImpactEstimator({}), workload, {"hot": "hot", "warm": "warm"},
    )
    result = simulate_policy(runner, "bucket", as_of=AS_OF)
    assert result["objects"][0]["baseline"]["tier"] == "hot"
    assert result["objects"][0]["baseline"]["objectives"]["selected"] is None
    equal = StorageImpactEstimator({"hot": estimator.profiles["hot"], "warm": estimator.profiles["hot"]})
    runner.policy = EstimatePolicy(catalog, equal, workload, {"hot": "hot", "warm": "warm"})
    assert simulate_policy(runner, "bucket", as_of=AS_OF)["objects"][0]["baseline"]["tier"] == "hot"


def test_importance_filters_objective_candidates_before_selecting_the_next_best_tier():
    catalog, runner, estimator, workload = _setup(estimates=True)
    catalog.register_tier("cold")
    catalog.register_pool("cold", "cold", region="west", members=("cold",))
    catalog.set_importance(
        "bucket", "a", ImportanceTag("high", "user", "operator", "test", AS_OF),
        audit_context=AuditContext("importance", actor_type="user", actor_id="operator"),
        occurred_at=AS_OF,
    )
    runner.allowed_tiers = frozenset(("hot", "warm", "cold"))
    runner.policy = EstimatePolicy(
        catalog, StorageImpactEstimator({**estimator.profiles, "cold": _profile("0", "0")}),
        workload, {"hot": "hot", "warm": "warm", "cold": "cold"},
    )
    item = simulate_policy(runner, "bucket", as_of=AS_OF)["objects"][0]["baseline"]
    assert item["tier"] == "warm"
    assert item["objectives"] == item["constraints"]["objectives"]
    assert {candidate["tier"] for candidate in item["objectives"]["candidates"]} == {"hot", "warm"}
    assert item["objectives"]["selected"]["tier"] == "warm"
    assert runner.policy.placement_constraints is None


def test_overlapping_budget_forecasts_are_unavailable_without_explicit_estimator():
    catalog, runner, estimator, workload = _setup()
    runner.budget_definitions = (
        _budget(estimator, workload, cost_limit_usd="100"),
        _budget(estimator, workload, budget_id="second", cost_limit_usd="100"),
    )
    result = simulate_policy(runner, "bucket", as_of=AS_OF)
    assert result["totals"]["current"]["cost"]["value"] is None
    explicit = simulate_policy(
        runner, "bucket", as_of=AS_OF, estimator=estimator, workload=workload,
    )
    assert explicit["totals"]["current"]["cost"]["value"] == "10"
    assert catalog.list_budget_reservations() == []


@pytest.mark.parametrize("overlapping", [False, True])
def test_conflicting_budget_or_objective_destination_pool_bindings_suppress_movement(overlapping):
    catalog, runner, estimator, workload = _setup(estimates=True)
    catalog.register_pool("warm-alternative", "warm", region="east", members=("other",))
    estimator = StorageImpactEstimator({
        **estimator.profiles, "warm-alternative": _profile("1", "2"),
    })
    conflicting = _budget(
        estimator, workload, budget_id="other-pool", cost_limit_usd="100",
        tier_pools={"hot": "hot", "warm": "warm-alternative"},
    )
    if overlapping:
        runner.policy = SimplePolicy(size_threshold=1)
        runner.budget_definitions = (
            _budget(estimator, workload, cost_limit_usd="100"), conflicting,
        )
    else:
        runner.policy = EstimatePolicy(
            catalog, estimator, workload, {"hot": "hot", "warm": "warm"},
        )
        runner.budget_definitions = (conflicting,)
    item = simulate_policy(runner, "bucket", as_of=AS_OF)["objects"][0]["baseline"]
    assert item["tier"] == "hot"
    assert item["constraints"]["suppression_reason"] == "budget"
    assert all(
        "destination_pool_binding_conflict" in check["binding_constraints"]
        for check in item["constraints"]["budgets"]
    )
    assert catalog.list_budget_reservations() == []


def test_simulation_requires_estimator_and_workload_together():
    _, runner, estimator, _ = _setup()
    with pytest.raises(ValueError, match="supplied together"):
        simulate_policy(runner, "bucket", as_of=AS_OF, estimator=estimator)
