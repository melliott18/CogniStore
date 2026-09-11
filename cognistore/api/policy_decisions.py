"""Public placement explanations from frozen evidence and causal execution events."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal, cast
from uuid import UUID

from cognistore.core.audit import AuditEvent, AuditEventType, AuditQuery
from cognistore.core.catalog import CatalogStore
from cognistore.core.policy_reasons import PolicyReason, reason_from_audit_details
from cognistore.core.policy_snapshot import snapshot_from_audit_details

from .errors import ResourceNotFoundError
from .models import (
    DecisionExecution,
    DecisionExplanation,
    DecisionPlacement,
    JobStatusResponse,
    PolicyDecisionResource,
)

_MOVE_TYPES = frozenset({
    AuditEventType.MOVE_PREPARED.value, AuditEventType.MOVE_TRANSITIONED.value,
    AuditEventType.MOVE_COMPLETED.value, AuditEventType.MOVE_FAILED.value,
    AuditEventType.MOVE_RETRY.value,
})
_MAX_EXECUTION_EVENTS = 1000


def project_decision(
    *, bucket: str, key: str, evaluated_at: str,
    details: Mapping[str, Any], execution: DecisionExecution,
    decision_id: str | None = None,
) -> PolicyDecisionResource:
    """Read reason and snapshot independently; unavailable evidence stays explicit."""
    reason_state: Literal["available", "legacy", "unavailable"]
    try:
        retained_reason = reason_from_audit_details(details)
        reason = None if retained_reason is None else PolicyReason.model_validate(retained_reason)
        reason_state = "legacy" if reason is None else "available"
    except (TypeError, ValueError):
        reason, reason_state = None, "unavailable"
    try:
        snapshot = snapshot_from_audit_details(details)
    except (TypeError, ValueError):
        snapshot = None

    # Legacy details retain some placement fields, but never manufacture a
    # structured reason from prose or from today's policy configuration.
    source = snapshot["decision"] if snapshot is not None else details
    current_tier = snapshot["object"]["tier"] if snapshot is not None else details.get("current_tier")
    if not isinstance(current_tier, str) or not 1 <= len(current_tier) <= 256:
        current_tier = None
    action = source.get("action")
    if not isinstance(action, str) or action not in {"move", "stay"}:
        action = None
    destination = source.get("destination_tier")
    if not isinstance(destination, str) or not 1 <= len(destination) <= 256:
        destination = None
    disposition = reason.disposition if reason is not None else (
        "rejected" if source.get("outcome") == "rejected"
        else "move" if action == "move" and destination is not None
        else "stay" if action == "stay" else "unavailable"
    )
    # A rejected destination is evidence of an attempted proposal, not an
    # effective placement change. Its target remains in the reason constraints.
    proposed_tier = destination if disposition == "move" else current_tier
    changed = current_tier is not None and proposed_tier is not None and current_tier != proposed_tier
    return PolicyDecisionResource(
        decision_id=decision_id, bucket=bucket, key=key,
        evaluated_at=(
            reason.constraints.evaluated_at if reason is not None
            else snapshot["decision_at"] if snapshot is not None else evaluated_at
        ),
        action=cast(Literal["move", "stay"] | None, action), disposition=disposition,
        current=DecisionPlacement(tier=current_tier),
        proposed=DecisionPlacement(tier=proposed_tier),
        changed_fields=["tier"] if changed else [],
        explanation=DecisionExplanation(
            state=reason_state, structured_reason=reason,
            model_details=(
                "unavailable" if reason is None
                or reason.code == "provider_error"
                else "available" if reason.policy.model is not None
                else "not_applicable" if reason.confidence.source == "not_applicable"
                else "unavailable"
            ),
        ),
        execution=execution,
    )


def persisted_execution(
    catalog: CatalogStore, decision: AuditEvent,
    get_job: Callable[[str], JobStatusResponse],
) -> DecisionExecution:
    """Require a causal move event to claim completion, even for successful jobs."""
    job = None
    if decision.job_id is not None:
        try:
            # Public job resources use UUID identities. Legacy/custom jobs can
            # retain arbitrary or pseudonymized identifiers without such a URL.
            UUID(decision.job_id)
            UUID(decision.correlation_id)
            job = get_job(decision.job_id)
        except (ResourceNotFoundError, ValueError):
            pass  # Legacy/non-API jobs may have no public status resource.
    result = DecisionExecution(
        mode="persisted", state="not_requested",
        job_id=decision.job_id, correlation_id=decision.correlation_id,
        move_id=decision.move_id, job=job,
        updated_at=decision.occurred_at,
    )
    if decision.outcome != "selected":
        return result

    events = catalog.list_audit_events(AuditQuery.from_event(
        decision,
        event_types=_MOVE_TYPES, limit=_MAX_EXECUTION_EVENTS, ascending=False,
    ))
    cache: dict[str, AuditEvent | None] = {event.event_id: event for event in events}
    cache[decision.event_id] = decision
    depths: dict[str, int | None] = {decision.event_id: 0}

    def depth(event: AuditEvent) -> int | None:
        # Iterative traversal tolerates retries while rejecting broken chains,
        # cycles, unrelated objects, and another decision for the same move.
        path: list[str] = []
        current = event
        seen: set[str] = set()
        base = None
        for _ in range(_MAX_EXECUTION_EVENTS):
            if current.event_id == decision.event_id:
                base = 0
                break
            if current.event_id in seen or current.event_type not in _MOVE_TYPES:
                break
            if (
                current.correlation_id != decision.correlation_id
                or current.bucket != decision.bucket
                or current.object_key != decision.object_key
                or current.move_id != event.move_id
                or decision.move_id is not None and current.move_id != decision.move_id
            ):
                break
            if current.event_id in depths:
                base = depths[current.event_id]
                break
            seen.add(current.event_id)
            path.append(current.event_id)
            parent_id = current.causation_id
            if parent_id is None:
                break
            if parent_id not in cache:
                cache[parent_id] = catalog.get_audit_event(parent_id)
            parent = cache[parent_id]
            if parent is None:
                break
            current = parent
        for event_id in reversed(path):
            base = None if base is None else base + 1
            depths[event_id] = base
        return depths.get(event.event_id)

    connected = [(distance, event) for event in events if (distance := depth(event)) is not None]
    if connected:
        _, latest = max(connected, key=lambda item: (item[0], item[1].occurred_at, item[1].event_id))
        result.event_id = latest.event_id
        result.move_id = latest.move_id
        result.updated_at = latest.occurred_at
        if latest.event_type == AuditEventType.MOVE_COMPLETED.value:
            result.state = "completed"
        elif job is not None and job.status == "succeeded":
            # A successful parent cannot prove what happened after a retained
            # nonterminal/failed move event. Missing causal evidence must not
            # leave an already-finished job looking as though it is moving.
            result.state = "unavailable"
        elif latest.event_type == AuditEventType.MOVE_FAILED.value:
            result.state = "retrying" if job is not None and job.status == "retrying" else "failed"
        elif latest.event_type == AuditEventType.MOVE_RETRY.value:
            result.state = "retrying"
        else:
            result.state = "failed" if job is not None and job.status == "failed" else "running"
    elif job is not None:
        result.updated_at = job.updated_at.isoformat().replace("+00:00", "Z")
        result.state = (
            "failed" if job.status == "failed"
            else "unavailable" if job.status == "succeeded"
            else "retrying" if job.status == "retrying" else "planned"
        )
    elif decision.move_id is not None:
        result.state = "planned" if not events else "unavailable"
    elif events:
        result.state = "unavailable"
    return result
