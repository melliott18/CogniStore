from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from copy import copy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Literal, Sequence
from uuid import uuid4

from cognistore.drivers.storage_driver import StorageDriver
from cognistore.utils.redaction import redact, redact_text

from .audit import (
    AuditContext,
    AuditEvent,
    AuditEventType,
    AuditOutcome,
    stable_audit_event_id,
)
from .catalog import CatalogStore, ObjectRecord
from .mover import Mover
from .placement_controls import (
    MovementConstraints,
    evaluate_movement_constraints,
    normalize_movement_constraints,
)
from .policy import ContentAwarePolicy, LLMPolicy, Policy, PolicyDecision, SimplePolicy
from .policy_factory import ThresholdProvider
from .policy_features import CatalogPolicyFeatureLoader, PolicyFeatures
from .policy_reasons import capture_policy_reason
from .policy_snapshot import (
    capture_policy_snapshot,
    policy_snapshot_features,
    snapshot_from_audit_details,
)

LOGGER = logging.getLogger(__name__)


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
    constraints: dict[str, object] = field(default_factory=dict)
    movement_constraints: MovementConstraints | None = None
    llm_audit: dict[str, object] | None = None


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
    constraints: dict[str, object] = field(default_factory=dict)
    llm_audit: dict[str, object] | None = None
    reason_code: str | None = None
    decisive_signals: list[dict[str, object]] = field(default_factory=list)

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
            "constraints": self.constraints,
            **({"llm_audit": self.llm_audit} if self.llm_audit is not None else {}),
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
        model_identity: str | None = None,
        model_version: str | None = None,
        movement_constraints: MovementConstraints | Mapping[str, object] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if model_version is not None and model_identity is None:
            raise ValueError("model_version requires model_identity")
        self.catalog = catalog
        self.drivers = drivers
        self.mover = mover
        self.policy = policy
        self.movement_constraints = normalize_movement_constraints(movement_constraints)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.feature_loader = feature_loader or CatalogPolicyFeatureLoader(access_catalog=catalog)
        self.idempotency_namespace = idempotency_namespace
        self.policy_name = policy_name or self._default_policy_name(policy)
        self.policy_version = policy_version
        self.model_identity = model_identity
        self.model_version = model_version
        self.audit_context = audit_context or AuditContext(
            correlation_id=idempotency_namespace or str(uuid4()),
            actor_type="system",
            actor_id="policy-runner",
        )
        self.audit_occurred_at = audit_occurred_at
        self.allowed_tiers = frozenset(
            drivers if allowed_tiers is None else allowed_tiers
        )
        unknown_tiers = self.allowed_tiers.difference(drivers)
        if unknown_tiers:
            names = ", ".join(sorted(unknown_tiers))
            raise ValueError(f"Unknown allowed tier(s): {names}")

    def run_once(
        self, bucket: str, prefix: str = "", dry_run: bool = False,
        *, as_of: str | datetime | None = None,
    ) -> List[ActionResult]:
        results = self.plan_once(bucket, prefix=prefix, dry_run=dry_run, as_of=as_of)
        if not dry_run:
            for result in results:
                self.execute(result)
        return results

    def plan_once(
        self, bucket: str, prefix: str = "", dry_run: bool = False,
        *, as_of: str | datetime | None = None,
    ) -> List[ActionResult]:
        """Validate a complete policy batch without applying its moves."""

        results: List[ActionResult] = []
        records = self.catalog.list(bucket, prefix=prefix)
        recorded: dict[tuple[str, str], AuditEvent] = {}

        def retain(record: ObjectRecord, evaluation: PolicyEvaluationResult) -> None:
            recorded[(record.bucket, record.key)] = self._record_evaluation(
                record, evaluation, plan_move=True,
            )

        evaluated = self._evaluate_records(
            records, as_of=as_of, on_evaluated=None if dry_run else retain,
        )
        for rec, evaluation in zip(records, evaluated):
            features = evaluation.features
            decision = PolicyDecision(
                action=evaluation.action,
                dst_tier=evaluation.destination_tier,
                reason=evaluation.reason,
                llm_audit=evaluation.llm_audit,
                reason_code=evaluation.reason_code,
                decisive_signals=evaluation.decisive_signals,
            )
            decision_event = recorded.get((rec.bucket, rec.key))
            if decision_event is not None:
                snapshot = snapshot_from_audit_details(decision_event.details)
                if snapshot is not None:
                    persisted = snapshot["decision"]
                    # A fresh failed inference is authoritative for this run.
                    # Never let an older selected snapshot turn that fail-closed
                    # result into a move, including on a concurrent retry.
                    if (
                        evaluation.llm_audit is not None
                        and evaluation.llm_audit.get("fallback_reason") is not None
                    ):
                        continue
                    persisted_llm_audit = decision_event.details.get("llm_audit")
                    decision = PolicyDecision(
                        persisted["action"],
                        persisted["reason"],
                        persisted["destination_tier"],
                        llm_audit=(
                            dict(persisted_llm_audit)
                            if isinstance(persisted_llm_audit, Mapping)
                            else None
                        ),
                    )
                    features = policy_snapshot_features(snapshot)
                    if persisted["outcome"] != AuditOutcome.SELECTED.value:
                        continue
            if decision.action != "move" or not decision.dst_tier or decision.dst_tier == rec.tier:
                continue
            if decision.dst_tier not in self.allowed_tiers:
                continue
            from_tier = rec.tier
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
                    features=features,
                    expected_source_sha256=self._feature_content_sha256(
                        features
                    ),
                    constraints=evaluation.constraints,
                    movement_constraints=self.movement_constraints,
                    llm_audit=decision.llm_audit,
                )
            )

        # Persist every evaluated object before storage preflight can fail.
        # The complete batch still validates before any bytes are moved.
        for result in results:
            self.mover.plan(
                result.from_tier, result.to_tier, result.bucket, result.key,
                movement_constraints=self.movement_constraints,
                as_of=str(result.constraints["as_of"]) if dry_run else None,
            )
        return results

    def preview_once(
        self,
        bucket: str,
        prefix: str = "",
        *,
        as_of: str | datetime | None = None,
    ) -> List[PolicyEvaluationResult]:
        """Return every dry-run decision with feature provenance and freshness."""

        records = self.catalog.list(bucket, prefix=prefix)
        evaluations = self._evaluate_records(records, as_of=as_of)
        for rec, evaluation in zip(records, evaluations):
            decision = PolicyDecision(
                action=evaluation.action,
                dst_tier=evaluation.destination_tier,
                reason=evaluation.reason,
            )
            if self._is_actionable(rec, decision):
                assert decision.dst_tier is not None
                self.mover.plan(
                    rec.tier, decision.dst_tier, rec.bucket, rec.key,
                    movement_constraints=self.movement_constraints,
                    as_of=str(evaluation.constraints["as_of"]),
                )
        return evaluations

    def evaluate_once(
        self, bucket: str, key: str, *, as_of: str | datetime | None = None,
    ) -> PolicyEvaluationResult:
        """Evaluate one catalog object without auditing or reading object storage."""

        record = self.catalog.get(bucket, key)
        if record is None:
            raise KeyError(f"object not found: {bucket}/{key}")
        return self._evaluate_records((record,), as_of=as_of)[0]

    def preview_decision(
        self, bucket: str, key: str, *, as_of: str | datetime | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Capture the same decision evidence as execution without any writes."""

        record = self.catalog.get(bucket, key)
        if record is None:
            raise KeyError(f"object not found: {bucket}/{key}")
        evaluation = self._evaluate_records((record,), as_of=as_of)[0]
        decision = PolicyDecision(
            evaluation.action, evaluation.reason, evaluation.destination_tier,
            llm_audit=evaluation.llm_audit,
            reason_code=evaluation.reason_code,
            decisive_signals=evaluation.decisive_signals,
        )
        return self._capture_decision_evidence(
            record, evaluation.features, decision,
            AuditOutcome.SELECTED if self._is_actionable(record, decision)
            else AuditOutcome.REJECTED if decision.action == "move"
            else AuditOutcome.STAYED,
            evaluation.constraints, str(evaluation.constraints["as_of"]),
        )

    def reevaluate_object(
        self, bucket: str, key: str, *, as_of: str | datetime | None = None,
    ) -> PolicyEvaluationResult:
        """Reevaluate the latest tag and append its decision without moving bytes."""

        record = self.catalog.get(bucket, key)
        if record is None:
            raise KeyError(f"object not found: {bucket}/{key}")
        return self.reevaluate_record(record, as_of=as_of)

    def reevaluate_record(
        self, record: ObjectRecord, *, as_of: str | datetime | None = None,
    ) -> PolicyEvaluationResult:
        """Audit the exact committed tag revision, even if a later update races it."""

        evaluation = self._evaluate_records((record,), as_of=as_of)[0]
        self._record_evaluation(record, evaluation)
        return evaluation

    def _record_evaluation(
        self, record: ObjectRecord, evaluation: PolicyEvaluationResult,
        *, plan_move: bool = False,
    ) -> AuditEvent:
        decision = PolicyDecision(
            evaluation.action, evaluation.reason, evaluation.destination_tier,
            llm_audit=evaluation.llm_audit,
            reason_code=evaluation.reason_code,
            decisive_signals=evaluation.decisive_signals,
        )
        actionable = self._is_actionable(record, decision)
        return self._record_decision(
            record=record,
            features=evaluation.features,
            decision=decision,
            bucket=record.bucket,
            key=record.key,
            current_tier=record.tier,
            size=record.size,
            action=decision.action,
            destination=decision.dst_tier,
            outcome=(
                AuditOutcome.SELECTED if actionable
                else AuditOutcome.REJECTED
                if decision.action == "move"
                else AuditOutcome.STAYED
            ),
            move_id=(
                self._move_idempotency_key_for(
                    record.bucket, record.key, record.tier, decision.dst_tier or "",
                ) if plan_move and actionable else None
            ),
            importance_revision=record.importance_revision,
            constraints=evaluation.constraints,
            reason=evaluation.reason,
        )

    def evaluate_record(
        self, record: ObjectRecord, *, as_of: str | datetime | None = None,
    ) -> PolicyEvaluationResult:
        """Evaluate a detached snapshot, including a proposed tag, without writes."""

        return self._evaluate_records((record,), as_of=as_of)[0]

    def _evaluate_records(
        self,
        records: Sequence[ObjectRecord],
        *,
        as_of: str | datetime | None = None,
        on_evaluated: Callable[[ObjectRecord, PolicyEvaluationResult], None] | None = None,
    ) -> List[PolicyEvaluationResult]:
        evaluated_at = self.clock() if as_of is None else as_of
        constraints: dict[tuple[str, str], dict[str, object]] = {}
        eligible_records: list[ObjectRecord] = []
        blocked_records: list[ObjectRecord] = []
        for rec in records:
            tier = self.catalog.get_tier(rec.tier)
            evidence = evaluate_movement_constraints(
                rec, self.movement_constraints,
                tier_metadata=None if tier is None else tier.metadata,
                as_of=evaluated_at,
            )
            evidence["size_hysteresis_bytes"] = max(
                self.movement_constraints.size_hysteresis_bytes,
                getattr(self.policy, "size_hysteresis_bytes", 0),
            )
            evidence["similarity_hysteresis"] = max(
                self.movement_constraints.similarity_hysteresis,
                getattr(self.policy, "similarity_hysteresis", 0.0),
            )
            configured_destinations = evidence["allowed_destination_tiers"]
            evidence["importance_allowed_tiers"] = configured_destinations
            allowed = self.allowed_tiers
            if isinstance(configured_destinations, list):
                allowed = allowed.intersection(configured_destinations)
            evidence["allowed_destination_tiers"] = sorted(allowed)
            evidence["blocked_reason"] = (
                evidence["residency_reason"] if evidence["residency_active"]
                else "importance constraint permits no other destination tier"
                if isinstance(configured_destinations, list)
                and not set(configured_destinations).difference({rec.tier})
                else evidence["cooldown_reason"]
                if evidence["cooldown_active"] and evidence["stability_override"] is None
                else None
            )
            evidence["suppression_reason"] = (
                "cooldown" if evidence["blocked_reason"] == evidence["cooldown_reason"]
                and evidence["cooldown_active"] and evidence["stability_override"] is None
                else None
            )
            constraints[(rec.bucket, rec.key)] = evidence
            (blocked_records if evidence["blocked_reason"] else eligible_records).append(rec)
        requests = tuple(getattr(self.policy, "feature_requests", ()))
        projections = {}
        if eligible_records:
            if isinstance(self.feature_loader, CatalogPolicyFeatureLoader):
                projections = self.feature_loader.load(
                    eligible_records, requests, as_of=evaluated_at
                )
            else:
                # Preserve the original two-argument custom loader contract.
                # Such loaders supply their own already-frozen feature times.
                projections = self.feature_loader.load(eligible_records, requests)
        # Blocked objects need only catalog evidence. Do not invoke semantic
        # providers for decisions that a hard constraint has already settled.
        projections.update(CatalogPolicyFeatureLoader(access_catalog=self.catalog).load(
            blocked_records, requests, as_of=evaluated_at
        ))
        results: List[PolicyEvaluationResult] = []
        for rec in records:
            coordinate = (rec.bucket, rec.key)
            try:
                features = projections[coordinate]
            except KeyError as exc:
                raise RuntimeError(
                    f"policy feature loader omitted {rec.bucket}/{rec.key}"
                ) from exc
            evidence = constraints[coordinate]
            if evidence["blocked_reason"]:
                decision = PolicyDecision("stay", str(evidence["blocked_reason"]))
            else:
                # Built-in policies (including LLM payloads) see only eligible
                # tiers. Copy per record to avoid changing shared policy state.
                policy = self.policy
                if isinstance(policy, (SimplePolicy, ContentAwarePolicy, LLMPolicy)):
                    policy = copy(policy)
                    eligible_tiers = evidence[
                        "allowed_destination_tiers" if isinstance(policy, LLMPolicy)
                        else "importance_allowed_tiers"
                    ]
                    if isinstance(eligible_tiers, list):
                        policy.allowed_tiers = set(eligible_tiers).intersection(policy.allowed_tiers)
                    bypass = self.movement_constraints.stability_override is not None
                    policy.size_hysteresis_bytes = (
                        0 if bypass else max(
                            self.movement_constraints.size_hysteresis_bytes,
                            policy.size_hysteresis_bytes,
                        )
                    )
                    if isinstance(policy, ContentAwarePolicy):
                        policy.similarity_hysteresis = (
                            0.0 if bypass else max(
                                self.movement_constraints.similarity_hysteresis,
                                policy.similarity_hysteresis,
                            )
                        )
                evaluate_features = getattr(policy, "evaluate_features", None)
                if callable(evaluate_features):
                    decision = evaluate_features(rec, features)
                else:
                    evaluate_record = getattr(policy, "evaluate_record", None)
                    decision = (
                        evaluate_record(rec)
                        if callable(evaluate_record)
                        else policy.evaluate(rec.tier, rec.size)
                    )
                hysteresis = (
                    decision.hysteresis
                    if type(policy) in {SimplePolicy, ContentAwarePolicy}
                    or type(policy) is LLMPolicy and type(policy.provider) is ThresholdProvider
                    else None
                )
                if hysteresis is not None:
                    evidence["hysteresis"] = hysteresis
                    suppressed = hysteresis.get("suppressed") and decision.action == "stay"
                    if type(policy) in {SimplePolicy, ContentAwarePolicy} or (
                        type(policy) is LLMPolicy and type(policy.provider) is ThresholdProvider
                    ):
                        baseline = copy(policy)
                        assert isinstance(baseline, (SimplePolicy, ContentAwarePolicy, LLMPolicy))
                        baseline.size_hysteresis_bytes = 0
                        baseline.similarity_hysteresis = 0.0
                        candidate = (
                            baseline.evaluate_features(rec, features)
                            if isinstance(baseline, ContentAwarePolicy)
                            else baseline.evaluate(rec.tier, rec.size)
                        )
                        hysteresis["candidate_action"] = candidate.action
                        hysteresis["candidate_destination_tier"] = candidate.dst_tier
                        suppressed = self._is_actionable(rec, candidate) and decision.action == "stay"
                        hysteresis["suppressed"] = suppressed
                    if suppressed:
                        evidence["suppression_reason"] = "hysteresis"
                        if "hysteresis" not in decision.reason:
                            decision.reason = f"hysteresis holds current tier; {decision.reason}"
                allowed_destinations = evidence["importance_allowed_tiers"]
                proposed_destination = (
                    decision.dst_tier if decision.action == "move"
                    else decision.proposed_dst_tier
                    if type(policy) in {SimplePolicy, ContentAwarePolicy, LLMPolicy} else None
                )
                if (
                    proposed_destination is not None
                    and proposed_destination != rec.tier
                    and isinstance(allowed_destinations, list)
                    and proposed_destination not in allowed_destinations
                ):
                    reason = "movement constraints forbid policy destination"
                    evidence["blocked_reason"] = reason
                    evidence["rejected_destination_tier"] = proposed_destination
                    decision = replace(decision, action="stay", reason=reason, dst_tier=None)
                    evidence["suppression_reason"] = None
                elif decision.reason_code == "destination_not_allowed" and proposed_destination:
                    evidence["rejected_destination_tier"] = proposed_destination
            if decision.llm_audit is not None:
                # Preserve the provider's proposal and explain the actual
                # outcome after every runner guardrail. Redaction also detaches
                # this evidence from mutable provider-owned dictionaries.
                llm_audit = redact(decision.llm_audit)
                llm_audit["final_decision"] = {
                    "action": redact_text(decision.action),
                    "destination_tier": (
                        redact_text(decision.dst_tier) if decision.dst_tier is not None else None
                    ),
                    "reason": redact_text(decision.reason),
                    "outcome": (
                        AuditOutcome.SELECTED.value if self._is_actionable(rec, decision)
                        else AuditOutcome.REJECTED.value if decision.action == "move"
                        else AuditOutcome.STAYED.value
                    ),
                }
                decision.llm_audit = llm_audit
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
                    constraints=evidence,
                    llm_audit=decision.llm_audit,
                    reason_code=decision.reason_code,
                    decisive_signals=decision.decisive_signals,
                )
            )
            if on_evaluated is not None:
                # Retain completed evaluations even if a later custom policy
                # raises. Read-only callers supply no persistence callback.
                on_evaluated(rec, results[-1])
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
            movement_constraints=result.movement_constraints or self.movement_constraints,
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

    def _capture_decision_evidence(
        self, record: ObjectRecord, features: PolicyFeatures,
        decision: PolicyDecision, outcome: AuditOutcome,
        constraints: Mapping[str, object], decision_at: str | datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Share redacted reason capture across previews and retained decisions."""

        snapshot_policy = self.policy
        if isinstance(snapshot_policy, LLMPolicy):
            snapshot_policy = copy(snapshot_policy)
            snapshot_policy.allowed_tiers = set(snapshot_policy.allowed_tiers).intersection(
                self.allowed_tiers
            )
        # Provider/custom prose may contain raw sensitive content that ordinary
        # credential redaction cannot recognize. Persist a static description.
        external = type(self.policy) not in {SimplePolicy, ContentAwarePolicy} and not (
            type(self.policy) is LLMPolicy and type(self.policy.provider) is ThresholdProvider
        )
        if external:
            safe_reason = (
                "external provider decision" if type(self.policy) is LLMPolicy
                else "custom policy decision"
            )
            decision = replace(decision, reason=safe_reason)
        snapshot = capture_policy_snapshot(
            record=record,
            features=features,
            policy=snapshot_policy,
            allowed_tiers=sorted(self.allowed_tiers),
            decision=decision,
            outcome=outcome,
            policy_name=self.policy_name,
            policy_version=self.policy_version,
            decision_at=decision_at,
            model_identity=self.model_identity,
            model_version=self.model_version,
        )
        structured_reason = capture_policy_reason(
            code=decision.reason_code,
            signals=decision.decisive_signals,
            constraints=constraints,
            policy_metadata=snapshot["policy"],
            action=decision.action,
            destination=decision.dst_tier,
            current_tier=record.tier,
            trusted_policy=type(self.policy) in {SimplePolicy, ContentAwarePolicy, LLMPolicy},
            embedding_rule_names=(
                tuple(rule.name for rule in self.policy.embedding_rules)
                if type(self.policy) is ContentAwarePolicy else ()
            ),
        )
        return snapshot, structured_reason

    def _record_decision(
        self,
        *,
        record: ObjectRecord,
        features: PolicyFeatures,
        decision: PolicyDecision,
        bucket: str,
        key: str,
        current_tier: str,
        size: int,
        action: str,
        destination: str | None,
        outcome: AuditOutcome,
        move_id: str | None,
        importance_revision: int = 0,
        constraints: Mapping[str, object] | None = None,
        reason: str = "",
    ) -> AuditEvent:
        safe_current_tier = redact_text(current_tier)
        safe_action = redact_text(action)
        safe_destination = None if destination is None else redact_text(destination)
        safe_reason = redact_text(reason)
        stable_constraints = {
            name: value for name, value in (constraints or {}).items() if name != "as_of"
        }
        # Freeze a retry's first policy output and features, while recognizing
        # new authoritative importance or residency inputs as a new decision.
        # The runner's ordinary destination filter is deliberately excluded:
        # expanding it must not turn a persisted rejection into a selected move.
        hard_constraint_identity = {
            name: value for name, value in stable_constraints.items()
            if name not in {
                "allowed_destination_tiers", "blocked_reason", "rejected_destination_tier",
                "hysteresis", "suppression_reason",
            }
        }
        evidence_identity = hashlib.sha256(json.dumps(
            hard_constraint_identity,
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        event_id = stable_audit_event_id(
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
            str(importance_revision),
            evidence_identity,
        )
        # A retried batch may load newer feature evidence. The immutable first
        # observation owns this logical decision's snapshot and timestamps.
        inference_retry_of = None
        existing = self.catalog.get_audit_event(event_id)
        if existing is not None:
            safe_llm_audit = redact(decision.llm_audit)
            if (
                isinstance(safe_llm_audit, dict)
                and safe_llm_audit.get("fallback_reason") is not None
                and existing.details.get("llm_audit") != safe_llm_audit
            ):
                # A retry's new inference failure must remain auditable even
                # when its stay shares the first decision's identity. Keep
                # this attempt evidence separate from hard-constraint identity
                # and deduplicate only identical redacted failures.
                inference_retry_of = existing.event_id
                inference_identity = hashlib.sha256(json.dumps(
                    safe_llm_audit, sort_keys=True, ensure_ascii=False,
                    separators=(",", ":"), allow_nan=False,
                ).encode("utf-8")).hexdigest()
                event_id = stable_audit_event_id(
                    "policy-inference-fallback", event_id, inference_identity
                )
                existing = self.catalog.get_audit_event(event_id)
                if existing is not None:
                    return existing
            else:
                return existing
        decision_at = datetime.now(timezone.utc)
        snapshot, structured_reason = self._capture_decision_evidence(
            record, features, decision, outcome, constraints or {}, decision_at,
        )
        safe_reason = str(snapshot["decision"]["reason"])
        if snapshot["replay"]["supported"] and (
            stable_constraints.get("minimum_residency_seconds")
            or stable_constraints.get("importance_allowed_tiers") is not None
            or stable_constraints.get("cooldown_seconds")
            or stable_constraints.get("size_hysteresis_bytes")
            or stable_constraints.get("similarity_hysteresis")
            or stable_constraints.get("stability_override") is not None
        ):
            # Snapshot v1 has no fields for these hard inputs. Preserve the
            # observed result for datasets without claiming an exact replay
            # from only the unconstrained underlying policy configuration.
            snapshot["replay"] = {
                "supported": False,
                "reason": "movement_constraints_not_in_snapshot_v1",
            }
        event = AuditEvent.create(
            AuditEventType.POLICY_DECISION,
            outcome,
            self.audit_context,
            event_id=event_id,
            occurred_at=self.audit_occurred_at or decision_at,
            recorded_at=self.audit_occurred_at or decision_at,
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
                "dataset": snapshot,
                "structured_reason": structured_reason,
                "importance_revision": importance_revision,
                "reason": safe_reason,
                # The evaluation instant changes on retries; eligibility and
                # its stable source evidence describe the logical decision.
                "constraints": stable_constraints,
                "stability_override": stable_constraints.get("stability_override"),
                **({"llm_audit": redact(decision.llm_audit)}
                   if decision.llm_audit is not None else {}),
                **({"inference_retry_of": inference_retry_of}
                   if inference_retry_of is not None else {}),
            },
        )
        try:
            persisted = self.catalog.append_audit_event(event)
            if stable_constraints.get("suppression_reason") is not None:
                LOGGER.info(
                    "policy move suppressed",
                    extra={
                        "event_id": persisted.event_id,
                        "correlation_id": self.audit_context.correlation_id,
                        "policy_name": self.policy_name,
                        "suppression_reason": stable_constraints["suppression_reason"],
                    },
                )
            return persisted
        except ValueError:
            # A concurrent retry can win between the read and append. Preserve
            # its immutable snapshot, while routing a distinct failed inference
            # through the same supplemental evidence path used above.
            existing = self.catalog.get_audit_event(event_id)
            if existing is not None:
                if (
                    decision.llm_audit is not None
                    and decision.llm_audit.get("fallback_reason") is not None
                    and existing.details.get("llm_audit") != redact(decision.llm_audit)
                ):
                    # The next call observes the immutable winner and derives
                    # an ID from this failure's evidence. A conflict on that
                    # derived ID has identical evidence and returns normally.
                    return self._record_decision(
                        record=record, features=features, decision=decision,
                        bucket=bucket, key=key, current_tier=current_tier,
                        size=size, action=action, destination=destination,
                        outcome=outcome, move_id=move_id,
                        importance_revision=importance_revision,
                        constraints=constraints, reason=reason,
                    )
                return existing
            raise

    @staticmethod
    def _default_policy_name(policy: Policy) -> str:
        name = type(policy).__name__
        return name[:-6].lower() if name.endswith("Policy") else name.lower()
