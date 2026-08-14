from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, TypeAlias
from uuid import UUID, uuid4

JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

SCHEMA_VERSION = 1


class JobEnvelopeError(ValueError):
    """Raised when a queued job does not satisfy the wire contract."""


class InvalidJobError(ValueError):
    """Raised when a valid envelope has an unsupported job contract."""


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JobEnvelopeError(f"{field_name} must be a non-empty string")
    return value


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

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise JobEnvelopeError(
                f"unsupported schema_version {self.schema_version}; expected {SCHEMA_VERSION}"
            )
        object.__setattr__(self, "job_id", _uuid_string(self.job_id, "job_id"))
        object.__setattr__(self, "job_type", _required_string(self.job_type, "job_type"))
        object.__setattr__(self, "created_at", _created_at(self.created_at))
        object.__setattr__(
            self,
            "correlation_id",
            _required_string(self.correlation_id, "correlation_id"),
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
        object.__setattr__(self, "metadata", normalized_metadata)

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
    ) -> "JobEnvelope":
        identifier = job_id or str(uuid4())
        timestamp = created_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise JobEnvelopeError("created_at must include a timezone")
        return cls(
            schema_version=SCHEMA_VERSION,
            job_id=identifier,
            job_type=job_type,
            created_at=timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            correlation_id=correlation_id or identifier,
            payload=payload,
            metadata=metadata or {},
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
                "metadata": self.metadata,
            },
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> "JobEnvelope":
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
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

        return cls(
            schema_version=value["schema_version"],
            job_id=value["job_id"],
            job_type=value["job_type"],
            created_at=value["created_at"],
            correlation_id=value["correlation_id"],
            payload=value["payload"],
            metadata=value["metadata"],
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
            "error": self.error,
        }
