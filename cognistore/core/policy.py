from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


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

    def __init__(self, size_threshold: int = 1024 * 1024):
        self.size_threshold = size_threshold

    def evaluate(self, current_tier: str, size: int) -> PolicyDecision:
        if size <= self.size_threshold and current_tier != "hot":
            return PolicyDecision(action="move", dst_tier="hot", reason="small object -> hot tier")
        if size > self.size_threshold and current_tier != "warm":
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
        except Exception as e:  # defensive
            return PolicyDecision(action="stay", dst_tier=None, reason=f"provider_error: {e}")

        action = result.get("action")
        dst = result.get("dst_tier")
        reason = result.get("reason", "llm policy no reason provided")

        if action == "move" and isinstance(dst, str) and dst in self.allowed_tiers and dst != current_tier:
            return PolicyDecision(action="move", dst_tier=dst, reason=reason)
        # default safe behavior
        return PolicyDecision(action="stay", dst_tier=None, reason=reason)


class PolicyLLMProvider(Protocol):  # pragma: no cover - interface
    def decide(self, inputs: dict) -> dict:
        ...
