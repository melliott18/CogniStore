"""Authenticated, self-audited access to one tenant's retained audit evidence."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING
from uuid import uuid4

from cognistore.auth.authorization import AuthorizationError, audit_authorization_denial
from cognistore.auth.principal import current_principal
from cognistore.core.audit import AuditContext, AuditEvent, AuditEventType, AuditOutcome, AuditQuery
from cognistore.observability import current_audit_correlation_id

from .errors import BackendUnavailableError, RequestContractError, ResourceNotFoundError
from .models import (
    AuditEventPage,
    AuditEventResource,
    AuditExportResponse,
    AuditVerificationRequest,
    AuditVerificationResponse,
    PageMetadata,
)
from .pagination import CursorError, decode_cursor, encode_cursor
from .permissions import OPERATION_PERMISSIONS

if TYPE_CHECKING:
    from cognistore.core.catalog import CatalogStore


class AuditAccessMixin:
    """Shared gateway implementation; the owning gateway supplies tenant scoping."""

    @property
    def catalog(self) -> CatalogStore:
        raise NotImplementedError

    def _authorize(self, operation: str) -> None:
        raise NotImplementedError

    def _authorize_audit(self, operation: str) -> None:
        # Unlike trusted local object operations, audit disclosure always needs
        # an authenticated actor and an explicit AUDIT grant.
        if current_principal() is None:
            audit_authorization_denial(
                None, OPERATION_PERMISSIONS[operation], operation=operation,
                boundary="service", catalog=self.catalog,
            )
            raise AuthorizationError()
        self._authorize(operation)

    def _audit_access(
        self, event_type: AuditEventType, operation: str, details: dict[str, object],
        *, outcome: AuditOutcome = AuditOutcome.SUCCEEDED,
    ) -> None:
        principal = current_principal()
        assert principal is not None  # established before accessing evidence
        event = AuditEvent.create(
            event_type, outcome,
            AuditContext(
                current_audit_correlation_id() or str(uuid4()), principal.actor_type, principal.actor_id,
            ),
            details={"operation": operation, **details},
        )
        try:
            self.catalog.append_audit_event(event)
        except Exception:
            # Never return evidence if recording the disclosure itself failed.
            raise BackendUnavailableError("Audit access could not be recorded") from None

    def get_audit_event(self, event_id: str) -> AuditEventResource:
        self._authorize_audit("get_audit_event")
        event = self.catalog.get_audit_event(event_id)
        self._audit_access(
            AuditEventType.AUDIT_ACCESS, "get_audit_event",
            {"event_id": event_id, "found": event is not None},
        )
        if event is None:
            raise ResourceNotFoundError("audit event", event_id)
        return AuditEventResource.model_validate(event)

    def list_audit_events(
        self, *, bucket: str | None = None, key: str | None = None,
        job_id: str | None = None, correlation_id: str | None = None,
        actor_id: str | None = None, event_type: str | None = None,
        outcome: str | None = None, occurred_after: str | None = None,
        occurred_before: str | None = None, limit: int = 50, cursor: str | None = None,
    ) -> AuditEventPage:
        self._authorize_audit("list_audit_events")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise RequestContractError("limit must be between 1 and 200")
        filters = {
            "bucket": bucket, "key": key, "job_id": job_id, "correlation_id": correlation_id,
            "actor_id": actor_id, "event_type": event_type, "outcome": outcome,
            "occurred_after": occurred_after, "occurred_before": occurred_before,
        }
        try:
            last = decode_cursor(cursor, resource="audit.events", filters=filters)
            boundary = None if last is None else json.loads(last)
            if boundary is not None and (not isinstance(boundary, list) or len(boundary) != 2):
                raise ValueError("invalid boundary")
            query = AuditQuery(
                bucket=bucket, object_key=key, job_id=job_id, correlation_id=correlation_id,
                actor_id=actor_id, event_types=None if event_type is None else frozenset({event_type}),
                outcomes=None if outcome is None else frozenset({outcome}), occurred_after=occurred_after,
                occurred_before=occurred_before, ascending=False, limit=limit + 1,
                before_event=None if boundary is None else tuple(boundary),
            )
        except (TypeError, ValueError) as exc:
            raise RequestContractError(
                "Invalid audit query or cursor", code="invalid_cursor" if cursor else "validation_error",
            ) from exc
        events = self.catalog.list_audit_events(query)
        next_cursor = None
        if len(events) > limit:
            boundary_event = events[limit - 1]
            next_cursor = encode_cursor(
                resource="audit.events", filters=filters,
                last_key=json.dumps([boundary_event.occurred_at, boundary_event.event_id]),
            )
        result = AuditEventPage(
            items=[AuditEventResource.model_validate(event) for event in events[:limit]],
            page=PageMetadata(limit=limit, next_cursor=next_cursor),
        )
        self._audit_access(
            AuditEventType.AUDIT_ACCESS, "list_audit_events",
            {"filters": filters, "limit": limit, "returned": len(result.items)},
        )
        return result

    def export_audit_events(
        self, *, limit: int = 100, cursor: str | None = None,
    ) -> AuditExportResponse:
        self._authorize_audit("export_audit_events")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise RequestContractError("limit must be between 1 and 200")
        try:
            last = decode_cursor(cursor, resource="audit.export", filters={})
            if last is None:
                checkpoint = asdict(self.catalog.audit_checkpoint())
                after_sequence = 0
            else:
                boundary = json.loads(last)
                if not isinstance(boundary, dict) or set(boundary) != {"checkpoint", "after_sequence"}:
                    raise ValueError("invalid boundary")
                checkpoint, after_sequence = boundary["checkpoint"], boundary["after_sequence"]
            evidence = self.catalog.export_audit_evidence(
                after_sequence=after_sequence, limit=limit, checkpoint=checkpoint,
            )
        except (TypeError, ValueError, CursorError) as exc:
            raise RequestContractError("Invalid audit export cursor", code="invalid_cursor") from exc
        next_cursor = None
        if not evidence["complete"]:
            next_cursor = encode_cursor(
                resource="audit.export", filters={}, last_key=json.dumps({
                    "checkpoint": evidence["checkpoint"], "after_sequence": evidence["next_sequence"],
                }),
            )
        result = AuditExportResponse.model_validate({
            **evidence, "page": {"limit": limit, "next_cursor": next_cursor},
        })
        self._audit_access(
            AuditEventType.AUDIT_EXPORT, "export_audit_events",
            {"checkpoint": checkpoint, "after_sequence": after_sequence,
             "returned": len(result.records), "complete": result.complete},
        )
        return result

    def verify_audit_integrity(
        self, request: AuditVerificationRequest,
    ) -> AuditVerificationResponse:
        self._authorize_audit("verify_audit_integrity")
        checkpoint = None if request.checkpoint is None else request.checkpoint.model_dump()
        try:
            verification = self.catalog.verify_audit_integrity(checkpoint)
        except (TypeError, ValueError) as exc:
            raise RequestContractError("Invalid audit checkpoint") from exc
        result = AuditVerificationResponse.model_validate({
            **asdict(verification), "issues": list(verification.issues),
        })
        self._audit_access(
            AuditEventType.AUDIT_VERIFICATION, "verify_audit_integrity",
            {"valid": result.valid, "anchored": result.anchored,
             "checkpoint": result.checkpoint.model_dump(), "checked_entries": result.checked_entries},
            outcome=AuditOutcome.SUCCEEDED if result.valid else AuditOutcome.FAILED,
        )
        return result
