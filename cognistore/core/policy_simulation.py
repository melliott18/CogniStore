"""Read-only comparisons of catalog placement and two policy scenarios."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any

from .budgets import BudgetDefinition
from .catalog import ObjectRecord
from .estimation import (
    _CONTEXT,
    EstimationWorkload,
    ObjectPlacementEstimates,
    PlacementEstimate,
    StorageImpactEstimator,
    _text,
    _time,
)
from .impact_policy import EstimatePolicy
from .policy import Policy, SimplePolicy
from .policy_features import CatalogPolicyFeatureLoader
from .policy_runner import PolicyEvaluationResult, PolicyRunner
from .topology import _timestamp

SIMULATION_SCHEMA_VERSION = 1


def _estimate_budget(
    runner: PolicyRunner, record: ObjectRecord, as_of: str
) -> BudgetDefinition | None:
    definitions = runner.budget_definitions
    assert definitions is not None
    active = [
        budget for budget in definitions
        if budget.matches(record.bucket, record.key) and budget.active_at(as_of)
    ]
    return active[0] if len(active) == 1 else None


def _estimates(
    runner: PolicyRunner, record: ObjectRecord, result: PolicyEvaluationResult, as_of: str,
    estimator: StorageImpactEstimator | None, workload: EstimationWorkload | None,
) -> ObjectPlacementEstimates | None:
    if estimator is not None:
        assert workload is not None
        return estimator.estimate_object(runner.catalog, record, workload, as_of=as_of)
    if isinstance(runner.policy, EstimatePolicy):
        return runner.policy.estimate_record(record, as_of=as_of)
    if result.features.placement_estimates is not None:
        return result.features.placement_estimates
    loader = runner.feature_loader
    if isinstance(loader, CatalogPolicyFeatureLoader) and loader.impact_estimator is not None:
        # Hard movement blockers deliberately skip optional policy features.
        # Their current placement must still be included in aggregate impact.
        assert loader.estimation_catalog is not None
        assert loader.estimation_workload_factory is not None
        return loader.impact_estimator.estimate_object(
            loader.estimation_catalog, record, loader.estimation_workload_factory(record),
            loader.estimation_constraints, as_of=as_of,
        )
    budget = _estimate_budget(runner, record, as_of)
    if budget is not None:
        remaining = _timestamp(budget.period_end, "period_end") - _timestamp(as_of, "as_of")
        with localcontext(_CONTEXT):
            hours = (
                Decimal(remaining.days * 86400 + remaining.seconds)
                + Decimal(remaining.microseconds) / Decimal(1000000)
            ) / Decimal(3600)
        return StorageImpactEstimator(budget.profiles).estimate_object(
            runner.catalog, record, replace(budget.workload, duration_hours=hours), as_of=as_of
        )
    return None


def _selected(
    runner: PolicyRunner, record: ObjectRecord, result: PolicyEvaluationResult,
    estimates: ObjectPlacementEstimates | None,
    as_of: str, tier_pools: Mapping[str, str] | None,
) -> tuple[str, str | None, PlacementEstimate | None]:
    if (
        result.action != "move" or not result.destination_tier
        or result.destination_tier == record.tier
        or result.destination_tier not in runner.allowed_tiers
    ):
        return record.tier, record.pool_id, None if estimates is None else estimates.current
    tier = result.destination_tier
    if estimates is None:
        return tier, None, None
    candidates = [item for item in estimates.candidates if item.tier == tier]
    budget = _estimate_budget(runner, record, as_of)
    bindings = tier_pools
    if bindings is None and isinstance(runner.policy, EstimatePolicy):
        bindings = runner.policy.tier_pools
    if bindings is None and budget is not None:
        bindings = budget.tier_pools
    bound_pool = None if bindings is None else bindings.get(tier)
    if bound_pool is not None:
        candidates = [item for item in candidates if item.pool_id == bound_pool]
    # A tier-only policy does not resolve multiple physical pools. Do not
    # silently choose the cheapest pool and imply that the mover will use it.
    estimate = candidates[0] if len(candidates) == 1 else None
    return tier, bound_pool if estimate is None else estimate.pool_id, estimate


def _total(estimates: Sequence[PlacementEstimate | None], objective: str) -> dict[str, Any]:
    totals = [None if item is None else getattr(item, objective) for item in estimates]
    available = all(item is not None and item.value is not None for item in totals)
    bounded = available and all(
        all(component.lower is not None for component in item.components)
        for item in totals if item is not None
    )
    with localcontext(_CONTEXT):
        known = sum((item.known_subtotal for item in totals if item is not None), Decimal(0))
        lower = sum((
            component.lower for item in totals if item is not None
            for component in item.components if component.lower is not None
        ), Decimal(0))
        upper = sum((
            component.upper for item in totals if item is not None
            for component in item.components if component.upper is not None
        ), Decimal(0))
    return {
        "unit": "USD" if objective == "cost" else "gCO2e",
        "state": "available" if available else "unavailable",
        "value": _text(known) if available else None,
        "known_subtotal": _text(known),
        "lower": _text(lower) if bounded else None,
        "upper": _text(upper) if bounded else None,
        "uncertainty": "bounded" if bounded else "unquantified",
        "unavailable_object_count": sum(item is None or item.value is None for item in totals),
    }


def _delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    available = before["value"] is not None and after["value"] is not None
    bounded = before["lower"] is not None and after["lower"] is not None
    with localcontext(_CONTEXT):
        value = Decimal(after["value"]) - Decimal(before["value"]) if available else None
        lower = Decimal(after["lower"]) - Decimal(before["upper"]) if bounded else None
        upper = Decimal(after["upper"]) - Decimal(before["lower"]) if bounded else None
    return {
        "unit": before["unit"],
        "state": "available" if available else "unavailable",
        "value": _text(value),
        "lower": _text(lower),
        "upper": _text(upper),
        "uncertainty": "bounded" if bounded else "unquantified",
    }


def _bindings(result: PolicyEvaluationResult, runner: PolicyRunner) -> list[dict[str, Any]]:
    evidence = result.constraints
    bindings = []
    if evidence.get("blocked_reason"):
        bindings.append({
            "kind": evidence.get("suppression_reason") or "movement_constraint",
            "reason": evidence["blocked_reason"],
        })
    elif evidence.get("suppression_reason"):
        bindings.append({"kind": evidence["suppression_reason"], "reason": result.reason})
    if result.action == "move" and result.destination_tier not in runner.allowed_tiers:
        bindings.append({"kind": "allowed_tiers", "reason": "destination tier is not allowed"})
    budgets = evidence.get("budgets")
    if isinstance(budgets, list):
        for budget in budgets:
            if isinstance(budget, Mapping) and (
                budget.get("allowed") is False or budget.get("binding") is True
            ):
                bindings.append({"kind": "budget", **deepcopy(budget)})
    return bindings


def _configuration(runner: PolicyRunner) -> dict[str, Any]:
    definitions = runner.budget_definitions
    if definitions is None:
        definitions = runner.catalog.list_budgets()
    result: dict[str, Any] = {
        "policy": {"name": runner.policy_name, "version": runner.policy_version},
        "budgets": [budget.to_dict() for budget in definitions],
        "allowed_tiers": sorted(runner.allowed_tiers),
        "movement_constraints": runner.movement_constraints.to_dict(),
        "budget_override": None if runner.budget_override is None else runner.budget_override.to_dict(),
    }
    if isinstance(runner.policy, SimplePolicy):
        result["policy"]["size_threshold"] = runner.policy.size_threshold
        result["policy"]["size_hysteresis_bytes"] = runner.policy.size_hysteresis_bytes
    if isinstance(runner.policy, EstimatePolicy):
        result["objectives"] = {
            "weights": runner.policy.weights.to_dict(),
            "preferred_region": runner.policy.preferred_region,
            "preferred_localities": list(runner.policy.preferred_localities),
        }
    return result


def simulate_policy(
    runner: PolicyRunner,
    bucket: str,
    prefix: str = "",
    *,
    as_of: str | datetime | None = None,
    proposed_policy: Policy | None = None,
    proposed_budget_definitions: Sequence[BudgetDefinition] | None = None,
    estimator: StorageImpactEstimator | None = None,
    workload: EstimationWorkload | None = None,
    tier_pools: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Compare current placement, baseline policy, and a proposed configuration.

    The same detached object snapshot and timestamp are evaluated twice. No
    planning, audit, reservation, job, storage, or catalog mutation API is
    invoked. ``None`` inherits the runner's budgets; an empty sequence models
    removing them. Callers supply read-only policies and feature providers.
    """
    if (estimator is None) != (workload is None):
        raise ValueError("simulation estimator and workload must be supplied together")
    evaluated_at = _time(runner.clock() if as_of is None else as_of, "as_of")
    records = sorted(runner.catalog.list(bucket, prefix=prefix), key=lambda item: item.key)
    baseline = copy(runner)
    baseline.simulation_only = True
    baseline.budget_definitions = tuple(
        runner.catalog.list_budgets() if runner.budget_definitions is None
        else runner.budget_definitions
    )
    baseline.policy = copy(runner.policy)
    proposed = copy(runner)
    proposed.simulation_only = True
    proposed.budget_definitions = baseline.budget_definitions
    proposed.policy = copy(runner.policy if proposed_policy is None else proposed_policy)
    if proposed_policy is not None:
        proposed.policy_name = proposed._default_policy_name(proposed.policy)
    if proposed_budget_definitions is not None:
        proposed.budget_definitions = tuple(proposed_budget_definitions)
    baseline_results = baseline._evaluate_records(deepcopy(records), as_of=evaluated_at)
    proposed_results = proposed._evaluate_records(deepcopy(records), as_of=evaluated_at)
    selected: dict[str, list[PlacementEstimate | None]] = {
        "current": [], "baseline": [], "proposed": [],
    }
    objects = []
    affected = []
    for record, before, after in zip(records, baseline_results, proposed_results):
        before_estimates = _estimates(baseline, record, before, evaluated_at, estimator, workload)
        after_estimates = _estimates(proposed, record, after, evaluated_at, estimator, workload)
        current_estimates = before_estimates if before_estimates is not None else after_estimates
        current = None if current_estimates is None else current_estimates.current
        selected["current"].append(current)
        item: dict[str, Any] = {
            "bucket": record.bucket, "key": record.key, "size": record.size,
            "current": {
                "tier": record.tier, "pool_id": record.pool_id,
                "estimate": None if current is None else current.to_dict(),
            },
        }
        for name, scenario_runner, evaluation, estimates in (
            ("baseline", baseline, before, before_estimates),
            ("proposed", proposed, after, after_estimates),
        ):
            tier, pool_id, estimate = _selected(
                scenario_runner, record, evaluation, estimates, evaluated_at, tier_pools
            )
            selected[name].append(estimate)
            item[name] = {
                "mode": "simulation", "action": evaluation.action,
                "destination_tier": evaluation.destination_tier,
                "tier": tier, "pool_id": pool_id, "reason": evaluation.reason,
                "estimate": None if estimate is None else estimate.to_dict(),
                "constraints": deepcopy(evaluation.constraints),
                "binding_constraints": _bindings(evaluation, scenario_runner),
            }
            if isinstance(scenario_runner.policy, EstimatePolicy):
                item[name]["objectives"] = deepcopy(evaluation.constraints.get("objectives"))
        item["changed_from_current"] = item["proposed"]["tier"] != record.tier
        item["changed_from_baseline"] = (
            (item["baseline"]["tier"], item["baseline"]["pool_id"])
            != (item["proposed"]["tier"], item["proposed"]["pool_id"])
        )
        if item["changed_from_current"] or item["changed_from_baseline"]:
            affected.append({"bucket": record.bucket, "key": record.key})
        objects.append(item)
    totals = {
        name: {objective: _total(values, objective) for objective in ("cost", "carbon")}
        for name, values in selected.items()
    }
    return {
        "schema_version": SIMULATION_SCHEMA_VERSION,
        "mode": "simulation", "as_of": evaluated_at, "bucket": bucket, "prefix": prefix,
        "object_count": len(records), "affected_object_count": len(affected),
        "affected_objects": affected,
        "configurations": {"baseline": _configuration(baseline), "proposed": _configuration(proposed)},
        "totals": totals,
        "deltas": {
            f"{after}_minus_{before}": {
                objective: _delta(totals[before][objective], totals[after][objective])
                for objective in ("cost", "carbon")
            }
            for before, after in (("current", "baseline"), ("current", "proposed"), ("baseline", "proposed"))
        },
        "objects": objects,
        "assumptions": [
            "Simulation only; no moves, object reads, writes, audit events, jobs, or budget reservations.",
            "Objects use one detached catalog snapshot and explicit as_of; policy providers must be read-only.",
            "Totals model placement holding and workload estimates over each supplied forecast horizon.",
            "Explicit estimator/workload inputs apply the same forecast to both scenarios; otherwise each scenario uses its own policy or loader estimates, or one unambiguous active budget's remaining-period forecast.",
            "Migration is excluded from placement totals; conservative budget checks include migration separately.",
            "Missing or stale evidence and unresolved destination pools yield unavailable totals and deltas, never zero.",
            "Known subtotals exclude unknown components and are not complete forecasts.",
            "Rate and workload assumptions are retained per object; these are estimates, not provider billing or measured emissions.",
            "Budget checks project sequential charges within each scenario without persisting reservations.",
            "Negative deltas mean modeled reductions; uncertainty ranges are scenario bounds, not confidence intervals.",
        ],
    }
