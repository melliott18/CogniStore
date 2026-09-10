from __future__ import annotations

import fnmatch
import json
import math
from dataclasses import dataclass
from numbers import Real
from typing import Iterable, Mapping, Protocol, Sequence, cast

from cognistore.utils.redaction import redact

from .catalog import ObjectRecord
from .placement_llm import (
    CallablePlacementProvider,
    PlacementInference,
    PlacementLLMProvider,
)
from .policy_features import (
    CatalogPolicyFeatureLoader,
    EmbeddingFeatureRequest,
    FeatureState,
    PolicyFeatures,
)
from .policy_stability import NumericalHysteresis, size_boundary

MAX_EMBEDDING_RULE_NAME_BYTES = 256
MAX_EMBEDDING_RULE_QUERY_BYTES = 16 * 1024
MAX_EMBEDDING_RULES = 100
MAX_POLICY_CONFIG_BYTES = 64 * 1024


@dataclass
class PolicyDecision:
    action: str  # e.g., "stay", "move"
    reason: str
    dst_tier: str | None = None
    hysteresis: dict[str, object] | None = None
    llm_audit: dict[str, object] | None = None


class Policy(Protocol):
    def evaluate(self, current_tier: str, size: int) -> PolicyDecision:  # pragma: no cover - interface
        ...


class SimplePolicy(NumericalHysteresis):
    """A placeholder policy: small files -> hot, else warm.

    In the future this will be replaced or augmented by LLM-based reasoning.
    """

    def __init__(
        self,
        size_threshold: int = 1024 * 1024,
        allowed_tiers: Sequence[str] = ("hot", "warm"),
        *,
        size_hysteresis_bytes: int = 0,
    ):
        self.size_threshold = size_threshold
        self.allowed_tiers = frozenset(allowed_tiers)
        self.size_hysteresis_bytes = size_hysteresis_bytes
        self.similarity_hysteresis = 0

    def evaluate(self, current_tier: str, size: int) -> PolicyDecision:
        threshold, evidence = size_boundary(
            threshold=self.size_threshold,
            band=self.size_hysteresis_bytes,
            current_tier=current_tier,
            size=size,
            allowed_tiers=self.allowed_tiers,
        )
        if (
            size <= threshold
            and current_tier != "hot"
            and "hot" in self.allowed_tiers
        ):
            return PolicyDecision(
                action="move", dst_tier="hot", reason="small object -> hot tier",
                hysteresis=evidence,
            )
        if (
            size > threshold
            and current_tier != "warm"
            and "warm" in self.allowed_tiers
        ):
            return PolicyDecision(
                action="move", dst_tier="warm", reason="large object -> warm tier",
                hysteresis=evidence,
            )
        reason = "meets tier policy"
        if evidence and evidence["suppressed"]:
            reason = f"size hysteresis holds current tier at {threshold} bytes"
        return PolicyDecision("stay", reason, hysteresis=evidence)


