from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Literal, Sequence
from uuid import uuid4

from cognistore.drivers.storage_driver import StorageDriver
from cognistore.utils.redaction import redact_text

from .audit import (
    AuditContext,
    AuditEvent,
    AuditEventType,
    AuditOutcome,
    stable_audit_event_id,
)
from .catalog import CatalogStore, ObjectRecord
from .mover import Mover
from .policy import Policy, PolicyDecision
from .policy_features import CatalogPolicyFeatureLoader, PolicyFeatures


@dataclass
class ActionResult:
    bucket: str
    key: str
    from_tier: str
    to_tier: str
    reason: str
    status: Literal["planned", "completed"] = "completed"
    decision_event_id: str | None = None
    correlation_id: str | None = None
    job_id: str | None = None
    features: PolicyFeatures | None = None
    expected_source_sha256: str | None = None


@dataclass(frozen=True)
class PolicyEvaluationResult:
    """One side-effect-free policy evaluation, including feature evidence."""

    bucket: str
    key: str
    current_tier: str
    size: int
    action: str
    destination_tier: str | None
    reason: str
    features: PolicyFeatures

    def to_mapping(self) -> dict[str, object]:
        return {
            "bucket": self.bucket,
            "key": self.key,
            "current_tier": self.current_tier,
            "size": self.size,
            "action": self.action,
            "destination_tier": self.destination_tier,
            "reason": self.reason,
            "features": self.features.to_dict(),
        }


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
        policy_name: str | None = None,
        policy_version: str = "1",
        audit_context: AuditContext | None = None,
        audit_occurred_at: str | datetime | None = None,
        feature_loader: CatalogPolicyFeatureLoader | None = None,
    ) -> None:
        self.catalog = catalog
        self.drivers = drivers
        self.mover = mover
        self.policy = policy
        self.feature_loader = feature_loader or CatalogPolicyFeatureLoader(access_catalog=catalog)
        self.idempotency_namespace = idempotency_namespace
        self.policy_name = policy_name or self._default_policy_name(policy)
        self.policy_version = policy_version
        self.audit_context = audit_context or AuditContext(
            correlation_id=idempotency_namespace or str(uuid4()),
            actor_type="system",
            actor_id="policy-runner",
        )
        self.audit_occurred_at = (
            datetime.now(timezone.utc) if audit_occurred_at is None else audit_occurred_at
        )
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
        evaluated = self._evaluate_records(records)
        for rec, evaluation in zip(records, evaluated):
            decision = PolicyDecision(
                action=evaluation.action,
                dst_tier=evaluation.destination_tier,
                reason=evaluation.reason,
            )
            actionable = self._is_actionable(rec, decision)
            outcome = (
                AuditOutcome.SELECTED
                if actionable
                else AuditOutcome.REJECTED
                if decision.action == "move"
                else AuditOutcome.STAYED
            )
            move_id = (
                self._move_idempotency_key_for(
                    rec.bucket,
                    rec.key,
                    rec.tier,
                    decision.dst_tier or "",
                )
                if actionable
                else None
            )
            decision_event = None
            if not dry_run:
                decision_event = self._record_decision(
                    bucket=rec.bucket,
                    key=rec.key,
                    current_tier=rec.tier,
                    size=rec.size,
                    action=decision.action,
                    destination=decision.dst_tier,
                    outcome=outcome,
                    move_id=move_id,
                )
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
                    decision_event_id=(None if decision_event is None else decision_event.event_id),
                    correlation_id=self.audit_context.correlation_id,
                    job_id=self.audit_context.job_id,
                    features=evaluation.features,
                    expected_source_sha256=self._feature_content_sha256(
                        evaluation.features
                    ),
                )
            )

        return results

    def preview_once(
        self,
        bucket: str,
        prefix: str = "",
    ) -> List[PolicyEvaluationResult]:
        """Return every dry-run decision with feature provenance and freshness."""

        records = self.catalog.list(bucket, prefix=prefix)
        evaluations = self._evaluate_records(records)
        for rec, evaluation in zip(records, evaluations):
            decision = PolicyDecision(
                action=evaluation.action,
                dst_tier=evaluation.destination_tier,
                reason=evaluation.reason,
            )
            if self._is_actionable(rec, decision):
                assert decision.dst_tier is not None
                self.mover.plan(rec.tier, decision.dst_tier, rec.bucket, rec.key)
        return evaluations

    def _evaluate_records(
        self,
        records: Sequence[ObjectRecord],
    ) -> List[PolicyEvaluationResult]:
        requests = tuple(getattr(self.policy, "feature_requests", ()))
        projections = self.feature_loader.load(records, requests)
        results: List[PolicyEvaluationResult] = []
        for rec in records:
            coordinate = (rec.bucket, rec.key)
            try:
                features = projections[coordinate]
            except KeyError as exc:
                raise RuntimeError(
                    f"policy feature loader omitted {rec.bucket}/{rec.key}"
                ) from exc
            evaluate_features = getattr(self.policy, "evaluate_features", None)
            if callable(evaluate_features):
                decision = evaluate_features(rec, features)
            else:
                evaluate_record = getattr(self.policy, "evaluate_record", None)
                decision = (
                    evaluate_record(rec)
                    if callable(evaluate_record)
                    else self.policy.evaluate(rec.tier, rec.size)
                )
            results.append(
                PolicyEvaluationResult(
                    bucket=rec.bucket,
                    key=rec.key,
                    current_tier=rec.tier,
                    size=rec.size,
                    action=decision.action,
                    destination_tier=decision.dst_tier,
                    reason=decision.reason,
                    features=features,
                )
            )
        return results

    def _is_actionable(
        self,
        rec: ObjectRecord,
        decision: PolicyDecision,
    ) -> bool:
        return (
            decision.action == "move"
            and bool(decision.dst_tier)
            and decision.dst_tier != rec.tier
            and decision.dst_tier in self.allowed_tiers
        )

    @staticmethod
    def _feature_content_sha256(features: PolicyFeatures) -> str | None:
        # MIME is projected directly from the current catalog snapshot, so its
        # provenance carries that snapshot's source identity even when the MIME
        # signal itself is missing or stale.  Embedding provenance can instead
        # retain an older evidence digest specifically to explain staleness; it
        # must not replace the identity used to fence a selected move.
        return features.mime.provenance.content_sha256

    def execute(self, result: ActionResult) -> None:
        """Execute one previously validated action."""

        self.mover.move(
            result.from_tier,
            result.to_tier,
            result.bucket,
            result.key,
            idempotency_key=self._move_idempotency_key(result),
            expected_source_sha256=result.expected_source_sha256,
            audit_context=AuditContext(
                correlation_id=result.correlation_id or self.audit_context.correlation_id,
                actor_type=self.audit_context.actor_type,
                actor_id=self.audit_context.actor_id,
                job_id=result.job_id or self.audit_context.job_id,
                causation_id=result.decision_event_id,
            ),
        )

    def _move_idempotency_key(self, result: ActionResult) -> str | None:
        return self._move_idempotency_key_for(
            result.bucket,
            result.key,
            result.from_tier,
            result.to_tier,
        )

    def _move_idempotency_key_for(
        self,
        bucket: str,
        key: str,
        from_tier: str,
        to_tier: str,
    ) -> str | None:
        if self.idempotency_namespace is None:
            return None
        identity = json.dumps(
            [
                from_tier,
                to_tier,
                bucket,
                key,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"{self.idempotency_namespace}:{hashlib.sha256(identity).hexdigest()}"

    def _record_decision(
        self,
        *,
        bucket: str,
        key: str,
        current_tier: str,
        size: int,
        action: str,
        destination: str | None,
        outcome: AuditOutcome,
        move_id: str | None,
    ) -> AuditEvent:
        safe_current_tier = redact_text(current_tier)
        safe_action = redact_text(action)
        safe_destination = None if destination is None else redact_text(destination)
        event = AuditEvent.create(
            AuditEventType.POLICY_DECISION,
            outcome,
            self.audit_context,
            event_id=stable_audit_event_id(
                "policy-decision",
                self.audit_context.correlation_id,
                self.audit_context.job_id or "",
                self.policy_name,
                self.policy_version,
                bucket,
                key,
                current_tier,
                action,
                destination or "",
                str(size),
            ),
            occurred_at=self.audit_occurred_at,
            recorded_at=self.audit_occurred_at,
            bucket=bucket,
            object_key=key,
            move_id=move_id,
            policy_name=self.policy_name,
            policy_version=self.policy_version,
            details={
                "action": safe_action,
                "current_tier": safe_current_tier,
                "destination_tier": safe_destination,
                "size": size,
            },
        )
        return self.catalog.append_audit_event(event)

    @staticmethod
    def _default_policy_name(policy: Policy) -> str:
        name = type(policy).__name__
        return name[:-6].lower() if name.endswith("Policy") else name.lower()
