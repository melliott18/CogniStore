"""Validated, deterministic constraints shared by policy and move enforcement."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING

from .audit import canonical_audit_timestamp

if TYPE_CHECKING:
    from .catalog import ObjectRecord

IMPORTANCE_LEVELS = frozenset({"low", "normal", "high", "critical"})
MAX_MINIMUM_RESIDENCY_SECONDS = 315_360_000
MOVEMENT_CONSTRAINTS_METADATA_KEY = "cognistore_movement_constraints"


def _text(value: object, name: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty text without outer whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} must not contain control characters")
    try:
        length = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8") from exc
    if length > limit:
        raise ValueError(f"{name} must be at most {limit} UTF-8 bytes")
    return value


def validate_minimum_residency_seconds(value: object) -> int:
    """Validate the duration accepted in tier metadata and policy configuration."""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_MINIMUM_RESIDENCY_SECONDS
    ):
        raise ValueError(
            "minimum_residency_seconds must be an integer between 0 and "
            f"{MAX_MINIMUM_RESIDENCY_SECONDS}"
        )
    return value


def validate_importance_provenance(value: object) -> str:
    """Validate provenance for both tag assignment and tag removal."""

    return _text(value, "provenance", 2048)


@dataclass(frozen=True)
class ImportanceTag:
    """An explicit importance classification with attributable provenance."""

    level: str
    actor_type: str
    actor_id: str
    provenance: str
    updated_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.level, str) or self.level not in IMPORTANCE_LEVELS:
            raise ValueError("importance level must be low, normal, high, or critical")
        for name, limit in (("actor_type", 64), ("actor_id", 256)):
            _text(getattr(self, name), name, limit)
        validate_importance_provenance(self.provenance)
        object.__setattr__(
            self, "updated_at", canonical_audit_timestamp(self.updated_at, "updated_at")
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "provenance": self.provenance,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ImportanceTag:
        fields = {"level", "actor_type", "actor_id", "provenance", "updated_at"}
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("importance requires level, actor_type, actor_id, provenance, updated_at")
        return cls(**value)  # type: ignore[arg-type]


def _default_importance_tiers() -> dict[str, tuple[str, ...]]:
    return {"high": ("hot", "warm"), "critical": ("hot",)}


@dataclass(frozen=True)
class MovementConstraints:
    """Hard constraints; unspecified importance levels allow every destination.

    Supplying an importance mapping overrides individual defaults. An empty
    sequence intentionally permits no movement for that classification.
    """

    minimum_residency_seconds: Mapping[str, int] = field(default_factory=dict)
    importance_tiers: Mapping[str, tuple[str, ...]] = field(default_factory=_default_importance_tiers)

    def __post_init__(self) -> None:
        if not isinstance(self.minimum_residency_seconds, Mapping):
            raise ValueError("minimum_residency_seconds must map tier names to durations")
        if len(self.minimum_residency_seconds) > 100:
            raise ValueError("minimum_residency_seconds supports at most 100 tiers")
        durations = {
            _text(tier, "tier"): validate_minimum_residency_seconds(duration)
            for tier, duration in self.minimum_residency_seconds.items()
        }
        if not isinstance(self.importance_tiers, Mapping):
            raise ValueError("importance_tiers must map importance levels to tier sequences")
        mappings = _default_importance_tiers()
        for level, tiers in self.importance_tiers.items():
            if level not in IMPORTANCE_LEVELS:
                raise ValueError("importance_tiers levels must be low, normal, high, or critical")
            if not isinstance(tiers, Sequence) or isinstance(tiers, (str, bytes)):
                raise ValueError("importance_tiers values must be tier sequences")
            if len(tiers) > 100:
                raise ValueError("importance_tiers supports at most 100 tiers per level")
            normalized = tuple(_text(tier, "importance tier") for tier in tiers)
            if len(set(normalized)) != len(normalized):
                raise ValueError("importance_tiers must not contain duplicate tiers")
            mappings[level] = normalized
        object.__setattr__(self, "minimum_residency_seconds", MappingProxyType(durations))
        object.__setattr__(self, "importance_tiers", MappingProxyType(mappings))

    def to_dict(self) -> dict[str, object]:
        return {
            "minimum_residency_seconds": dict(sorted(self.minimum_residency_seconds.items())),
            "importance_tiers": {
                level: list(tiers) for level, tiers in sorted(self.importance_tiers.items())
            },
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> MovementConstraints:
        if not isinstance(value, Mapping) or set(value).difference(
            {"minimum_residency_seconds", "importance_tiers"}
        ):
            raise ValueError("movement constraints contain unknown fields or are not a mapping")
        return cls(**value)  # type: ignore[arg-type]


def normalize_movement_constraints(
    value: MovementConstraints | Mapping[str, object] | None,
) -> MovementConstraints:
    if value is None:
        return MovementConstraints()
    return value if isinstance(value, MovementConstraints) else MovementConstraints.from_mapping(value)


class MovementConstraintError(ValueError):
    """The current authoritative placement forbids a requested movement."""

    def __init__(self, message: str, evidence: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.evidence = {} if evidence is None else evidence


def evaluate_movement_constraints(
    record: ObjectRecord,
    controls: MovementConstraints | Mapping[str, object] | None = None,
    *,
    tier_metadata: Mapping[str, object] | None = None,
    as_of: str | datetime | None = None,
) -> dict[str, object]:
    """Return JSON evidence at one instant, without consulting storage or mutating state."""

    config = normalize_movement_constraints(controls)
    evaluated_at = canonical_audit_timestamp(
        datetime.now(timezone.utc) if as_of is None else as_of, "as_of"
    )
    metadata_duration = validate_minimum_residency_seconds(
        (tier_metadata or {}).get("minimum_residency_seconds", 0)
    )
    duration = max(metadata_duration, config.minimum_residency_seconds.get(record.tier, 0))
    placement_started_at = record.placement_started_at
    expires_at: str | None = None
    active = False
    residency_reason: str | None = None
    if duration:
        if placement_started_at is None:
            active = True
            residency_reason = "minimum residency blocks movement: placement start is unknown"
        else:
            start = datetime.fromisoformat(
                canonical_audit_timestamp(placement_started_at).replace("Z", "+00:00")
            )
            try:
                expires_at = canonical_audit_timestamp(start + timedelta(seconds=duration))
            except OverflowError:
                active = True
                residency_reason = "minimum residency blocks movement: expiry exceeds timestamp range"
            else:
                active = evaluated_at < expires_at
                if active:
                    residency_reason = f"minimum residency blocks movement until {expires_at}"
    importance = record.importance
    if importance is not None and not isinstance(importance, ImportanceTag):
        importance = ImportanceTag.from_mapping(importance)
    tiers = None if importance is None else config.importance_tiers.get(importance.level)
    return {
        "as_of": evaluated_at,
        "placement_started_at": placement_started_at,
        "minimum_residency_seconds": duration,
        "residency_expires_at": expires_at,
        "residency_active": active,
        "residency_reason": residency_reason,
        "importance": None if importance is None else importance.to_dict(),
        "importance_revision": record.importance_revision,
        "allowed_destination_tiers": None if tiers is None else list(tiers),
    }


def assert_move_allowed(
    record: ObjectRecord,
    destination: str,
    controls: MovementConstraints | Mapping[str, object] | None = None,
    *,
    tier_metadata: Mapping[str, object] | None = None,
    as_of: str | datetime | None = None,
) -> dict[str, object]:
    """Fail closed on active residency or a forbidden importance destination."""

    evidence = evaluate_movement_constraints(
        record, controls, tier_metadata=tier_metadata, as_of=as_of
    )
    if destination == record.tier:
        return evidence
    if evidence["residency_active"]:
        raise MovementConstraintError(str(evidence["residency_reason"]), evidence)
    allowed = evidence["allowed_destination_tiers"]
    if isinstance(allowed, list) and destination not in allowed:
        raise MovementConstraintError(
            f"importance constraint forbids movement to tier {destination}", evidence
        )
    return evidence
