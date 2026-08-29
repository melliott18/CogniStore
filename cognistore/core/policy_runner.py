from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Dict, Iterable, List, Literal

from cognistore.drivers.storage_driver import StorageDriver

from .catalog import CatalogStore
from .mover import Mover
from .policy import Policy


@dataclass
class ActionResult:
    bucket: str
    key: str
    from_tier: str
    to_tier: str
    reason: str
    status: Literal["planned", "completed"] = "completed"


class PolicyRunner:
    """Runs policy decisions across a bucket/prefix and applies moves via Mover."""

    def __init__(
        self,
        catalog: CatalogStore,
        drivers: Dict[str, StorageDriver],
        mover: Mover,
        policy: Policy,
        allowed_tiers: Iterable[str] | None = None,
        idempotency_namespace: str | None = None,
    ) -> None:
        self.catalog = catalog
        self.drivers = drivers
        self.mover = mover
        self.policy = policy
        self.idempotency_namespace = idempotency_namespace
        self.allowed_tiers = frozenset(
            drivers if allowed_tiers is None else allowed_tiers
        )
        unknown_tiers = self.allowed_tiers.difference(drivers)
        if unknown_tiers:
            names = ", ".join(sorted(unknown_tiers))
            raise ValueError(f"Unknown allowed tier(s): {names}")

    def run_once(self, bucket: str, prefix: str = "", dry_run: bool = False) -> List[ActionResult]:
        results = self.plan_once(bucket, prefix=prefix, dry_run=dry_run)
        if not dry_run:
            for result in results:
                self.execute(result)
        return results

    def plan_once(
        self, bucket: str, prefix: str = "", dry_run: bool = False
    ) -> List[ActionResult]:
        """Validate a complete policy batch without applying its moves."""

        results: List[ActionResult] = []
        records = self.catalog.list(bucket, prefix=prefix)
        for rec in records:
            # Prefer record-aware policy evaluation when available
            if hasattr(self.policy, "evaluate_record"):
                decision = getattr(self.policy, "evaluate_record")(rec)
            else:
                decision = self.policy.evaluate(rec.tier, rec.size)
            if decision.action != "move" or not decision.dst_tier or decision.dst_tier == rec.tier:
                continue
            if decision.dst_tier not in self.allowed_tiers:
                continue
            from_tier = rec.tier
            # Validate the entire batch before any move executes. A known
            # collision or invalid path must reject the run without partially
            # applying earlier decisions.
            self.mover.plan(from_tier, decision.dst_tier, rec.bucket, rec.key)
            status: Literal["planned", "completed"] = (
                "planned" if dry_run else "completed"
            )
            results.append(
                ActionResult(
                    bucket=rec.bucket,
                    key=rec.key,
                    from_tier=from_tier,
                    to_tier=decision.dst_tier,
                    reason=decision.reason,
                    status=status,
                )
            )

        return results

    def execute(self, result: ActionResult) -> None:
        """Execute one previously validated action."""

        self.mover.move(
            result.from_tier,
            result.to_tier,
            result.bucket,
            result.key,
            idempotency_key=self._move_idempotency_key(result),
        )

    def _move_idempotency_key(self, result: ActionResult) -> str | None:
        if self.idempotency_namespace is None:
            return None
        identity = json.dumps(
            [
                result.from_tier,
                result.to_tier,
                result.bucket,
                result.key,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"{self.idempotency_namespace}:{hashlib.sha256(identity).hexdigest()}"
