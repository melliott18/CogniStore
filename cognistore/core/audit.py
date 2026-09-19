"""Versioned, secret-safe domain contracts for operational audit events."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, TypeAlias
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from cognistore.utils.redaction import redact, redact_text

AUDIT_SCHEMA_NAME = "cognistore.audit"
AUDIT_SCHEMA_VERSION = 1
DEFAULT_AUDIT_RETENTION_MAX_AGE = 30 * 24 * 60 * 60

_AUDIT_EVENT_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "https://cognistore.dev/audit-events",
)
_VOCABULARY = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,127}$")
_PSEUDONYM = re.compile(r"^\[REDACTED:sha256:[0-9a-f]{64}\]$")

JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


class AuditEventType(str, Enum):
    """Stable event vocabulary for the initial audit schema."""

    MOVE_PREPARED = "move.prepared"
    MOVE_TRANSITIONED = "move.transitioned"
    MOVE_COMPLETED = "move.completed"
    MOVE_FAILED = "move.failed"
    MOVE_RETRY = "move.retry"
    POLICY_DECISION = "policy.decision"
    JOB_QUEUED = "job.queued"
    JOB_STARTED = "job.started"
    JOB_SUCCEEDED = "job.succeeded"
    JOB_SUBMISSION_FAILED = "job.submission_failed"
    JOB_FAILURE = "job.failure"
    JOB_RETRY = "job.retry_scheduled"
    JOB_DEAD_LETTERED = "job.dead_lettered"
    MANUAL_ACTION = "manual.action"
    IMPORTANCE_CHANGED = "importance.changed"
    BUDGET_CONFIGURED = "budget.configured"
    BUDGET_RESERVED = "budget.reserved"
    CONSISTENCY_STARTED = "consistency.started"
    CONSISTENCY_RESUMED = "consistency.resumed"
    CONSISTENCY_CHECKPOINT = "consistency.checkpoint"
    CONSISTENCY_COMPLETED = "consistency.completed"
    CONSISTENCY_FAILED = "consistency.failed"
    CONSISTENCY_EXPORTED = "consistency.exported"
    AUTHORIZATION_DECISION = "authorization.decision"
    LEGAL_HOLD_PLACED = "legal_hold.placed"
    LEGAL_HOLD_RELEASED = "legal_hold.released"
    LEGAL_HOLD_DENIED = "legal_hold.denied"


class AuditOutcome(str, Enum):
    """Backend-neutral outcomes shared by audit event producers."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"
    SELECTED = "selected"
    STAYED = "stayed"
    REJECTED = "rejected"
    REQUESTED = "requested"
    ALLOWED = "allowed"
    DENIED = "denied"


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _required_vocabulary_text(value: object, field_name: str) -> str:
    normalized = _required_text(value, field_name)
    if "\0" in normalized:
        raise ValueError(f"{field_name} cannot contain NUL characters")
    if not _VOCABULARY.fullmatch(normalized) or redact_text(normalized) != normalized:
        raise ValueError(
            f"{field_name} must be a bounded identifier-style vocabulary token"
        )
    return normalized


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def audit_text_identity(
    value: str,
    *,
    _allow_pseudonym: bool = False,
) -> str:
    """Return a stable persistence/query identity without storing recognized secrets.

    Safe values remain human-readable. Values changed by the shared credential
    redactor become distinct SHA-256 pseudonyms, preventing two credentials from
    collapsing onto the same query key. The digest is pseudonymization rather
    than encryption, so callers must still avoid putting secrets in identifiers.
    """

    normalized = _required_text(value, "audit identity")
    if _PSEUDONYM.fullmatch(normalized):
        if _allow_pseudonym:
            return normalized
        raise ValueError("audit identity uses the reserved pseudonym namespace")
    if redact_text(normalized) == normalized and len(normalized.encode("utf-8")) <= 512:
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"[REDACTED:sha256:{digest}]"


