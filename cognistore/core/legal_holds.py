"""Immutable legal-hold evidence and catalog mutation guards.

Holds are independent of object metadata and survive removal of an object from
the catalog. Exact keys, literal prefixes, and complete buckets can be held,
including keys that have not been catalogued yet.
"""

from __future__ import annotations

import posixpath
import unicodedata
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, TypeVar, cast
from uuid import UUID, uuid4

from cognistore.auth.tenancy import validate_tenant_id
from cognistore.core.audit import (
    AuditContext,
    AuditEvent,
    AuditEventType,
    AuditOutcome,
    AuditRetentionPolicy,
    audit_text_identity,
    canonical_audit_timestamp,
)
from cognistore.utils.redaction import redact_text


class LegalHoldError(RuntimeError):
    """A legal hold prevented an operation; messages never expose hold evidence."""


LEGAL_HOLD_EVENT_TYPES = frozenset({
    "legal_hold.placed", "legal_hold.released", "legal_hold.denied",
})


def _required(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def legal_hold_actor(context: AuditContext) -> None:
    """Require a trusted explicit actor; API authorization lives at its boundary."""
    if not isinstance(context, AuditContext) or context.actor_type not in {
        "user", "authenticated",
    }:
        raise ValueError("legal hold changes require an explicit authenticated actor")


def _scope_identity(value: str, *, prefix: bool = False) -> str:
    """Overprotect names that a filesystem can interpret as the same path.

    Storage keeps the original spelling. Hold matching additionally folds case,
    Unicode canonical equivalents, and slash/dot path aliases, even on backends
    that distinguish those names. This intentionally prefers an extra protected
    object over allowing an alias to replace preserved bytes.
    """
    normalized = unicodedata.normalize("NFC", value.casefold()).replace("\\", "/")
    identity = posixpath.normpath("/" + normalized.lstrip("/")).lstrip("/")
    if prefix and identity and (
        normalized.endswith("/") or normalized.rsplit("/", 1)[-1] in {".", ".."}
    ):
        identity += "/"
    return identity


@dataclass(frozen=True)
class LegalHold:
    hold_id: str
    tenant_id: str
    bucket: str
    key: str | None
    prefix: str | None
    reason: str
    created_at: str
    actor_type: str
    actor_id: str
    correlation_id: str
    released_at: str | None = None
    released_reason: str | None = None
    released_actor_type: str | None = None
    released_actor_id: str | None = None
    released_correlation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "hold_id", str(UUID(str(self.hold_id))))
        validate_tenant_id(self.tenant_id)
        _required(self.bucket, "bucket")
        if self.key is not None:
            _required(self.key, "key")
        if self.prefix is not None and not isinstance(self.prefix, str):
            raise ValueError("prefix must be a string or null")
        if self.key is not None and self.prefix is not None:
            raise ValueError("key and prefix are mutually exclusive")
        _required(self.reason, "reason")
        _required(self.actor_type, "actor_type")
        _required(self.actor_id, "actor_id")
        _required(self.correlation_id, "correlation_id")
        object.__setattr__(self, "created_at", canonical_audit_timestamp(self.created_at))
        release = (
            self.released_at, self.released_reason, self.released_actor_type,
            self.released_actor_id, self.released_correlation_id,
        )
        if any(value is not None for value in release):
            for value in release:
                _required(value, "release evidence")
            assert self.released_at is not None
            object.__setattr__(self, "released_at", canonical_audit_timestamp(self.released_at))

    @property
    def active(self) -> bool:
        return self.released_at is None

    def matches_bucket(self, bucket: str) -> bool:
        return _scope_identity(self.bucket) == _scope_identity(bucket)

    def matches(self, bucket: str, key: str) -> bool:
        if not self.matches_bucket(bucket):
            return False
        if self.key is not None:
            return _scope_identity(self.key) == _scope_identity(key)
        if self.prefix is None:
            return True
        # Preserve the original literal-prefix contract for object stores whose
        # keys may contain dot components, while also protecting path aliases.
        return key.startswith(self.prefix) or _scope_identity(key).startswith(
            _scope_identity(self.prefix, prefix=True),
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "active": self.active}

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> LegalHold:
        fields = dict(value)
        fields.pop("active", None)
        return cls(**fields)

    @classmethod
    def create(
        cls, tenant_id: str, bucket: str, *, key: str | None,
        prefix: str | None, reason: str, context: AuditContext,
    ) -> LegalHold:
        legal_hold_actor(context)
        return cls(
            hold_id=str(uuid4()), tenant_id=tenant_id, bucket=bucket, key=key, prefix=prefix,
            reason=redact_text(_required(reason, "reason")),
            created_at=canonical_audit_timestamp(datetime.now(timezone.utc)),
            actor_type=context.actor_type, actor_id=audit_text_identity(context.actor_id),
            correlation_id=audit_text_identity(context.correlation_id),
        )

    def release(self, *, reason: str, context: AuditContext) -> LegalHold:
        legal_hold_actor(context)
        if not self.active:
            raise LegalHoldError("Legal hold is already released")
        return replace(
            self, released_at=canonical_audit_timestamp(datetime.now(timezone.utc)),
            released_reason=redact_text(_required(reason, "reason")),
            released_actor_type=context.actor_type,
            released_actor_id=audit_text_identity(context.actor_id),
            released_correlation_id=audit_text_identity(context.correlation_id),
        )


def legal_hold_event(hold: LegalHold, context: AuditContext, *, released: bool) -> AuditEvent:
    """Preserve scope and lifecycle actor evidence indefinitely."""
    return AuditEvent.create(
        AuditEventType.LEGAL_HOLD_RELEASED if released else AuditEventType.LEGAL_HOLD_PLACED,
        AuditOutcome.SUCCEEDED, context,
        retention=AuditRetentionPolicy(max_age_seconds=None),
        # Audit object identities are paired. Bucket/prefix scopes are carried
        # in details rather than inventing a reserved wildcard object key.
        bucket=hold.bucket if hold.key is not None else None, object_key=hold.key,
        details={"legal_hold": hold.to_dict()},
    )


_Method = TypeVar("_Method", bound=Callable[..., Any])


def serialize_cleanup(method: _Method) -> _Method:
    """Keep job creation/state changes outside an exclusive cleanup attempt.

    This only serializes lifecycle changes; existing legal-hold authorization
    and recovery behavior remain the caller's responsibility. Lease heartbeats
    need not join this fence because they cannot create a new protection.
    """
    @wraps(method)
    def guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._legal_hold_serialization():
            return method(self, *args, **kwargs)
    return cast(_Method, guarded)


def guard_legal_hold(
    operation: str, *, scan: bool = False, move: bool = False,
) -> Callable[[_Method], _Method]:
    """Keep the hold fence around complete public catalog mutations.

SQL implementations acquire this fence before opening their transaction, so
denial audits can commit even though the rejected mutation never begins.
"""
    def decorate(method: _Method) -> _Method:
        @wraps(method)
        def guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
            if move:
                move_id = args[0] if args else kwargs["idempotency_key"]
                job = self.get_move_job(move_id)
                if job is None:
                    return method(self, *args, **kwargs)
                bucket, key = job.bucket, job.key
            else:
                bucket = args[0] if args else kwargs["bucket"]
                key = args[1] if len(args) > 1 else kwargs["key"]
            try:
                with self.destructive_operation(
                    bucket, key, operation=operation, context=kwargs.get("audit_context"),
                ):
                    return method(self, *args, **kwargs)
            except LegalHoldError:
                if scan:
                    return False
                raise
        return cast(_Method, guarded)
    return decorate
