from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, TypeAlias
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from cognistore.auth.principal import PRINCIPAL_METADATA, Principal
from cognistore.observability import current_correlation_id, inject_trace_context

JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

JOB_SCHEMA_VERSION_V1 = 1
JOB_SCHEMA_VERSION_V2 = 2
JOB_SCHEMA_VERSION_V3 = 3
# Feature-free jobs remain v1 so they can still be handled by older workers.
SCHEMA_VERSION = JOB_SCHEMA_VERSION_V1
SUPPORTED_JOB_SCHEMA_VERSIONS = frozenset(
    {JOB_SCHEMA_VERSION_V1, JOB_SCHEMA_VERSION_V2, JOB_SCHEMA_VERSION_V3}
)
STATUS_TRACKING_METADATA = "cognistore_status_tracking"
DEAD_LETTER_SCHEMA_VERSION = 1
REDRIVE_SCHEMA_VERSION = 1
DEAD_LETTER_NAMESPACE = uuid5(NAMESPACE_URL, "https://cognistore.dev/dead-letters")
DEAD_LETTER_CHAIN_METADATA = "cognistore.dead_letter_chain"
REDRIVE_COUNT_METADATA = "cognistore.redrive_count"
ATTEMPT_OFFSET_METADATA = "cognistore.attempt_offset"
REDRIVEN_FROM_METADATA = "cognistore.redriven_from"


class JobEnvelopeError(ValueError):
    """Raised when a queued job does not satisfy the wire contract."""


class QueueSaturatedError(RuntimeError):
    """Raised when a bounded work queue rejects new durable work."""

    retryable = True

    def __init__(self, stream: str, reason: str) -> None:
        self.stream = stream
        self.reason = reason
        super().__init__(f"NATS stream {stream!r} is saturated: {reason}")


class InvalidJobError(ValueError):
    """Raised when a valid envelope has an unsupported job contract."""


class DeadLetterRecordError(ValueError):
    """Raised when a dead-letter or redrive audit record is malformed."""


class DeadLetterNotFoundError(LookupError):
    """Raised when an operator references an unknown dead-letter ID."""


class DeadLetterRedriveError(RuntimeError):
    """Raised when a dead-letter entry cannot be safely redriven."""


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JobEnvelopeError(f"{field_name} must be a non-empty string")
    return value


def _header_string(value: object, field_name: str) -> str:
    text = _required_string(value, field_name)
    if "\r" in text or "\n" in text:
        raise JobEnvelopeError(f"{field_name} must not contain CR or LF")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise JobEnvelopeError(f"{field_name} must be valid UTF-8") from exc
    return text


def _uuid_string(value: object, field_name: str) -> str:
    text = _required_string(value, field_name)
    try:
        parsed = UUID(text)
    except (ValueError, AttributeError) as exc:
        raise JobEnvelopeError(f"{field_name} must be a UUID") from exc
    if str(parsed) != text.lower():
        raise JobEnvelopeError(f"{field_name} must use canonical UUID format")
    return str(parsed)


