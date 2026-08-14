from __future__ import annotations

from collections.abc import Sequence

from .policy import ContentAwarePolicy, LLMPolicy, Policy, SimplePolicy


class ThresholdProvider:
    def __init__(self, threshold: int, allowed_tiers: Sequence[str]) -> None:
        self.threshold = threshold
        self.allowed_tiers = frozenset(allowed_tiers)

    def decide(self, inputs: dict) -> dict:
        size = int(inputs.get("size", 0))
        current = inputs.get("current_tier")
        if (
            size <= self.threshold
            and "hot" in self.allowed_tiers
            and current != "hot"
        ):
            return {
                "action": "move",
                "dst_tier": "hot",
                "reason": f"<= {self.threshold} bytes",
            }
        if (
            size > self.threshold
            and "warm" in self.allowed_tiers
            and current != "warm"
        ):
            return {
                "action": "move",
                "dst_tier": "warm",
                "reason": f"> {self.threshold} bytes",
            }
        return {"action": "stay", "reason": "already optimal"}


def build_policy(
    policy_name: str,
    *,
    threshold: int,
    allowed_tiers: Sequence[str],
    llm_threshold: int | None = None,
    hot_name_patterns: Sequence[str] = (),
    warm_name_patterns: Sequence[str] = (),
    cold_name_patterns: Sequence[str] = (),
    hot_mime_prefixes: Sequence[str] = (),
    warm_mime_prefixes: Sequence[str] = (),
    cold_mime_prefixes: Sequence[str] = (),
) -> Policy:
    if policy_name == "llm":
        selected_threshold = llm_threshold if llm_threshold is not None else threshold
        return LLMPolicy(
            provider=ThresholdProvider(selected_threshold, allowed_tiers),
            allowed_tiers=allowed_tiers,
        )
    if policy_name == "content":
        policy = ContentAwarePolicy(
            size_threshold=threshold,
            allowed_tiers=allowed_tiers,
            hot_name_patterns=hot_name_patterns,
            warm_name_patterns=warm_name_patterns,
            hot_mime_prefixes=hot_mime_prefixes,
            warm_mime_prefixes=warm_mime_prefixes,
        )
        if hasattr(policy, "cold_name_patterns"):
            policy.cold_name_patterns = list(cold_name_patterns)
        if hasattr(policy, "cold_mime_prefixes"):
            policy.cold_mime_prefixes = list(cold_mime_prefixes)
        return policy
    if policy_name == "simple":
        return SimplePolicy(size_threshold=threshold, allowed_tiers=allowed_tiers)
    raise ValueError(f"unknown policy: {policy_name}")