def _canonical_uuid(value: object, field_name: str) -> str:
    try:
        parsed = value if isinstance(value, UUID) else UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc
    return str(parsed)


def _timestamp_datetime(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical_timestamp(value: object, field_name: str) -> str:
    return (
        _timestamp_datetime(value, field_name)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def canonical_audit_timestamp(
    value: str | datetime,
    field_name: str = "audit timestamp",
) -> str:
    """Return the canonical UTC representation used by persisted audit metadata."""

    return _canonical_timestamp(value, field_name)


def _copy_json_value(value: object, path: str, active: set[int]) -> JSONValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{path} contains a reference cycle")
        active.add(identity)
        try:
            return [
                _copy_json_value(item, f"{path}[{index}]", active)
                for index, item in enumerate(value)
            ]
        finally:
            active.remove(identity)
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{path} contains a reference cycle")
        active.add(identity)
        try:
            output: dict[str, JSONValue] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError(f"{path} keys must be strings")
                output[key] = _copy_json_value(item, f"{path}.{key}", active)
            return output
        finally:
            active.remove(identity)
    raise ValueError(f"{path} contains a non-JSON value: {type(value).__name__}")


def _copy_json_object(value: object, field_name: str = "details") -> dict[str, JSONValue]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    try:
        copied = _copy_json_value(value, field_name, set())
    except RecursionError as exc:
        raise ValueError(f"{field_name} nesting is too deep") from exc
    if not isinstance(copied, dict):  # pragma: no cover - guarded by Mapping above
        raise ValueError(f"{field_name} must be a JSON object")
    return copied


@dataclass(frozen=True)
class AuditRetentionPolicy:
    """Compute event expiry while allowing explicit indefinite retention."""

    max_age_seconds: float | None = DEFAULT_AUDIT_RETENTION_MAX_AGE

    def __post_init__(self) -> None:
        value = self.max_age_seconds
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("max_age_seconds must be a finite positive number or null")
        normalized = float(value)
        if not math.isfinite(normalized) or normalized <= 0:
            raise ValueError("max_age_seconds must be a finite positive number or null")
        object.__setattr__(self, "max_age_seconds", normalized)

    def expires_at(self, occurred_at: str | datetime) -> str | None:
        """Return the canonical expiry for a canonicalizable occurrence time."""

        if self.max_age_seconds is None:
            return None
        occurred = _timestamp_datetime(occurred_at, "occurred_at")
        try:
            expires = occurred + timedelta(seconds=self.max_age_seconds)
        except OverflowError as exc:
            raise ValueError("max_age_seconds exceeds the supported timestamp range") from exc
        return _canonical_timestamp(expires, "expires_at")


@dataclass(frozen=True)
class AuditContext:
    """Correlation and actor identity inherited by events from one operation."""

    correlation_id: str
    actor_type: str
    actor_id: str
    job_id: str | None = None
    causation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "correlation_id",
            _required_text(self.correlation_id, "correlation_id"),
        )
        object.__setattr__(
            self,
            "actor_type",
            _required_vocabulary_text(self.actor_type, "actor_type"),
        )
        object.__setattr__(self, "actor_id", _required_text(self.actor_id, "actor_id"))
        object.__setattr__(self, "job_id", _optional_text(self.job_id, "job_id"))
        if self.causation_id is not None:
            object.__setattr__(
                self,
                "causation_id",
                _canonical_uuid(self.causation_id, "causation_id"),
            )


@dataclass(frozen=True)
class AuditEvent:
    """One append-oriented, versioned operational audit event."""

    event_id: str
    schema_version: int
    event_type: str
    outcome: str
    occurred_at: str
    recorded_at: str
    correlation_id: str
    actor_type: str
    actor_id: str
    expires_at: str | None = None
    causation_id: str | None = None
    bucket: str | None = None
    object_key: str | None = None
    job_id: str | None = None
    move_id: str | None = None
    policy_name: str | None = None
    policy_version: str | None = None
    details: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version <= 0
        ):
            raise ValueError("schema_version must be a positive integer")
        object.__setattr__(self, "event_id", _canonical_uuid(self.event_id, "event_id"))
        object.__setattr__(
            self,
            "event_type",
            _required_vocabulary_text(self.event_type, "event_type"),
        )
        object.__setattr__(
            self,
            "outcome",
            _required_vocabulary_text(self.outcome, "outcome"),
        )
        object.__setattr__(
            self,
            "occurred_at",
            _canonical_timestamp(self.occurred_at, "occurred_at"),
        )
        object.__setattr__(
            self,
            "recorded_at",
            _canonical_timestamp(self.recorded_at, "recorded_at"),
        )
        if self.expires_at is not None:
            expires_at = _canonical_timestamp(self.expires_at, "expires_at")
            if expires_at <= self.occurred_at:
                raise ValueError("expires_at must be later than occurred_at")
            object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(
            self,
            "correlation_id",
            _required_text(self.correlation_id, "correlation_id"),
        )
        if self.causation_id is not None:
            object.__setattr__(
                self,
                "causation_id",
                _canonical_uuid(self.causation_id, "causation_id"),
            )
        object.__setattr__(
            self,
            "actor_type",
            _required_vocabulary_text(self.actor_type, "actor_type"),
        )
        object.__setattr__(self, "actor_id", _required_text(self.actor_id, "actor_id"))
        object.__setattr__(self, "job_id", _optional_text(self.job_id, "job_id"))
        object.__setattr__(self, "move_id", _optional_text(self.move_id, "move_id"))
        object.__setattr__(
            self,
            "policy_name",
            _optional_text(self.policy_name, "policy_name"),
        )
        object.__setattr__(
            self,
            "policy_version",
            _optional_text(self.policy_version, "policy_version"),
        )
        bucket = _optional_text(self.bucket, "bucket")
        object_key = _optional_text(self.object_key, "object_key")
        if (bucket is None) != (object_key is None):
            raise ValueError("bucket and object_key must be provided together")
        object.__setattr__(self, "bucket", bucket)
        object.__setattr__(self, "object_key", object_key)
        object.__setattr__(self, "details", _copy_json_object(self.details))

    @classmethod
    def create(
        cls,
        event_type: AuditEventType | str,
        outcome: AuditOutcome | str,
        context: AuditContext,
        *,
        event_id: str | UUID | None = None,
        occurred_at: str | datetime | None = None,
        recorded_at: str | datetime | None = None,
        retention: AuditRetentionPolicy | None = None,
        bucket: str | None = None,
        object_key: str | None = None,
        move_id: str | None = None,
        policy_name: str | None = None,
        policy_version: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        """Create a canonical event and calculate retention from occurrence time."""

        if not isinstance(context, AuditContext):
            raise ValueError("context must be an AuditContext")
        now = datetime.now(timezone.utc)
        occurred = _canonical_timestamp(
            now if occurred_at is None else occurred_at,
            "occurred_at",
        )
        recorded = _canonical_timestamp(
            now if recorded_at is None else recorded_at,
            "recorded_at",
        )
        retention_policy = retention or AuditRetentionPolicy()
        if not isinstance(retention_policy, AuditRetentionPolicy):
            raise ValueError("retention must be an AuditRetentionPolicy")
        return cls(
            event_id=str(uuid4() if event_id is None else event_id),
            schema_version=AUDIT_SCHEMA_VERSION,
            event_type=AuditEventType(event_type).value,
            outcome=AuditOutcome(outcome).value,
            occurred_at=occurred,
            recorded_at=recorded,
            expires_at=retention_policy.expires_at(occurred),
            correlation_id=context.correlation_id,
            causation_id=context.causation_id,
            actor_type=context.actor_type,
            actor_id=context.actor_id,
            bucket=bucket,
            object_key=object_key,
            job_id=context.job_id,
            move_id=move_id,
            policy_name=policy_name,
            policy_version=policy_version,
            details={} if details is None else details,
        )


def _text_filter(values: object, field_name: str) -> frozenset[str] | None:
    if values is None:
        return None
    if isinstance(values, str):
        candidates: Iterable[object] = (values,)
    elif isinstance(values, Iterable):
        candidates = values
    else:
        raise ValueError(f"{field_name} must be an iterable")
    normalized = frozenset(
        _required_vocabulary_text(
            value.value if isinstance(value, Enum) else value,
            f"{field_name} item",
        )
        for value in candidates
    )
    if not normalized:
        raise ValueError(f"{field_name} cannot be empty")
    return normalized


@dataclass(frozen=True)
class AuditQuery:
    """Bounded filters for stable audit-event queries."""

    correlation_id: str | None = None
    job_id: str | None = None
    move_id: str | None = None
    bucket: str | None = None
    object_key: str | None = None
    policy_name: str | None = None
    policy_version: str | None = None
    actor_type: str | None = None
    actor_id: str | None = None
    event_types: frozenset[str] | None = None
    outcomes: frozenset[str] | None = None
    occurred_after: str | None = None
    occurred_before: str | None = None
    limit: int = 100
    ascending: bool = True
    # Exclusive chronological boundary, including the UUID tie-breaker. This
    # remains a "before" filter regardless of the requested result ordering.
    before_event: tuple[str, str] | None = None

    @classmethod
    def from_event(
        cls, event: AuditEvent, *, event_types: frozenset[str] | None = None,
        limit: int = 100, ascending: bool = True,
    ) -> AuditQuery:
        """Query a retained event's execution identities, including pseudonyms.

        Public query construction continues to reject the reserved namespace.
        This path accepts an AuditEvent, never caller-supplied query identities;
        normal identities are redacted and already-retained pseudonyms survive.
        """
        if not isinstance(event, AuditEvent):
            raise ValueError("event must be an AuditEvent")
        query = cls(event_types=event_types, limit=limit, ascending=ascending)
        for name in ("correlation_id", "job_id", "move_id", "bucket", "object_key"):
            value = getattr(event, name)
            object.__setattr__(
                query, name,
                None if value is None else audit_text_identity(value, _allow_pseudonym=True),
            )
        return query

    def __post_init__(self) -> None:
        for field_name in (
            "correlation_id",
            "job_id",
            "move_id",
            "bucket",
            "object_key",
            "policy_name",
            "policy_version",
            "actor_id",
        ):
            value = _optional_text(getattr(self, field_name), field_name)
            object.__setattr__(
                self,
                field_name,
                None if value is None else audit_text_identity(value),
            )
        if self.actor_type is not None:
            object.__setattr__(
                self,
                "actor_type",
                _required_vocabulary_text(self.actor_type, "actor_type"),
            )
        if self.object_key is not None and self.bucket is None:
            raise ValueError("object_key requires bucket")
        object.__setattr__(
            self,
            "event_types",
            _text_filter(self.event_types, "event_types"),
        )
        object.__setattr__(
            self,
            "outcomes",
            _text_filter(self.outcomes, "outcomes"),
        )
        if self.occurred_after is not None:
            object.__setattr__(
                self,
                "occurred_after",
                _canonical_timestamp(self.occurred_after, "occurred_after"),
            )
        if self.occurred_before is not None:
            object.__setattr__(
                self,
                "occurred_before",
                _canonical_timestamp(self.occurred_before, "occurred_before"),
            )
        if (
            self.occurred_after is not None
            and self.occurred_before is not None
            and self.occurred_after >= self.occurred_before
        ):
            raise ValueError("occurred_after must be earlier than occurred_before")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= 10_000
        ):
            raise ValueError("limit must be an integer between 1 and 10000")
        if not isinstance(self.ascending, bool):
            raise ValueError("ascending must be a boolean")
        if self.before_event is not None:
            if not isinstance(self.before_event, tuple) or len(self.before_event) != 2:
                raise ValueError("before_event must be a timestamp and event ID tuple")
            timestamp, event_id = self.before_event
            object.__setattr__(self, "before_event", (
                _canonical_timestamp(timestamp, "before_event timestamp"),
                _canonical_uuid(event_id, "before_event event ID"),
            ))


