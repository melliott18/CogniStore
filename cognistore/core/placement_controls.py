"""Validated, deterministic constraints shared by policy and move enforcement."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from numbers import Real
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
class StabilityOverride:
    """An attributable exception to stability guards, never to hard constraints.

    The policy or move audit context supplies the actor; reasons cannot be
    supplied by a policy provider or inferred from an importance tag.
    """

    kind: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {"emergency", "compliance"}:
            raise ValueError("stability override kind must be emergency or compliance")
        _text(self.reason, "stability override reason", 2048)

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "reason": self.reason}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> StabilityOverride:
        if not isinstance(value, Mapping) or set(value) != {"kind", "reason"}:
            raise ValueError("stability override requires kind and reason")
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True)
class MovementConstraints:
    """Hard constraints; unspecified importance levels allow every destination.

    Supplying an importance mapping overrides individual defaults. An empty
    sequence intentionally permits no movement for that classification.
    """

    minimum_residency_seconds: Mapping[str, int] = field(default_factory=dict)
    importance_tiers: Mapping[str, tuple[str, ...]] = field(default_factory=_default_importance_tiers)
    cooldown_seconds: int = 0
    size_hysteresis_bytes: int = 0
    similarity_hysteresis: float = 0.0
    stability_override: StabilityOverride | None = None

    def __post_init__(self) -> None:
        for name, maximum in (
            ("cooldown_seconds", MAX_MINIMUM_RESIDENCY_SECONDS),
            ("size_hysteresis_bytes", 2**63 - 1),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 0 and {maximum}")
        band = self.similarity_hysteresis
        if (
            isinstance(band, bool) or not isinstance(band, Real)
            or not 0 <= band <= 2 or not math.isfinite(float(band))
        ):
            raise ValueError("similarity_hysteresis must be a finite number between 0 and 2")
        object.__setattr__(self, "similarity_hysteresis", float(band))
        override = self.stability_override
        if override is not None and not isinstance(override, StabilityOverride):
            object.__setattr__(self, "stability_override", StabilityOverride.from_mapping(override))
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
        result: dict[str, object] = {
            "minimum_residency_seconds": dict(sorted(self.minimum_residency_seconds.items())),
            "importance_tiers": {
                level: list(tiers) for level, tiers in sorted(self.importance_tiers.items())
            },
        }
        # Preserve old durable move contracts when the new guards are disabled.
        if self.cooldown_seconds:
            result["cooldown_seconds"] = self.cooldown_seconds
        if self.size_hysteresis_bytes:
            result["size_hysteresis_bytes"] = self.size_hysteresis_bytes
        if self.similarity_hysteresis:
            result["similarity_hysteresis"] = self.similarity_hysteresis
        if self.stability_override is not None:
            result["stability_override"] = self.stability_override.to_dict()
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> MovementConstraints:
        if not isinstance(value, Mapping) or set(value).difference(
            {"minimum_residency_seconds", "importance_tiers", "cooldown_seconds",
             "size_hysteresis_bytes", "similarity_hysteresis", "stability_override"}
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
    cooldown_expires_at: str | None = None
    cooldown_active = False
    cooldown_reason: str | None = None
    if config.cooldown_seconds and record.last_tier_move_at is not None:
        move_at = datetime.fromisoformat(
            canonical_audit_timestamp(record.last_tier_move_at).replace("Z", "+00:00")
        )
        try:
            cooldown_expires_at = canonical_audit_timestamp(
                move_at + timedelta(seconds=config.cooldown_seconds)
            )
        except OverflowError:
            cooldown_active = True
            cooldown_reason = "post-move cooldown blocks movement: expiry exceeds timestamp range"
        else:
            cooldown_active = evaluated_at < cooldown_expires_at
            if cooldown_active:
                cooldown_reason = f"post-move cooldown blocks movement until {cooldown_expires_at}"
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
        "last_tier_move_at": record.last_tier_move_at,
        "cooldown_seconds": config.cooldown_seconds,
        "cooldown_expires_at": cooldown_expires_at,
        "cooldown_active": cooldown_active,
        "cooldown_reason": cooldown_reason,
        "size_hysteresis_bytes": config.size_hysteresis_bytes,
        "similarity_hysteresis": config.similarity_hysteresis,
        "stability_override": (
            None if config.stability_override is None else config.stability_override.to_dict()
        ),
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
    if evidence["cooldown_active"] and evidence["stability_override"] is None:
        raise MovementConstraintError(str(evidence["cooldown_reason"]), evidence)
    return evidence
