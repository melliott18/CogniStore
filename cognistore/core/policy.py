from __future__ import annotations

import fnmatch
import math
from dataclasses import dataclass
from numbers import Real
from typing import Iterable, Mapping, Protocol, Sequence

from .catalog import ObjectRecord
from .policy_features import (
    CatalogPolicyFeatureLoader,
    EmbeddingFeatureRequest,
    FeatureState,
    PolicyFeatures,
)

MAX_EMBEDDING_RULE_NAME_BYTES = 256
MAX_EMBEDDING_RULE_QUERY_BYTES = 16 * 1024
MAX_EMBEDDING_RULES = 100
MAX_POLICY_CONFIG_BYTES = 64 * 1024


@dataclass
class PolicyDecision:
    action: str  # e.g., "stay", "move"
    reason: str
    dst_tier: str | None = None


class Policy(Protocol):
    def evaluate(self, current_tier: str, size: int) -> PolicyDecision:  # pragma: no cover - interface
        ...


class SimplePolicy:
    """A placeholder policy: small files -> hot, else warm.

    In the future this will be replaced or augmented by LLM-based reasoning.
    """

    def __init__(
        self,
        size_threshold: int = 1024 * 1024,
        allowed_tiers: Sequence[str] = ("hot", "warm"),
    ):
        self.size_threshold = size_threshold
        self.allowed_tiers = frozenset(allowed_tiers)

    def evaluate(self, current_tier: str, size: int) -> PolicyDecision:
        if (
            size <= self.size_threshold
            and current_tier != "hot"
            and "hot" in self.allowed_tiers
        ):
            return PolicyDecision(action="move", dst_tier="hot", reason="small object -> hot tier")
        if (
            size > self.size_threshold
            and current_tier != "warm"
            and "warm" in self.allowed_tiers
        ):
            return PolicyDecision(action="move", dst_tier="warm", reason="large object -> warm tier")
        return PolicyDecision(action="stay", dst_tier=None, reason="meets tier policy")


class LLMPolicy:
    """Policy that delegates decision-making to an LLM Provider.

    The provider receives a structured payload and must return a dict with keys:
      {"action": "move"|"stay", "dst_tier": Optional[str], "reason": str}
    Unknown or invalid outputs default to a safe "stay" decision.
    """

    def __init__(self, provider: "PolicyLLMProvider", allowed_tiers: Sequence[str] = ("hot", "warm")):
        self.provider = provider
        self.allowed_tiers = set(allowed_tiers)

    def evaluate(self, current_tier: str, size: int) -> PolicyDecision:
        payload = {
            "current_tier": current_tier,
            "size": size,
            "allowed_tiers": sorted(self.allowed_tiers),
        }
        try:
            result = self.provider.decide(payload) or {}
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

        if action == "move" and isinstance(dst, str) and dst in self.allowed_tiers and dst != current_tier:
            return PolicyDecision(action="move", dst_tier=dst, reason=reason)
        # default safe behavior
        return PolicyDecision(action="stay", dst_tier=None, reason=reason)


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


class ContentAwarePolicy:
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
    ) -> None:
        self.size_threshold = size_threshold
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
        if size <= self.size_threshold and current_tier != "hot" and "hot" in self.allowed:
            return PolicyDecision(action="move", dst_tier="hot", reason=f"<= {self.size_threshold} bytes")
        if size > self.size_threshold and current_tier != "warm" and "warm" in self.allowed:
            return PolicyDecision(action="move", dst_tier="warm", reason=f"> {self.size_threshold} bytes")
        return PolicyDecision(action="stay", reason="meets content policy by size", dst_tier=None)

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
        for rule in self.embedding_rules:
            feature = semantic_features.get(rule.name)
            if feature is None or feature.state is not FeatureState.FRESH:
                state = FeatureState.MISSING if feature is None else feature.state
                return PolicyDecision(
                    action="stay",
                    dst_tier=None,
                    reason=(
                        "required policy features unavailable: "
                        f"embedding:{rule.name}={state.value}"
                    ),
                )
            similarity = feature.similarity
            if similarity is not None and similarity >= rule.minimum_similarity:
                reason = (
                    f"embedding {rule.name} similarity {similarity:.6f} "
                    f">= {rule.minimum_similarity:.6f} -> {rule.destination_tier}"
                )
                if (
                    rule.destination_tier in self.allowed
                    and rule.destination_tier != current_tier
                ):
                    return PolicyDecision(
                        "move",
                        reason,
                        rule.destination_tier,
                    )
                return PolicyDecision("stay", f"{reason}; already constrained", None)

        # 4) Every configured signal was fresh but non-matching.
        return self._evaluate_size(current_tier, rec.size)
