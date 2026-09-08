"""Backend-neutral storage topology and hard placement eligibility.

Topology decides which destinations may be considered. Optimization attributes
are separate observations; their absence or freshness never relaxes a hard
region or locality constraint.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .catalog import CatalogStore

AttributeKind = Literal["configured", "measured"]
AttributeState = Literal["fresh", "stale", "future", "missing"]

ATTRIBUTE_UNITS: Mapping[str, str] = MappingProxyType({
    "latency": "ms",
    "capacity": "bytes",
    "price": "USD/GiB-month",
    "carbon_intensity": "gCO2e/kWh",
})


def _name(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty string without surrounding whitespace")
    return value


def _names(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a sequence of names")
    result = tuple(_name(item, field_name) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return result


def _metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError("metadata must be a mapping with string keys")
    return deepcopy(dict(value))


def _timestamp(value: str | datetime, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _now(value: str | datetime | None) -> datetime:
    return datetime.now(timezone.utc) if value is None else _timestamp(value, "now")


def _number(value: object, field_name: str, *, positive: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number")
    try:
        valid = math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid or value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{field_name} must be a finite {qualifier} number")
    return value


def _fields(value: object, *, required: set[str], optional: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("configuration entry must be a mapping")
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise ValueError(f"missing required fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(str(key) for key in unknown))}")
    return dict(value)


@dataclass(frozen=True)
class AttributeValue:
    """A non-negative observation with explicit provenance and expiration.

    Both configured and measured values expire. A future-dated observation is
    reported separately from a fresh value and must not be treated as current.
    ``observed_at`` is normalized to UTC on construction.
    """

    value: int | float
    unit: str
    source: str
    kind: AttributeKind
    observed_at: str
    max_age_seconds: float

    def __post_init__(self) -> None:
        _number(self.value, "attribute value")
        _name(self.unit, "unit")
        _name(self.source, "source")
        if self.kind not in ("configured", "measured"):
            raise ValueError("attribute kind must be configured or measured")
        _number(self.max_age_seconds, "max_age_seconds", positive=True)
        observed = _timestamp(self.observed_at, "observed_at")
        object.__setattr__(
            self,
            "observed_at",
            observed.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        )

    def freshness(self, *, now: str | datetime | None = None) -> AttributeState:
        age = (_now(now) - _timestamp(self.observed_at, "observed_at")).total_seconds()
        if age < 0:
            return "future"
        return "fresh" if age <= self.max_age_seconds else "stale"

    def is_fresh(self, *, now: str | datetime | None = None) -> bool:
        return self.freshness(now=now) == "fresh"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "kind": self.kind,
            "observed_at": self.observed_at,
            "max_age_seconds": self.max_age_seconds,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> AttributeValue:
        values = _fields(
            value,
            required={"value", "unit", "source", "kind", "observed_at", "max_age_seconds"},
            optional=set(),
        )
        return cls(**values)


@dataclass(frozen=True)
class Tier:
    """A named logical storage class; physical destinations belong to pools."""

    name: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    active: bool = True

    def __post_init__(self) -> None:
        _name(self.name, "tier name")
        if not isinstance(self.active, bool):
            raise ValueError("active must be a boolean")
        object.__setattr__(self, "metadata", _metadata(self.metadata))

    def to_mapping(self) -> dict[str, Any]:
        return {"name": self.name, "metadata": deepcopy(dict(self.metadata)), "active": self.active}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Tier:
        return cls(**_fields(value, required={"name"}, optional={"metadata", "active"}))


@dataclass(frozen=True)
class Pool:
    """One region-scoped group of opaque storage member identifiers.

    Active pools require a region and at least one member. Inactive pools may
    retain incomplete legacy topology until an operator explicitly fills it in.
    Localities are exact capability labels, with all required labels enforced.
    """

    pool_id: str
    tier: str
    region: str | None = None
    members: tuple[str, ...] = ()
    localities: tuple[str, ...] = ()
    attributes: Mapping[str, AttributeValue] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    active: bool = True

    def __post_init__(self) -> None:
        _name(self.pool_id, "pool_id")
        _name(self.tier, "tier")
        if not isinstance(self.active, bool):
            raise ValueError("active must be a boolean")
        if self.region is not None:
            _name(self.region, "region")
        members = _names(self.members, "members")
        localities = _names(self.localities, "localities")
        if self.active and (self.region is None or not members):
            raise ValueError("active pools require a region and at least one member")
        if not isinstance(self.attributes, Mapping):
            raise ValueError("attributes must be a mapping")
        attributes: dict[str, AttributeValue] = {}
        for name, attribute in self.attributes.items():
            if name not in ATTRIBUTE_UNITS:
                raise ValueError(f"unsupported pool attribute: {name!r}")
            if not isinstance(attribute, AttributeValue):
                raise ValueError(f"attribute {name!r} must be an AttributeValue")
            if attribute.unit != ATTRIBUTE_UNITS[name]:
                raise ValueError(f"{name} unit must be {ATTRIBUTE_UNITS[name]!r}")
            attributes[name] = attribute
        object.__setattr__(self, "members", members)
        object.__setattr__(self, "localities", localities)
        object.__setattr__(self, "attributes", deepcopy(attributes))
        object.__setattr__(self, "metadata", _metadata(self.metadata))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "pool_id": self.pool_id,
            "tier": self.tier,
            "region": self.region,
            "members": list(self.members),
            "localities": list(self.localities),
            "attributes": {
                name: attribute.to_mapping() for name, attribute in sorted(self.attributes.items())
            },
            "metadata": deepcopy(dict(self.metadata)),
            "active": self.active,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Pool:
        values = _fields(
            value,
            required={"pool_id", "tier"},
            optional={"region", "members", "localities", "attributes", "metadata", "active"},
        )
        attributes = values.get("attributes", {})
        if not isinstance(attributes, Mapping):
            raise ValueError("attributes must be a mapping")
        values["attributes"] = {
            name: AttributeValue.from_mapping(attribute) for name, attribute in attributes.items()
        }
        return cls(**values)


@dataclass(frozen=True)
class PlacementConstraints:
    """Hard allowlists and locality requirements, separate from any score.

    ``None`` is unrestricted; an empty allowlist permits no destinations. All
    required localities must be present on a pool. Matching is exact and case
    sensitive; region membership does not imply additional locality labels.
    """

    allowed_regions: tuple[str, ...] | None = None
    required_localities: tuple[str, ...] = ()
    allowed_tiers: tuple[str, ...] | None = None
    allowed_pools: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        for name in ("allowed_regions", "allowed_tiers", "allowed_pools"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _names(value, name))
        object.__setattr__(
            self, "required_localities", _names(self.required_localities, "required_localities")
        )


@dataclass(frozen=True)
class PlacementCandidate:
    tier: Tier
    pool: Pool
    attribute_states: Mapping[str, AttributeState]


def eligible_candidates(
    tiers: Iterable[Tier],
    pools: Iterable[Pool],
    constraints: PlacementConstraints | None = None,
    *,
    now: str | datetime | None = None,
) -> list[PlacementCandidate]:
    """Filter hard constraints before exposing attributes for optimization.

    Missing, stale, and future observations are explicit states, not implicit
    zeros. They do not alter topology eligibility; an objective may reject or
    penalize them. No score callback runs inside this topology-only function.
    """

    constraints = PlacementConstraints() if constraints is None else constraints
    if not isinstance(constraints, PlacementConstraints):
        raise ValueError("constraints must be PlacementConstraints")
    evaluated_at = _now(now)
    by_name: dict[str, Tier] = {}
    for tier_definition in tiers:
        if not isinstance(tier_definition, Tier):
            raise ValueError("tiers must contain Tier values")
        if tier_definition.name in by_name:
            raise ValueError(f"duplicate tier: {tier_definition.name!r}")
        by_name[tier_definition.name] = tier_definition
    result: list[PlacementCandidate] = []
    seen: set[str] = set()
    for pool in pools:
        if not isinstance(pool, Pool):
            raise ValueError("pools must contain Pool values")
        if pool.pool_id in seen:
            raise ValueError(f"duplicate pool: {pool.pool_id!r}")
        seen.add(pool.pool_id)
        tier = by_name.get(pool.tier)
        if tier is None or not tier.active or not pool.active:
            continue
        if pool.region is None or not pool.members:
            continue
        if constraints.allowed_regions is not None and pool.region not in constraints.allowed_regions:
            continue
        if constraints.allowed_tiers is not None and pool.tier not in constraints.allowed_tiers:
            continue
        if constraints.allowed_pools is not None and pool.pool_id not in constraints.allowed_pools:
            continue
        if not set(constraints.required_localities).issubset(pool.localities):
            continue
        states: dict[str, AttributeState] = {
            name: (
                pool.attributes[name].freshness(now=evaluated_at)
                if name in pool.attributes
                else "missing"
            )
            for name in ATTRIBUTE_UNITS
        }
        result.append(PlacementCandidate(deepcopy(tier), deepcopy(pool), states))
    return sorted(result, key=lambda candidate: (candidate.tier.name, candidate.pool.pool_id))


def rank_placements(
    catalog: CatalogStore,
    constraints: PlacementConstraints | None,
    score: Callable[[PlacementCandidate], float],
    *,
    now: str | datetime | None = None,
) -> list[PlacementCandidate]:
    """Score only eligible placements; lower finite scores rank first.

    Tie-breaking uses tier and pool names. This helper provides ordering only;
    budgets and optimization policies remain the caller's responsibility.
    """

    ranked: list[tuple[float, str, str, PlacementCandidate]] = []
    for candidate in catalog.eligible_placements(constraints, now=now):
        value = score(candidate)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("placement score must be a finite number")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("placement score must be a finite number")
        ranked.append((value, candidate.tier.name, candidate.pool.pool_id, candidate))
    ranked.sort(key=lambda entry: entry[:3])
    return [entry[3] for entry in ranked]
