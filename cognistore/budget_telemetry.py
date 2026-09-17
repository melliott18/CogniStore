"""Read-only, bounded telemetry from the durable policy-budget ledger.

Admission freezes the versioned estimator's conservative charges in reservations.
Scrapes reuse those amounts and the admission balance arithmetic; they never
forecast invoices or re-estimate old reservations using today's rates.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Any

from prometheus_client import CollectorRegistry, Gauge, generate_latest

from cognistore.core.budgets import (
    BudgetDefinition,
    active_budget_definitions,
    evaluate_budget,
)
from cognistore.core.catalog import CatalogStore
from cognistore.core.estimation import _CONTEXT

_DIMENSIONS = {"cost": "cost_usd", "carbon": "carbon_gco2e"}


@dataclass(frozen=True)
class BudgetDimensionSnapshot:
    consumption_ratio: float
    unknown: int


def budget_snapshot(
    catalog: CatalogStore | None, *, as_of: str | datetime | None = None,
) -> dict[str, BudgetDimensionSnapshot]:
    """Return each dimension's worst independently scoped balance-to-limit ratio.

    Unknown scopes make the ratio NaN, with an explicit unknown count. A complete
    empty ledger has zero ratios and unknown counts. An unavailable ledger has
    NaN ratios and one unknown per dimension. Exact-scope renewals follow the
    same half-open period selection as admission, including fail-closed gaps.
    """
    evaluated_at = as_of if as_of is not None else datetime.now(timezone.utc)
    try:
        if catalog is None:
            raise ValueError("budget catalog is unavailable")
        definitions, reservations = catalog.read_budget_snapshot()
        scopes: dict[tuple[str | None, str], list[BudgetDefinition]] = defaultdict(list)
        held: dict[str, list[dict[str, Any]]] = defaultdict(list)
        identifiers = {definition.budget_id for definition in definitions}
        for reservation in reservations:
            if reservation["budget_id"] not in identifiers:
                raise ValueError("budget reservation has no definition")
            held[reservation["budget_id"]].append(reservation)
        for definition in definitions:
            scopes[(definition.bucket, definition.prefix)].append(definition)

        ratios: dict[str, list[float]] = {dimension: [] for dimension in _DIMENSIONS}
        unknown = dict.fromkeys(_DIMENSIONS, 0)
        for (bucket, prefix), matching in scopes.items():
            # The representative key matches this exact scope. Grouping first
            # prevents a broader replacement from masking a narrower expiry.
            relevant = active_budget_definitions(
                matching, bucket or "", prefix, as_of=evaluated_at,
            )
            for definition in relevant:
                evidence = evaluate_budget(
                    definition, {amount: "0" for amount in _DIMENSIONS.values()},
                    held[definition.budget_id], as_of=evaluated_at,
                )
                for dimension, amount in _DIMENSIONS.items():
                    limit = evidence["limits"][amount]
                    if limit is None:
                        continue
                    balance = evidence["before"][amount]
                    if balance is None or not definition.active_at(evaluated_at):
                        unknown[dimension] += 1
                        continue
                    with localcontext(_CONTEXT):
                        value, ceiling = Decimal(balance), Decimal(limit)
                        ratio = (
                            float(value / ceiling) if ceiling else
                            float("inf") if value else 0.0
                        )
                    ratios[dimension].append(ratio)
        return {
            dimension: BudgetDimensionSnapshot(
                float("nan") if unknown[dimension] else max(ratios[dimension], default=0.0),
                unknown[dimension],
            )
            for dimension in _DIMENSIONS
        }
    except Exception:
        # Do not expose catalog errors, identifiers, or connection credentials.
        # Scraping operational metrics must remain possible during an outage.
        return {dimension: BudgetDimensionSnapshot(float("nan"), 1)
                for dimension in _DIMENSIONS}


def budget_metrics_response(catalog: CatalogStore | None) -> bytes:
    """Build four API-only samples from one snapshot, isolated per scrape."""
    snapshot = budget_snapshot(catalog)
    registry = CollectorRegistry()
    ratio = Gauge(
        "cognistore_policy_budget_consumption_ratio",
        "Maximum independent policy-budget balance/limit; NaN when unknown.",
        ("dimension",), registry=registry,
    )
    unknown = Gauge(
        "cognistore_policy_budget_unknown",
        "Unknown relevant policy budgets; one per dimension if ledger unavailable.",
        ("dimension",), registry=registry,
    )
    for dimension, state in snapshot.items():
        ratio.labels(dimension).set(state.consumption_ratio)
        unknown.labels(dimension).set(state.unknown)
    return generate_latest(registry)
