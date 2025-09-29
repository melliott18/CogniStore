from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .catalog import Catalog, ObjectRecord
from .mover import Mover
from .policy import PolicyDecision, Policy
from cognistore.drivers.storage_driver import StorageDriver


@dataclass
class ActionResult:
    bucket: str
    key: str
    from_tier: str
    to_tier: str
    reason: str


class PolicyRunner:
    """Runs policy decisions across a bucket/prefix and applies moves via Mover."""

    def __init__(
        self,
        catalog: Catalog,
        drivers: Dict[str, StorageDriver],
        mover: Mover,
    policy: Policy,
    ) -> None:
        self.catalog = catalog
        self.drivers = drivers
        self.mover = mover
        self.policy = policy

    def run_once(self, bucket: str, prefix: str = "") -> List[ActionResult]:
        results: List[ActionResult] = []
        records = self.catalog.list(bucket, prefix=prefix)
        for rec in records:
            # Prefer record-aware policy evaluation when available
            if hasattr(self.policy, "evaluate_record"):
                decision = getattr(self.policy, "evaluate_record")(rec)  # type: ignore[call-arg]
            else:
                decision = self.policy.evaluate(rec.tier, rec.size)
            if decision.action != "move" or not decision.dst_tier or decision.dst_tier == rec.tier:
                continue
            # Ensure drivers exist
            if rec.tier not in self.drivers or decision.dst_tier not in self.drivers:
                continue
            from_tier = rec.tier
            self.mover.move(from_tier, decision.dst_tier, rec.bucket, rec.key)
            results.append(
                ActionResult(
                    bucket=rec.bucket,
                    key=rec.key,
                    from_tier=from_tier,
                    to_tier=decision.dst_tier,
                    reason=decision.reason,
                )
            )
        return results
