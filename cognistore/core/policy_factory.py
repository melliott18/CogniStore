from __future__ import annotations

from collections.abc import Sequence

from .placement_llm import PlacementLLMProvider
from .placement_llm_http import placement_inference_from_env
from .policy import (
    ContentAwarePolicy,
    EmbeddingPolicyRule,
    LLMPolicy,
    Policy,
    SimplePolicy,
)
from .policy_stability import size_boundary, validate_size_hysteresis_bytes


class ThresholdProvider:
    def __init__(self, threshold: int, allowed_tiers: Sequence[str]) -> None:
        self.threshold = threshold
        self.allowed_tiers = frozenset(allowed_tiers)

    def decide(self, inputs: dict) -> dict:
        size = int(inputs.get("size", 0))
        current = inputs.get("current_tier")
        band = validate_size_hysteresis_bytes(inputs.get("size_hysteresis_bytes", 0))
        threshold, evidence = size_boundary(
            threshold=self.threshold,
            band=band,
            current_tier=current if isinstance(current, str) else "",
            size=size,
            allowed_tiers=self.allowed_tiers,
        )
        if (
            size <= threshold
            and "hot" in self.allowed_tiers
            and current != "hot"
        ):
            return {
                "action": "move",
                "dst_tier": "hot",
                "reason": f"<= {threshold} bytes",
                **({"hysteresis": evidence} if evidence is not None else {}),
            }
        if (
            size > threshold
            and "warm" in self.allowed_tiers
            and current != "warm"
        ):
            return {
                "action": "move",
                "dst_tier": "warm",
                "reason": f"> {threshold} bytes",
                **({"hysteresis": evidence} if evidence is not None else {}),
            }
        reason = "already optimal"
        if evidence and evidence["suppressed"]:
            reason = f"size hysteresis holds current tier at {threshold} bytes"
        return {
            "action": "stay",
            "reason": reason,
            **({"hysteresis": evidence} if evidence is not None else {}),
        }


def build_policy(
    policy_name: str,
    *,
    threshold: int,
    allowed_tiers: Sequence[str],
    llm_threshold: int | None = None,
    llm_provider: PlacementLLMProvider | None = None,
    hot_name_patterns: Sequence[str] = (),
    warm_name_patterns: Sequence[str] = (),
    cold_name_patterns: Sequence[str] = (),
    hot_mime_prefixes: Sequence[str] = (),
    warm_mime_prefixes: Sequence[str] = (),
    cold_mime_prefixes: Sequence[str] = (),
    embedding_rules: Sequence[EmbeddingPolicyRule] = (),
) -> Policy:
    if embedding_rules and policy_name != "content":
        raise ValueError("embedding rules require the content policy")
    if policy_name == "llm":
        # llm_threshold is retained for old CLI/API/job payloads. Inference
        # failures must never turn into an unrelated size-based move.
        if llm_provider is not None:
            return LLMPolicy(provider=llm_provider, allowed_tiers=allowed_tiers)
        return LLMPolicy(
            inference=placement_inference_from_env(), allowed_tiers=allowed_tiers,
        )
    if policy_name == "content":
        policy = ContentAwarePolicy(
            size_threshold=threshold,
            allowed_tiers=allowed_tiers,
            hot_name_patterns=hot_name_patterns,
            warm_name_patterns=warm_name_patterns,
            hot_mime_prefixes=hot_mime_prefixes,
            warm_mime_prefixes=warm_mime_prefixes,
            embedding_rules=embedding_rules,
        )
        if hasattr(policy, "cold_name_patterns"):
            policy.cold_name_patterns = list(cold_name_patterns)
        if hasattr(policy, "cold_mime_prefixes"):
            policy.cold_mime_prefixes = list(cold_mime_prefixes)
        return policy
    if policy_name == "simple":
        return SimplePolicy(size_threshold=threshold, allowed_tiers=allowed_tiers)
    raise ValueError(f"unknown policy: {policy_name}")
