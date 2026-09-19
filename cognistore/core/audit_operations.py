"""Durable intent and outcome evidence for non-transactional storage operations."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace

from .audit import AuditContext, AuditEvent, AuditEventType, AuditOutcome
from .catalog import CatalogStore


@contextmanager
def audit_storage_operation(
    catalog: CatalogStore,
    context: AuditContext,
    *,
    operation: str,
    tier: str,
    bucket: str,
    key: str,
) -> Iterator[None]:
    """Persist intent before I/O, then record its correlated terminal outcome.

    Storage and catalog cannot share a transaction. A crash or unavailable
    audit store can leave a started event without a terminal outcome; that
    uncertainty remains visible and must be reconciled by an operator.
    """
    details = {"operation": operation, "tier": tier}
    started = catalog.append_audit_event(AuditEvent.create(
        AuditEventType.STORAGE_OPERATION, AuditOutcome.STARTED, context,
        bucket=bucket, object_key=key, details=details,
    ))
    terminal_context = replace(context, causation_id=started.event_id)
    try:
        yield
    except Exception as exc:
        catalog.append_audit_event(AuditEvent.create(
            AuditEventType.STORAGE_OPERATION, AuditOutcome.FAILED, terminal_context,
            bucket=bucket, object_key=key,
            details={**details, "error_type": type(exc).__name__},
        ))
        raise
    else:
        catalog.append_audit_event(AuditEvent.create(
            AuditEventType.STORAGE_OPERATION, AuditOutcome.SUCCEEDED, terminal_context,
            bucket=bucket, object_key=key, details=details,
        ))
