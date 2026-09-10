"""Versioned, redacted decision evidence and deterministic offline policy replay.

Snapshot v1 is additive inside audit-v1 ``details.dataset``. Legacy audit rows
have no snapshot; they cannot be upcast into evidence that was never captured.
Replay only constructs explicitly supported builtins and never loads providers.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from cognistore.utils.redaction import redact

from .access import (
    ACCESS_KINDS,
    AccessConfig,
    AccessFeatures,
    AccessSnapshot,
    AccessWindow,
)
from .audit import AuditOutcome, audit_text_identity, canonical_audit_timestamp
from .catalog import ObjectRecord
from .estimation import (
    RATE_UNITS,
    EstimationProfile,
    EstimationWorkload,
    ObjectPlacementEstimates,
    PlacementEstimate,
    RateAssumption,
    StorageImpactEstimator,
    formula_manifest,
)
from .policy import ContentAwarePolicy, EmbeddingPolicyRule, LLMPolicy, PolicyDecision, SimplePolicy
from .policy_factory import ThresholdProvider
from .policy_features import (
    EmbeddingPolicyFeature,
    FeatureState,
    MimePolicyFeature,
    PolicyFeatureProvenance,
    PolicyFeatures,
)
from .topology import Pool

POLICY_SNAPSHOT_SCHEMA_VERSION = 1
_CONTENT_LISTS = (
    "hot_name_patterns",
    "warm_name_patterns",
    "cold_name_patterns",
    "hot_mime_prefixes",
    "warm_mime_prefixes",
    "cold_mime_prefixes",
)


def _object(
    value: object, fields: set[str], name: str, optional: set[str] | None = None
) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be a JSON object")
    if fields - value.keys() or value.keys() - fields - (optional or set()):
        raise ValueError(f"{name} has missing or unknown fields")
    return value


def _version(value: object, name: str) -> None:
    if type(value) is not int or value != 1:
        raise ValueError(f"unsupported {name} schema_version: {value!r}")


def _text(value: object, name: str, *, nullable: bool = False, empty: bool = False) -> Any:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or (not value and not empty):
        raise ValueError(f"{name} must be text")
    return value


def _strings(value: object, name: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return [_text(item, name) for item in value]


def _number(value: object, name: str, *, integer: bool = False) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a nonnegative number")
    if integer and not isinstance(value, int):
        raise ValueError(f"{name} must be a nonnegative integer")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a nonnegative finite number")
    return value


def _integer(value: object, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return value


def _timestamp(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 timestamp")
    return canonical_audit_timestamp(value, name)


def _json_copy(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("policy snapshot must be a JSON object")

    def check_keys(item: object) -> None:
        if isinstance(item, Mapping):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("snapshot object keys must be strings")
            for child in item.values():
                check_keys(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check_keys(child)

    try:
        # JSON round trips detach caller-owned data and reject nonfinite values.
        check_keys(value)
        result = json.loads(json.dumps(value, allow_nan=False, ensure_ascii=False))
        json.dumps(result, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError) as exc:
        raise ValueError("policy snapshot must contain finite JSON values") from exc
    return result


def _access_from_dict(value: object) -> AccessFeatures:
    data = _object(
        value,
        {
            "schema_version",
            "bucket",
            "key",
            "as_of",
            "windows",
            "estimated_windows",
            "last_access_at",
            "observed_since",
            "observed_events",
            "recency_seconds",
            "freshness",
            "freshness_seconds",
            "freshness_basis",
            "sampling",
            "missing",
            "partial",
            "coverage",
            "retention_seconds",
            "reason",
        },
        "access features",
    )
    _version(data["schema_version"], "access feature")
    _text(data["bucket"], "access bucket")
    _text(data["key"], "access key", nullable=True)
    as_of = _timestamp(data["as_of"], "access as_of")
    windows = data["windows"]
    estimates = data["estimated_windows"]
    if (
        not isinstance(windows, dict)
        or not isinstance(estimates, dict)
        or set(windows) != set(estimates)
    ):
        raise ValueError("access windows and estimated_windows must match")
    sampling = _object(
        data["sampling"], {"configured_rate", "minimum_rate", "sampled"}, "access sampling"
    )
    try:
        seconds = tuple(int(key) for key in windows)
    except (ValueError, TypeError) as exc:
        raise ValueError("access window keys must be integer seconds") from exc
    if any(str(second) not in windows for second in seconds):
        raise ValueError("access window keys must be canonical integer seconds")
    config = AccessConfig(
        windows_seconds=seconds,
        retention_seconds=data["retention_seconds"],
        freshness_seconds=data["freshness_seconds"],
        sample_rate=sampling["configured_rate"],
    )
    projected_windows = []
    for second in config.windows_seconds:
        counts = _object(windows[str(second)], set(ACCESS_KINDS), "access window counts")
        estimated = _object(estimates[str(second)], set(ACCESS_KINDS), "estimated access counts")
        projected_windows.append(
            AccessWindow(
                second,
                tuple(_number(counts[kind], "access count", integer=True) for kind in ACCESS_KINDS),
                tuple(_number(estimated[kind], "estimated access count") for kind in ACCESS_KINDS),
            )
        )
    _number(data["observed_events"], "observed_events", integer=True)
    minimum_rate = sampling["minimum_rate"]
    if minimum_rate is not None:
        _number(minimum_rate, "minimum_rate")
        if not 1e-9 <= minimum_rate <= 1:
            raise ValueError("access minimum_rate must be between 1e-9 and 1")
    for name in ("last_access_at", "observed_since"):
        if data[name] is not None and _timestamp(data[name], name) > as_of:
            raise ValueError(f"access {name} cannot be after as_of")
    if data["recency_seconds"] is not None:
        _number(data["recency_seconds"], "recency_seconds")
    if data["freshness"] not in {"fresh", "stale", "missing", "unavailable"}:
        raise ValueError("invalid access freshness")
    _text(data["reason"], "access reason", nullable=True, empty=True)
    feature = AccessFeatures(
        data["bucket"],
        data["key"],
        data["as_of"],
        AccessSnapshot(
            tuple(projected_windows),
            data["observed_events"],
            data["observed_since"],
            data["last_access_at"],
            minimum_rate,
        ),
        config,
        data["freshness"],
        data["recency_seconds"],
        data["reason"],
    )
    for name in ("missing", "partial"):
        if type(data[name]) is not bool:
            raise ValueError(f"access {name} must be boolean")
    if type(sampling["sampled"]) is not bool or feature.to_dict() != data:
        raise ValueError("access feature derived fields are inconsistent")
    return feature


def _placement_estimate_from_dict(value: object) -> PlacementEstimate:
    data = _object(
        value,
        {
            "schema_version", "formulas", "tier", "pool_id", "region", "backend",
            "profile_version", "as_of", "workload", "cost", "carbon", "rates", "reasons",
        },
        "placement estimate",
    )
    _version(data["schema_version"], "placement estimate")
    if data["formulas"] != formula_manifest():
        raise ValueError("unsupported placement estimate formulas")
    for name in ("tier", "pool_id", "region", "backend", "profile_version"):
        _text(data[name], f"placement estimate {name}", nullable=True)
    as_of = _timestamp(data["as_of"], "placement estimate as_of")
    workload = EstimationWorkload.from_mapping(data["workload"])
    if not isinstance(data["rates"], list) or len(data["rates"]) != len(RATE_UNITS):
        raise ValueError("placement estimate must retain every rate")
    assumptions = {}
    calibration = {}
    origins = []
    for name, value in zip(RATE_UNITS, data["rates"]):
        rate = _object(
            value,
            {
                "name", "unit", "state", "origin", "value", "lower", "upper",
                "assumption", "calibration", "reasons",
            },
            "estimated rate",
        )
        if rate["name"] != name or rate["origin"] not in {"profile", "topology"}:
            raise ValueError("invalid placement estimate rate name or origin")
        if rate["origin"] == "topology" and name not in {"storage_price", "carbon_intensity"}:
            raise ValueError("unsupported topology rate origin")
        origins.append(rate["origin"])
        if rate["assumption"] is not None:
            assumptions[name] = RateAssumption.from_mapping(rate["assumption"])
        if rate["calibration"] is not None:
            calibration[name] = RateAssumption.from_mapping(rate["calibration"])
    # Recompute only from frozen evidence: this validates derived states,
    # decimal totals and bounds without looking up live topology or profiles.
    profile = EstimationProfile("snapshot", "snapshot", assumptions, calibration)
    pool = Pool("snapshot", "snapshot", region="snapshot", members=("snapshot",))
    estimate = StorageImpactEstimator({pool.pool_id: profile}).estimate(
        pool, workload, as_of=as_of
    )
    estimate = replace(
        estimate,
        **{name: data[name] for name in ("tier", "pool_id", "region", "backend", "profile_version")},
        rates=tuple(replace(rate, origin=origin) for rate, origin in zip(estimate.rates, origins)),
        reasons=tuple(_strings(data["reasons"], "placement estimate reasons")),
    )
    if estimate.to_dict() != data:
        raise ValueError("placement estimate derived fields are inconsistent")
    return estimate


def _placement_estimates_from_dict(value: object) -> ObjectPlacementEstimates:
    data = _object(value, {"schema_version", "current", "candidates"}, "placement estimates")
    _version(data["schema_version"], "placement estimates")
    if not isinstance(data["candidates"], list):
        raise ValueError("placement estimate candidates must be a list")
    current = _placement_estimate_from_dict(data["current"])
    candidates = tuple(_placement_estimate_from_dict(item) for item in data["candidates"])
    if any(
        candidate.as_of != current.as_of or candidate.workload != current.workload
        for candidate in candidates
    ):
        raise ValueError("placement estimates must share a workload and as_of")
    return ObjectPlacementEstimates(current, candidates)


def _features_from_dict(value: object) -> PolicyFeatures:
    data = _object(
        value, {"schema_version", "mime", "embeddings"}, "features",
        {"access", "placement_estimates"},
    )
    _version(data["schema_version"], "policy feature")

    def provenance(value: object) -> PolicyFeatureProvenance:
        fields = _object(
            value, {"source", "source_version", "content_sha256", "details"}, "feature provenance"
        )
        return PolicyFeatureProvenance(**fields)

    mime = _object(data["mime"], {"state", "value", "provenance"}, "MIME feature")
    if not isinstance(data["embeddings"], list):
        raise ValueError("embeddings must be a list")
    embeddings = []
    for value in data["embeddings"]:
        embedding = _object(
            value, {"name", "query", "state", "similarity", "provenance"}, "embedding feature"
        )
        embeddings.append(
            EmbeddingPolicyFeature(
                name=embedding["name"],
                query=embedding["query"],
                state=FeatureState(embedding["state"]),
                similarity=embedding["similarity"],
                provenance=provenance(embedding["provenance"]),
            )
        )
    return PolicyFeatures(
        mime=MimePolicyFeature(
            FeatureState(mime["state"]), provenance(mime["provenance"]), mime["value"]
        ),
        embeddings=tuple(embeddings),
        access=_access_from_dict(data["access"]) if "access" in data else None,
        placement_estimates=(
            _placement_estimates_from_dict(data["placement_estimates"])
            if "placement_estimates" in data else None
        ),
    )


def _policy_description(policy: object) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    # Exact classes deliberately exclude subclasses with altered evaluation.
    if type(policy) is SimplePolicy:
        return (
            "simple",
            {
                "size_threshold": policy.size_threshold,
                "allowed_tiers": sorted(policy.allowed_tiers),
            },
            None,
        )
    if type(policy) is ContentAwarePolicy:
        config = {
            "size_threshold": policy.size_threshold,
            "allowed_tiers": sorted(policy.allowed_tiers),
        }
        config.update({name: list(getattr(policy, name)) for name in _CONTENT_LISTS})
        config["embedding_rules"] = [rule.to_mapping() for rule in policy.embedding_rules]
        return "content", config, None
    if type(policy) is LLMPolicy and type(policy.provider) is ThresholdProvider:
        return (
            "llm-threshold",
            {
                "allowed_tiers": sorted(policy.allowed_tiers),
                "threshold": policy.provider.threshold,
                "provider_allowed_tiers": sorted(policy.provider.allowed_tiers),
            },
            {"identity": "ThresholdProvider", "version": "1"},
        )
    return "unsupported", {}, None


def _policy_from_dict(
    value: dict[str, Any],
) -> SimplePolicy | ContentAwarePolicy | LLMPolicy | None:
    implementation = value["implementation"]
    config = value["config"]
    if implementation == "unsupported":
        _object(config, set(), "unsupported policy config")
        return None
    if implementation == "llm-threshold":
        _object(
            config,
            {"allowed_tiers", "threshold", "provider_allowed_tiers"},
            "threshold policy config",
        )
        return LLMPolicy(
            ThresholdProvider(
                _integer(config["threshold"], "threshold"),
                _strings(config["provider_allowed_tiers"], "provider allowed_tiers"),
            ),
            _strings(config["allowed_tiers"], "policy allowed_tiers"),
        )
    fields = {"size_threshold", "allowed_tiers"}
    if implementation == "content":
        fields.update((*_CONTENT_LISTS, "embedding_rules"))
    elif implementation != "simple":
        raise ValueError(f"unsupported policy implementation: {implementation!r}")
    _object(config, fields, "policy config")
    threshold = _integer(config["size_threshold"], "size_threshold")
    tiers = _strings(config["allowed_tiers"], "policy allowed_tiers")
    if implementation == "simple":
        return SimplePolicy(threshold, tiers)
    lists = {name: _strings(config[name], name) for name in _CONTENT_LISTS}
    if not isinstance(config["embedding_rules"], list):
        raise ValueError("embedding_rules must be a list")
    policy = ContentAwarePolicy(
        size_threshold=threshold,
        allowed_tiers=tiers,
        hot_name_patterns=lists["hot_name_patterns"],
        warm_name_patterns=lists["warm_name_patterns"],
        hot_mime_prefixes=lists["hot_mime_prefixes"],
        warm_mime_prefixes=lists["warm_mime_prefixes"],
        embedding_rules=[
            EmbeddingPolicyRule.from_mapping(rule) for rule in config["embedding_rules"]
        ],
    )
    policy.cold_name_patterns = lists["cold_name_patterns"]
    policy.cold_mime_prefixes = lists["cold_mime_prefixes"]
    return policy


def _validate_policy_snapshot(value: object) -> dict[str, Any]:
    data = _json_copy(value)
    _version(data.get("schema_version"), "policy snapshot")
    _object(
        data,
        {
            "schema_version",
            "decision_at",
            "object",
            "features",
            "policy",
            "allowed_tiers",
            "decision",
            "provenance",
            "replay",
        },
        "policy snapshot",
    )
    decision_at = _timestamp(data["decision_at"], "decision_at")
    record = _object(data["object"], {"bucket", "key", "size", "tier", "pool_id"}, "object")
    for name in ("bucket", "key", "tier"):
        _text(record[name], f"object {name}")
    _text(record["pool_id"], "pool_id", nullable=True)
    _number(record["size"], "object size", integer=True)
    features = _features_from_dict(data["features"])
    if features.placement_estimates is not None:
        current = features.placement_estimates.current
        if _timestamp(current.as_of, "placement estimate as_of") > decision_at:
            raise ValueError("placement estimate as_of cannot be after decision_at")
        if current.workload.stored_bytes != record["size"] or (
            current.tier, current.pool_id
        ) != (record["tier"], record["pool_id"]):
            raise ValueError("placement estimates must identify the snapshot object")
    if features.access is not None:
        if _timestamp(features.access.as_of, "access as_of") > decision_at:
            raise ValueError("access as_of cannot be after decision_at")
        if (features.access.bucket, features.access.key) != (record["bucket"], record["key"]):
            raise ValueError("access features must identify the snapshot object")
    _strings(data["allowed_tiers"], "allowed_tiers")
    policy = _object(
        data["policy"], {"name", "version", "implementation", "config", "model"}, "policy"
    )
    for name in ("name", "version", "implementation"):
        _text(policy[name], f"policy {name}")
    if policy["model"] is not None:
        model = _object(policy["model"], {"identity", "version"}, "model")
        _text(model["identity"], "model identity")
        _text(model["version"], "model version", nullable=True)
    reconstructed = _policy_from_dict(policy)
    decision = _object(
        data["decision"], {"action", "destination_tier", "reason", "outcome"}, "decision"
    )
    _text(decision["action"], "decision action")
    _text(decision["destination_tier"], "decision destination_tier", nullable=True)
    _text(decision["reason"], "decision reason", empty=True)
    if decision["outcome"] not in {
        AuditOutcome.SELECTED.value,
        AuditOutcome.REJECTED.value,
        AuditOutcome.STAYED.value,
    }:
        raise ValueError("invalid policy decision outcome")
    expected_outcome = (
        AuditOutcome.SELECTED.value
        if decision["action"] == "move"
        and decision["destination_tier"]
        and decision["destination_tier"] != record["tier"]
        and decision["destination_tier"] in data["allowed_tiers"]
        else AuditOutcome.REJECTED.value
        if decision["action"] == "move"
        else AuditOutcome.STAYED.value
    )
    inputs_redacted = data["replay"] == {
        "supported": False,
        "reason": "required_inputs_redacted",
    }
    # Redacted tier identities can lose destination equality/membership facts,
    # but they never change whether the original action was a move or stay.
    if (decision["action"] == "move") != (decision["outcome"] != AuditOutcome.STAYED.value):
        raise ValueError("decision outcome contradicts action")
    if decision["outcome"] == AuditOutcome.SELECTED.value and not decision["destination_tier"]:
        raise ValueError("selected decisions require a destination")
    if not inputs_redacted and decision["outcome"] != expected_outcome:
        raise ValueError("decision outcome contradicts action or allowed destinations")
    provenance = _object(data["provenance"], {"source", "schema_version"}, "provenance")
    _version(provenance["schema_version"], "snapshot provenance")
    if provenance["source"] != "policy-runner":
        raise ValueError("unsupported snapshot provenance source")
    replay = _object(data["replay"], {"supported", "reason"}, "replay")
    if type(replay["supported"]) is not bool:
        raise ValueError("replay supported must be boolean")
    _text(replay["reason"], "replay reason", nullable=True)
    if replay["supported"] and (reconstructed is None or replay["reason"] is not None):
        raise ValueError("replay support contradicts policy or reason")
    if not replay["supported"] and replay["reason"] is None:
        raise ValueError("non-replayable snapshots require a reason")
    return data


def validate_policy_snapshot(value: object) -> dict[str, Any]:
    """Validate snapshot v1 and return detached JSON, rejecting unknown versions."""
    try:
        return _validate_policy_snapshot(value)
    except (TypeError, KeyError, OverflowError, RecursionError) as exc:
        raise ValueError("malformed policy snapshot") from exc


def capture_policy_snapshot(
    *,
    record: ObjectRecord,
    features: PolicyFeatures,
    policy: object,
    allowed_tiers: Sequence[str],
    decision: PolicyDecision,
    outcome: AuditOutcome | str,
    policy_name: str,
    policy_version: str,
    decision_at: str | datetime | None = None,
    model_identity: str | None = None,
    model_version: str | None = None,
) -> dict[str, Any]:
    """Freeze allowlisted inputs and actual output, redacting before persistence.

    Custom/external policies retain output for analysis but cannot be replayed.
    Model metadata is explicit: provider ``__dict__`` and arbitrary catalog
    metadata are never inspected or serialized.
    """
    if model_version is not None and model_identity is None:
        raise ValueError("model_version requires model_identity")
    implementation, config, model = _policy_description(policy)
    # Inference snapshots retain only the explicit, already-redacted model
    # identity. The full prompt/response audit lives beside snapshot v1 in
    # audit details; provider attributes and credentials are never inspected.
    if isinstance(decision.llm_audit, Mapping):
        inferred_identity = decision.llm_audit.get("model")
        inferred_version = decision.llm_audit.get("model_version")
        if isinstance(inferred_identity, str) and inferred_identity:
            model = {
                "identity": inferred_identity,
                "version": inferred_version if isinstance(inferred_version, str) else None,
            }
    if model_identity is not None:
        model = {"identity": model_identity, "version": model_version}
    supported = implementation != "unsupported"
    replay_reason = None if supported else "unsupported_policy_or_provider"
    # Snapshot v1 predates numerical entry/exit bands. Preserve its schema and
    # disable replay instead of reconstructing a zero-band policy whose result
    # can disagree with a directly configured built-in policy.
    if supported and (
        bool(getattr(policy, "size_hysteresis_bytes", 0))
        or implementation == "content" and bool(getattr(policy, "similarity_hysteresis", 0))
    ):
        supported = False
        replay_reason = "hysteresis_not_in_snapshot_v1"
    snapshot: dict[str, Any] = {
        "schema_version": POLICY_SNAPSHOT_SCHEMA_VERSION,
        "decision_at": canonical_audit_timestamp(
            datetime.now(timezone.utc) if decision_at is None else decision_at
        ),
        "object": {
            "bucket": record.bucket,
            "key": record.key,
            "size": record.size,
            "tier": record.tier,
            "pool_id": record.pool_id,
        },
        "features": features.to_dict(),
        "policy": {
            "name": policy_name,
            "version": policy_version,
            "implementation": implementation,
            "config": config,
            "model": model,
        },
        "allowed_tiers": sorted(allowed_tiers),
        "decision": {
            "action": decision.action,
            "destination_tier": decision.dst_tier,
            "reason": decision.reason,
            "outcome": AuditOutcome(outcome).value,
        },
        "provenance": {"source": "policy-runner", "schema_version": 1},
        "replay": {
            "supported": supported,
            "reason": replay_reason,
        },
    }
    safe = redact(snapshot)
    # Keep redacted rule identities distinct and aligned with feature names.
    # A shared literal replacement could merge otherwise valid semantic rules
    # and make even their non-replayable evidence impossible to deserialize.
    for original, redacted in zip(
        snapshot["features"]["embeddings"], safe["features"]["embeddings"]
    ):
        if original["name"] != redacted["name"]:
            redacted["name"] = audit_text_identity(original["name"])
    if implementation == "content":
        for original, redacted in zip(
            config["embedding_rules"], safe["policy"]["config"]["embedding_rules"]
        ):
            if original["name"] != redacted["name"]:
                redacted["name"] = audit_text_identity(original["name"])
    # Any redacted evidence is unsuitable for claiming an exact replay.
    if any(
        safe[name] != snapshot[name] for name in ("object", "features", "policy", "allowed_tiers")
    ):
        safe["replay"] = {"supported": False, "reason": "required_inputs_redacted"}
    return validate_policy_snapshot(safe)


def replay_policy_snapshot(value: object) -> PolicyDecision:
    """Evaluate a supported frozen policy without live catalog or provider reads."""
    snapshot = validate_policy_snapshot(value)
    if not snapshot["replay"]["supported"]:
        raise ValueError(f"policy snapshot is not replayable: {snapshot['replay']['reason']}")
    policy = _policy_from_dict(snapshot["policy"])
    record = ObjectRecord(**snapshot["object"])
    features = _features_from_dict(snapshot["features"])
    if isinstance(policy, ContentAwarePolicy):
        return policy.evaluate_features(record, features)
    assert policy is not None
    return policy.evaluate(record.tier, record.size)


def policy_snapshot_features(value: object) -> PolicyFeatures:
    """Restore the recorded feature projection for a retry's source fencing."""
    return _features_from_dict(validate_policy_snapshot(value)["features"])


def snapshot_from_audit_details(details: object) -> dict[str, Any] | None:
    """Upcast audit details: absence is explicit legacy evidence, never fabricated.

    Future snapshot versions must add an explicit migration here (and document
    its information loss) before they become readable by v1 consumers.
    """
    if not isinstance(details, Mapping):
        raise ValueError("audit details must be a JSON object")
    if "dataset" not in details:
        return None
    return validate_policy_snapshot(details["dataset"])
