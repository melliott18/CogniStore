"""Tenant-bound, resumable consistency observations with no source mutations.

A scan inventories keys into a separate report database, then inspects each key
against fresh catalog/move snapshots and generation-bound backend reads. It is
an observation over time, not a distributed snapshot or a repair plan.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from cognistore.core.audit import AuditOutcome
from cognistore.core.catalog import CatalogStore, ObjectRecord
from cognistore.core.consistency_report import (
    REPORT_VERSION,
    append_event,
    create_report,
    encode,
    export_report,
    open_report,
    read_state,
    report_summary,
    save_state,
    summarize,
)
from cognistore.core.move_jobs import MoveJobState
from cognistore.db.factory import sqlite_catalog_path
from cognistore.drivers.observed import ObservedStorageDriver, suppress_access_capture
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.drivers.storage_driver import ObjectGenerationMismatchError, StorageDriver
from cognistore.utils.redaction import redact

__all__ = ["ConsistencyScanner", "ScanScope", "export_report", "report_summary",
           "validate_consistency_bucket"]

_CHUNK_SIZE = 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive(value: object, name: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


def validate_consistency_bucket(value: object) -> str:
    """Require one bucket component so POSIX namespaces cannot nest across tenants."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("bucket must be a non-empty string")
    if value in {".", ".."} or any(character in value for character in ("/", "\\", "\0")):
        raise ValueError("consistency bucket must be a single path component without NUL")
    return value


@dataclass(frozen=True)
class ScanScope:
    """An effective scope derived from a trusted tenant namespace binding.

    The caller is responsible for authorizing this binding. Catalog bucket/key
    rows do not yet carry tenant ownership; a caller-supplied label alone is not
    an authorization mechanism. The CLI requires an operator-owned scope config.
    """

    tenant_id: str
    bucket: str
    prefix: str
    tiers: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("tenant_id", "bucket"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        validate_consistency_bucket(self.bucket)
        if not isinstance(self.prefix, str):
            raise ValueError("prefix must be a string")
        if (not isinstance(self.tiers, tuple) or not self.tiers
                or any(not isinstance(tier, str) or not tier.strip() for tier in self.tiers)
                or len(set(self.tiers)) != len(self.tiers)):
            raise ValueError("tiers must be a non-empty tuple of distinct tier names")
        object.__setattr__(self, "tiers", tuple(sorted(self.tiers)))

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "tiers": list(self.tiers)}