class LLMPolicy(NumericalHysteresis):
    """Schema-validated, bounded inference with an auditable stay fallback.

    Deployment adapters implement ``PlacementLLMProvider``. The historical
    ``decide(dict)`` interface remains supported through the same validator;
    its optional stay destination is normalized to JSON null. Only explicit
    historical ThresholdProvider instances retain their numerical replay path.
    """

    def __init__(
        self,
        provider: PlacementLLMProvider | PolicyLLMProvider | None = None,
        allowed_tiers: Sequence[str] = ("hot", "warm"),
        *,
        size_hysteresis_bytes: int = 0,
        inference: PlacementInference | None = None,
        timeout_seconds: float = 10,
        max_attempts: int = 2,
    ):
        if provider is not None and inference is not None:
            raise ValueError("supply either provider or inference")
        self.provider = provider if inference is None else inference.provider
        self.allowed_tiers = set(allowed_tiers)
        self.size_hysteresis_bytes = size_hysteresis_bytes
        self.similarity_hysteresis = 0
        self.inference = inference or PlacementInference(
            cast(PlacementLLMProvider | None, provider),
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )

    def evaluate(self, current_tier: str, size: int) -> PolicyDecision:
        from .policy_factory import ThresholdProvider

        if type(self.provider) is ThresholdProvider:
            return self._evaluate_threshold(current_tier, size)
        inference = self.inference
        if self.provider is not None and not callable(getattr(self.provider, "complete", None)):
            # Compatibility adapts only the old wire format, never model output
            # into a valid move. No catalog identity/content crosses this boundary.
            inputs = redact({
                "current_tier": current_tier,
                "size": size,
                "allowed_tiers": sorted(self.allowed_tiers),
            })
            legacy = cast(PolicyLLMProvider, self.provider)

            def complete(prompt: str, schema: dict, timeout_seconds: float) -> str:
                result = legacy.decide(inputs)
                if isinstance(result, dict) and result.get("action") == "stay":
                    result = {"dst_tier": None, **result}
                return json.dumps(result, allow_nan=False)

            inference = PlacementInference(
                CallablePlacementProvider(
                    complete, provider_id="legacy-decide",
                    model=type(self.provider).__name__, version="1",
                ),
                timeout_seconds=self.inference.timeout_seconds,
                max_attempts=self.inference.max_attempts,
            )
        result = inference.evaluate(
            current_tier=current_tier, size=size,
            allowed_tiers=sorted(self.allowed_tiers),
        )
        return PolicyDecision(
            result.action, result.reason, result.dst_tier, llm_audit=result.audit,
        )

    def _evaluate_threshold(self, current_tier: str, size: int) -> PolicyDecision:
        """Preserve explicit historical snapshot replay; never a default fallback."""
        payload = {
            "current_tier": current_tier,
            "size": size,
            "allowed_tiers": sorted(self.allowed_tiers),
        }
        if self.size_hysteresis_bytes:
            payload["size_hysteresis_bytes"] = self.size_hysteresis_bytes
        try:
            result = cast(PolicyLLMProvider, self.provider).decide(payload) or {}
        except Exception as error:  # defensive
            return PolicyDecision(
                action="stay",
                dst_tier=None,
                reason=f"provider_error:{type(error).__name__}",
            )

        action = result.get("action")
        dst = result.get("dst_tier")
        provider_reason = result.get("reason")
        reason = (
            provider_reason[:512]
            if isinstance(provider_reason, str) and provider_reason
            else "llm policy no reason provided"
        )
        evidence = result.get("hysteresis")
        if not isinstance(evidence, dict):
            evidence = None

        if action == "move" and isinstance(dst, str) and dst in self.allowed_tiers and dst != current_tier:
            return PolicyDecision(action="move", dst_tier=dst, reason=reason, hysteresis=evidence)
        # default safe behavior
        return PolicyDecision(action="stay", dst_tier=None, reason=reason, hysteresis=evidence)


class PolicyLLMProvider(Protocol):  # pragma: no cover - interface
    def decide(self, inputs: dict) -> dict:
        ...


