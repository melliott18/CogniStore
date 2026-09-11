"""Immutable budget assumptions, conservative reservations, and soft objectives.

Budgets bound modeled expenditure, not provider invoices. Reservations include
the destination's entire remaining-period workload and migration overhead; they
never subtract speculative savings at the source. This module is pure: catalog
implementations provide atomic accounting around these calculations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .estimation import (
    _CONTEXT,
    EstimationProfile,
    EstimationWorkload,
    PlacementEstimate,
    StorageImpactEstimator,
    _decimal,
    _text,
    _time,
)
from .topology import Pool, _fields, _name, _names, _timestamp

if TYPE_CHECKING:
    from .catalog import ObjectRecord

BUDGET_SCHEMA_VERSION = 1
_AMOUNTS = ("cost_usd", "carbon_gco2e")
_USAGE = ("read_requests", "write_requests", "transfer_bytes", "retrieval_bytes")
_OBJECTIVES = ("cost", "carbon", "latency", "locality")


class BudgetConstraintError(ValueError):
    """An action cannot safely obtain its required budget reservations."""

    def __init__(self, reason: str, *, evidence: list[dict[str, Any]] | None = None) -> None:
        super().__init__(reason)
        self.evidence = [] if evidence is None else evidence


@dataclass(frozen=True)
class BudgetDefinition:
    """A fixed UTC half-open period and exact bucket/literal key-prefix scope.

    ``bucket=None`` covers all buckets. Opening amounts represent operator-
    supplied existing commitments. Usage counts describe each admitted object's
    remaining period; stored bytes and duration are derived at admission.
    Definitions are immutable so reservation evidence can be replayed.
    """

    budget_id: str
    period_start: str
    period_end: str
    cost_limit_usd: Decimal | str | int | float | None = None
    carbon_limit_gco2e: Decimal | str | int | float | None = None
    bucket: str | None = None
    prefix: str = ""
    opening_cost_usd: Decimal | str | int | float = "0"
    opening_carbon_gco2e: Decimal | str | int | float = "0"
    tier_pools: Mapping[str, str] = field(default_factory=dict)
    profiles: Mapping[str, EstimationProfile] = field(default_factory=dict)
    workload: EstimationWorkload = field(default_factory=EstimationWorkload)
    version: int = BUDGET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _name(self.budget_id, "budget_id")
        if type(self.version) is not int or self.version != BUDGET_SCHEMA_VERSION:
            raise ValueError("unsupported budget schema version")
        if self.bucket is not None:
            _name(self.bucket, "bucket")
        if not isinstance(self.prefix, str):
            raise ValueError("prefix must be a string")
        start = _time(self.period_start, "period_start")
        end = _time(self.period_end, "period_end")
        if _timestamp(end, "period_end") <= _timestamp(start, "period_start"):
            raise ValueError("period_end must follow period_start")
        object.__setattr__(self, "period_start", start)
        object.__setattr__(self, "period_end", end)
        if self.cost_limit_usd is None and self.carbon_limit_gco2e is None:
            raise ValueError("at least one hard budget limit is required")
        for name in (
            "cost_limit_usd", "carbon_limit_gco2e", "opening_cost_usd", "opening_carbon_gco2e"
        ):
            value = getattr(self, name)
            if value is not None or name.startswith("opening_"):
                object.__setattr__(self, name, _decimal(value, name))
        if not isinstance(self.tier_pools, Mapping):
            raise ValueError("tier_pools must be a mapping")
        bindings = {
            _name(tier, "tier"): _name(pool_id, "pool_id")
            for tier, pool_id in self.tier_pools.items()
        }
        if not bindings:
            raise ValueError("tier_pools must explicitly bind at least one tier")
        object.__setattr__(self, "tier_pools", MappingProxyType(dict(sorted(bindings.items()))))
        # The estimator validates and detaches the profile mapping.
        object.__setattr__(self, "profiles", StorageImpactEstimator(self.profiles).profiles)
        if not isinstance(self.workload, EstimationWorkload):
            raise ValueError("workload must be EstimationWorkload")
        if any(getattr(self.workload, name) is None for name in _USAGE):
            raise ValueError("budget workload must explicitly supply all request and byte counts")

    def matches(self, bucket: str, key: str) -> bool:
        return (self.bucket is None or self.bucket == bucket) and key.startswith(self.prefix)

    def active_at(self, as_of: str | datetime) -> bool:
        return (
            _timestamp(self.period_start, "period_start")
            <= _timestamp(as_of, "as_of")
            < _timestamp(self.period_end, "period_end")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "budget_id": self.budget_id,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "bucket": self.bucket,
            "prefix": self.prefix,
            "cost_limit_usd": None if self.cost_limit_usd is None else _text(
                _decimal(self.cost_limit_usd, "cost_limit_usd")
            ),
            "carbon_limit_gco2e": None if self.carbon_limit_gco2e is None else _text(
                _decimal(self.carbon_limit_gco2e, "carbon_limit_gco2e")
            ),
            "opening_cost_usd": _text(_decimal(self.opening_cost_usd, "opening_cost_usd")),
            "opening_carbon_gco2e": _text(
                _decimal(self.opening_carbon_gco2e, "opening_carbon_gco2e")
            ),
            "tier_pools": dict(self.tier_pools),
            "profiles": {key: value.to_dict() for key, value in sorted(self.profiles.items())},
            "workload": self.workload.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> BudgetDefinition:
        values = _fields(
            value,
            required={"budget_id", "period_start", "period_end", "tier_pools", "workload"},
            optional={
                "version", "bucket", "prefix", "cost_limit_usd", "carbon_limit_gco2e",
                "opening_cost_usd", "opening_carbon_gco2e", "profiles",
            },
        )
        profiles = values.get("profiles", {})
        if not isinstance(profiles, Mapping):
            raise ValueError("profiles must be a mapping")
        values["profiles"] = {
            key: EstimationProfile.from_mapping(profile) for key, profile in profiles.items()
        }
        values["workload"] = EstimationWorkload.from_mapping(values["workload"])
        return cls(**values)


def active_budget_definitions(
    definitions: Sequence[BudgetDefinition],
    bucket: str,
    key: str,
    *,
    as_of: str | datetime,
) -> list[BudgetDefinition]:
    """Apply overlapping scopes independently and allow exact-scope renewals.

    An expired/future scope fails closed until it has an active replacement.
    A wider active budget never masks an expired narrower scope.
    """
    scopes: dict[tuple[str | None, str], list[BudgetDefinition]] = {}
    for definition in definitions:
        if definition.matches(bucket, key):
            scopes.setdefault((definition.bucket, definition.prefix), []).append(definition)
    selected = []
    for matching in scopes.values():
        active = [definition for definition in matching if definition.active_at(as_of)]
        selected.extend(active or matching)
    return sorted(selected, key=lambda definition: definition.budget_id)


@dataclass(frozen=True)
class BudgetOverride:
    """An attributable exception to numeric limits, retained with admission."""

    actor_id: str
    reason: str

    def __post_init__(self) -> None:
        _name(self.actor_id, "actor_id")
        _name(self.reason, "override reason")

    def to_dict(self) -> dict[str, str]:
        return {"actor_id": self.actor_id, "reason": self.reason}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> BudgetOverride:
        return cls(**_fields(value, required={"actor_id", "reason"}, optional=set()))


def _bound_pool(
    definition: BudgetDefinition, tier: str, pools: Mapping[str, Pool], role: str
) -> tuple[Pool | None, list[str]]:
    pool_id = definition.tier_pools.get(tier)
    pool = None if pool_id is None else pools.get(pool_id)
    if pool_id is None:
        return None, [f"{role}_tier_unbound"]
    if pool is None:
        return None, [f"{role}_pool_missing"]
    if pool.pool_id != pool_id or pool.tier != tier:
        return None, [f"{role}_pool_mismatch"]
    if not pool.active:
        return None, [f"{role}_pool_inactive"]
    return pool, []


def estimate_budget_charge(
    definition: BudgetDefinition,
    record: ObjectRecord,
    dst_tier: str,
    pools: Mapping[str, Pool],
    *,
    as_of: str | datetime,
) -> dict[str, Any]:
    """Charge full remaining destination impact plus source/destination copy I/O.

    Upper bounds are used for each fully bounded total; otherwise the nominal
    amount is used and its unquantified uncertainty remains explicit. Missing
    rates, even on zero quantities, remain unavailable as in the estimator.
    """
    from .catalog import ObjectRecord

    if not isinstance(definition, BudgetDefinition):
        raise ValueError("definition must be BudgetDefinition")
    if not isinstance(record, ObjectRecord):
        raise ValueError("record must be ObjectRecord")
    _name(dst_tier, "dst_tier")
    evaluated_at = _time(as_of, "as_of")
    now = _timestamp(evaluated_at, "as_of")
    remaining = max(_timestamp(definition.period_end, "period_end") - now, timedelta(0))
    with localcontext(_CONTEXT):
        hours = (Decimal(remaining.days * 86400 + remaining.seconds)
                 + Decimal(remaining.microseconds) / Decimal(1000000)) / Decimal(3600)
    source, reasons = _bound_pool(definition, record.tier, pools, "source")
    destination, destination_reasons = _bound_pool(definition, dst_tier, pools, "destination")
    reasons.extend(destination_reasons)
    if record.pool_id is not None and (source is None or record.pool_id != source.pool_id):
        reasons.append("source_record_pool_mismatch")
    estimator = StorageImpactEstimator(definition.profiles)
    usage = replace(definition.workload, stored_bytes=record.size, duration_hours=hours)
    estimates = {"destination_holding": estimator.estimate(destination, usage, as_of=evaluated_at)}
    if record.tier != dst_tier:
        estimates["source_migration"] = estimator.estimate(
            source,
            EstimationWorkload(
                stored_bytes=0, duration_hours=0, read_requests=4, write_requests=1,
                transfer_bytes=4 * record.size, retrieval_bytes=4 * record.size,
            ),
            as_of=evaluated_at,
        )
        estimates["destination_migration"] = estimator.estimate(
            destination,
            EstimationWorkload(
                stored_bytes=0, duration_hours=0, read_requests=3, write_requests=1,
                transfer_bytes=3 * record.size, retrieval_bytes=3 * record.size,
            ),
            as_of=evaluated_at,
        )
    payloads = {name: estimate.to_dict() for name, estimate in estimates.items()}
    amounts: dict[str, Any] = {}
    bases: dict[str, dict[str, str]] = {}
    for amount, objective in zip(_AMOUNTS, ("cost", "carbon")):
        chosen: list[Decimal | None] = []
        bases[amount] = {}
        for name, payload in payloads.items():
            total = payload[objective]
            basis = "upper" if total["upper"] is not None else "value"
            bases[amount][name] = "upper_bound" if basis == "upper" else "nominal_unquantified"
            value = total[basis]
            if value is None:
                reasons.extend(
                    f"{name}.{objective}.{reason}"
                    for component in total["components"] for reason in component["reasons"]
                )
                chosen.append(None)
            else:
                chosen.append(_decimal(value, amount))
        with localcontext(_CONTEXT):
            amounts[amount] = (
                None if any(item is None for item in chosen)
                else _text(sum((item for item in chosen if item is not None), Decimal(0)))
            )
    # A mismatched binding cannot be made safe by complete rates elsewhere.
    if source is None or destination is None or "source_record_pool_mismatch" in reasons:
        amounts.update(dict.fromkeys(_AMOUNTS))
    return {
        **amounts,
        "as_of": evaluated_at,
        "reasons": sorted(set(reasons)),
        "assumptions": {
            "formula": "remaining-destination-plus-migration-v1",
            "source_savings_credited": False,
            "remaining_hours": _text(hours),
            "usage_scope": "per-object counts for the entire remaining budget period",
            "migration_io": (
                "four full source reads, three full destination reads, and one write "
                "at each endpoint; source reads cover content validation, copying, "
                "retained-source restoration, and restored-source verification; "
                "destination reads cover copy and cleanup verification with one spare; "
                "each read includes transfer and retrieval bytes; "
                "metadata probes and backend request splitting are not modeled"
            ),
            "rate_forecast": (
                "rates fresh at admission are held fixed through period_end; "
                "future rate changes are not predicted"
            ),
            "amount_basis": bases,
            "estimates": payloads,
        },
    }


def evaluate_budget(
    definition: BudgetDefinition,
    charge: Mapping[str, Any],
    reservations: Sequence[Mapping[str, Any]],
    *,
    as_of: str | datetime,
    override: BudgetOverride | None = None,
) -> dict[str, Any]:
    """Evaluate one reservation against operator opening amounts and held usage.

    Callers pass only held/committed reservations. Numeric limit exceptions
    require an actor and reason; unknown amounts and invalid periods cannot be
    overridden because there is no auditable amount to reserve.
    """
    if override is not None and not isinstance(override, BudgetOverride):
        raise ValueError("override must be BudgetOverride")
    evaluated_at = _time(as_of, "as_of")
    now = _timestamp(evaluated_at, "as_of")
    blocking: list[str] = []
    exceeded: list[str] = []
    if now < _timestamp(definition.period_start, "period_start"):
        blocking.append("budget_not_started")
    elif now >= _timestamp(definition.period_end, "period_end"):
        blocking.append("budget_expired")
    before: dict[str, str | None] = {}
    after: dict[str, str | None] = {}
    limits: dict[str, str | None] = {}
    for amount, limit_name, opening_name in (
        ("cost_usd", "cost_limit_usd", "opening_cost_usd"),
        ("carbon_gco2e", "carbon_limit_gco2e", "opening_carbon_gco2e"),
    ):
        limit = getattr(definition, limit_name)
        limits[amount] = None if limit is None else _text(_decimal(limit, limit_name))
        values = [_decimal(getattr(definition, opening_name), opening_name)]
        unknown = False
        for reservation in reservations:
            usage = reservation.get("charge", reservation)
            raw = usage.get(amount)
            if raw is None:
                unknown = True
            else:
                values.append(_decimal(raw, amount))
        with localcontext(_CONTEXT):
            current = None if unknown else sum(values, Decimal(0))
            raw_charge = charge.get(amount)
            addition = None if raw_charge is None else _decimal(raw_charge, amount)
            projected = None if current is None or addition is None else current + addition
        before[amount] = _text(current)
        after[amount] = _text(projected)
        if limit is not None:
            if projected is None:
                blocking.append(f"{amount}_unavailable")
            elif projected > _decimal(limit, limit_name):
                exceeded.append(f"{amount}_limit")
    return {
        "budget_id": definition.budget_id,
        "as_of": evaluated_at,
        "allowed": not blocking and (not exceeded or override is not None),
        "binding_constraints": blocking + exceeded,
        "limits": limits,
        "before": before,
        "after": after,
        "charge": dict(charge),
        "override": None if override is None else override.to_dict(),
        "override_applied": bool(override is not None and exceeded and not blocking),
    }


@dataclass(frozen=True)
class ObjectiveWeights:
    """Non-negative weights applied to normalized costs; lower is better."""

    cost: Decimal | str | int | float = "1"
    carbon: Decimal | str | int | float = "0"
    latency: Decimal | str | int | float = "0"
    locality: Decimal | str | int | float = "0"

    def __post_init__(self) -> None:
        for name in _OBJECTIVES:
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        if not any(getattr(self, name) for name in _OBJECTIVES):
            raise ValueError("at least one objective weight must be positive")

    def to_dict(self) -> dict[str, str | None]:
        return {name: _text(_decimal(getattr(self, name), name)) for name in _OBJECTIVES}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ObjectiveWeights:
        return cls(**_fields(value, required=set(), optional=set(_OBJECTIVES)))


def score_candidates(
    estimates: Sequence[PlacementEstimate],
    pools: Mapping[str, Pool],
    weights: ObjectiveWeights,
    *,
    as_of: str | datetime,
    preferred_region: str | None = None,
    preferred_localities: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Rank already-eligible candidates with explicit, replayable tradeoffs.

    Each objective uses min-max normalization over known values. An unavailable
    positively weighted objective excludes that candidate. Locality is the
    number of unmet soft labels plus a possible preferred-region mismatch;
    hard topology constraints must have been applied before this function.
    """
    if not isinstance(weights, ObjectiveWeights):
        raise ValueError("weights must be ObjectiveWeights")
    evaluated_at = _time(as_of, "as_of")
    if preferred_region is not None:
        _name(preferred_region, "preferred_region")
    labels = _names(preferred_localities, "preferred_localities")
    rows: list[dict[str, Any]] = []
    for estimate in estimates:
        pool = None if estimate.pool_id is None else pools.get(estimate.pool_id)
        raw: dict[str, Decimal | None] = {
            "cost": estimate.cost.value, "carbon": estimate.carbon.value,
            "latency": None, "locality": None,
        }
        if pool is not None:
            latency = pool.attributes.get("latency")
            if latency is not None and latency.is_fresh(now=evaluated_at):
                raw["latency"] = _decimal(latency.value, "latency")
            if preferred_region is not None or labels:
                raw["locality"] = Decimal(
                    len(set(labels) - set(pool.localities))
                    + int(preferred_region is not None and pool.region != preferred_region)
                )
        reasons = [
            f"objective_{name}_unavailable" for name in _OBJECTIVES
            if getattr(weights, name) and raw[name] is None
        ]
        rows.append({
            "pool_id": estimate.pool_id, "tier": estimate.tier,
            "objectives": raw, "reasons": reasons,
        })
    spans: dict[str, tuple[Decimal | None, Decimal | None]] = {}
    for name in _OBJECTIVES:
        known: list[Decimal] = [
            row["objectives"][name] for row in rows if row["objectives"][name] is not None
        ]
        spans[name] = (min(known), max(known)) if known else (None, None)
    with localcontext(_CONTEXT):
        for row in rows:
            normalized: dict[str, str | None] = {}
            score = Decimal(0)
            for name in _OBJECTIVES:
                value = row["objectives"][name]
                low, high = spans[name]
                normal = None
                if value is not None:
                    assert low is not None and high is not None
                    normal = Decimal(0) if low == high else (value - low) / (high - low)
                normalized[name] = _text(normal)
                if normal is not None:
                    score += normal * _decimal(getattr(weights, name), name)
            row["score"] = None if row["reasons"] else _text(score)
            row["normalized"] = normalized
            row["objectives"] = {key: _text(value) for key, value in row["objectives"].items()}
    return sorted(rows, key=lambda row: (
        row["score"] is None,
        Decimal(0) if row["score"] is None else Decimal(row["score"]),
        row["tier"] or "", row["pool_id"] or "",
    ))
