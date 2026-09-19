from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID, uuid4

import yaml

from cognistore.core.placement_controls import MovementConstraints
from cognistore.core.policy import EmbeddingPolicyRule

from .handlers import (
    CATALOG_SCAN_JOB,
    POLICY_RUN_JOB,
    policy_job_payload,
    policy_job_schema_version,
)
from .models import (
    ATTEMPT_OFFSET_METADATA,
    DEAD_LETTER_CHAIN_METADATA,
    JOB_SCHEMA_VERSION_V1,
    REDRIVE_COUNT_METADATA,
    REDRIVEN_FROM_METADATA,
    DeadLetterDisposition,
    InvalidJobError,
    JobContext,
    JobEnvelope,
    JobEnvelopeError,
    JSONValue,
)

LOGGER = logging.getLogger(__name__)

SCHEDULE_ID_METADATA = "cognistore.schedule_id"
SCHEDULE_SCOPE_METADATA = "cognistore.schedule_scope"
SCHEDULED_FOR_METADATA = "cognistore.scheduled_for"

_TOP_LEVEL_FIELDS = frozenset({"jobs"})
_JOB_FIELDS = frozenset({"type", "enabled", "interval_seconds", "payload"})
_CATALOG_SCAN_FIELDS = frozenset({"tier", "bucket", "prefix"})
_POLICY_RUN_FIELDS = frozenset(
    {
        "bucket",
        "prefix",
        "policy",
        "threshold",
        "llm_threshold",
        "allowed_tiers",
        "hot_name_patterns",
        "warm_name_patterns",
        "cold_name_patterns",
        "hot_mime_prefixes",
        "warm_mime_prefixes",
        "cold_mime_prefixes",
        "embedding_rules",
        "movement_constraints",
    }
)
_TERMINAL_RUN_STATES = frozenset(
    {"succeeded", "dead_lettered", "publication_failed"}
)
_ACTIVE_RUN_STATES = frozenset({"reserved", "enqueued", "running", "retry_wait"})
_ALL_RUN_STATES = _TERMINAL_RUN_STATES | _ACTIVE_RUN_STATES
SCHEDULED_RUN_STATES = tuple(sorted(_ALL_RUN_STATES))
_REDRIVE_METADATA = frozenset(
    {
        ATTEMPT_OFFSET_METADATA,
        DEAD_LETTER_CHAIN_METADATA,
        REDRIVE_COUNT_METADATA,
        REDRIVEN_FROM_METADATA,
    }
)
_MAX_DURATION = timedelta(days=36_525)

PayloadNormalizer = Callable[[Mapping[str, Any], frozenset[str] | None], Mapping[str, JSONValue]]
ScopeFactory = Callable[[Mapping[str, JSONValue]], Sequence[JSONValue] | JSONValue]
Clock = Callable[[], datetime]


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects silently shadowed configuration keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate mapping key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class ScheduledRunLockedError(RuntimeError):
    """Raised when another worker delivery durably owns a running schedule."""

    retryable = True
    defer_without_exhaustion = True


class ScheduledRunLeaseLostError(RuntimeError):
    """Raised when a stale worker attempts to mutate a successor's lease."""

    retryable = True


class ScheduledRunStateUnavailableError(InvalidJobError):
    """Fail a worker closed when a scheduled run's durable state is unavailable."""

    fail_worker = True


class ScheduledRunRecoveryError(RuntimeError):
    """Raised when an operator recovery request cannot be applied safely."""


class JobPublisher(Protocol):
    async def connect(self) -> None: ...

    async def enqueue(self, job: JobEnvelope, *, message_id: str | None = None) -> Any: ...

    async def close(self, *, graceful: bool = True) -> None: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime, field_name: str = "clock") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return (
        _aware_utc(value)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _aware_utc(parsed, "scheduler timestamp")
    except (AttributeError, ValueError) as exc:
        raise RuntimeError(f"invalid scheduler timestamp: {value!r}") from exc


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} keys must be strings")
    return value


def _nonempty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _header_string(value: object, field_name: str) -> str:
    text = _nonempty_string(value, field_name)
    if "\r" in text or "\n" in text:
        raise ValueError(f"{field_name} must not contain CR or LF")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8") from exc
    return text


def _canonical_uuid(value: object, field_name: str) -> str:
    text = _header_string(value, field_name)
    try:
        parsed = UUID(text)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc
    if str(parsed) != text.lower():
        raise ValueError(f"{field_name} must use canonical UUID format")
    return str(parsed)


def _audit_text(value: object, field_name: str, *, maximum: int) -> str:
    text = _header_string(value, field_name).strip()
    if len(text) > maximum:
        raise ValueError(f"{field_name} must not exceed {maximum} characters")
    return text