def _json_value(value: object, path: str = "payload") -> JSONValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise JobEnvelopeError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, list):
        return [_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        output: dict[str, JSONValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise JobEnvelopeError(f"{path} keys must be strings")
            output[key] = _json_value(item, f"{path}.{key}")
        return output
    raise JobEnvelopeError(f"{path} contains a non-JSON value: {type(value).__name__}")


def _created_at(value: object) -> str:
    text = _required_string(value, "created_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JobEnvelopeError("created_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise JobEnvelopeError("created_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class JobEnvelope:
    """Versioned JSON job passed across the worker boundary.

    Delivery is at least once. ``job_id`` identifies the logical job across
    every delivery attempt; consumers must make side effects idempotent.
    """

    schema_version: int
    job_id: str
    job_type: str
    created_at: str
    correlation_id: str
    payload: Mapping[str, JSONValue]
    metadata: Mapping[str, str] = field(default_factory=dict)
    _principal: Principal | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version not in SUPPORTED_JOB_SCHEMA_VERSIONS
        ):
            supported = ", ".join(
                str(version) for version in sorted(SUPPORTED_JOB_SCHEMA_VERSIONS)
            )
            raise JobEnvelopeError(
                f"unsupported schema_version {self.schema_version}; "
                f"expected one of {supported}"
            )
        object.__setattr__(self, "job_id", _uuid_string(self.job_id, "job_id"))
        object.__setattr__(self, "job_type", _header_string(self.job_type, "job_type"))
        object.__setattr__(self, "created_at", _created_at(self.created_at))
        object.__setattr__(
            self,
            "correlation_id",
            _header_string(self.correlation_id, "correlation_id"),
        )
        normalized_payload = _json_value(self.payload)
        if not isinstance(normalized_payload, dict):
            raise JobEnvelopeError("payload must be a JSON object")
        object.__setattr__(self, "payload", normalized_payload)

        if not isinstance(self.metadata, Mapping):
            raise JobEnvelopeError("metadata must be an object of string values")
        normalized_metadata: dict[str, str] = {}
        for key, value in self.metadata.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise JobEnvelopeError("metadata keys and values must be strings")
            normalized_metadata[key] = value
        if PRINCIPAL_METADATA in normalized_metadata:
            try:
                principal = Principal.from_json(normalized_metadata[PRINCIPAL_METADATA])
            except (TypeError, ValueError) as exc:
                raise JobEnvelopeError("invalid normalized principal metadata") from exc
            normalized_metadata[PRINCIPAL_METADATA] = principal.to_json()
            object.__setattr__(self, "_principal", principal)
        object.__setattr__(self, "metadata", normalized_metadata)

    @property
    def principal(self) -> Principal | None:
        """The submitting identity, normalized at the trusted API boundary."""

        return self._principal

    def _wire_metadata(self) -> dict[str, str]:
        # Handlers may mutate ordinary metadata, but the submitting identity
        # remains the one validated when the envelope crossed the boundary.
        metadata = dict(self.metadata)
        if self.principal is None:
            metadata.pop(PRINCIPAL_METADATA, None)
        else:
            metadata[PRINCIPAL_METADATA] = self.principal.to_json()
        return metadata

    @classmethod
    def create(
        cls,
        job_type: str,
        payload: Mapping[str, JSONValue],
        *,
        job_id: str | None = None,
        correlation_id: str | None = None,
        metadata: Mapping[str, str] | None = None,
        created_at: datetime | None = None,
        schema_version: int = SCHEMA_VERSION,
    ) -> "JobEnvelope":
        identifier = job_id or str(uuid4())
        timestamp = created_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise JobEnvelopeError("created_at must include a timezone")
        return cls(
            schema_version=schema_version,
            job_id=identifier,
            job_type=job_type,
            created_at=timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            correlation_id=correlation_id or current_correlation_id() or identifier,
            payload=payload,
            # Trace context uses the existing extensible metadata contract, so
            # legacy workers and all supported schema versions still decode it.
            metadata={**inject_trace_context(), **(metadata or {})},
        )

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "job_id": self.job_id,
                "job_type": self.job_type,
                "created_at": self.created_at,
                "correlation_id": self.correlation_id,
                "payload": self.payload,
                "metadata": self._wire_metadata(),
            },
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> "JobEnvelope":
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise JobEnvelopeError("job envelope must be UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise JobEnvelopeError("job envelope must be a JSON object")

        expected = {
            "schema_version",
            "job_id",
            "job_type",
            "created_at",
            "correlation_id",
            "payload",
            "metadata",
        }
        missing = sorted(expected.difference(value))
        extra = sorted(set(value).difference(expected))
        if missing:
            raise JobEnvelopeError(f"job envelope is missing fields: {', '.join(missing)}")
        if extra:
            raise JobEnvelopeError(f"job envelope has unknown fields: {', '.join(extra)}")
        if not isinstance(value["schema_version"], int) or isinstance(
            value["schema_version"], bool
        ):
            raise JobEnvelopeError("schema_version must be an integer")

        try:
            return cls(
                schema_version=value["schema_version"],
                job_id=value["job_id"],
                job_type=value["job_type"],
                created_at=value["created_at"],
                correlation_id=value["correlation_id"],
                payload=value["payload"],
                metadata=value["metadata"],
            )
        except RecursionError as exc:
            raise JobEnvelopeError("job envelope nesting is too deep") from exc


class DeadLetterDisposition(str, Enum):
    TERMINAL = "terminal"
    EXHAUSTED = "exhausted"


def _dead_letter_string(value: object, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        qualification = "a string" if allow_empty else "a non-empty string"
        raise DeadLetterRecordError(f"{field_name} must be {qualification}")
    return value


def _dead_letter_uuid(value: object, field_name: str) -> str:
    text = _dead_letter_string(value, field_name)
    try:
        parsed = UUID(text)
    except (ValueError, AttributeError) as exc:
        raise DeadLetterRecordError(f"{field_name} must be a UUID") from exc
    if str(parsed) != text.lower():
        raise DeadLetterRecordError(f"{field_name} must use canonical UUID format")
    return str(parsed)


def _dead_letter_timestamp(value: object, field_name: str) -> str:
    text = _dead_letter_string(value, field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeadLetterRecordError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DeadLetterRecordError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _positive_record_integer(value: object, field_name: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise DeadLetterRecordError(f"{field_name} must be a {qualifier} integer")
    return value


def _record_string_map(value: object, field_name: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise DeadLetterRecordError(f"{field_name} must be an object of string values")
    output: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise DeadLetterRecordError(f"{field_name} keys and values must be strings")
        output[key] = item
    return output


def _metadata_integer(metadata: Mapping[str, str], key: str) -> int:
    value = metadata.get(key, "0")
    try:
        parsed = int(value)
    except ValueError:
        return 0
    return max(0, parsed)


def _metadata_audit_chain(metadata: Mapping[str, str]) -> tuple[str, ...]:
    encoded = metadata.get(DEAD_LETTER_CHAIN_METADATA)
    if encoded is None:
        return ()
    try:
        value = json.loads(encoded)
    except (ValueError, RecursionError):
        return ()
    if not isinstance(value, list):
        return ()
    try:
        return tuple(_dead_letter_uuid(item, "audit_chain item") for item in value)
    except DeadLetterRecordError:
        return ()


@dataclass(frozen=True)
class DeadLetterRecord:
    """Lossless diagnostic record for a terminal or exhausted delivery."""

    schema_version: int
    dead_letter_id: str
    failed_at: str
    disposition: DeadLetterDisposition
    retryable: bool
    category: str
    classification_reason: str
    attempt: int
    max_attempts: int
    cumulative_attempt: int
    source_stream: str
    source_published_at: str
    source_consumer: str
    stream_sequence: int
    consumer_sequence: int
    exception_type: str
    exception_message: str
    traceback: str
    raw_data: bytes
    headers: Mapping[str, str]
    job: JobEnvelope | None = None
    audit_chain: tuple[str, ...] = ()
    redrive_count: int = 0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != DEAD_LETTER_SCHEMA_VERSION
        ):
            raise DeadLetterRecordError(
                "unsupported dead-letter schema_version "
                f"{self.schema_version}; expected {DEAD_LETTER_SCHEMA_VERSION}"
            )
        object.__setattr__(
            self, "dead_letter_id", _dead_letter_uuid(self.dead_letter_id, "dead_letter_id")
        )
        object.__setattr__(self, "failed_at", _dead_letter_timestamp(self.failed_at, "failed_at"))
        try:
            disposition = DeadLetterDisposition(self.disposition)
        except ValueError as exc:
            raise DeadLetterRecordError("disposition must be terminal or exhausted") from exc
        object.__setattr__(self, "disposition", disposition)
        if not isinstance(self.retryable, bool):
            raise DeadLetterRecordError("retryable must be a boolean")
        for field_name in (
            "category",
            "classification_reason",
            "source_stream",
            "source_consumer",
            "exception_type",
        ):
            object.__setattr__(
                self,
                field_name,
                _dead_letter_string(getattr(self, field_name), field_name),
            )
        if "\r" in self.category or "\n" in self.category:
            raise DeadLetterRecordError("category must not contain CR or LF")
        try:
            self.category.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise DeadLetterRecordError("category must be valid UTF-8") from exc
        object.__setattr__(
            self,
            "source_published_at",
            _dead_letter_timestamp(self.source_published_at, "source_published_at"),
        )
        for field_name in ("attempt", "max_attempts", "cumulative_attempt"):
            object.__setattr__(
                self,
                field_name,
                _positive_record_integer(getattr(self, field_name), field_name),
            )
        for field_name in ("stream_sequence", "consumer_sequence"):
            object.__setattr__(
                self,
                field_name,
                _positive_record_integer(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "exception_message",
            _dead_letter_string(self.exception_message, "exception_message", allow_empty=True),
        )
        object.__setattr__(
            self,
            "traceback",
            _dead_letter_string(self.traceback, "traceback", allow_empty=True),
        )
        if not isinstance(self.raw_data, bytes):
            raise DeadLetterRecordError("raw_data must be bytes")
        object.__setattr__(self, "headers", _record_string_map(self.headers, "headers"))
        if self.job is not None and not isinstance(self.job, JobEnvelope):
            raise DeadLetterRecordError("job must be a JobEnvelope or null")
        try:
            raw_job = JobEnvelope.from_bytes(self.raw_data)
        except JobEnvelopeError:
            raw_job = None
        if self.job is not None:
            if raw_job is None:
                raise DeadLetterRecordError(
                    "raw_data must contain the recorded job envelope"
                )
            try:
                recorded_job_bytes = self.job.to_bytes()
            except (TypeError, ValueError, RecursionError) as exc:
                raise DeadLetterRecordError("job cannot be serialized") from exc
            if raw_job.to_bytes() != recorded_job_bytes:
                raise DeadLetterRecordError("raw_data and job envelope do not match")
        elif raw_job is not None:
            raise DeadLetterRecordError(
                "job must contain the valid envelope present in raw_data"
            )
        normalized_chain = tuple(
            _dead_letter_uuid(item, "audit_chain item") for item in self.audit_chain
        )
        if not normalized_chain or normalized_chain[-1] != self.dead_letter_id:
            raise DeadLetterRecordError("audit_chain must end with dead_letter_id")
        object.__setattr__(self, "audit_chain", normalized_chain)
        object.__setattr__(
            self,
            "redrive_count",
            _positive_record_integer(self.redrive_count, "redrive_count", allow_zero=True),
        )

    @classmethod
    def create(
        cls,
        *,
        failed_at: datetime,
        disposition: DeadLetterDisposition,
        retryable: bool,
        category: str,
        classification_reason: str,
        attempt: int,
        max_attempts: int,
        cumulative_attempt: int,
        source_stream: str,
        source_published_at: datetime,
        source_consumer: str,
        stream_sequence: int,
        consumer_sequence: int,
        exception_type: str,
        exception_message: str,
        traceback: str,
        raw_data: bytes,
        headers: Mapping[str, str],
        job: JobEnvelope | None,
    ) -> "DeadLetterRecord":
        if failed_at.tzinfo is None or failed_at.utcoffset() is None:
            raise DeadLetterRecordError("failed_at must include a timezone")
        if (
            source_published_at.tzinfo is None
            or source_published_at.utcoffset() is None
        ):
            raise DeadLetterRecordError("source_published_at must include a timezone")
        raw_digest = hashlib.sha256(raw_data).hexdigest()
        source_timestamp = (
            source_published_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        identifier = str(
            uuid5(
                DEAD_LETTER_NAMESPACE,
                f"{source_stream}:{source_timestamp}:{stream_sequence}:{raw_digest}",
            )
        )
        prior_chain = _metadata_audit_chain(job.metadata) if job is not None else ()
        redrive_count = (
            _metadata_integer(job.metadata, REDRIVE_COUNT_METADATA) if job is not None else 0
        )
        return cls(
            schema_version=DEAD_LETTER_SCHEMA_VERSION,
            dead_letter_id=identifier,
            failed_at=failed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            disposition=disposition,
            retryable=retryable,
            category=category,
            classification_reason=classification_reason,
            attempt=attempt,
            max_attempts=max_attempts,
            cumulative_attempt=cumulative_attempt,
            source_stream=source_stream,
            source_published_at=source_timestamp,
            source_consumer=source_consumer,
            stream_sequence=stream_sequence,
            consumer_sequence=consumer_sequence,
            exception_type=exception_type,
            exception_message=exception_message,
            traceback=traceback,
            raw_data=raw_data,
            headers=headers,
            job=job,
            audit_chain=(*prior_chain, identifier),
            redrive_count=redrive_count,
        )

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "dead_letter_id": self.dead_letter_id,
                "failed_at": self.failed_at,
                "disposition": self.disposition.value,
                "retryable": self.retryable,
                "category": self.category,
                "classification_reason": self.classification_reason,
                "attempt": self.attempt,
                "max_attempts": self.max_attempts,
                "cumulative_attempt": self.cumulative_attempt,
                "source_stream": self.source_stream,
                "source_published_at": self.source_published_at,
                "source_consumer": self.source_consumer,
                "stream_sequence": self.stream_sequence,
                "consumer_sequence": self.consumer_sequence,
                "exception_type": self.exception_type,
                "exception_message": self.exception_message,
                "traceback": self.traceback,
                "raw_data_base64": base64.b64encode(self.raw_data).decode("ascii"),
                "headers": self.headers,
                "audit_chain": list(self.audit_chain),
                "redrive_count": self.redrive_count,
            },
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> "DeadLetterRecord":
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise DeadLetterRecordError("dead-letter record must be UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise DeadLetterRecordError("dead-letter record must be a JSON object")
        expected = {
            "schema_version",
            "dead_letter_id",
            "failed_at",
            "disposition",
            "retryable",
            "category",
            "classification_reason",
            "attempt",
            "max_attempts",
            "cumulative_attempt",
            "source_stream",
            "source_published_at",
            "source_consumer",
            "stream_sequence",
            "consumer_sequence",
            "exception_type",
            "exception_message",
            "traceback",
            "raw_data_base64",
            "headers",
            "audit_chain",
            "redrive_count",
        }
        missing = sorted(expected.difference(value))
        extra = sorted(set(value).difference(expected))
        if missing:
            raise DeadLetterRecordError(
                f"dead-letter record is missing fields: {', '.join(missing)}"
            )
        if extra:
            raise DeadLetterRecordError(
                f"dead-letter record has unknown fields: {', '.join(extra)}"
            )
        try:
            raw_data = base64.b64decode(value["raw_data_base64"], validate=True)
        except (TypeError, ValueError) as exc:
            raise DeadLetterRecordError("raw_data_base64 must be valid base64") from exc
        audit_chain = value["audit_chain"]
        if not isinstance(audit_chain, list):
            raise DeadLetterRecordError("audit_chain must be a list")
        try:
            parsed_job = JobEnvelope.from_bytes(raw_data)
        except JobEnvelopeError:
            parsed_job = None
        return cls(
            schema_version=value["schema_version"],
            dead_letter_id=value["dead_letter_id"],
            failed_at=value["failed_at"],
            disposition=value["disposition"],
            retryable=value["retryable"],
            category=value["category"],
            classification_reason=value["classification_reason"],
            attempt=value["attempt"],
            max_attempts=value["max_attempts"],
            cumulative_attempt=value["cumulative_attempt"],
            source_stream=value["source_stream"],
            source_published_at=value["source_published_at"],
            source_consumer=value["source_consumer"],
            stream_sequence=value["stream_sequence"],
            consumer_sequence=value["consumer_sequence"],
            exception_type=value["exception_type"],
            exception_message=value["exception_message"],
            traceback=value["traceback"],
            raw_data=raw_data,
            headers=value["headers"],
            job=parsed_job,
            audit_chain=tuple(audit_chain),
            redrive_count=value["redrive_count"],
        )

    def job_for_redrive(self) -> JobEnvelope:
        if self.job is None:
            raise DeadLetterRedriveError(
                f"dead-letter {self.dead_letter_id} has no valid job envelope"
            )
        metadata = self.job._wire_metadata()
        metadata.update(
            {
                DEAD_LETTER_CHAIN_METADATA: json.dumps(
                    list(self.audit_chain), separators=(",", ":")
                ),
                REDRIVE_COUNT_METADATA: str(self.redrive_count + 1),
                ATTEMPT_OFFSET_METADATA: str(self.cumulative_attempt),
                REDRIVEN_FROM_METADATA: self.dead_letter_id,
            }
        )
        return JobEnvelope(
            schema_version=self.job.schema_version,
            job_id=self.job.job_id,
            job_type=self.job.job_type,
            created_at=self.job.created_at,
            correlation_id=self.job.correlation_id,
            payload=self.job.payload,
            metadata=metadata,
        )


@dataclass(frozen=True)
class DeadLetterReceipt:
    dead_letter_id: str
    stream: str
    sequence: int
    duplicate: bool = False


@dataclass(frozen=True)
class RedriveReceipt:
    dead_letter_id: str
    job_id: str
    correlation_id: str
    stream: str
    sequence: int
    redrive_count: int
    audit_chain: tuple[str, ...]
    duplicate: bool = False


@dataclass(frozen=True)
class RedriveAuditRecord:
    schema_version: int
    dead_letter_id: str
    redriven_at: str
    job_id: str
    correlation_id: str
    stream: str
    sequence: int
    redrive_count: int
    audit_chain: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != REDRIVE_SCHEMA_VERSION
        ):
            raise DeadLetterRecordError(
                f"unsupported redrive schema_version {self.schema_version}; "
                f"expected {REDRIVE_SCHEMA_VERSION}"
            )
        object.__setattr__(
            self, "dead_letter_id", _dead_letter_uuid(self.dead_letter_id, "dead_letter_id")
        )
        object.__setattr__(
            self, "redriven_at", _dead_letter_timestamp(self.redriven_at, "redriven_at")
        )
        for field_name in ("job_id", "correlation_id", "stream"):
            object.__setattr__(
                self,
                field_name,
                _dead_letter_string(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "job_id", _dead_letter_uuid(self.job_id, "job_id"))
        object.__setattr__(self, "sequence", _positive_record_integer(self.sequence, "sequence"))
        object.__setattr__(
            self,
            "redrive_count",
            _positive_record_integer(self.redrive_count, "redrive_count"),
        )
        normalized_chain = tuple(
            _dead_letter_uuid(item, "audit_chain item") for item in self.audit_chain
        )
        if not normalized_chain or normalized_chain[-1] != self.dead_letter_id:
            raise DeadLetterRecordError("audit_chain must end with dead_letter_id")
        object.__setattr__(self, "audit_chain", normalized_chain)

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "dead_letter_id": self.dead_letter_id,
                "redriven_at": self.redriven_at,
                "job_id": self.job_id,
                "correlation_id": self.correlation_id,
                "stream": self.stream,
                "sequence": self.sequence,
                "redrive_count": self.redrive_count,
                "audit_chain": list(self.audit_chain),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> "RedriveAuditRecord":
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise DeadLetterRecordError("redrive audit record must be UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise DeadLetterRecordError("redrive audit record must be a JSON object")
        expected = {
            "schema_version",
            "dead_letter_id",
            "redriven_at",
            "job_id",
            "correlation_id",
            "stream",
            "sequence",
            "redrive_count",
            "audit_chain",
        }
        missing = sorted(expected.difference(value))
        extra = sorted(set(value).difference(expected))
        if missing or extra:
            detail = "missing " + ", ".join(missing) if missing else "unknown " + ", ".join(extra)
            raise DeadLetterRecordError(f"redrive audit record fields are invalid: {detail}")
        if not isinstance(value["audit_chain"], list):
            raise DeadLetterRecordError("audit_chain must be a list")
        return cls(
            schema_version=value["schema_version"],
            dead_letter_id=value["dead_letter_id"],
            redriven_at=value["redriven_at"],
            job_id=value["job_id"],
            correlation_id=value["correlation_id"],
            stream=value["stream"],
            sequence=value["sequence"],
            redrive_count=value["redrive_count"],
            audit_chain=tuple(value["audit_chain"]),
        )

    def to_receipt(self, *, duplicate: bool) -> RedriveReceipt:
        return RedriveReceipt(
            dead_letter_id=self.dead_letter_id,
            job_id=self.job_id,
            correlation_id=self.correlation_id,
            stream=self.stream,
            sequence=self.sequence,
            redrive_count=self.redrive_count,
            audit_chain=self.audit_chain,
            duplicate=duplicate,
        )


@dataclass(frozen=True)
class EnqueueReceipt:
    job_id: str
    correlation_id: str
    stream: str
    sequence: int
    duplicate: bool = False


@dataclass(frozen=True)
class JobContext:
    attempt: int
    redelivered: bool
    stream_sequence: int
    consumer_sequence: int
    shutdown_requested: Any
    cumulative_attempt: int | None = None
    redrive_count: int = 0
    principal: Principal | None = None


class BusState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DRAINING = "draining"
    CLOSED = "closed"


@dataclass(frozen=True)
class QueueHealth:
    state: BusState
    ready: bool
    jetstream: bool
    stream: str
    consumer: str
    pending: int | None = None
    ack_pending: int | None = None
    redelivered: int | None = None
    error: str | None = None
    # Appended after the original fields to preserve positional construction.
    stored_messages: int | None = None
    stored_bytes: int | None = None
    max_messages: int | None = None
    max_bytes: int | None = None
    utilization: float | None = None
    saturated: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "ready": self.ready,
            "jetstream": self.jetstream,
            "stream": self.stream,
            "consumer": self.consumer,
            "pending": self.pending,
            "ack_pending": self.ack_pending,
            "redelivered": self.redelivered,
            "stored_messages": self.stored_messages,
            "stored_bytes": self.stored_bytes,
            "max_messages": self.max_messages,
            "max_bytes": self.max_bytes,
            "utilization": self.utilization,
            "saturated": self.saturated,
            "error": self.error,
        }
