"""Immutable observed-access events and deterministic, explicitly partial signals.

Only successful operations observed by an instrumented entry point are evidence.
An empty history is never a claim that the object is cold or that observation was
continuous. Timestamps use event time; windows are (as_of - seconds, as_of].
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol
from uuid import uuid4

ACCESS_KINDS = ("read", "write", "list", "touch")
AccessKind = Literal["read", "write", "list", "touch"]
AccessFreshness = Literal["fresh", "stale", "missing", "unavailable"]


def access_timestamp(value: str | datetime | None = None) -> str:
    """Normalize an explicitly timezone-aware instant to sortable UTC text."""
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("access timestamp must be an ISO 8601 instant") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("access timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def access_cutoff(as_of: str, seconds: int) -> str:
    instant = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    try:
        return access_timestamp(instant - timedelta(seconds=seconds))
    except OverflowError as exc:
        raise ValueError("access window exceeds the timestamp range") from exc


def _positive_seconds(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 315360000:
        raise ValueError(f"{name} must be an integer from 1 to 315360000 seconds")
    return value


def _sample_rate(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("sample_rate must be a finite number in [1e-9, 1]")
    if not 1e-9 <= value <= 1 or not math.isfinite(value):
        raise ValueError("sample_rate must be a finite number in [1e-9, 1]")
    return float(value)


def _text(value: object, name: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value or (maximum is not None and len(value) > maximum):
        raise ValueError(
            f"{name} must be a nonempty string"
            + (f" of at most {maximum} characters" if maximum else "")
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must contain valid UTF-8 text") from exc
    return value


@dataclass(frozen=True)
class AccessConfig:
    windows_seconds: tuple[int, ...] = (3600, 86400, 604800)
    retention_seconds: int = 2592000
    sample_rate: float = 1.0
    freshness_seconds: int = 86400

    def __post_init__(self) -> None:
        retention = _positive_seconds(self.retention_seconds, "retention_seconds")
        _positive_seconds(self.freshness_seconds, "freshness_seconds")
        if (
            not isinstance(self.windows_seconds, (tuple, list))
            or not 1 <= len(self.windows_seconds) <= 16
        ):
            raise ValueError("windows_seconds must contain between 1 and 16 distinct windows")
        windows = tuple(
            sorted(_positive_seconds(window, "window") for window in self.windows_seconds)
        )
        if len(set(windows)) != len(windows) or windows[-1] > retention:
            raise ValueError(
                "windows_seconds must be distinct and no longer than retention_seconds"
            )
        object.__setattr__(self, "windows_seconds", windows)
        object.__setattr__(self, "sample_rate", _sample_rate(self.sample_rate))


@dataclass(frozen=True)
class AccessEvent:
    event_id: str
    operation_id: str
    correlation_id: str
    occurred_at: str
    kind: AccessKind
    bucket: str
    key: str | None = None
    tier: str | None = None
    source: str = "api"
    sample_rate: float = 1.0
    schema_version: int = 1

    def __post_init__(self) -> None:
        _text(self.event_id, "event_id", maximum=256)
        _text(self.operation_id, "operation_id", maximum=256)
        _text(self.correlation_id, "correlation_id", maximum=256)
        _text(self.bucket, "bucket")
        _text(self.source, "source", maximum=128)
        if self.kind not in ACCESS_KINDS:
            raise ValueError("kind must be read, write, list, or touch")
        if self.key is None:
            if self.kind != "list":
                raise ValueError("object access events require a key")
        else:
            _text(self.key, "key")
        if self.tier is not None:
            _text(self.tier, "tier")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != 1
        ):
            raise ValueError("unsupported access event schema_version")
        object.__setattr__(self, "occurred_at", access_timestamp(self.occurred_at))
        object.__setattr__(self, "sample_rate", _sample_rate(self.sample_rate))

    @classmethod
    def create(
        cls,
        *,
        kind: AccessKind,
        bucket: str,
        key: str | None = None,
        tier: str | None = None,
        source: str = "api",
        correlation_id: str | None = None,
        operation_id: str | None = None,
        occurred_at: str | datetime | None = None,
        sample_rate: float = 1.0,
    ) -> AccessEvent:
        operation_id = operation_id if operation_id is not None else correlation_id
        operation_id = operation_id if operation_id is not None else str(uuid4())
        correlation_id = correlation_id if correlation_id is not None else operation_id
        # Source and tier are deliberately absent: observing the same operation
        # through nested wrappers or after a placement change is one access.
        identity = json.dumps(
            [operation_id, kind, bucket, key], ensure_ascii=True, separators=(",", ":")
        )
        return cls(
            event_id="access:" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            operation_id=operation_id,
            correlation_id=correlation_id,
            occurred_at=access_timestamp(occurred_at),
            kind=kind,
            bucket=bucket,
            key=key,
            tier=tier,
            source=source,
            sample_rate=sample_rate,
        )

    def replay_identity(self) -> tuple[object, ...]:
        """Retry-stable identity; the first timestamp, sampling and provenance win."""
        return (
            self.operation_id,
            self.kind,
            self.bucket,
            self.key,
            self.schema_version,
        )


@dataclass(frozen=True)
class AccessWindow:
    seconds: int
    counts: tuple[int, ...]
    estimated_counts: tuple[float, ...]


@dataclass(frozen=True)
class AccessSnapshot:
    """A bounded aggregate; no raw access-event enumeration is required."""

    windows: tuple[AccessWindow, ...]
    observed_events: int
    observed_since: str | None
    last_access_at: str | None
    minimum_sample_rate: float | None


class AccessStore(Protocol):
    def append_access_event(self, event: AccessEvent) -> AccessEvent: ...

    def aggregate_access_events(
        self,
        bucket: str,
        key: str | None,
        *,
        config: AccessConfig,
        as_of: str | datetime,
    ) -> AccessSnapshot: ...


@dataclass(frozen=True)
class AccessFeatures:
    bucket: str
    key: str | None
    as_of: str
    snapshot: AccessSnapshot
    config: AccessConfig
    freshness: AccessFreshness
    recency_seconds: float | None
    reason: str | None = None

    @property
    def missing(self) -> bool:
        return self.snapshot.observed_events == 0

    @property
    def partial(self) -> bool:
        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "bucket": self.bucket,
            "key": self.key,
            "as_of": self.as_of,
            "windows": {
                str(window.seconds): dict(zip(ACCESS_KINDS, window.counts))
                for window in self.snapshot.windows
            },
            "estimated_windows": {
                str(window.seconds): dict(zip(ACCESS_KINDS, window.estimated_counts))
                for window in self.snapshot.windows
            },
            "last_access_at": self.snapshot.last_access_at,
            "observed_since": self.snapshot.observed_since,
            "observed_events": self.snapshot.observed_events,
            "recency_seconds": self.recency_seconds,
            "freshness": self.freshness,
            "freshness_seconds": self.config.freshness_seconds,
            "freshness_basis": "latest_observed_event",
            "sampling": {
                "configured_rate": self.config.sample_rate,
                "minimum_rate": self.snapshot.minimum_sample_rate,
                "sampled": self.config.sample_rate < 1
                or (
                    self.snapshot.minimum_sample_rate is not None
                    and self.snapshot.minimum_sample_rate < 1
                ),
            },
            "missing": self.missing,
            "partial": True,
            "coverage": "observed_operations_only",
            "retention_seconds": self.config.retention_seconds,
            "reason": self.reason,
        }


def empty_access_snapshot(config: AccessConfig) -> AccessSnapshot:
    return AccessSnapshot(
        windows=tuple(
            AccessWindow(seconds, (0, 0, 0, 0), (0.0, 0.0, 0.0, 0.0))
            for seconds in config.windows_seconds
        ),
        observed_events=0,
        observed_since=None,
        last_access_at=None,
        minimum_sample_rate=None,
    )


def compute_access_features(
    catalog: AccessStore | None,
    bucket: str,
    key: str | None,
    config: AccessConfig | None = None,
    as_of: str | datetime | None = None,
) -> AccessFeatures:
    config = config or AccessConfig()
    instant = access_timestamp(as_of)
    # Reject bad time bounds independently of collector availability.
    access_cutoff(instant, config.retention_seconds)
    snapshot = empty_access_snapshot(config)
    if catalog is None:
        return AccessFeatures(
            bucket, key, instant, snapshot, config, "missing", None, "history_not_configured"
        )
    try:
        snapshot = catalog.aggregate_access_events(bucket, key, config=config, as_of=instant)
    except Exception:
        # Policy inputs must report unavailability, never interpret a database
        # outage as observed inactivity. Do not expose backend error messages.
        return AccessFeatures(
            bucket, key, instant, snapshot, config, "unavailable", None, "history_unavailable"
        )
    recency = None
    state: AccessFreshness = "missing"
    if snapshot.last_access_at is not None:
        recency = (
            datetime.fromisoformat(instant.replace("Z", "+00:00"))
            - datetime.fromisoformat(snapshot.last_access_at.replace("Z", "+00:00"))
        ).total_seconds()
        state = "fresh" if recency <= config.freshness_seconds else "stale"
    return AccessFeatures(bucket, key, instant, snapshot, config, state, recency)