def _positive_duration_seconds(value: object, field_name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive finite number")
    try:
        seconds = float(value)
    except OverflowError as exc:
        raise ValueError(f"{field_name} is too large") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{field_name} must be a positive finite number")
    try:
        duration = timedelta(seconds=seconds)
    except OverflowError as exc:
        raise ValueError(f"{field_name} is too large") from exc
    if seconds < 0.000001 or duration <= timedelta(0):
        raise ValueError(f"{field_name} must be at least one microsecond")
    if duration.total_seconds() != seconds:
        raise ValueError(f"{field_name} must use whole-microsecond precision")
    if duration > _MAX_DURATION:
        raise ValueError(f"{field_name} must not exceed 100 years")
    return seconds


def _string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _integer(value: object, field_name: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        qualification = "an integer or null" if optional else "an integer"
        raise ValueError(f"{field_name} must be {qualification}")
    if value < 0:
        raise ValueError(f"{field_name} cannot be negative")
    return value


def _string_list(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return tuple(value)


def _reject_unknown(value: Mapping[str, Any], allowed: frozenset[str], field_name: str) -> None:
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise ValueError(f"{field_name} has unsupported fields: {', '.join(unknown)}")


def _canonical_json(value: object, field_name: str) -> str:
    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        rendered.encode("utf-8")
        return rendered
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
        raise ValueError(f"{field_name} must contain only finite JSON values") from exc


def _normalize_scan_payload(
    raw_payload: Mapping[str, Any], known_tiers: frozenset[str] | None
) -> Mapping[str, JSONValue]:
    payload = dict(raw_payload)
    _reject_unknown(payload, _CATALOG_SCAN_FIELDS, "catalog.scan payload")
    tier = _nonempty_string(payload.get("tier"), "catalog.scan payload.tier")
    bucket = _nonempty_string(payload.get("bucket"), "catalog.scan payload.bucket")
    prefix = _string(payload.get("prefix", ""), "catalog.scan payload.prefix")
    if known_tiers is not None and tier not in known_tiers:
        raise ValueError(f"catalog.scan payload contains unknown tier: {tier}")
    return {"tier": tier, "bucket": bucket, "prefix": prefix}


def _normalize_policy_payload(
    raw_payload: Mapping[str, Any], known_tiers: frozenset[str] | None
) -> Mapping[str, JSONValue]:
    payload = dict(raw_payload)
    _reject_unknown(payload, _POLICY_RUN_FIELDS, "policy.run payload")
    bucket = _nonempty_string(payload.get("bucket"), "policy.run payload.bucket")
    prefix = _string(payload.get("prefix", ""), "policy.run payload.prefix")
    policy = _nonempty_string(payload.get("policy"), "policy.run payload.policy")
    if policy not in {"simple", "llm", "content"}:
        raise ValueError(f"unknown policy: {policy}")
    threshold = _integer(payload.get("threshold"), "policy.run payload.threshold")
    llm_threshold = _integer(
        payload.get("llm_threshold"),
        "policy.run payload.llm_threshold",
        optional=True,
    )
    allowed_tiers = _string_list(payload.get("allowed_tiers"), "policy.run payload.allowed_tiers")
    if not allowed_tiers:
        raise ValueError("policy.run payload.allowed_tiers cannot be empty")
    if any(not tier.strip() for tier in allowed_tiers):
        raise ValueError("policy.run payload.allowed_tiers must contain non-empty names")
    if len(set(allowed_tiers)) != len(allowed_tiers):
        raise ValueError("policy.run payload.allowed_tiers cannot contain duplicates")
    if known_tiers is not None:
        unknown = sorted(set(allowed_tiers).difference(known_tiers))
        if unknown:
            raise ValueError(
                "policy.run payload contains unknown allowed tier(s): " + ", ".join(unknown)
            )
    raw_embedding_rules = payload.get("embedding_rules", [])
    if not isinstance(raw_embedding_rules, list) or len(raw_embedding_rules) > 100:
        raise ValueError(
            "policy.run payload.embedding_rules must be a list of at most 100 rules"
        )
    embedding_rules: list[EmbeddingPolicyRule] = []
    for index, raw_rule in enumerate(raw_embedding_rules):
        if not isinstance(raw_rule, Mapping):
            raise ValueError(
                f"policy.run payload.embedding_rules item {index} must be an object"
            )
        try:
            rule = EmbeddingPolicyRule.from_mapping(raw_rule)
        except ValueError as exc:
            raise ValueError(
                f"invalid policy.run payload.embedding_rules item {index}: {exc}"
            ) from exc
        embedding_rules.append(rule)
    if len({rule.name for rule in embedding_rules}) != len(embedding_rules):
        raise ValueError(
            "policy.run payload.embedding_rules cannot contain duplicate rule names"
        )
    if embedding_rules and policy != "content":
        raise ValueError(
            "policy.run payload.embedding_rules require the content policy"
        )
    unknown_rule_tiers = sorted(
        {
            rule.destination_tier
            for rule in embedding_rules
            if rule.destination_tier not in allowed_tiers
        }
    )
    if unknown_rule_tiers:
        raise ValueError(
            "policy.run payload.embedding_rules contain disallowed destination tier(s): "
            + ", ".join(unknown_rule_tiers)
        )
    movement_constraints = None
    if "movement_constraints" in payload:
        raw_constraints = _mapping(
            payload["movement_constraints"], "policy.run payload.movement_constraints"
        )
        try:
            movement_constraints = MovementConstraints.from_mapping(raw_constraints)
        except ValueError as exc:
            raise ValueError(
                f"invalid policy.run payload.movement_constraints: {exc}"
            ) from exc
        if known_tiers is not None:
            unknown_residency = sorted(
                set(movement_constraints.minimum_residency_seconds).difference(known_tiers)
            )
            if unknown_residency:
                raise ValueError(
                    "policy.run payload.movement_constraints contains unknown residency tier(s): "
                    + ", ".join(unknown_residency)
                )
    assert threshold is not None
    return policy_job_payload(
        bucket=bucket,
        prefix=prefix,
        policy=policy,
        threshold=threshold,
        llm_threshold=llm_threshold,
        allowed_tiers=allowed_tiers,
        hot_name_patterns=_string_list(
            payload.get("hot_name_patterns"), "policy.run payload.hot_name_patterns"
        ),
        warm_name_patterns=_string_list(
            payload.get("warm_name_patterns"), "policy.run payload.warm_name_patterns"
        ),
        cold_name_patterns=_string_list(
            payload.get("cold_name_patterns"), "policy.run payload.cold_name_patterns"
        ),
        hot_mime_prefixes=_string_list(
            payload.get("hot_mime_prefixes"), "policy.run payload.hot_mime_prefixes"
        ),
        warm_mime_prefixes=_string_list(
            payload.get("warm_mime_prefixes"), "policy.run payload.warm_mime_prefixes"
        ),
        cold_mime_prefixes=_string_list(
            payload.get("cold_mime_prefixes"), "policy.run payload.cold_mime_prefixes"
        ),
        embedding_rules=embedding_rules,
        movement_constraints=movement_constraints,
    )


@dataclass(frozen=True)
class ScheduledJobDefinition:
    """Pluggable validation and scope derivation for one queued job type."""

    job_type: str
    normalize_payload: PayloadNormalizer
    scope_for: ScopeFactory

    def __post_init__(self) -> None:
        _header_string(self.job_type, "job_type")
        if not callable(self.normalize_payload) or not callable(self.scope_for):
            raise ValueError("schedule job definition callbacks must be callable")


class ScheduleRegistry:
    """Registry used to add future recurring job types without scheduler branching."""

    def __init__(self) -> None:
        self._definitions: dict[str, ScheduledJobDefinition] = {}

    def register(self, definition: ScheduledJobDefinition) -> None:
        if definition.job_type in self._definitions:
            raise ValueError(
                f"schedule definition is already registered for {definition.job_type!r}"
            )
        self._definitions[definition.job_type] = definition

    def resolve(self, job_type: str) -> ScheduledJobDefinition:
        try:
            return self._definitions[job_type]
        except KeyError as exc:
            raise ValueError(f"unsupported scheduled job type: {job_type}") from exc


def default_schedule_registry() -> ScheduleRegistry:
    registry = ScheduleRegistry()
    registry.register(
        ScheduledJobDefinition(
            CATALOG_SCAN_JOB,
            _normalize_scan_payload,
            lambda payload: (
                CATALOG_SCAN_JOB,
                payload["tier"],
                payload["bucket"],
                payload["prefix"],
            ),
        )
    )
    registry.register(
        ScheduledJobDefinition(
            POLICY_RUN_JOB,
            _normalize_policy_payload,
            lambda payload: (
                POLICY_RUN_JOB,
                payload["bucket"],
                payload["prefix"],
            ),
        )
    )
    return registry


@dataclass(frozen=True)
class ScheduledJob:
    schedule_id: str
    job_type: str
    interval_seconds: float
    enabled: bool
    payload: Mapping[str, JSONValue]
    scope: str

    def __post_init__(self) -> None:
        _header_string(self.schedule_id, "schedule_id")
        _header_string(self.job_type, "job_type")
        interval_seconds = _positive_duration_seconds(
            self.interval_seconds, "interval_seconds"
        )
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        payload_json = _canonical_json(dict(self.payload), "payload")
        normalized = json.loads(payload_json)
        if not isinstance(normalized, dict):
            raise ValueError("payload must be a mapping")
        object.__setattr__(self, "interval_seconds", interval_seconds)
        object.__setattr__(self, "payload", MappingProxyType(normalized))
        _header_string(self.scope, "scope")

    @property
    def payload_json(self) -> str:
        return _canonical_json(dict(self.payload), "payload")

    @property
    def config_digest(self) -> str:
        value = _canonical_json(
            {
                "job_type": self.job_type,
                "payload": dict(self.payload),
                "interval_seconds": self.interval_seconds,
            },
            "schedule",
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_schedule_config(
    config_path: str | Path,
    *,
    registry: ScheduleRegistry | None = None,
    known_tiers: Iterable[str] | None = None,
) -> tuple[ScheduledJob, ...]:
    """Load strict periodic job definitions from a standalone YAML document."""

    path = Path(config_path)
    try:
        with path.open("r", encoding="utf-8") as stream:
            loader = _UniqueKeyLoader(stream)
            try:
                loaded = loader.get_single_data()
            finally:
                loader.dispose()
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid scheduler YAML in {path}: {exc}") from exc
    if loaded is None:
        loaded = {}
    document = dict(_mapping(loaded, "scheduler configuration"))
    _reject_unknown(document, _TOP_LEVEL_FIELDS, "scheduler configuration")
    if "jobs" not in document:
        raise ValueError("scheduler configuration.jobs is required")
    raw_jobs = _mapping(document["jobs"], "scheduler configuration.jobs")
    active_registry = registry or default_schedule_registry()
    tiers = None if known_tiers is None else frozenset(known_tiers)
    if tiers is not None and any(not isinstance(tier, str) or not tier.strip() for tier in tiers):
        raise ValueError("known tier names must be non-empty strings")

    schedules: list[ScheduledJob] = []
    seen_scopes: dict[str, str] = {}
    for raw_schedule_id, raw_job in raw_jobs.items():
        schedule_id = _header_string(raw_schedule_id, "schedule id")
        job = dict(_mapping(raw_job, f"jobs.{schedule_id}"))
        _reject_unknown(job, _JOB_FIELDS, f"jobs.{schedule_id}")
        job_type = _header_string(job.get("type"), f"jobs.{schedule_id}.type")
        enabled = job.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError(f"jobs.{schedule_id}.enabled must be a boolean")
        interval = _positive_duration_seconds(
            job.get("interval_seconds"), f"jobs.{schedule_id}.interval_seconds"
        )
        definition = active_registry.resolve(job_type)
        raw_payload = _mapping(job.get("payload"), f"jobs.{schedule_id}.payload")
        payload = definition.normalize_payload(raw_payload, tiers)
        scope_identity = definition.scope_for(payload)
        scope_json = _canonical_json(scope_identity, f"jobs.{schedule_id} scope")
        scope = hashlib.sha256(scope_json.encode("utf-8")).hexdigest()
        conflicting = seen_scopes.get(scope)
        if conflicting is not None:
            raise ValueError(
                f"jobs.{schedule_id} and jobs.{conflicting} target the same execution scope"
            )
        seen_scopes[scope] = schedule_id
        schedules.append(
            ScheduledJob(
                schedule_id=schedule_id,
                job_type=job_type,
                interval_seconds=interval,
                enabled=enabled,
                payload=payload,
                scope=scope,
            )
        )
    return tuple(schedules)


@dataclass(frozen=True)
class ScheduledRun:
    job: JobEnvelope
    schedule_id: str
    scope: str
    scheduled_for: datetime


@dataclass(frozen=True)
class ScheduledRunRecord:
    """Operator-facing durable state for one scheduled occurrence."""

    job_id: str
    run_sequence: int
    schedule_id: str
    scope: str
    state: str
    scheduled_for: datetime
    published_at: datetime | None
    execution_owner: str | None
    execution_lease_expires_at: datetime | None
    execution_generation: int
    completed_at: datetime | None
    outcome: str | None
    created_at: datetime
    updated_at: datetime
    stale: bool


@dataclass(frozen=True)
class ScheduledRunRecovery:
    """Immutable audit record for one fenced stale-run recovery."""

    recovery_id: str
    job_id: str
    schedule_id: str
    scope: str
    execution_generation: int
    prior_execution_owner: str
    prior_lease_expires_at: datetime
    operator: str
    reason: str
    fence_evidence: str
    recovered_at: datetime


@dataclass(frozen=True)
class _ExecutionClaim:
    execute: bool
    owner_id: str | None


class SQLiteScheduleStore:
    """Durable timing, publication, and execution leases for recurring jobs."""

    def __init__(self, db_path: str | Path, *, read_only: bool = False) -> None:
        self.db_path = str(db_path)
        self.read_only = read_only
        self._lock = threading.RLock()
        if read_only:
            if self.db_path == ":memory:":
                raise ValueError("an in-memory schedule store cannot be opened read-only")
            uri = f"{Path(self.db_path).expanduser().resolve().as_uri()}?mode=ro"
            self._conn = sqlite3.connect(
                uri,
                uri=True,
                check_same_thread=False,
                timeout=5.0,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._conn.execute("PRAGMA query_only = ON")
            return
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS scheduled_jobs (
                scope TEXT PRIMARY KEY,
                schedule_id TEXT NOT NULL,
                job_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                config_digest TEXT NOT NULL,
                interval_seconds REAL NOT NULL,
                enabled INTEGER NOT NULL,
                next_run_at TEXT NOT NULL,
                next_run_immediate INTEGER NOT NULL DEFAULT 1,
                active_job_id TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS scheduled_jobs_id_idx
                ON scheduled_jobs(schedule_id);
            CREATE INDEX IF NOT EXISTS scheduled_jobs_due_idx
                ON scheduled_jobs(enabled, next_run_at);
            CREATE TABLE IF NOT EXISTS scheduled_runs (
                job_id TEXT PRIMARY KEY,
                run_sequence INTEGER NOT NULL,
                scope TEXT NOT NULL,
                schedule_id TEXT NOT NULL,
                scheduled_for TEXT NOT NULL,
                envelope BLOB NOT NULL,
                state TEXT NOT NULL,
                publish_owner TEXT,
                publish_lease_expires_at TEXT,
                published_at TEXT,
                execution_owner TEXT,
                execution_lease_expires_at TEXT,
                execution_generation INTEGER NOT NULL DEFAULT 0,
                completed_at TEXT,
                outcome TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS scheduled_runs_publish_idx
                ON scheduled_runs(state, publish_lease_expires_at);
            CREATE INDEX IF NOT EXISTS scheduled_runs_scope_idx
                ON scheduled_runs(scope, created_at);
            CREATE INDEX IF NOT EXISTS scheduled_runs_execution_idx
                ON scheduled_runs(state, execution_lease_expires_at);
            CREATE TABLE IF NOT EXISTS scheduled_run_recoveries (
                recovery_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                schedule_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                execution_generation INTEGER NOT NULL,
                prior_execution_owner TEXT NOT NULL,
                prior_lease_expires_at TEXT NOT NULL,
                operator TEXT NOT NULL,
                reason TEXT NOT NULL,
                fence_evidence TEXT NOT NULL,
                recovered_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES scheduled_runs(job_id)
            );
            CREATE INDEX IF NOT EXISTS scheduled_run_recoveries_job_idx
                ON scheduled_run_recoveries(job_id, recovered_at);
            """
        )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            run_columns = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(scheduled_runs)")
            }
            if "execution_generation" not in run_columns:
                self._conn.execute(
                    "ALTER TABLE scheduled_runs ADD COLUMN "
                    "execution_generation INTEGER NOT NULL DEFAULT 0"
                )
            if "run_sequence" not in run_columns:
                self._conn.execute(
                    "ALTER TABLE scheduled_runs ADD COLUMN run_sequence INTEGER"
                )
            self._conn.execute(
                "UPDATE scheduled_runs SET run_sequence=rowid "
                "WHERE run_sequence IS NULL"
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS scheduled_runs_sequence_idx "
                "ON scheduled_runs(run_sequence)"
            )
            job_columns = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(scheduled_jobs)")
            }
            if "next_run_immediate" not in job_columns:
                self._conn.execute(
                    "ALTER TABLE scheduled_jobs ADD COLUMN "
                    "next_run_immediate INTEGER NOT NULL DEFAULT 0"
                )
                self._conn.execute(
                    """
                    UPDATE scheduled_jobs
                    SET next_run_immediate=1
                    WHERE active_job_id IS NULL
                      AND NOT EXISTS(
                        SELECT 1 FROM scheduled_runs
                        WHERE scheduled_runs.scope=scheduled_jobs.scope
                      )
                    """
                )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def validate_recovery_request(
        recovery_id: str,
        operator: str,
        reason: str,
        fence_evidence: str,
    ) -> tuple[str, str, str, str]:
        """Normalize operator evidence without opening a write transaction."""

        return (
            _canonical_uuid(recovery_id, "recovery_id"),
            _audit_text(operator, "operator", maximum=256),
            _audit_text(reason, "reason", maximum=2048),
            _audit_text(fence_evidence, "fence_evidence", maximum=4096),
        )

    def get_run(
        self,
        job_id: str,
        *,
        now: datetime,
    ) -> ScheduledRunRecord | None:
        current = _aware_utc(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scheduled_runs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return None if row is None else self._record_from_row(row, current)

    def list_runs(
        self,
        *,
        now: datetime,
        states: Iterable[str] | None = None,
        schedule_id: str | None = None,
        stale_only: bool = False,
    ) -> tuple[ScheduledRunRecord, ...]:
        current = _aware_utc(now)
        requested_states = None if states is None else frozenset(states)
        if requested_states is not None:
            unknown = sorted(requested_states.difference(_ALL_RUN_STATES))
            if unknown:
                raise ValueError(
                    "unsupported scheduled run state(s): " + ", ".join(unknown)
                )
        if schedule_id is not None:
            schedule_id = _header_string(schedule_id, "schedule_id")

        # Keep the statement text fixed and bind every operator filter. Besides
        # avoiding dynamic SQL, padding to the finite state vocabulary keeps an
        # empty/repeated ``--state`` selection equivalent to no state filter.
        state_values = sorted(requested_states or _ALL_RUN_STATES)
        state_values.extend("" for _ in range(len(_ALL_RUN_STATES) - len(state_values)))
        values: tuple[object, ...] = (
            *state_values,
            schedule_id,
            schedule_id,
            int(stale_only),
            _timestamp(current),
        )
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM scheduled_runs
                WHERE state IN (?,?,?,?,?,?,?)
                  AND (? IS NULL OR schedule_id=?)
                  AND (
                    ?=0 OR (
                      state='running'
                      AND execution_owner IS NOT NULL
                      AND execution_lease_expires_at IS NOT NULL
                      AND execution_lease_expires_at<=?
                    )
                  )
                ORDER BY run_sequence
                """,
                values,
            ).fetchall()
        return tuple(self._record_from_row(row, current) for row in rows)

    def list_recoveries(self, job_id: str) -> tuple[ScheduledRunRecovery, ...]:
        with self._lock:
            table = self._conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='scheduled_run_recoveries'
                """
            ).fetchone()
            if table is None:
                return ()
            rows = self._conn.execute(
                """
                SELECT * FROM scheduled_run_recoveries
                WHERE job_id=?
                ORDER BY recovered_at, recovery_id
                """,
                (job_id,),
            ).fetchall()
        return tuple(self._recovery_from_row(row) for row in rows)

    def inspect_recovery(
        self,
        job_id: str,
        *,
        expected_owner: str,
        former_worker_fenced: bool,
        now: datetime,
    ) -> ScheduledRunRecord:
        """Validate a recovery candidate without changing durable state."""

        current = _aware_utc(now)
        expected = _header_string(expected_owner, "expected_owner")
        if former_worker_fenced is not True:
            raise ScheduledRunRecoveryError(
                "recovery requires explicit confirmation that the former worker is fenced"
            )
        with self._lock:
            row = self._recovery_candidate(job_id, expected, current)
        return self._record_from_row(row, current)

    def recover_stale_run(
        self,
        job_id: str,
        *,
        recovery_id: str,
        expected_owner: str,
        operator: str,
        reason: str,
        fence_evidence: str,
        former_worker_fenced: bool,
        now: datetime,
    ) -> ScheduledRunRecovery:
        """Release one explicitly fenced stale owner and retain immutable evidence."""

        identifier, actor, recovery_reason, evidence = self.validate_recovery_request(
            recovery_id,
            operator,
            reason,
            fence_evidence,
        )
        expected = _header_string(expected_owner, "expected_owner")
        current = _aware_utc(now)
        recovered_at = _timestamp(current)
        if former_worker_fenced is not True:
            raise ScheduledRunRecoveryError(
                "recovery requires explicit confirmation that the former worker is fenced"
            )

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                prior = self._conn.execute(
                    "SELECT * FROM scheduled_run_recoveries WHERE recovery_id=?",
                    (identifier,),
                ).fetchone()
                if prior is not None:
                    recovery = self._recovery_from_row(prior)
                    if (
                        recovery.job_id != job_id
                        or recovery.prior_execution_owner != expected
                        or recovery.operator != actor
                        or recovery.reason != recovery_reason
                        or recovery.fence_evidence != evidence
                    ):
                        raise ScheduledRunRecoveryError(
                            "recovery_id is already assigned to a different request"
                        )
                    self._conn.commit()
                    return recovery

                row = self._recovery_candidate(job_id, expected, current)
                generation = int(row["execution_generation"])
                prior_expiry = row["execution_lease_expires_at"]
                assert isinstance(prior_expiry, str)
                self._conn.execute(
                    """
                    INSERT INTO scheduled_run_recoveries(
                        recovery_id, job_id, schedule_id, scope,
                        execution_generation, prior_execution_owner,
                        prior_lease_expires_at, operator, reason,
                        fence_evidence, recovered_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        identifier,
                        job_id,
                        row["schedule_id"],
                        row["scope"],
                        generation,
                        expected,
                        prior_expiry,
                        actor,
                        recovery_reason,
                        evidence,
                        recovered_at,
                    ),
                )
                updated = self._conn.execute(
                    """
                    UPDATE scheduled_runs
                    SET state='retry_wait', execution_owner=NULL,
                        execution_lease_expires_at=NULL, updated_at=?
                    WHERE job_id=? AND state='running'
                      AND execution_owner=? AND execution_generation=?
                    """,
                    (recovered_at, job_id, expected, generation),
                )
                if updated.rowcount != 1:
                    raise ScheduledRunRecoveryError(
                        "scheduled run changed while fenced recovery was being applied"
                    )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

        LOGGER.info(
            "recovered fenced stale scheduled run",
            extra={"job_id": job_id, "recovery_id": identifier},
        )
        return ScheduledRunRecovery(
            recovery_id=identifier,
            job_id=job_id,
            schedule_id=str(row["schedule_id"]),
            scope=str(row["scope"]),
            execution_generation=generation,
            prior_execution_owner=expected,
            prior_lease_expires_at=_parse_timestamp(prior_expiry),
            operator=actor,
            reason=recovery_reason,
            fence_evidence=evidence,
            recovered_at=current,
        )

    def _recovery_candidate(
        self,
        job_id: str,
        expected_owner: str,
        now: datetime,
    ) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM scheduled_runs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ScheduledRunRecoveryError(f"scheduled run not found: {job_id}")
        if row["state"] != "running":
            raise ScheduledRunRecoveryError(
                f"scheduled run is not running: state={row['state']}"
            )
        if row["execution_owner"] != expected_owner:
            raise ScheduledRunRecoveryError(
                "scheduled run owner changed; inspect it again before recovery"
            )
        lease = row["execution_lease_expires_at"]
        if lease is None:
            raise ScheduledRunRecoveryError(
                "running scheduled run is missing its execution lease"
            )
        if _parse_timestamp(lease) > now:
            raise ScheduledRunRecoveryError(
                "scheduled run lease has not expired; fence and inspect the owner again"
            )
        active = self._conn.execute(
            "SELECT active_job_id FROM scheduled_jobs WHERE scope=?",
            (row["scope"],),
        ).fetchone()
        if active is None or active["active_job_id"] != job_id:
            raise ScheduledRunRecoveryError(
                "scheduled run no longer owns its durable execution scope"
            )
        return row

    def sync(self, schedules: Sequence[ScheduledJob], now: datetime) -> None:
        current = _aware_utc(now)
        timestamp = _timestamp(current)
        configured_scopes = {schedule.scope for schedule in schedules}
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing_rows = {
                    row["scope"]: row for row in self._conn.execute("SELECT * FROM scheduled_jobs")
                }
                for scope, row in existing_rows.items():
                    if scope not in configured_scopes and bool(row["enabled"]):
                        self._conn.execute(
                            "UPDATE scheduled_jobs SET enabled=0, updated_at=? WHERE scope=?",
                            (timestamp, scope),
                        )

                for schedule in schedules:
                    row = existing_rows.get(schedule.scope)
                    if row is None:
                        self._conn.execute(
                            """
                            INSERT INTO scheduled_jobs(
                                scope, schedule_id, job_type, payload, config_digest,
                                interval_seconds, enabled, next_run_at,
                                next_run_immediate, active_job_id, updated_at
                            ) VALUES(?,?,?,?,?,?,?,?,1,NULL,?)
                            """,
                            (
                                schedule.scope,
                                schedule.schedule_id,
                                schedule.job_type,
                                schedule.payload_json,
                                schedule.config_digest,
                                schedule.interval_seconds,
                                int(schedule.enabled),
                                timestamp,
                                timestamp,
                            ),
                        )
                        continue

                    next_run_at = row["next_run_at"]
                    next_run_immediate = bool(row["next_run_immediate"])
                    if schedule.enabled and not bool(row["enabled"]):
                        # Enabling is explicit operator intent; run once immediately
                        # rather than replaying every interval missed while disabled.
                        next_run_at = timestamp
                        next_run_immediate = True
                    elif (
                        schedule.enabled
                        and bool(row["enabled"])
                        and schedule.interval_seconds
                        != float(row["interval_seconds"])
                    ):
                        prior_run = self._conn.execute(
                            "SELECT 1 FROM scheduled_runs WHERE scope=? LIMIT 1",
                            (schedule.scope,),
                        ).fetchone()
                        if next_run_immediate:
                            next_run_at = row["next_run_at"]
                        elif prior_run is None:
                            next_run_at = timestamp
                            next_run_immediate = True
                        else:
                            # The stored deadline is the last reservation plus
                            # the old interval. Retain that anchor so a changed
                            # interval takes effect as soon as config reloads.
                            last_reserved_at = _parse_timestamp(
                                row["next_run_at"]
                            ) - timedelta(
                                seconds=float(row["interval_seconds"])
                            )
                            next_run_at = _timestamp(
                                last_reserved_at
                                + timedelta(seconds=schedule.interval_seconds)
                            )
                    self._conn.execute(
                        """
                        UPDATE scheduled_jobs
                        SET schedule_id=?, job_type=?, payload=?, config_digest=?,
                            interval_seconds=?, enabled=?, next_run_at=?,
                            next_run_immediate=?, updated_at=?
                        WHERE scope=?
                        """,
                        (
                            schedule.schedule_id,
                            schedule.job_type,
                            schedule.payload_json,
                            schedule.config_digest,
                            schedule.interval_seconds,
                            int(schedule.enabled),
                            next_run_at,
                            int(next_run_immediate),
                            timestamp,
                            schedule.scope,
                        ),
                    )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def reserve_due(
        self,
        schedule: ScheduledJob,
        now: datetime,
        *,
        publisher_id: str,
        publication_lease_seconds: float,
    ) -> ScheduledRun | None:
        current = _aware_utc(now)
        now_text = _timestamp(current)
        lease_expires = _timestamp(current + timedelta(seconds=publication_lease_seconds))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM scheduled_jobs WHERE scope=?", (schedule.scope,)
                ).fetchone()
                if (
                    row is None
                    or not bool(row["enabled"])
                    or row["config_digest"] != schedule.config_digest
                    or row["active_job_id"] is not None
                    or _parse_timestamp(row["next_run_at"]) > current
                ):
                    self._conn.commit()
                    return None

                scheduled_for = _parse_timestamp(row["next_run_at"])
                job = JobEnvelope.create(
                    schedule.job_type,
                    schedule.payload,
                    correlation_id=f"schedule:{schedule.schedule_id}",
                    metadata={
                        SCHEDULE_ID_METADATA: schedule.schedule_id,
                        SCHEDULE_SCOPE_METADATA: schedule.scope,
                        SCHEDULED_FOR_METADATA: _timestamp(scheduled_for),
                    },
                    created_at=current,
                    schema_version=(
                        policy_job_schema_version(schedule.payload)
                        if schedule.job_type == POLICY_RUN_JOB
                        else JOB_SCHEMA_VERSION_V1
                    ),
                )
                next_run = _timestamp(current + timedelta(seconds=schedule.interval_seconds))
                run_sequence = self._conn.execute(
                    "SELECT COALESCE(MAX(run_sequence), 0) + 1 FROM scheduled_runs"
                ).fetchone()[0]
                self._conn.execute(
                    """
                    INSERT INTO scheduled_runs(
                        job_id, run_sequence, scope, schedule_id, scheduled_for,
                        envelope, state,
                        publish_owner, publish_lease_expires_at, published_at,
                        execution_owner, execution_lease_expires_at,
                        execution_generation, completed_at, outcome, created_at,
                        updated_at
                    ) VALUES(?,?,?,?,?,?,'reserved',?,?,NULL,NULL,NULL,0,NULL,NULL,?,?)
                    """,
                    (
                        job.job_id,
                        run_sequence,
                        schedule.scope,
                        schedule.schedule_id,
                        _timestamp(scheduled_for),
                        sqlite3.Binary(job.to_bytes()),
                        publisher_id,
                        lease_expires,
                        now_text,
                        now_text,
                    ),
                )
                updated = self._conn.execute(
                    """
                    UPDATE scheduled_jobs
                    SET active_job_id=?, next_run_at=?, next_run_immediate=0,
                        updated_at=?
                    WHERE scope=? AND active_job_id IS NULL
                    """,
                    (job.job_id, next_run, now_text, schedule.scope),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("scheduled job reservation lost its scope lock")
                self._conn.commit()
                return ScheduledRun(job, schedule.schedule_id, schedule.scope, scheduled_for)
            except BaseException:
                self._conn.rollback()
                raise

    def claim_pending_publications(
        self,
        now: datetime,
        *,
        publisher_id: str,
        publication_lease_seconds: float,
    ) -> tuple[ScheduledRun, ...]:
        current = _aware_utc(now)
        now_text = _timestamp(current)
        lease_expires = _timestamp(current + timedelta(seconds=publication_lease_seconds))
        claimed: list[ScheduledRun] = []
        corrupt: list[tuple[str, Exception]] = []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    """
                    SELECT * FROM scheduled_runs
                    WHERE state='reserved'
                      AND (
                        publish_owner IS NULL OR publish_lease_expires_at IS NULL
                        OR publish_lease_expires_at <= ?
                      )
                    ORDER BY created_at, job_id
                    """,
                    (now_text,),
                ).fetchall()
                for row in rows:
                    try:
                        run = self._run_from_row(row)
                    except Exception as exc:
                        outcome = f"invalid durable envelope: {type(exc).__name__}: {exc}"
                        self._conn.execute(
                            """
                            UPDATE scheduled_runs
                            SET state='publication_failed', outcome=?, completed_at=?,
                                publish_owner=NULL, publish_lease_expires_at=NULL,
                                updated_at=?
                            WHERE job_id=? AND state='reserved'
                            """,
                            (outcome, now_text, now_text, row["job_id"]),
                        )
                        self._conn.execute(
                            """
                            UPDATE scheduled_jobs
                            SET active_job_id=NULL, updated_at=?
                            WHERE scope=? AND active_job_id=?
                            """,
                            (now_text, row["scope"], row["job_id"]),
                        )
                        corrupt.append((row["job_id"], exc))
                        continue
                    self._conn.execute(
                        """
                        UPDATE scheduled_runs
                        SET publish_owner=?, publish_lease_expires_at=?, updated_at=?
                        WHERE job_id=? AND state='reserved'
                        """,
                        (publisher_id, lease_expires, now_text, row["job_id"]),
                    )
                    claimed.append(run)
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        for job_id, error in corrupt:
            LOGGER.error(
                "scheduled publication has invalid durable state job_id=%s",
                job_id,
            )
        return tuple(claimed)

    def mark_enqueued(self, job_id: str, *, publisher_id: str, now: datetime) -> None:
        timestamp = _timestamp(now)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT state, publish_owner FROM scheduled_runs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise ScheduledRunStateUnavailableError(
                        f"scheduled publication {job_id!r} is missing durable state"
                    )
                if row["state"] in _TERMINAL_RUN_STATES:
                    self._conn.commit()
                    return
                if row["publish_owner"] != publisher_id:
                    raise ScheduledRunLeaseLostError(
                        f"publication lease for scheduled run {job_id!r} was lost"
                    )
                self._conn.execute(
                    """
                    UPDATE scheduled_runs
                    SET state=CASE WHEN state='reserved' THEN 'enqueued' ELSE state END,
                        published_at=COALESCE(published_at, ?), publish_owner=NULL,
                        publish_lease_expires_at=NULL, updated_at=?
                    WHERE job_id=?
                    """,
                    (timestamp, timestamp, job_id),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def release_publication(self, job_id: str, *, publisher_id: str) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    UPDATE scheduled_runs
                    SET publish_owner=NULL, publish_lease_expires_at=NULL
                    WHERE job_id=? AND state='reserved' AND publish_owner=?
                    """,
                    (job_id, publisher_id),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def fail_publication(
        self,
        job_id: str,
        *,
        publisher_id: str,
        outcome: str,
        now: datetime,
    ) -> None:
        timestamp = _timestamp(now)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT scope, state, publish_owner FROM scheduled_runs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise ScheduledRunStateUnavailableError(
                        f"scheduled publication {job_id!r} is missing durable state"
                    )
                if row["state"] in _TERMINAL_RUN_STATES:
                    self._conn.commit()
                    return
                if row["state"] != "reserved" or row["publish_owner"] != publisher_id:
                    raise ScheduledRunLeaseLostError(
                        f"publication lease for scheduled run {job_id!r} was lost"
                    )
                self._conn.execute(
                    """
                    UPDATE scheduled_runs
                    SET state='publication_failed', outcome=?, completed_at=?,
                        publish_owner=NULL, publish_lease_expires_at=NULL,
                        updated_at=?
                    WHERE job_id=? AND state='reserved' AND publish_owner=?
                    """,
                    (outcome, timestamp, timestamp, job_id, publisher_id),
                )
                updated = self._conn.execute(
                    """
                    UPDATE scheduled_jobs
                    SET active_job_id=NULL, updated_at=?
                    WHERE scope=? AND active_job_id=?
                    """,
                    (timestamp, row["scope"], job_id),
                )
                if updated.rowcount != 1:
                    raise ScheduledRunLeaseLostError(
                        f"scheduled publication {job_id!r} lost its logical scope lock"
                    )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def begin_execution(
        self,
        job: JobEnvelope,
        *,
        scope: str,
        schedule_id: str,
        owner_id: str,
        now: datetime,
        lease_seconds: float,
        redrive_count: int = 0,
    ) -> _ExecutionClaim:
        current = _aware_utc(now)
        timestamp = _timestamp(current)
        expires = _timestamp(current + timedelta(seconds=lease_seconds))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM scheduled_runs WHERE job_id=?", (job.job_id,)
                ).fetchone()
                if row is None:
                    raise ScheduledRunStateUnavailableError(
                        f"scheduled run {job.job_id!r} is missing durable state; "
                        "verify that the worker and scheduler use the same catalog database"
                    )
                self._validate_durable_envelope(row, job, redrive_count)
                if row["scope"] != scope or row["schedule_id"] != schedule_id:
                    raise ScheduledRunStateUnavailableError(
                        "scheduled job metadata does not match durable state"
                    )
                generation = int(row["execution_generation"])
                if row["state"] in _TERMINAL_RUN_STATES:
                    if row["state"] != "dead_lettered":
                        self._conn.commit()
                        return _ExecutionClaim(False, None)
                    if redrive_count <= generation:
                        self._conn.commit()
                        return _ExecutionClaim(False, None)
                    if redrive_count != generation + 1:
                        raise InvalidJobError(
                            "scheduled redrive generation does not follow durable state"
                        )
                    active_job = self._conn.execute(
                        "SELECT active_job_id FROM scheduled_jobs WHERE scope=?",
                        (scope,),
                    ).fetchone()
                    if active_job is None:
                        raise ScheduledRunStateUnavailableError(
                            "scheduled execution scope is missing durable state"
                        )
                    if active_job["active_job_id"] not in (None, job.job_id):
                        raise ScheduledRunLockedError(
                            f"scheduled scope {scope!r} has a newer active occurrence"
                        )
                    newer_occurrence = self._conn.execute(
                        """
                        SELECT job_id FROM scheduled_runs
                        WHERE scope=? AND run_sequence>?
                        ORDER BY run_sequence
                        LIMIT 1
                        """,
                        (scope, row["run_sequence"]),
                    ).fetchone()
                    if newer_occurrence is not None:
                        raise InvalidJobError(
                            "a scheduled occurrence cannot be redriven after a newer "
                            "occurrence for the same scope"
                        )
                    self._conn.execute(
                        """
                        UPDATE scheduled_jobs
                        SET active_job_id=?, updated_at=?
                        WHERE scope=?
                        """,
                        (job.job_id, timestamp, scope),
                    )
                    self._conn.execute(
                        """
                        UPDATE scheduled_runs
                        SET state='retry_wait', completed_at=NULL, outcome=NULL,
                            execution_owner=NULL, execution_lease_expires_at=NULL,
                            execution_generation=?, updated_at=?
                        WHERE job_id=?
                        """,
                        (redrive_count, timestamp, job.job_id),
                    )
                    row = self._conn.execute(
                        "SELECT * FROM scheduled_runs WHERE job_id=?", (job.job_id,)
                    ).fetchone()
                    assert row is not None
                if row["state"] not in _ACTIVE_RUN_STATES:
                    raise ScheduledRunStateUnavailableError(
                        f"scheduled run has unsupported durable state: {row['state']}"
                    )
                durable_generation = int(row["execution_generation"])
                if redrive_count < durable_generation:
                    # An ACK-uncertain source delivery from a prior generation
                    # can arrive after an operator redrive has already advanced
                    # the logical run. It is settled history, not corrupt state.
                    self._conn.commit()
                    return _ExecutionClaim(False, None)
                if redrive_count == durable_generation + 1:
                    # DLQ publication precedes the durable dead-letter state
                    # transition. An operator may redrive during that window;
                    # defer it until the prior generation settles rather than
                    # treating this valid ordering race as corrupt state.
                    raise ScheduledRunLockedError(
                        "scheduled redrive is waiting for its prior generation "
                        "to settle"
                    )
                if redrive_count != durable_generation:
                    raise ScheduledRunStateUnavailableError(
                        "scheduled delivery redrive generation does not match durable state"
                    )
                active_job = self._conn.execute(
                    "SELECT active_job_id FROM scheduled_jobs WHERE scope=?", (scope,)
                ).fetchone()
                if active_job is None or active_job["active_job_id"] != job.job_id:
                    raise ScheduledRunStateUnavailableError(
                        "scheduled run no longer owns its durable execution scope"
                    )

                current_owner = row["execution_owner"]
                current_expiry = row["execution_lease_expires_at"]
                if row["state"] == "running":
                    if current_owner is None:
                        raise ScheduledRunStateUnavailableError(
                            "running scheduled execution is missing its durable owner"
                        )
                    # Expiry is a liveness signal, not proof that the previous
                    # handler stopped. Python cannot cancel storage work already
                    # running in a thread, so automatic takeover here could run
                    # the same scoped side effects concurrently. Only an explicit
                    # retry transition clears the durable owner.
                    raise ScheduledRunLockedError(
                        f"scheduled scope {scope!r} is leased and durably owned by "
                        "another delivery"
                    )
                if current_owner is not None or current_expiry is not None:
                    raise ScheduledRunStateUnavailableError(
                        "non-running scheduled execution contains stale owner state"
                    )
                self._conn.execute(
                    """
                    UPDATE scheduled_runs
                    SET state='running', execution_owner=?,
                        execution_lease_expires_at=?, updated_at=?
                    WHERE job_id=?
                    """,
                    (owner_id, expires, timestamp, job.job_id),
                )
                self._conn.commit()
                return _ExecutionClaim(True, owner_id)
            except BaseException:
                self._conn.rollback()
                raise

    def renew_execution(
        self,
        job_id: str,
        *,
        owner_id: str,
        now: datetime,
        lease_seconds: float,
    ) -> None:
        current = _aware_utc(now)
        timestamp = _timestamp(current)
        expires = _timestamp(current + timedelta(seconds=lease_seconds))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._owned_run(job_id, owner_id)
                if (
                    row["execution_lease_expires_at"] is None
                    or _parse_timestamp(row["execution_lease_expires_at"]) <= current
                ):
                    raise ScheduledRunLeaseLostError(
                        f"execution lease for scheduled run {job_id!r} expired"
                    )
                self._conn.execute(
                    """
                    UPDATE scheduled_runs
                    SET execution_lease_expires_at=?, updated_at=?
                    WHERE job_id=? AND execution_owner=?
                    """,
                    (expires, timestamp, job_id, owner_id),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def retry_execution(self, job_id: str, *, owner_id: str, now: datetime) -> None:
        timestamp = _timestamp(now)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._owned_run(job_id, owner_id)
                self._conn.execute(
                    """
                    UPDATE scheduled_runs
                    SET state='retry_wait', execution_owner=NULL,
                        execution_lease_expires_at=NULL, updated_at=?
                    WHERE job_id=? AND execution_owner=?
                    """,
                    (timestamp, job_id, owner_id),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def finish_execution(
        self,
        job_id: str,
        *,
        owner_id: str,
        state: str,
        outcome: str,
        now: datetime,
    ) -> None:
        if state not in _TERMINAL_RUN_STATES:
            raise ValueError(f"invalid terminal scheduled run state: {state}")
        timestamp = _timestamp(now)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._owned_run(job_id, owner_id)
                self._conn.execute(
                    """
                    UPDATE scheduled_runs
                    SET state=?, outcome=?, completed_at=?, execution_owner=NULL,
                        execution_lease_expires_at=NULL, publish_owner=NULL,
                        publish_lease_expires_at=NULL, updated_at=?
                    WHERE job_id=? AND execution_owner=?
                    """,
                    (state, outcome, timestamp, timestamp, job_id, owner_id),
                )
                updated = self._conn.execute(
                    """
                    UPDATE scheduled_jobs
                    SET active_job_id=NULL, updated_at=?
                    WHERE scope=? AND active_job_id=?
                    """,
                    (timestamp, row["scope"], job_id),
                )
                if updated.rowcount != 1:
                    raise ScheduledRunLeaseLostError(
                        f"scheduled run {job_id!r} lost its logical scope lock"
                    )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def _owned_run(self, job_id: str, owner_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM scheduled_runs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None or row["state"] != "running" or row["execution_owner"] != owner_id:
            raise ScheduledRunLeaseLostError(
                f"execution lease for scheduled run {job_id!r} was lost"
            )
        return row

    @staticmethod
    def _validate_durable_envelope(
        row: sqlite3.Row,
        job: JobEnvelope,
        redrive_count: int,
    ) -> None:
        stored = SQLiteScheduleStore._stored_envelope_from_row(row)

        extra_metadata = set(job.metadata).difference(stored.metadata)
        # A producer replaces traceparent at publication; it is transport
        # context, not part of the durable scheduled operation's identity.
        transport_metadata = {"traceparent"}
        if extra_metadata.difference(_REDRIVE_METADATA | transport_metadata):
            raise ScheduledRunStateUnavailableError(
                "scheduled job contains unsupported metadata"
            )
        base_metadata = {
            key: value
            for key, value in job.metadata.items()
            if key not in _REDRIVE_METADATA and key not in transport_metadata
        }
        if (
            job.tenant_id != stored.tenant_id
            or job.principal != stored.principal
            or job.schema_version != stored.schema_version
            or job.job_type != stored.job_type
            or job.created_at != stored.created_at
            or job.correlation_id != stored.correlation_id
            or job.payload != stored.payload
            or base_metadata != {
                key: value for key, value in stored.metadata.items()
                if key not in transport_metadata
            }
        ):
            raise ScheduledRunStateUnavailableError(
                "scheduled delivery does not match its durable envelope"
            )
        redrive_metadata = set(job.metadata).intersection(_REDRIVE_METADATA)
        if redrive_count == 0 and redrive_metadata:
            raise ScheduledRunStateUnavailableError(
                "scheduled delivery has unexpected redrive metadata"
            )
        if redrive_count > 0 and job.metadata.get(REDRIVE_COUNT_METADATA) != str(
            redrive_count
        ):
            raise ScheduledRunStateUnavailableError(
                "scheduled delivery has an invalid redrive count"
            )

    @staticmethod
    def _stored_envelope_from_row(row: sqlite3.Row) -> JobEnvelope:
        raw = row["envelope"]
        if not isinstance(raw, bytes):
            raw = bytes(raw)
        try:
            stored = JobEnvelope.from_bytes(raw)
        except JobEnvelopeError as exc:
            raise ScheduledRunStateUnavailableError(
                "scheduled run contains an invalid durable envelope"
            ) from exc
        if (
            stored.job_id != row["job_id"]
            or stored.metadata.get(SCHEDULE_ID_METADATA) != row["schedule_id"]
            or stored.metadata.get(SCHEDULE_SCOPE_METADATA) != row["scope"]
            or stored.metadata.get(SCHEDULED_FOR_METADATA) != row["scheduled_for"]
        ):
            raise ScheduledRunStateUnavailableError(
                "scheduled run envelope does not match its durable state"
            )
        return stored

    @staticmethod
    def _record_from_row(row: sqlite3.Row, now: datetime) -> ScheduledRunRecord:
        lease = (
            None
            if row["execution_lease_expires_at"] is None
            else _parse_timestamp(row["execution_lease_expires_at"])
        )
        owner = row["execution_owner"]
        stale = (
            row["state"] == "running"
            and owner is not None
            and lease is not None
            and lease <= now
        )
        return ScheduledRunRecord(
            job_id=str(row["job_id"]),
            run_sequence=int(row["run_sequence"]),
            schedule_id=str(row["schedule_id"]),
            scope=str(row["scope"]),
            state=str(row["state"]),
            scheduled_for=_parse_timestamp(row["scheduled_for"]),
            published_at=(
                None
                if row["published_at"] is None
                else _parse_timestamp(row["published_at"])
            ),
            execution_owner=None if owner is None else str(owner),
            execution_lease_expires_at=lease,
            execution_generation=int(row["execution_generation"]),
            completed_at=(
                None
                if row["completed_at"] is None
                else _parse_timestamp(row["completed_at"])
            ),
            outcome=None if row["outcome"] is None else str(row["outcome"]),
            created_at=_parse_timestamp(row["created_at"]),
            updated_at=_parse_timestamp(row["updated_at"]),
            stale=stale,
        )

    @staticmethod
    def _recovery_from_row(row: sqlite3.Row) -> ScheduledRunRecovery:
        return ScheduledRunRecovery(
            recovery_id=str(row["recovery_id"]),
            job_id=str(row["job_id"]),
            schedule_id=str(row["schedule_id"]),
            scope=str(row["scope"]),
            execution_generation=int(row["execution_generation"]),
            prior_execution_owner=str(row["prior_execution_owner"]),
            prior_lease_expires_at=_parse_timestamp(row["prior_lease_expires_at"]),
            operator=str(row["operator"]),
            reason=str(row["reason"]),
            fence_evidence=str(row["fence_evidence"]),
            recovered_at=_parse_timestamp(row["recovered_at"]),
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> ScheduledRun:
        return ScheduledRun(
            job=SQLiteScheduleStore._stored_envelope_from_row(row),
            schedule_id=row["schedule_id"],
            scope=row["scope"],
            scheduled_for=_parse_timestamp(row["scheduled_for"]),
        )


class PeriodicScheduler:
    """Reserves due occurrences and publishes their durable job envelopes."""

    def __init__(
        self,
        queue: JobPublisher,
        store: SQLiteScheduleStore,
        schedules: Sequence[ScheduledJob],
        *,
        clock: Clock | None = None,
        publication_lease_seconds: float = 30.0,
    ) -> None:
        self.queue = queue
        self.store = store
        self.schedules = tuple(schedules)
        self._clock = clock or _utc_now
        self.publication_lease_seconds = _positive_duration_seconds(
            publication_lease_seconds, "publication_lease_seconds"
        )
        self.publisher_id = str(uuid4())
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._started or self._closed:
            raise RuntimeError("scheduler has already been started or closed")
        await self.queue.connect()
        try:
            await asyncio.to_thread(self.store.sync, self.schedules, _aware_utc(self._clock()))
        except BaseException:
            await self.queue.close(graceful=False)
            raise
        self._started = True

    async def run_due(self) -> int:
        if not self._started or self._closed:
            raise RuntimeError("scheduler is not running")
        claim_time = _aware_utc(self._clock())
        pending = await asyncio.to_thread(
            self.store.claim_pending_publications,
            claim_time,
            publisher_id=self.publisher_id,
            publication_lease_seconds=self.publication_lease_seconds,
        )
        published = 0
        publication_errors: list[tuple[ScheduledRun, Exception]] = []
        for run in pending:
            try:
                await self._publish(run)
            except Exception as exc:
                publication_errors.append((run, exc))
            else:
                published += 1

        for schedule in self.schedules:
            if not schedule.enabled:
                continue
            # Publication can block on broker flow control. Sample wall time
            # per reservation so a slow earlier enqueue cannot backdate later
            # occurrences or make their next interval immediately overdue.
            reservation_time = _aware_utc(self._clock())
            reserved = await asyncio.to_thread(
                self.store.reserve_due,
                schedule,
                reservation_time,
                publisher_id=self.publisher_id,
                publication_lease_seconds=self.publication_lease_seconds,
            )
            if reserved is None:
                continue
            try:
                await self._publish(reserved)
            except Exception as exc:
                publication_errors.append((reserved, exc))
            else:
                published += 1
        for run, error in publication_errors:
            LOGGER.error(
                "scheduled publication failed",
                extra={"job_id": run.job.job_id},
            )
        if publication_errors:
            raise publication_errors[0][1]
        return published

    async def _publish(self, run: ScheduledRun) -> None:
        try:
            await self.queue.enqueue(run.job, message_id=run.job.job_id)
            await asyncio.to_thread(
                self.store.mark_enqueued,
                run.job.job_id,
                publisher_id=self.publisher_id,
                now=_aware_utc(self._clock()),
            )
        except JobEnvelopeError as exc:
            await asyncio.to_thread(
                self.store.fail_publication,
                run.job.job_id,
                publisher_id=self.publisher_id,
                outcome=f"{type(exc).__name__}: {exc}",
                now=_aware_utc(self._clock()),
            )
            raise
        except BaseException:
            await asyncio.to_thread(
                self.store.release_publication,
                run.job.job_id,
                publisher_id=self.publisher_id,
            )
            raise

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started:
            await self.queue.close(graceful=False)


class ScheduledRunExecution:
    """One owner-fenced worker attempt for a scheduled logical run."""

    def __init__(
        self,
        store: SQLiteScheduleStore,
        job_id: str,
        owner_id: str | None,
        *,
        execute: bool,
        clock: Clock,
        lease_seconds: float,
    ) -> None:
        self.store = store
        self.job_id = job_id
        self.owner_id = owner_id
        self.execute = execute
        self._clock = clock
        self.lease_seconds = lease_seconds
        self.heartbeat_interval = lease_seconds / 3.0 if execute else 0.0

    async def renew(self) -> None:
        if not self.execute:
            return
        assert self.owner_id is not None
        await asyncio.to_thread(
            self.store.renew_execution,
            self.job_id,
            owner_id=self.owner_id,
            now=_aware_utc(self._clock()),
            lease_seconds=self.lease_seconds,
        )

    async def retry(self) -> None:
        if not self.execute:
            return
        assert self.owner_id is not None
        await asyncio.to_thread(
            self.store.retry_execution,
            self.job_id,
            owner_id=self.owner_id,
            now=_aware_utc(self._clock()),
        )

    async def succeed(self) -> None:
        if not self.execute:
            return
        assert self.owner_id is not None
        await asyncio.to_thread(
            self.store.finish_execution,
            self.job_id,
            owner_id=self.owner_id,
            state="succeeded",
            outcome="succeeded",
            now=_aware_utc(self._clock()),
        )

    async def dead_letter(self, disposition: DeadLetterDisposition) -> None:
        if not self.execute:
            return
        assert self.owner_id is not None
        await asyncio.to_thread(
            self.store.finish_execution,
            self.job_id,
            owner_id=self.owner_id,
            state="dead_lettered",
            outcome=disposition.value,
            now=_aware_utc(self._clock()),
        )


class ScheduledRunCoordinator:
    """Worker runtime extension that enforces scheduled-job execution leases."""

    def __init__(
        self,
        store: SQLiteScheduleStore,
        *,
        clock: Clock | None = None,
        lease_seconds: float = 60.0,
    ) -> None:
        self.store = store
        self._clock = clock or _utc_now
        self.lease_seconds = _positive_duration_seconds(lease_seconds, "lease_seconds")

    async def begin(self, job: JobEnvelope, context: JobContext) -> ScheduledRunExecution | None:
        present = {
            key
            for key in (
                SCHEDULE_ID_METADATA,
                SCHEDULE_SCOPE_METADATA,
                SCHEDULED_FOR_METADATA,
            )
            if key in job.metadata
        }
        if not present:
            return None
        if len(present) != 3:
            raise ScheduledRunStateUnavailableError(
                "scheduled job metadata is incomplete"
            )
        schedule_id = job.metadata[SCHEDULE_ID_METADATA]
        scope = job.metadata[SCHEDULE_SCOPE_METADATA]
        try:
            _parse_timestamp(job.metadata[SCHEDULED_FOR_METADATA])
        except RuntimeError as exc:
            raise ScheduledRunStateUnavailableError(
                "scheduled job due timestamp is invalid"
            ) from exc
        try:
            claim_time = _aware_utc(self._clock())
        except ValueError as exc:
            raise ScheduledRunStateUnavailableError(
                "scheduled execution clock is unavailable"
            ) from exc
        owner_id = str(uuid4())
        claim_task = asyncio.create_task(
            asyncio.to_thread(
                self.store.begin_execution,
                job,
                scope=scope,
                schedule_id=schedule_id,
                owner_id=owner_id,
                now=claim_time,
                lease_seconds=self.lease_seconds,
                redrive_count=context.redrive_count,
            ),
            name=f"cognistore-schedule-claim-{job.job_id}",
        )
        cancelled = False
        try:
            while True:
                try:
                    claim = await asyncio.shield(claim_task)
                    break
                except asyncio.CancelledError:
                    # sqlite3 work already running in a thread cannot be
                    # cancelled. Resolve its transaction before allowing the
                    # delivery to settle, or it could commit a running owner
                    # that no task knows how to release.
                    cancelled = True
                    if claim_task.cancelled():
                        raise
        except (ScheduledRunLockedError, InvalidJobError):
            raise
        except Exception as exc:
            raise ScheduledRunStateUnavailableError(
                "scheduled execution state could not be claimed"
            ) from exc

        execution = ScheduledRunExecution(
            self.store,
            job.job_id,
            claim.owner_id,
            execute=claim.execute,
            clock=self._clock,
            lease_seconds=self.lease_seconds,
        )
        if not cancelled:
            return execution

        if execution.execute:
            release_task = asyncio.create_task(
                execution.retry(),
                name=f"cognistore-schedule-claim-release-{job.job_id}",
            )
            try:
                while True:
                    try:
                        await asyncio.shield(release_task)
                        break
                    except asyncio.CancelledError:
                        if release_task.cancelled():
                            raise
            except Exception as exc:
                raise ScheduledRunStateUnavailableError(
                    "cancelled scheduled execution claim could not release its owner"
                ) from exc
        raise asyncio.CancelledError
