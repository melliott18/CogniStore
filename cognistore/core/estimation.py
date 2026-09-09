"""Versioned, deterministic operational storage cost and carbon estimates.

Rates are operator-supplied evidence, not live provider prices. Unknown evidence
is preserved and never substituted with a zero. See docs/storage_estimation.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .topology import AttributeValue, PlacementConstraints, Pool, _fields, _name, _timestamp

if TYPE_CHECKING:
    from .catalog import CatalogStore, ObjectRecord

ESTIMATION_SCHEMA_VERSION = 1
ESTIMATION_FORMULA_VERSION = "storage-impact-v1"
# A private context makes replay independent of the caller's Decimal settings.
_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
_GIB = Decimal(1073741824)
_MONTH_HOURS = Decimal(730)
_COMPONENTS = ("storage", "read_requests", "write_requests", "transfer", "retrieval")
_RATE_PREFIXES = ("storage", "read_request", "write_request", "transfer", "retrieval")
RATE_UNITS: Mapping[str, str] = MappingProxyType(
    {
        "storage_price": "USD/GiB-month",
        "read_request_price": "USD/request",
        "write_request_price": "USD/request",
        "transfer_price": "USD/GiB",
        "retrieval_price": "USD/GiB",
        "storage_energy": "kWh/GiB-month",
        "read_request_energy": "kWh/request",
        "write_request_energy": "kWh/request",
        "transfer_energy": "kWh/GiB",
        "retrieval_energy": "kWh/GiB",
        "carbon_intensity": "gCO2e/kWh",
    }
)
WORKLOAD_UNITS: Mapping[str, str] = MappingProxyType(
    {
        "stored_bytes": "bytes",
        "duration_hours": "hours",
        "read_requests": "requests",
        "write_requests": "requests",
        "transfer_bytes": "bytes",
        "retrieval_bytes": "bytes",
    }
)


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{name} must be a finite non-negative decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite non-negative decimal") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} must be a finite non-negative decimal")
    # Count significant digits independently of notation, so accepted values
    # survive serialization from scientific notation to fixed decimal strings.
    digits = result.as_tuple().digits
    significant_digits = len(digits)
    while significant_digits > 1 and digits[significant_digits - 1] == 0:
        significant_digits -= 1
    # Bound inputs so multiplication, JSON encoding and replay stay tractable.
    if significant_digits > 50 or (result and abs(result.adjusted()) > 100):
        raise ValueError(f"{name} exceeds the supported decimal range")
    return result


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if not value:
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _time(value: str | datetime, name: str) -> str:
    return _timestamp(value, name).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class RateAssumption:
    """A versioned rate (or calibration multiplier) and optional scenario bounds.

    Bounds are conservative input ranges, not statistical confidence intervals.
    Both dates are inclusive. Omitted bounds mean uncertainty is unquantified.
    """

    value: Decimal | str | int | float
    unit: str
    source: str
    source_version: str
    effective_from: str
    effective_until: str
    lower: Decimal | str | int | float | None = None
    upper: Decimal | str | int | float | None = None

    def __post_init__(self) -> None:
        value = _decimal(self.value, "rate value")
        for name in ("unit", "source", "source_version"):
            _name(getattr(self, name), name)
        start = _time(self.effective_from, "effective_from")
        end = _time(self.effective_until, "effective_until")
        if _timestamp(end, "effective_until") < _timestamp(start, "effective_from"):
            raise ValueError("effective_until must not precede effective_from")
        if (self.lower is None) != (self.upper is None):
            raise ValueError("lower and upper must be supplied together")
        lower = None if self.lower is None else _decimal(self.lower, "lower")
        upper = None if self.upper is None else _decimal(self.upper, "upper")
        if lower is not None and upper is not None and not lower <= value <= upper:
            raise ValueError("bounds must contain the rate value")
        for name, normalized in (
            ("value", value),
            ("lower", lower),
            ("upper", upper),
            ("effective_from", start),
            ("effective_until", end),
        ):
            object.__setattr__(self, name, normalized)

    def state(self, as_of: str | datetime) -> str:
        now = _timestamp(as_of, "as_of")
        if now < _timestamp(self.effective_from, "effective_from"):
            return "future"
        if now > _timestamp(self.effective_until, "effective_until"):
            return "stale"
        return "fresh"

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": _text(_decimal(self.value, "value")),
            "unit": self.unit,
            "source": self.source,
            "source_version": self.source_version,
            "effective_from": self.effective_from,
            "effective_until": self.effective_until,
            "lower": None if self.lower is None else _text(_decimal(self.lower, "lower")),
            "upper": None if self.upper is None else _text(_decimal(self.upper, "upper")),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RateAssumption:
        return cls(
            **_fields(
                value,
                required={
                    "value",
                    "unit",
                    "source",
                    "source_version",
                    "effective_from",
                    "effective_until",
                },
                optional={"lower", "upper"},
            )
        )


@dataclass(frozen=True)
class EstimationProfile:
    """An explicit pool/backend rate set with optional per-rate calibration."""

    version: str
    backend: str
    rates: Mapping[str, RateAssumption] = field(default_factory=dict)
    calibration: Mapping[str, RateAssumption] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _name(self.version, "profile version")
        _name(self.backend, "backend")
        for field_name in ("rates", "calibration"):
            values = getattr(self, field_name)
            if not isinstance(values, Mapping):
                raise ValueError(f"{field_name} must be a mapping")
            detached: dict[str, RateAssumption] = {}
            for name, assumption in values.items():
                if name not in RATE_UNITS:
                    raise ValueError(f"unsupported rate: {name!r}")
                if not isinstance(assumption, RateAssumption):
                    raise ValueError(f"{name} must be a RateAssumption")
                unit = RATE_UNITS[name] if field_name == "rates" else "multiplier"
                if assumption.unit != unit:
                    raise ValueError(f"{name} unit must be {unit!r}")
                detached[name] = assumption
            object.__setattr__(self, field_name, MappingProxyType(dict(sorted(detached.items()))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "backend": self.backend,
            "rates": {name: rate.to_dict() for name, rate in self.rates.items()},
            "calibration": {name: rate.to_dict() for name, rate in self.calibration.items()},
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> EstimationProfile:
        values = _fields(value, required={"version", "backend"}, optional={"rates", "calibration"})
        for name in ("rates", "calibration"):
            raw = values.get(name, {})
            if not isinstance(raw, Mapping):
                raise ValueError(f"{name} must be a mapping")
            values[name] = {key: RateAssumption.from_mapping(rate) for key, rate in raw.items()}
        return cls(**values)


@dataclass(frozen=True)
class EstimationWorkload:
    """Usage over one horizon; None means unknown, zero is an explicit input.

    Request counts and byte counts are non-negative integers. Transfer bytes
    represent traffic billed to this pool; retrieval is billed separately.
    """

    stored_bytes: Decimal | str | int | float | None = None
    duration_hours: Decimal | str | int | float | None = None
    read_requests: Decimal | str | int | float | None = None
    write_requests: Decimal | str | int | float | None = None
    transfer_bytes: Decimal | str | int | float | None = None
    retrieval_bytes: Decimal | str | int | float | None = None

    def __post_init__(self) -> None:
        for name in WORKLOAD_UNITS:
            raw = getattr(self, name)
            if raw is None:
                continue
            value = _decimal(raw, name)
            if name != "duration_hours" and value != value.to_integral_value():
                raise ValueError(f"{name} must be an integer")
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, Any]:
        return {
            name: None if getattr(self, name) is None else _text(getattr(self, name))
            for name in WORKLOAD_UNITS
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> EstimationWorkload:
        return cls(**_fields(value, required=set(), optional=set(WORKLOAD_UNITS)))


@dataclass(frozen=True)
class EstimateComponent:
    name: str
    unit: str
    value: Decimal | None
    lower: Decimal | None
    upper: Decimal | None
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "unit": self.unit,
            "state": "available" if self.value is not None else "unavailable",
            "value": _text(self.value),
            "lower": _text(self.lower),
            "upper": _text(self.upper),
            "uncertainty": "bounded" if self.lower is not None else "unquantified",
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ImpactTotal:
    unit: str
    components: tuple[EstimateComponent, ...]

    @property
    def value(self) -> Decimal | None:
        if any(component.value is None for component in self.components):
            return None
        return self.known_subtotal

    @property
    def known_subtotal(self) -> Decimal:
        with localcontext(_CONTEXT):
            return sum(
                (item.value for item in self.components if item.value is not None), Decimal(0)
            )

    def to_dict(self) -> dict[str, Any]:
        with localcontext(_CONTEXT):
            bounded = all(item.lower is not None for item in self.components)
            lower = sum(
                (item.lower for item in self.components if item.lower is not None), Decimal(0)
            )
            upper = sum(
                (item.upper for item in self.components if item.upper is not None), Decimal(0)
            )
        return {
            "unit": self.unit,
            "state": "available" if self.value is not None else "unavailable",
            "value": _text(self.value),
            "known_subtotal": _text(self.known_subtotal),
            "lower": _text(lower) if bounded else None,
            "upper": _text(upper) if bounded else None,
            "uncertainty": "bounded" if bounded else "unquantified",
            "components": [item.to_dict() for item in self.components],
        }


@dataclass(frozen=True)
class _ResolvedRate:
    name: str
    assumption: RateAssumption | None
    calibration: RateAssumption | None
    state: str
    origin: str
    value: Decimal | None
    lower: Decimal | None
    upper: Decimal | None
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "unit": RATE_UNITS[self.name],
            "state": self.state,
            "origin": self.origin,
            "value": _text(self.value),
            "lower": _text(self.lower),
            "upper": _text(self.upper),
            "assumption": None if self.assumption is None else self.assumption.to_dict(),
            "calibration": None if self.calibration is None else self.calibration.to_dict(),
            "reasons": list(self.reasons),
        }


def formula_manifest() -> dict[str, Any]:
    """Return the versioned units, conversions and equations used in every result."""
    return {
        "version": ESTIMATION_FORMULA_VERSION,
        "source": "cognistore/core/estimation.py",
        "effective_from": "2026-09-09T00:00:00Z",
        "decimal_precision": 50,
        "rounding": "ROUND_HALF_EVEN",
        "bytes_per_GiB": "1073741824",
        "hours_per_month": "730",
        "workload_units": dict(WORKLOAD_UNITS),
        "rate_units": dict(RATE_UNITS),
        "quantities": {
            "storage": "(stored_bytes / bytes_per_GiB) * (duration_hours / hours_per_month)",
            "read_requests": "read_requests",
            "write_requests": "write_requests",
            "transfer": "transfer_bytes / bytes_per_GiB",
            "retrieval": "retrieval_bytes / bytes_per_GiB",
        },
        "calibration": "effective_rate = rate * calibration_multiplier (default identity 1)",
        "cost": "sum(quantity[component] * effective_price[component])",
        "carbon": "sum(quantity[component] * effective_energy[component] * effective_carbon_intensity)",
        "bounds": "non-negative interval products per component; sum lower and upper separately",
        "scope": "operational electricity emissions and modeled usage charges over one horizon",
    }


@dataclass(frozen=True)
class PlacementEstimate:
    tier: str | None
    pool_id: str | None
    region: str | None
    backend: str | None
    profile_version: str | None
    as_of: str
    workload: EstimationWorkload
    cost: ImpactTotal
    carbon: ImpactTotal
    rates: tuple[_ResolvedRate, ...]
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ESTIMATION_SCHEMA_VERSION,
            "formulas": formula_manifest(),
            "tier": self.tier,
            "pool_id": self.pool_id,
            "region": self.region,
            "backend": self.backend,
            "profile_version": self.profile_version,
            "as_of": self.as_of,
            "workload": self.workload.to_dict(),
            "cost": self.cost.to_dict(),
            "carbon": self.carbon.to_dict(),
            "rates": [rate.to_dict() for rate in self.rates],
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ObjectPlacementEstimates:
    current: PlacementEstimate
    candidates: tuple[PlacementEstimate, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ESTIMATION_SCHEMA_VERSION,
            "current": self.current.to_dict(),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


def _from_topology(attribute: AttributeValue) -> RateAssumption:
    observed = _timestamp(attribute.observed_at, "observed_at")
    # Timestamps have microsecond resolution. Round expiration down so a
    # fractional freshness window never admits an already-stale observation.
    with localcontext(_CONTEXT):
        age_microseconds = int(Decimal(str(attribute.max_age_seconds)) * Decimal(1000000))
    # Extremely long freshness windows saturate at the representable UTC date.
    try:
        until = observed + timedelta(microseconds=age_microseconds)
    except OverflowError:
        until = datetime.max.replace(tzinfo=observed.tzinfo)
    return RateAssumption(
        value=attribute.value,
        unit=attribute.unit,
        source=attribute.source,
        source_version="topology-attribute-v1",
        effective_from=attribute.observed_at,
        effective_until=until.isoformat(),
    )


class StorageImpactEstimator:
    """Estimate supplied usage against explicit pool profiles and topology.

    No provider APIs, catalog writes, budget enforcement or scoring is performed.
    An explicit profile rate overrides topology, including when expired.
    """

    def __init__(self, profiles: Mapping[str, EstimationProfile]) -> None:
        if not isinstance(profiles, Mapping):
            raise ValueError("profiles must be a mapping")
        detached: dict[str, EstimationProfile] = {}
        for pool_id, profile in profiles.items():
            _name(pool_id, "pool_id")
            if not isinstance(profile, EstimationProfile):
                raise ValueError("profiles must contain EstimationProfile values")
            detached[pool_id] = profile
        self.profiles: Mapping[str, EstimationProfile] = MappingProxyType(detached)

    def estimate(
        self,
        pool: Pool | None,
        workload: EstimationWorkload,
        *,
        as_of: str | datetime,
    ) -> PlacementEstimate:
        if pool is not None and not isinstance(pool, Pool):
            raise ValueError("pool must be a Pool or None")
        if not isinstance(workload, EstimationWorkload):
            raise ValueError("workload must be EstimationWorkload")
        evaluated_at = _time(as_of, "as_of")
        profile = None if pool is None else self.profiles.get(pool.pool_id)
        with localcontext(_CONTEXT):
            rates = {name: self._resolve(name, pool, profile, evaluated_at) for name in RATE_UNITS}
            quantities = self._quantities(workload)
            cost = self._impact("USD", "price", quantities, rates)
            carbon = self._impact("gCO2e", "energy", quantities, rates)
        reasons = []
        if pool is None:
            reasons.append("placement_pool_unavailable")
        if profile is None:
            reasons.append("profile_missing")
        return PlacementEstimate(
            tier=None if pool is None else pool.tier,
            pool_id=None if pool is None else pool.pool_id,
            region=None if pool is None else pool.region,
            backend=None if profile is None else profile.backend,
            profile_version=None if profile is None else profile.version,
            as_of=evaluated_at,
            workload=workload,
            cost=cost,
            carbon=carbon,
            rates=tuple(rates.values()),
            reasons=tuple(reasons),
        )

    def estimate_object(
        self,
        catalog: CatalogStore,
        record: ObjectRecord,
        workload: EstimationWorkload,
        constraints: PlacementConstraints | None = None,
        *,
        as_of: str | datetime,
    ) -> ObjectPlacementEstimates:
        from .catalog import ObjectRecord

        if not isinstance(record, ObjectRecord):
            raise ValueError("record must be ObjectRecord")
        if not isinstance(workload, EstimationWorkload):
            raise ValueError("workload must be EstimationWorkload")
        evaluated_at = _time(as_of, "as_of")
        object_workload = replace(workload, stored_bytes=record.size)
        current_pool = None if record.pool_id is None else catalog.get_pool(record.pool_id)
        if current_pool is not None and current_pool.tier != record.tier:
            current_pool = None
        current = self.estimate(current_pool, object_workload, as_of=evaluated_at)
        if current_pool is None:
            current = replace(current, tier=record.tier, pool_id=record.pool_id)
        # Only destinations that passed hard eligibility reach the estimator.
        candidates = catalog.eligible_placements(constraints, now=evaluated_at)
        return ObjectPlacementEstimates(
            current,
            tuple(
                self.estimate(candidate.pool, object_workload, as_of=evaluated_at)
                for candidate in candidates
            ),
        )

    @staticmethod
    def _resolve(
        name: str,
        pool: Pool | None,
        profile: EstimationProfile | None,
        as_of: str,
    ) -> _ResolvedRate:
        assumption = None if profile is None else profile.rates.get(name)
        calibration = None if profile is None else profile.calibration.get(name)
        origin = "profile"
        if assumption is None and pool is not None:
            attribute_name = {"storage_price": "price", "carbon_intensity": "carbon_intensity"}.get(
                name
            )
            attribute = None if attribute_name is None else pool.attributes.get(attribute_name)
            if attribute is not None:
                assumption = _from_topology(attribute)
                origin = "topology"
        state = "missing" if assumption is None else assumption.state(as_of)
        reasons = [] if state == "fresh" else [f"{name}:{state}"]
        if calibration is not None and calibration.state(as_of) != "fresh":
            reasons.append(f"{name}:calibration_{calibration.state(as_of)}")
        value = lower = upper = None
        if not reasons:
            assert assumption is not None
            value = _decimal(assumption.value, name)
            lower = None if assumption.lower is None else _decimal(assumption.lower, name)
            upper = None if assumption.upper is None else _decimal(assumption.upper, name)
            if calibration is not None:
                value *= _decimal(calibration.value, name)
                if lower is not None and upper is not None and calibration.lower is not None:
                    lower *= _decimal(calibration.lower, name)
                    upper *= _decimal(calibration.upper, name)
                else:
                    lower = upper = None
        return _ResolvedRate(
            name,
            assumption,
            calibration,
            "fresh" if not reasons else state if state != "fresh" else "unavailable",
            origin,
            value,
            lower,
            upper,
            tuple(reasons),
        )

    @staticmethod
    def _quantities(
        workload: EstimationWorkload,
    ) -> dict[str, tuple[Decimal | None, tuple[str, ...]]]:
        quantities: dict[str, tuple[Decimal | None, tuple[str, ...]]] = {}
        for component, fields in (
            ("storage", ("stored_bytes", "duration_hours")),
            ("read_requests", ("read_requests",)),
            ("write_requests", ("write_requests",)),
            ("transfer", ("transfer_bytes",)),
            ("retrieval", ("retrieval_bytes",)),
        ):
            missing = tuple(
                f"workload.{name}:missing" for name in fields if getattr(workload, name) is None
            )
            if missing:
                quantities[component] = None, missing
                continue
            value = _decimal(getattr(workload, fields[0]), fields[0])
            if component in ("storage", "transfer", "retrieval"):
                value /= _GIB
            if component == "storage":
                value *= _decimal(workload.duration_hours, "duration_hours") / _MONTH_HOURS
            quantities[component] = value, ()
        return quantities

    @staticmethod
    def _impact(
        unit: str,
        suffix: str,
        quantities: Mapping[str, tuple[Decimal | None, tuple[str, ...]]],
        rates: Mapping[str, _ResolvedRate],
    ) -> ImpactTotal:
        components = []
        for name, prefix in zip(_COMPONENTS, _RATE_PREFIXES):
            quantity, missing = quantities[name]
            inputs = [rates[f"{prefix}_{suffix}"]]
            if suffix == "energy":
                inputs.append(rates["carbon_intensity"])
            reasons = (*missing, *(reason for rate in inputs for reason in rate.reasons))
            value = lower = upper = None
            if not reasons:
                assert quantity is not None
                value = lower = upper = quantity
                for rate in inputs:
                    assert rate.value is not None
                    value *= rate.value
                    if lower is not None and upper is not None and rate.lower is not None:
                        assert rate.upper is not None
                        lower *= rate.lower
                        upper *= rate.upper
                    else:
                        lower = upper = None
            components.append(EstimateComponent(name, unit, value, lower, upper, reasons))
        return ImpactTotal(unit, tuple(components))