@dataclass(frozen=True)
class EmbeddingPolicyRule:
    """One named prototype-query classification used by content policy.

    The embedding service projects a cosine-similarity score for ``query``.
    A fresh score at or above ``minimum_similarity`` classifies the object as
    ``name`` and selects ``destination_tier``.  Rule order is deterministic
    and first match wins.
    """

    name: str
    query: str
    minimum_similarity: float
    destination_tier: str

    def __post_init__(self) -> None:
        for field_name, limit in (
            ("name", MAX_EMBEDDING_RULE_NAME_BYTES),
            ("query", MAX_EMBEDDING_RULE_QUERY_BYTES),
            ("destination_tier", 256),
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(
                    f"embedding rule {field_name} must be a non-empty string "
                    "without outer whitespace"
                )
            if "\0" in value or any(ord(character) < 32 for character in value):
                raise ValueError(
                    f"embedding rule {field_name} must not contain control characters"
                )
            try:
                encoded = value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError(
                    f"embedding rule {field_name} must be valid UTF-8"
                ) from exc
            if len(encoded) > limit:
                raise ValueError(
                    f"embedding rule {field_name} must be at most {limit} UTF-8 bytes"
                )
        threshold = self.minimum_similarity
        if isinstance(threshold, bool) or not isinstance(threshold, Real):
            raise ValueError("embedding rule minimum_similarity must be a finite number")
        converted = float(threshold)
        if not math.isfinite(converted) or not -1.0 <= converted <= 1.0:
            raise ValueError(
                "embedding rule minimum_similarity must be between -1 and 1"
            )
        object.__setattr__(self, "minimum_similarity", converted)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "EmbeddingPolicyRule":
        if not isinstance(value, Mapping):
            raise ValueError("embedding rules must be mappings")
        if any(not isinstance(key, str) for key in value):
            raise ValueError("embedding rule field names must be strings")
        expected = {"name", "query", "minimum_similarity", "destination_tier"}
        unknown = sorted(set(value).difference(expected))
        missing = sorted(expected.difference(value))
        if unknown:
            raise ValueError(
                "embedding rule contains unknown field(s): " + ", ".join(unknown)
            )
        if missing:
            raise ValueError(
                "embedding rule is missing field(s): " + ", ".join(missing)
            )
        return cls(
            name=value["name"],  # type: ignore[arg-type]
            query=value["query"],  # type: ignore[arg-type]
            minimum_similarity=value["minimum_similarity"],  # type: ignore[arg-type]
            destination_tier=value["destination_tier"],  # type: ignore[arg-type]
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "query": self.query,
            "minimum_similarity": self.minimum_similarity,
            "destination_tier": self.destination_tier,
        }


def validate_policy_config_size(
    *,
    allowed_tiers: Iterable[str],
    hot_name_patterns: Iterable[str],
    warm_name_patterns: Iterable[str],
    cold_name_patterns: Iterable[str],
    hot_mime_prefixes: Iterable[str],
    warm_mime_prefixes: Iterable[str],
    cold_mime_prefixes: Iterable[str],
    embedding_rules: Iterable[EmbeddingPolicyRule],
) -> None:
    """Enforce the aggregate policy-string limit used by public requests."""

    values = [
        *allowed_tiers,
        *hot_name_patterns,
        *warm_name_patterns,
        *cold_name_patterns,
        *hot_mime_prefixes,
        *warm_mime_prefixes,
        *cold_mime_prefixes,
        *(
            value
            for rule in embedding_rules
            for value in (rule.name, rule.query, rule.destination_tier)
        ),
    ]
    try:
        payload_bytes = sum(len(value.encode("utf-8")) for value in values)
    except UnicodeEncodeError as exc:
        raise ValueError("policy strings must be valid UTF-8") from exc
    if payload_bytes > MAX_POLICY_CONFIG_BYTES:
        raise ValueError(
            f"policy strings must total at most {MAX_POLICY_CONFIG_BYTES} bytes"
        )


class ContentAwarePolicy(NumericalHysteresis):
    """Policy that uses content metadata to decide placement.

    Rules are evaluated in the following order (first match wins):
      1. Filename patterns for hot (move to hot) and warm (move to warm)
      2. MIME prefix lists for hot and warm
      3. Configured embedding classifications/similarity rules
      4. Fallback to size threshold (small -> hot, large -> warm)

    Notes:
      - Only tiers present in `allowed_tiers` are considered valid destinations.
      - If the suggested destination equals the current tier, action is "stay".
    """

    def __init__(
        self,
        *,
        size_threshold: int = 1024 * 1024,
        allowed_tiers: Sequence[str] = ("hot", "warm"),
        hot_name_patterns: Iterable[str] | None = None,
        warm_name_patterns: Iterable[str] | None = None,
        hot_mime_prefixes: Iterable[str] | None = None,
        warm_mime_prefixes: Iterable[str] | None = None,
        embedding_rules: Iterable[EmbeddingPolicyRule] | None = None,
        size_hysteresis_bytes: int = 0,
        similarity_hysteresis: float = 0,
    ) -> None:
        self.size_threshold = size_threshold
        self.size_hysteresis_bytes = size_hysteresis_bytes
        self.similarity_hysteresis = similarity_hysteresis
        # ``allowed`` predates the public ``allowed_tiers`` spelling and is
        # intentionally mutable for callers that add runtime content rules.
        self.allowed = set(allowed_tiers)
        self.hot_name_patterns = [p for p in (hot_name_patterns or []) if p]
        self.warm_name_patterns = [p for p in (warm_name_patterns or []) if p]
        self.hot_mime_prefixes = [m for m in (hot_mime_prefixes or []) if m]
        self.warm_mime_prefixes = [m for m in (warm_mime_prefixes or []) if m]
        self.embedding_rules = tuple(embedding_rules or ())
        if len(self.embedding_rules) > MAX_EMBEDDING_RULES:
            raise ValueError(
                f"content policy supports at most {MAX_EMBEDDING_RULES} embedding rules"
            )
        if any(not isinstance(rule, EmbeddingPolicyRule) for rule in self.embedding_rules):
            raise ValueError("embedding_rules must contain EmbeddingPolicyRule values")
        duplicate_names = sorted(
            name
            for name in {rule.name for rule in self.embedding_rules}
            if sum(rule.name == name for rule in self.embedding_rules) > 1
        )
        if duplicate_names:
            raise ValueError(
                "embedding rule names must be unique: " + ", ".join(duplicate_names)
            )
        unknown_destinations = sorted(
            {
                rule.destination_tier
                for rule in self.embedding_rules
                if rule.destination_tier not in self.allowed
            }
        )
        if unknown_destinations:
            raise ValueError(
                "embedding rule destination tier(s) are not allowed: "
                + ", ".join(unknown_destinations)
            )
        # Optional cold-tier hints
        self.cold_name_patterns: list[str] = []
        self.cold_mime_prefixes: list[str] = []

    @property
    def allowed_tiers(self) -> set[str]:
        return self.allowed

    @allowed_tiers.setter
    def allowed_tiers(self, tiers: Sequence[str]) -> None:
        self.allowed = set(tiers)

    @property
    def feature_requests(self) -> tuple[EmbeddingFeatureRequest, ...]:
        return tuple(
            EmbeddingFeatureRequest(name=rule.name, query=rule.query)
            for rule in self.embedding_rules
        )

    @property
    def _requires_features(self) -> bool:
        return bool(
            self.hot_mime_prefixes
            or self.warm_mime_prefixes
            or self.cold_mime_prefixes
            or self.embedding_rules
        )

    # Keep compatibility: provide size-based evaluate
    def evaluate(self, current_tier: str, size: int) -> PolicyDecision:
        # A caller that bypasses the feature-aware contract must never turn an
        # unevaluated semantic rule into an unrelated size-based move.
        if self._requires_features:
            return PolicyDecision(
                action="stay",
                reason="required policy features unavailable: features not evaluated",
                dst_tier=None,
            )
        return self._evaluate_size(current_tier, size)

    def _evaluate_size(self, current_tier: str, size: int) -> PolicyDecision:
        threshold, evidence = size_boundary(
            threshold=self.size_threshold,
            band=self.size_hysteresis_bytes,
            current_tier=current_tier,
            size=size,
            allowed_tiers=self.allowed,
        )
        if size <= threshold and current_tier != "hot" and "hot" in self.allowed:
            return PolicyDecision("move", f"<= {threshold} bytes", "hot", evidence)
        if size > threshold and current_tier != "warm" and "warm" in self.allowed:
            return PolicyDecision("move", f"> {threshold} bytes", "warm", evidence)
        reason = "meets content policy by size"
        if evidence and evidence["suppressed"]:
            reason = f"size hysteresis holds current tier at {threshold} bytes"
        return PolicyDecision("stay", reason, hysteresis=evidence)

    # Record-aware evaluation used by PolicyRunner when available
    def evaluate_record(self, rec: "ObjectRecord") -> PolicyDecision:
        # Preserve this public compatibility path without bypassing feature
        # validation. Semantic providers are intentionally absent here, so a
        # configured embedding rule deterministically projects as missing.
        features = CatalogPolicyFeatureLoader().load(
            (rec,),
            self.feature_requests,
        )[(rec.bucket, rec.key)]
        return self.evaluate_features(rec, features)

    def evaluate_features(
        self,
        rec: "ObjectRecord",
        features: PolicyFeatures,
    ) -> PolicyDecision:
        """Evaluate one backend-neutral, source-compatible feature projection.

        A configured rule may move an object only from a fresh signal. A
        missing, stale, or unavailable signal fails closed before any
        lower-priority semantic rule can select a destination whose precedence
        cannot be established. Independent filename rules retain their
        established highest precedence.
        """

        current_tier = rec.tier
        key = rec.key

        # 1) Stable catalog identity rules do not depend on indexing providers.
        for patterns, destination, label in (
            (self.hot_name_patterns, "hot", "name pattern -> hot"),
            (self.warm_name_patterns, "warm", "name pattern -> warm"),
            (self.cold_name_patterns, "cold", "name pattern -> cold"),
        ):
            if key and patterns and any(fnmatch.fnmatch(key, pattern) for pattern in patterns):
                if destination in self.allowed and destination != current_tier:
                    return PolicyDecision("move", label, destination)
                return PolicyDecision("stay", f"{label}; already constrained", None)

        # 2) MIME selection consumes the versioned projection, not catalog JSON.
        mime_rules = (
            (self.hot_mime_prefixes, "hot"),
            (self.warm_mime_prefixes, "warm"),
            (self.cold_mime_prefixes, "cold"),
        )
        if any(prefixes for prefixes, _destination in mime_rules):
            if features.mime.state is not FeatureState.FRESH:
                return PolicyDecision(
                    action="stay",
                    dst_tier=None,
                    reason=(
                        "required policy features unavailable: "
                        f"mime={features.mime.state.value}"
                    ),
                )
            assert features.mime.value is not None
            for prefixes, destination in mime_rules:
                if prefixes and any(
                    features.mime.value.startswith(prefix) for prefix in prefixes
                ):
                    reason = f"mime {features.mime.value} -> {destination}"
                    if destination in self.allowed and destination != current_tier:
                        return PolicyDecision("move", reason, destination)
                    return PolicyDecision(
                        "stay", f"{reason}; already constrained", None
                    )

        # 3) Named semantic rules are evaluated in configured order.
        semantic_features = {feature.name: feature for feature in features.embeddings}
        similarity_checks: list[dict[str, object]] = []

        def with_similarity_evidence(decision: PolicyDecision) -> PolicyDecision:
            if not similarity_checks:
                return decision
            size_evidence = decision.hysteresis or {}
            size_checks = size_evidence.get("checks", [])
            assert isinstance(size_checks, list)
            decision.hysteresis = {
                "checks": [*similarity_checks, *size_checks],
                # This describes a held classification. The runner compares
                # complete baseline/guarded decisions because a later rule
                # may still select a move after an earlier entry was held.
                "suppressed": bool(size_evidence.get("suppressed")) or any(
                    check["baseline_match"] != check["effective_match"]
                    for check in similarity_checks
                ),
            }
            return decision

        for rule in self.embedding_rules:
            feature = semantic_features.get(rule.name)
            if feature is None or feature.state is not FeatureState.FRESH:
                state = FeatureState.MISSING if feature is None else feature.state
                return with_similarity_evidence(PolicyDecision(
                    action="stay",
                    dst_tier=None,
                    reason=(
                        "required policy features unavailable: "
                        f"embedding:{rule.name}={state.value}"
                    ),
                ))
            similarity = feature.similarity
            # Retaining the current tier has the lower exit boundary; entering
            # any other tier has the higher entry boundary. Do not clamp these
            # boundaries to [-1, 1]: an unreachable entry is intentional.
            threshold = rule.minimum_similarity
            if self.similarity_hysteresis:
                threshold += (
                    -self.similarity_hysteresis
                    if rule.destination_tier == current_tier
                    else self.similarity_hysteresis
                )
            if self.similarity_hysteresis:
                similarity_checks.append({
                    "kind": "similarity",
                    "rule": rule.name,
                    "destination_tier": rule.destination_tier,
                    "configured_band": self.similarity_hysteresis,
                    "baseline_threshold": rule.minimum_similarity,
                    "effective_threshold": threshold,
                    "value": similarity,
                    "baseline_match": (
                        similarity is not None and similarity >= rule.minimum_similarity
                    ),
                    "effective_match": similarity is not None and similarity >= threshold,
                })
            if similarity is not None and similarity >= threshold:
                reason = (
                    f"embedding {rule.name} similarity {similarity:.6f} "
                    f">= {threshold:.6f} -> {rule.destination_tier}"
                )
                if (
                    rule.destination_tier in self.allowed
                    and rule.destination_tier != current_tier
                ):
                    return with_similarity_evidence(PolicyDecision(
                        "move",
                        reason,
                        rule.destination_tier,
                    ))
                return with_similarity_evidence(
                    PolicyDecision("stay", f"{reason}; already constrained", None)
                )

        # 4) Every configured signal was fresh but non-matching.
        return with_similarity_evidence(self._evaluate_size(current_tier, rec.size))
