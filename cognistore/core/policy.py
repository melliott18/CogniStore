from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, Iterable
import fnmatch

# Local imports kept optional to avoid cycles at import time; used in type hints only
try:  # pragma: no cover - type checking convenience
    from .catalog import ObjectRecord  # type: ignore
except Exception:  # pragma: no cover
    ObjectRecord = object  # fallback for type checkers


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


class ContentAwarePolicy:
    """Policy that uses content metadata to decide placement.

    Rules are evaluated in the following order (first match wins):
      1. Filename patterns for hot (move to hot) and warm (move to warm)
      2. MIME prefix lists for hot and warm
      3. Fallback to size threshold (small -> hot, large -> warm)

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
    ) -> None:
        self.size_threshold = size_threshold
        self.allowed = set(allowed_tiers)
        self.hot_name_patterns = [p for p in (hot_name_patterns or []) if p]
        self.warm_name_patterns = [p for p in (warm_name_patterns or []) if p]
        self.hot_mime_prefixes = [m for m in (hot_mime_prefixes or []) if m]
        self.warm_mime_prefixes = [m for m in (warm_mime_prefixes or []) if m]
        # Optional cold-tier hints
        self.cold_name_patterns: list[str] = []
        self.cold_mime_prefixes: list[str] = []

    # Keep compatibility: provide size-based evaluate
    def evaluate(self, current_tier: str, size: int) -> PolicyDecision:
        # Fallback purely on size if record-aware path isn't used
        if size <= self.size_threshold and current_tier != "hot" and "hot" in self.allowed:
            return PolicyDecision(action="move", dst_tier="hot", reason=f"<= {self.size_threshold} bytes")
        if size > self.size_threshold and current_tier != "warm" and "warm" in self.allowed:
            return PolicyDecision(action="move", dst_tier="warm", reason=f"> {self.size_threshold} bytes")
        return PolicyDecision(action="stay", reason="meets content policy by size", dst_tier=None)

    # Record-aware evaluation used by PolicyRunner when available
    def evaluate_record(self, rec: "ObjectRecord") -> PolicyDecision:  # type: ignore[override]
        current_tier = getattr(rec, "tier", "")
        key = getattr(rec, "key", "")
        size = getattr(rec, "size", 0)
        metadata = getattr(rec, "metadata", {}) or {}
        mime = metadata.get("mime") or ""

        # 1) Name patterns
        if key and self.hot_name_patterns and any(fnmatch.fnmatch(key, pat) for pat in self.hot_name_patterns):
            if current_tier != "hot" and "hot" in self.allowed:
                return PolicyDecision(action="move", dst_tier="hot", reason="name pattern -> hot")
        if key and self.warm_name_patterns and any(fnmatch.fnmatch(key, pat) for pat in self.warm_name_patterns):
            if current_tier != "warm" and "warm" in self.allowed:
                return PolicyDecision(action="move", dst_tier="warm", reason="name pattern -> warm")
        if key and self.cold_name_patterns and any(fnmatch.fnmatch(key, pat) for pat in self.cold_name_patterns):
            if current_tier != "cold" and "cold" in self.allowed:
                return PolicyDecision(action="move", dst_tier="cold", reason="name pattern -> cold")

        # 2) MIME prefixes
        if mime and self.hot_mime_prefixes and any(mime.startswith(pfx) for pfx in self.hot_mime_prefixes):
            if current_tier != "hot" and "hot" in self.allowed:
                return PolicyDecision(action="move", dst_tier="hot", reason=f"mime {mime} -> hot")
        if mime and self.warm_mime_prefixes and any(mime.startswith(pfx) for pfx in self.warm_mime_prefixes):
            if current_tier != "warm" and "warm" in self.allowed:
                return PolicyDecision(action="move", dst_tier="warm", reason=f"mime {mime} -> warm")
        if mime and self.cold_mime_prefixes and any(mime.startswith(pfx) for pfx in self.cold_mime_prefixes):
            if current_tier != "cold" and "cold" in self.allowed:
                return PolicyDecision(action="move", dst_tier="cold", reason=f"mime {mime} -> cold")

        # 3) Fallback to size threshold
        return self.evaluate(current_tier, size)