class _RateLimiter:
    """Pace logical operations/bytes with at most one operation of burst."""

    def __init__(self, rate: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.rate = _positive(rate, "rate")
        self.clock, self.sleep = clock, sleep
        self.next_at = clock()

    def acquire(self, amount: int = 1) -> None:
        now = self.clock()
        delay = self.next_at - now
        if delay > 0:
            self.sleep(delay)
        self.next_at = max(self.next_at, self.clock()) + amount / self.rate


class ConsistencyScanner:
    def __init__(self, catalog: CatalogStore, drivers: Mapping[str, StorageDriver],
                 scope: ScanScope, *, binding_id: str,
                 requests_per_second: float = 20, bytes_per_second: float = 8 * 1024 * 1024,
                 page_size: int = 100) -> None:
        if not isinstance(scope, ScanScope):
            raise ValueError("scope must be a ScanScope")
        if not isinstance(binding_id, str) or not binding_id.strip():
            raise ValueError("a trusted source/tenant binding_id is required")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 1000:
            raise ValueError("page_size must be between 1 and 1000")
        self.catalog, self.scope, self.binding_id = catalog, scope, binding_id
        self.page_size = page_size
        self.requests_per_second = _positive(requests_per_second, "requests_per_second")
        self.bytes_per_second = _positive(bytes_per_second, "bytes_per_second")
        self.requests = _RateLimiter(self.requests_per_second)
        self.bytes = _RateLimiter(self.bytes_per_second)
        self.drivers: dict[str, StorageDriver] = {}
        self.aliases: dict[str, str] = {}
        for tier in scope.tiers:
            if tier not in drivers:
                raise ValueError(f"scope tier is not configured: {tier}")
            driver = drivers[tier]
            canonical = next((name for name, existing in self.drivers.items()
                              if self._raw_driver(driver).same_backend(self._raw_driver(existing))), tier)
            self.aliases[tier] = canonical
            if canonical == tier:
                self.drivers[tier] = driver

    @staticmethod
    def _raw_driver(driver: StorageDriver) -> StorageDriver:
        while isinstance(driver, ObservedStorageDriver):
            driver = driver.raw_driver
        return driver

    def _protect_report(self, path: Path) -> None:
        # SQLite writes sidecars as well as the named database. Protect both
        # sides of every comparison, including a source named report-journal.
        candidates = [Path(str(base) + suffix) for base in (path, path.resolve())
                      for suffix in ("", "-wal", "-shm", "-journal")]
        for candidate in candidates:
            resolved = candidate.resolve()
            for driver in self.drivers.values():
                driver = self._raw_driver(driver)
                if isinstance(driver, PosixDriver):
                    if resolved == driver.base or driver.base in resolved.parents:
                        raise ValueError("report must be outside storage roots")
            locator = getattr(self.catalog, "db_path", None)
            if locator is not None:
                catalog_path = sqlite_catalog_path(locator)
                if catalog_path is not None:
                    for base in (catalog_path, catalog_path.resolve()):
                        for suffix in ("", "-wal", "-shm", "-journal"):
                            protected = Path(str(base) + suffix)
                            if (resolved == protected.resolve() or
                                    (candidate.exists() and protected.exists()
                                     and candidate.samefile(protected))):
                                raise ValueError("report must not alias the catalog or its journals")

    def run(self, report_path: Path, *, resume: bool = False,
            max_items: int | None = None, actor_id: str = "cli") -> dict[str, Any]:
        if max_items is not None and (isinstance(max_items, bool)
                                     or not isinstance(max_items, int) or max_items <= 0):
            raise ValueError("max_items must be a positive integer")
        report_path = Path(report_path)
        self._protect_report(report_path)
        if not resume:
            state = {
                "version": REPORT_VERSION, "scan_id": str(uuid4()),
                "scope": self.scope.to_dict(), "binding_id": self.binding_id,
                "phase": "catalog", "cursor": None, "tier_index": 0,
                "checked": 0, "started_at": _now(), "finished_at": None,
                "page_size": self.page_size, "requests_per_second": self.requests_per_second,
                "bytes_per_second": self.bytes_per_second,
            }
            create_report(report_path, state, actor_id)
        with open_report(report_path) as connection, suppress_access_capture():
            state = read_state(connection)
            self._verify_state(state)
            if resume and state["phase"] != "completed":
                with connection:
                    append_event(connection, state, "consistency.resumed", actor_id)
            remaining = max_items
            while remaining is None or remaining > 0:
                # Each page's cursor, observations and audit event commit together.
                # Concurrent authorized resumptions serialize at this boundary.
                connection.execute("BEGIN IMMEDIATE")
                state = read_state(connection)
                if state["phase"] == "completed":
                    connection.rollback()
                    break
                try:
                    limit = self.page_size if remaining is None else min(remaining, self.page_size)
                    consumed = self._step(connection, state, limit)
                    save_state(connection, state)
                    append_event(connection, state,
                                 "consistency.completed" if state["phase"] == "completed"
                                 else "consistency.checkpoint", actor_id)
                    connection.commit()
                    if remaining is not None:
                        remaining -= max(1, consumed)
                except BaseException as exc:
                    connection.rollback()
                    # Preserve the prior cursor on interruption/backend listing failure.
                    with connection:
                        append_event(connection, read_state(connection), "consistency.failed",
                                     actor_id, outcome=AuditOutcome.FAILED,
                                     details={"exception_type": type(exc).__name__})
                    raise
            return summarize(connection, read_state(connection))

    def _verify_state(self, state: dict[str, Any]) -> None:
        if state["scope"] != self.scope.to_dict() or state["binding_id"] != self.binding_id:
            raise ValueError("resume scope or source/tenant binding does not match report")
        if any(state[name] != getattr(self, name) for name in
               ("page_size", "requests_per_second", "bytes_per_second")):
            raise ValueError("resume scan options do not match report")

    def _step(self, connection: sqlite3.Connection, state: dict[str, Any], limit: int) -> int:
        bucket, prefix = self.scope.bucket, self.scope.prefix
        phase, cursor = state["phase"], state["cursor"]
        keys: list[str]
        if phase == "catalog":
            records = self.catalog.list_page(bucket, prefix, after_key=cursor, limit=limit)
            keys = [record.key for record in records]
            if records:
                state["cursor"] = records[-1].key
            else:
                state.update(phase="jobs", cursor=None)
        elif phase == "jobs":
            jobs = self.catalog.list_move_jobs_page(bucket, prefix, after_id=cursor, limit=limit)
            keys = [job.key for job in jobs]
            if jobs:
                state["cursor"] = jobs[-1].idempotency_key
            else:
                state.update(phase="storage", cursor=None)
        elif phase == "storage":
            tier = list(self.drivers)[state["tier_index"]]
            self.requests.acquire()
            page = self.drivers[tier].list_objects_page(bucket, prefix, cursor=cursor, limit=limit)
            keys = list(page.keys)
            if len(keys) > limit or any(not key.startswith(prefix) for key in keys):
                raise ValueError("storage listing returned keys outside the requested page/scope")
            if page.next_cursor is not None and page.next_cursor == cursor:
                raise ValueError("storage listing cursor did not advance")
            connection.executemany("INSERT OR IGNORE INTO sightings VALUES(?, ?)",
                                   ((key, tier) for key in keys))
            state["cursor"] = page.next_cursor
            if page.next_cursor is None:
                state["tier_index"] += 1
                if state["tier_index"] == len(self.drivers):
                    state.update(phase="check", cursor=None)
        elif phase == "check":
            # One inspection per transaction bounds replay after an interruption.
            statement = "SELECT object_key FROM inventory"
            parameters: tuple[str, ...] = ()
            if cursor is not None:
                statement += " WHERE object_key > ?"
                parameters = (cursor,)
            row = connection.execute(
                statement + " ORDER BY object_key COLLATE BINARY LIMIT 1", parameters,
            ).fetchone()
            if row is None:
                state.update(phase="completed", cursor=None, finished_at=_now())
                return 0
            key = row[0]
            listed = {row[0] for row in connection.execute(
                "SELECT tier FROM sightings WHERE object_key=?", (key,))}
            findings = self._inspect(key, listed)
            for finding in findings:
                payload = {"scan_id": state["scan_id"], "tenant_id": self.scope.tenant_id,
                           "bucket": bucket, "key": key, "observed_at": _now(), **finding}
                identity = [state["scan_id"], key, finding]
                payload["finding_id"] = hashlib.sha256(encode(identity).encode()).hexdigest()
                connection.execute("INSERT INTO findings(payload) VALUES(?)", (encode(redact(payload)),))
            state.update(cursor=key, checked=state["checked"] + 1)
            return 1
        else:
            raise ValueError("invalid consistency scan phase")
        if any(not key.startswith(prefix) for key in keys):
            raise ValueError("catalog returned keys outside the requested scope")
        connection.executemany("INSERT OR IGNORE INTO inventory VALUES(?)", ((key,) for key in keys))
        return len(keys)

    def _stat(self, driver: StorageDriver, key: str) -> dict[str, Any] | None:
        self.requests.acquire()
        try:
            stat = driver.stat_object(self.scope.bucket, key)
        except FileNotFoundError:
            return None
        size, generation = stat.get("size"), stat.get("generation")
        if (isinstance(size, bool) or not isinstance(size, int) or size < 0
                or not isinstance(generation, str) or not generation):
            raise ValueError("backend stat requires a valid size and generation")
        return {"size": size, "generation": generation}

    def _observe(self, driver: StorageDriver, key: str) -> dict[str, Any] | None:
        stat = self._stat(driver, key)
        if stat is None:
            return None
        self.requests.acquire()
        digest, count = hashlib.sha256(), 0
        with driver.open_object_reader_if_generation(self.scope.bucket, key, stat["generation"]) as reader:
            while True:
                # Read at most one byte beyond the observed length, even if a
                # broken backend ignores the generation or grows indefinitely.
                size = min(_CHUNK_SIZE, stat["size"] - count + 1)
                chunk = reader.read(size)
                self.bytes.acquire(len(chunk))
                if not chunk:
                    break
                if len(chunk) > size:
                    raise ValueError("backend exceeded bounded read size")
                count += len(chunk)
                if count > stat["size"]:
                    raise ObjectGenerationMismatchError("object size changed during read")
                digest.update(chunk)
        if count != stat["size"]:
            raise ObjectGenerationMismatchError("object size changed during read")
        return {**stat, "sha256": digest.hexdigest()}

    @staticmethod
    def _catalog_checksum(record: ObjectRecord | None) -> str | None:
        if record is None:
            return None
        identity = record.metadata.get("content_identity")
        digest = None
        if isinstance(identity, dict) and identity.get("digest_algorithm") == "sha256":
            digest = identity.get("sha256")
        elif record.metadata.get("sample_len") == record.size:
            # Legacy metadata hashes a sample. It is full-content evidence only
            # when its explicitly recorded sample length covers the whole object.
            digest = record.metadata.get("sha256")
        return ConsistencyScanner._digest(digest)

    @staticmethod
    def _digest(value: object) -> str | None:
        return value if (isinstance(value, str) and len(value) == 64
                         and all(char in "0123456789abcdef" for char in value)) else None

    def _inspect(self, key: str, listed: set[str]) -> list[dict[str, Any]]:
        bucket = self.scope.bucket
        fence = self.catalog.capture_scan_fence(bucket, key)
        record = self.catalog.get(bucket, key)
        latest = max(fence.move_jobs, key=lambda row: (row[6], row[7], row[0]), default=None)
        job = self.catalog.get_move_job(latest[0]) if latest else None
        findings: list[dict[str, Any]] = []

        def add(reason: str, severity: str, tier: str | None = None, **details: Any) -> None:
            findings.append({"reason_code": reason, "severity": severity, "tier": tier,
                             "details": details})

        observations: dict[str, dict[str, Any] | None] = {}
        errors: set[str] = set()
        changed = False
        for tier, driver in self.drivers.items():
            try:
                observations[tier] = self._observe(driver, key)
            except (ObjectGenerationMismatchError, FileNotFoundError):
                changed = True
                errors.add(tier)
            except Exception as exc:
                errors.add(tier)
                add("backend_error", "warning", tier, exception_type=type(exc).__name__)
        # Recheck every backend after all streams, including initially absent
        # copies. A racing create/delete/move invalidates absence as well as hashes.
        for tier, observation in observations.items():
            try:
                stat = self._stat(self.drivers[tier], key)
                before = ({name: observation[name] for name in ("size", "generation")}
                          if observation is not None else None)
                changed = changed or stat != before
            except Exception as exc:
                errors.add(tier)
                add("backend_error", "warning", tier, exception_type=type(exc).__name__)
        if (changed or record != self.catalog.get(bucket, key)
                or fence != self.catalog.capture_scan_fence(bucket, key)):
            return [{"reason_code": "object_changed", "severity": "warning", "tier": None,
                     "details": {"retry_with_new_scan": True}}]

        present = {tier for tier, value in observations.items() if value is not None and tier not in errors}
        missing = {tier for tier, value in observations.items() if value is None and tier not in errors}
        current = self.aliases.get(record.tier) if record else None
        expected: dict[str, str] = {}
        if record:
            if current is not None:
                expected[current] = "missing_placement"
            else:
                add("scope_incomplete", "info", expected_tier=record.tier)
        active = job is not None and job.state != MoveJobState.COMPLETED
        in_transit: set[str] = set()
        if active and job is not None:
            expired = (job.lease_expires_at is None or
                       datetime.fromisoformat(job.lease_expires_at.replace("Z", "+00:00"))
                       <= datetime.now(timezone.utc))
            add("partial_job", "warning" if expired or job.state == MoveJobState.FAILED else "info",
                job_id=job.idempotency_key, state=job.state.value, lease_expired=expired)
            source_required = job.state in {MoveJobState.PREPARED, MoveJobState.TRANSFERRED,
                                            MoveJobState.VERIFIED}
            destination_required = job.state in {MoveJobState.TRANSFERRED, MoveJobState.VERIFIED,
                                                 MoveJobState.COMMITTED, MoveJobState.CLEANUP}
            for tier, required, reason in ((job.src_tier, source_required, "missing_source"),
                                          (job.dst_tier, destination_required, "missing_destination")):
                canonical = self.aliases.get(tier)
                if canonical is not None:
                    in_transit.add(canonical)
                    if required:
                        expected[canonical] = reason
                elif required:
                    add("scope_incomplete", "info", expected_tier=tier, job_id=job.idempotency_key)
        for tier, reason in expected.items():
            if tier in missing:
                add(reason, "error", tier, job_id=job.idempotency_key if active and job else None)
        for tier in listed & missing - set(expected):
            add("object_changed", "warning", tier, disappeared_since_listing=True)

        if record is None and present and not active:
            for tier in sorted(present):
                add("untracked_object", "warning", tier)
        elif record is not None:
            for tier in sorted(present - ({current} if current is not None else set()) - in_transit):
                add("placement_mismatch", "warning", tier, catalog_tier=record.tier)
        # A move deliberately has two copies until source cleanup. Additional
        # physical backends, including failed-move leftovers, are discrepancies.
        if len(present) > 1 and not (active and job and job.state != MoveJobState.FAILED
                                    and present <= in_transit):
            add("duplicate_placement", "warning", tiers=sorted(present))
        expected_digest = self._catalog_checksum(record)
        expected_size = record.size if record else job.expected_size if active and job else None
        for tier in sorted(present):
            observed = observations[tier]
            assert observed is not None
            if expected_size is not None and observed["size"] != expected_size:
                add("size_mismatch", "error", tier, expected_size=expected_size,
                    observed_size=observed["size"])
            digest = expected_digest
            # A terminal/failed journal may outlive a rewritten logical key.
            # Its full checksum is evidence only for the recorded generation.
            if digest is None and job is not None:
                if (self.aliases.get(job.src_tier) == tier
                        and observed["generation"] == job.source_metadata.get("generation")):
                    digest = self._digest(job.source_checksum)
                elif (self.aliases.get(job.dst_tier) == tier
                        and observed["generation"] == job.destination_generation):
                    digest = self._digest(job.destination_checksum)
            if digest is not None and observed["sha256"] != digest:
                add("checksum_mismatch", "error", tier, expected_sha256=digest,
                    observed_sha256=observed["sha256"])
            elif digest is None and (record is not None or active):
                add("checksum_unavailable", "info", tier)
        digests = {observation["sha256"] for tier, observation in observations.items()
                   if tier in present and observation is not None}
        if expected_digest is None and len(digests) > 1:
            add("checksum_mismatch", "error", tiers=sorted(present), evidence="copies_disagree")
        return findings
