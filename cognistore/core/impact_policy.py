"""Deterministic placement using modeled cost, carbon, latency and locality."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

from .budgets import ObjectiveWeights, score_candidates
from .catalog import CatalogStore, ObjectRecord
from .estimation import EstimationWorkload, ObjectPlacementEstimates, StorageImpactEstimator
from .policy import PolicyDecision
from .policy_features import PolicyFeatures
from .topology import PlacementConstraints


class EstimatePolicy:
    """Choose only executable, hard-eligible destinations using explicit evidence.

    One physical pool is bound to each destination tier because the mover has
    one driver per tier. Other pools must not make that driver's tier appear
    cheaper or greener. Same-tier pool moves are not supported. Equal scores
    retain the current placement whenever it is an eligible scored candidate.
    """

    def __init__(
        self,
        catalog: CatalogStore,
        estimator: StorageImpactEstimator,
        workload: EstimationWorkload,
        tier_pools: Mapping[str, str],
        weights: ObjectiveWeights | None = None,
        *,
        placement_constraints: PlacementConstraints | None = None,
        preferred_region: str | None = None,
        preferred_localities: Sequence[str] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(estimator, StorageImpactEstimator):
            raise ValueError("estimator must be StorageImpactEstimator")
        if not isinstance(workload, EstimationWorkload):
            raise ValueError("workload must be EstimationWorkload")
        if weights is not None and not isinstance(weights, ObjectiveWeights):
            raise ValueError("weights must be ObjectiveWeights")
        if placement_constraints is not None and not isinstance(
            placement_constraints, PlacementConstraints
        ):
            raise ValueError("placement_constraints must be PlacementConstraints")
        bindings = dict(tier_pools)
        for tier, pool_id in bindings.items():
            pool = catalog.get_pool(pool_id)
            if pool is None or pool.tier != tier:
                raise ValueError(f"pool {pool_id!r} is not registered in tier {tier!r}")
        self.catalog = catalog
        self.estimator = estimator
        self.workload = workload
        self.tier_pools = MappingProxyType(bindings)
        self.weights = weights or ObjectiveWeights()
        self.placement_constraints = placement_constraints
        self.preferred_region = preferred_region
        self.preferred_localities = tuple(preferred_localities)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def estimate_record(
        self, record: ObjectRecord, *, as_of: str | datetime
    ) -> ObjectPlacementEstimates:
        return self.estimator.estimate_object(
            self.catalog, record, self.workload, self.placement_constraints, as_of=as_of
        )

    def score_record(self, record: ObjectRecord, *, as_of: str | datetime) -> dict[str, Any]:
        estimates = self.estimate_record(record, as_of=as_of)
        candidates = tuple(
            item for item in estimates.candidates
            if (
                item.tier == record.tier and item.pool_id == record.pool_id
                or item.tier != record.tier
                and self.tier_pools.get(item.tier or "") == item.pool_id
            )
        )
        pools = {}
        for item in candidates:
            if item.pool_id is not None:
                pool = self.catalog.get_pool(item.pool_id)
                if pool is not None:
                    pools[item.pool_id] = pool
        ranked = score_candidates(
            candidates, pools, self.weights, as_of=as_of,
            preferred_region=self.preferred_region,
            preferred_localities=self.preferred_localities,
        )
        available = [item for item in ranked if item["score"] is not None]
        selected = available[0] if available else None
        if selected is not None:
            # A deterministic tie never causes an unnecessary tier movement.
            selected = next((
                item for item in available
                if item["score"] == selected["score"]
                and item["tier"] == record.tier and item["pool_id"] == record.pool_id
            ), selected)
        return {
            "weights": self.weights.to_dict(),
            "preferred_region": self.preferred_region,
            "preferred_localities": list(self.preferred_localities),
            "tier_pools": dict(self.tier_pools),
            "candidates": ranked,
            "selected": selected,
            "estimates": {
                "current": estimates.current.to_dict(),
                "candidates": [item.to_dict() for item in candidates],
            },
            "normalization": "min-max over eligible executable candidates; lower is better",
        }

    def evaluate_features(self, record: ObjectRecord, features: PolicyFeatures) -> PolicyDecision:
        if features.access is None:
            return PolicyDecision("stay", "objective evaluation requires a frozen feature timestamp")
        return self.evaluate_record(record, as_of=features.access.as_of)

    def evaluate_record(
        self, record: ObjectRecord, *, as_of: str | datetime | None = None
    ) -> PolicyDecision:
        evidence = self.score_record(record, as_of=self.clock() if as_of is None else as_of)
        selected = evidence["selected"]
        if selected is None:
            decision = PolicyDecision(
                "stay", "no eligible destination has complete weighted objective evidence"
            )
        elif selected["tier"] == record.tier:
            decision = PolicyDecision("stay", "current placement has the best objective score")
        else:
            decision = PolicyDecision(
                "move", f"best modeled objective score {selected['score']}", selected["tier"]
            )
        decision.objective_evidence = evidence
        return decision

    def evaluate(self, current_tier: str, size: int) -> PolicyDecision:
        raise ValueError("EstimatePolicy requires a catalog object; use PolicyRunner")
