"""Strict, content-free reasons stored beside the immutable policy snapshot.

Audit v1 and snapshot v1 remain unchanged. Absence means legacy evidence, never
an inferred explanation. Confidence is explicitly unavailable until a provider
with a calibrated confidence contract is supported.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cognistore.utils.redaction import redact

from .audit import canonical_audit_timestamp

POLICY_REASON_SCHEMA_VERSION = 1
ReasonCode = Literal[
    "size_threshold", "name_rule", "mime_rule", "embedding_rule",
    "required_features_unavailable", "provider_decision", "provider_error",
    "provider_invalid_response", "provider_invalid_input", "custom_policy", "minimum_residency",
    "importance_restriction", "cooldown", "hysteresis", "destination_not_allowed",
    "destination_missing", "already_in_tier", "invalid_action",
]
Number = Annotated[int | float, Field(allow_inf_nan=False)]
NonnegativeInt = Annotated[int, Field(ge=0)]
Identity = Annotated[str, Field(min_length=1)]
FeatureState = Literal["fresh", "stale", "missing", "unavailable"]


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class DecisiveSignal(_Contract):
    name: Literal[
        "size_bytes", "name_match", "mime_match", "mime_state", "embedding_state",
        "features_evaluated", "embedding_similarity",
    ]
    value: Number | bool | FeatureState | None
    operator: Literal["<=", ">", ">=", "=="] | None
    threshold: Number | None
    rule_index: NonnegativeInt | None

    @model_validator(mode="after")
    def valid_signal(self) -> DecisiveSignal:
        if self.name in {"size_bytes", "embedding_similarity"}:
            if type(self.value) not in {int, float} or self.threshold is None:
                raise ValueError("numeric signals require a value and threshold")
            if self.operator not in {"<=", ">", ">="}:
                raise ValueError("numeric signals require a comparison")
        elif self.name.endswith("_state"):
            if self.value not in {"fresh", "stale", "missing", "unavailable"}:
                raise ValueError("state signals require a feature state")
        elif type(self.value) is not bool:
            raise ValueError("match signals require a boolean")
        return self


class HysteresisCheck(_Contract):
    kind: Literal["size", "similarity"]
    configured_band: Number
    baseline_threshold: Number
    effective_threshold: Number
    value: Number | None
    rule_index: NonnegativeInt | None


class ReasonConstraints(_Contract):
    evaluated_at: str
    importance_level: Literal["low", "normal", "high", "critical"] | None
    importance_revision: NonnegativeInt
    importance_allowed_tiers: list[Identity] | None
    allowed_destination_tiers: list[Identity]
    placement_started_at: str | None
    minimum_residency_seconds: NonnegativeInt
    residency_expires_at: str | None
    residency_active: bool
    last_tier_move_at: str | None
    cooldown_seconds: NonnegativeInt
    cooldown_expires_at: str | None
    cooldown_active: bool
    size_hysteresis_bytes: NonnegativeInt
    similarity_hysteresis: Annotated[float, Field(ge=0, le=2)]
    stability_override_kind: Literal["emergency", "compliance"] | None
    rejected_destination_tier: Identity | None
    candidate_action: Literal["move", "stay"] | None
    candidate_destination_tier: Identity | None
    hysteresis_checks: list[HysteresisCheck]

    @model_validator(mode="after")
    def valid_times(self) -> ReasonConstraints:
        for name in (
            "evaluated_at", "placement_started_at", "residency_expires_at",
            "last_tier_move_at", "cooldown_expires_at",
        ):
            value = getattr(self, name)
            if value is not None:
                setattr(self, name, canonical_audit_timestamp(value, name))
        return self


class ReasonModel(_Contract):
    identity: Identity
    version: Identity | None


class ReasonPolicy(_Contract):
    name: Identity
    version: Identity
    model: ReasonModel | None


class ReasonConfidence(_Contract):
    # No current policy supplies calibrated probabilities. Similarity is a signal.
    value: None
    source: Literal["not_reported", "not_applicable"]


class PolicyReason(_Contract):
    schema_version: Literal[1]
    code: ReasonCode
    disposition: Literal["move", "stay", "suppressed", "rejected"]
    decisive_signals: list[DecisiveSignal]
    constraints: ReasonConstraints
    policy: ReasonPolicy
    confidence: ReasonConfidence


def validate_policy_reason(value: object) -> dict[str, Any]:
    """Detach and validate the entire v1 contract, rejecting unknown fields."""
    if not isinstance(value, Mapping) or type(value.get("schema_version")) is not int:
        raise ValueError("policy reason requires an integer schema_version")
    result = PolicyReason.model_validate(value).model_dump(mode="json")
    if redact(result) != result:
        raise ValueError("policy reason contains unredacted credentials")
    return result


def reason_from_audit_details(details: object) -> dict[str, Any] | None:
    """Read a retained reason; legacy rows return None, corrupt rows fail closed."""
    if not isinstance(details, Mapping) or "structured_reason" not in details:
        return None
    return validate_policy_reason(details["structured_reason"])


def capture_policy_reason(
    *,
    code: str | None,
    signals: list[dict[str, object]],
    constraints: Mapping[str, Any],
    policy_metadata: Mapping[str, Any],
    action: str,
    destination: str | None,
    current_tier: str,
    trusted_policy: bool,
    embedding_rule_names: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Project trusted evaluator evidence; never serialize arbitrary reason text."""
    code = code if trusted_policy and code is not None else "custom_policy"
    signals = signals if trusted_policy else []
    disposition = "move" if action == "move" else "stay"
    if constraints.get("residency_active"):
        code, disposition = "minimum_residency", "suppressed"
    elif constraints.get("blocked_reason") and constraints.get("suppression_reason") != "cooldown":
        code, disposition = "importance_restriction", "suppressed"
    elif constraints.get("suppression_reason") in {"cooldown", "hysteresis"}:
        code, disposition = constraints["suppression_reason"], "suppressed"
    elif code == "destination_not_allowed" and action == "stay":
        disposition = "suppressed"
    elif action == "move":
        if not destination:
            code, disposition = "destination_missing", "rejected"
        elif destination == current_tier:
            code, disposition = "already_in_tier", "rejected"
        elif destination not in constraints["allowed_destination_tiers"]:
            code, disposition = "destination_not_allowed", "rejected"
    elif action != "stay":
        code, disposition = "invalid_action", "rejected"

    importance = constraints.get("importance") or {}
    override = constraints.get("stability_override") or {}
    hysteresis = (constraints.get("hysteresis") or {}) if trusted_policy else {}
    checks = []
    for check in hysteresis.get("checks", []):
        rule = check.get("rule")
        checks.append({
            name: check.get(name) for name in (
                "kind", "configured_band", "baseline_threshold", "effective_threshold", "value",
            )
        } | {"rule_index": (
            embedding_rule_names.index(rule) if rule in embedding_rule_names else None
        )})
    evidence = {name: constraints.get(name) for name in (
        "importance_revision", "importance_allowed_tiers", "allowed_destination_tiers",
        "placement_started_at", "minimum_residency_seconds", "residency_expires_at",
        "residency_active", "last_tier_move_at", "cooldown_seconds", "cooldown_expires_at",
        "cooldown_active", "size_hysteresis_bytes", "similarity_hysteresis",
        "rejected_destination_tier",
    )}
    evidence.update({
        "evaluated_at": constraints["as_of"],
        "importance_level": importance.get("level"),
        "stability_override_kind": override.get("kind"),
        "candidate_action": hysteresis.get("candidate_action"),
        "candidate_destination_tier": hysteresis.get("candidate_destination_tier"),
        "hysteresis_checks": checks,
    })
    return validate_policy_reason(redact({
        "schema_version": POLICY_REASON_SCHEMA_VERSION,
        "code": code,
        "disposition": disposition,
        "decisive_signals": signals,
        "constraints": evidence,
        "policy": {name: policy_metadata[name] for name in ("name", "version", "model")},
        "confidence": {
            "value": None,
            "source": (
                "not_applicable" if code not in {
                    "provider_decision", "provider_error", "provider_invalid_response",
                    "provider_invalid_input", "custom_policy",
                } else "not_reported"
            ),
        },
    }))