def stable_audit_event_id(*parts: str) -> str:
    """Return a deterministic UUID for one logical event identity."""

    if not parts:
        raise ValueError("stable audit event identity requires at least one part")
    if any(not isinstance(part, str) for part in parts):
        raise ValueError("stable audit event identity parts must be strings")
    identity = json.dumps(
        ["" if part == "" else audit_text_identity(part) for part in parts],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return str(uuid5(_AUDIT_EVENT_NAMESPACE, identity))


def audit_event_replay_digest(event: AuditEvent) -> str:
    """Hash immutable event content using the catalog's replay semantics."""

    if not isinstance(event, AuditEvent):
        raise ValueError("event must be an AuditEvent")
    payload = {
        "event_id": event.event_id,
        "schema_version": event.schema_version,
        "event_type": event.event_type,
        "outcome": event.outcome,
        "occurred_at": event.occurred_at,
        "recorded_at": event.recorded_at,
        "correlation_id": event.correlation_id,
        "causation_id": (
            None if event.move_id is not None else event.causation_id
        ),
        "actor_type": event.actor_type,
        "actor_id": event.actor_id,
        "bucket": event.bucket,
        "object_key": event.object_key,
        "job_id": event.job_id,
        "move_id": event.move_id,
        "policy_name": event.policy_name,
        "policy_version": event.policy_version,
        "details": event.details,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def redact_audit_event(
    event: AuditEvent,
    *,
    _allow_pseudonyms: bool = False,
) -> AuditEvent:
    """Return a validated copy with every persistable free-text value redacted."""

    if not isinstance(event, AuditEvent):
        raise ValueError("event must be an AuditEvent")

    def safe_optional(value: str | None) -> str | None:
        return (
            None
            if value is None
            else audit_text_identity(
                value,
                _allow_pseudonym=_allow_pseudonyms,
            )
        )

    safe_details = redact(event.details)
    if not isinstance(safe_details, Mapping):  # pragma: no cover - redaction preserves mappings
        raise ValueError("redacted audit details must remain a JSON object")
    return AuditEvent(
        event_id=event.event_id,
        schema_version=event.schema_version,
        event_type=event.event_type,
        outcome=event.outcome,
        occurred_at=event.occurred_at,
        recorded_at=event.recorded_at,
        expires_at=event.expires_at,
        correlation_id=audit_text_identity(
            event.correlation_id,
            _allow_pseudonym=_allow_pseudonyms,
        ),
        causation_id=event.causation_id,
        actor_type=event.actor_type,
        actor_id=audit_text_identity(
            event.actor_id,
            _allow_pseudonym=_allow_pseudonyms,
        ),
        bucket=safe_optional(event.bucket),
        object_key=safe_optional(event.object_key),
        job_id=safe_optional(event.job_id),
        move_id=safe_optional(event.move_id),
        policy_name=safe_optional(event.policy_name),
        policy_version=safe_optional(event.policy_version),
        details=safe_details,
    )


__all__ = [
    "AUDIT_SCHEMA_NAME",
    "AUDIT_SCHEMA_VERSION",
    "DEFAULT_AUDIT_RETENTION_MAX_AGE",
    "AuditContext",
    "AuditEvent",
    "AuditEventType",
    "AuditOutcome",
    "AuditQuery",
    "AuditRetentionPolicy",
    "audit_event_replay_digest",
    "audit_text_identity",
    "canonical_audit_timestamp",
    "redact_audit_event",
    "stable_audit_event_id",
]
