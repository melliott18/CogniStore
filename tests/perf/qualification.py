from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from array import array
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

from cognistore.core.move_jobs import MoveJobState
from cognistore.core.mover import Mover, MoveVerificationResult
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.driver_loader import load_drivers
from cognistore.drivers.storage_driver import ReadableStream, StorageDriver
from cognistore.jobs.retry import RetryPolicy, classify_job_error

SCHEMA_VERSION = 1
FAULT_KINDS = (
    "timeout",
    "throttling",
    "backend_unavailable",
    "worker_termination",
)
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_KEY_WIDTH = 12
_T = TypeVar("_T")


@dataclass(frozen=True)
class SizeClass:
    bytes: int
    weight: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.bytes, int)
            or isinstance(self.bytes, bool)
            or self.bytes < 0
        ):
            raise ValueError("size bytes must be a non-negative integer")
        if (
            not isinstance(self.weight, int)
            or isinstance(self.weight, bool)
            or self.weight < 1
        ):
            raise ValueError("size weight must be a positive integer")

    def to_dict(self) -> dict[str, int]:
        return {"bytes": self.bytes, "weight": self.weight}


FULL_QUALIFYING_SIZE_CLASSES = (
    SizeClass(256, 50),
    SizeClass(1024, 30),
    SizeClass(4096, 15),
    SizeClass(16384, 5),
)


@dataclass(frozen=True)
class PathConfig:
    name: str
    source_tier: str
    destination_tier: str

    def __post_init__(self) -> None:
        for field_name in ("name", "source_tier", "destination_tier"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
            if "/" in value or ":" in value:
                raise ValueError(f"{field_name} cannot contain '/' or ':'")
        if self.source_tier == self.destination_tier:
            raise ValueError("source and destination tiers must be different")

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "source_tier": self.source_tier,
            "destination_tier": self.destination_tier,
        }


@dataclass(frozen=True)
class QualificationConfig:
    drivers_path: Path
    catalog_path: Path
    output_path: Path
    run_id: str
    bucket: str
    object_count: int
    size_classes: tuple[SizeClass, ...]
    paths: tuple[PathConfig, ...]
    workers: int = 4
    seed: int = 29
    profile: str = "reduced"
    faults: str = "standard"
    max_attempts: int = 3
    retry_base_delay: float = 0.1
    retry_max_delay: float = 0.2
    lease_seconds: float = 0.05
    worker_termination_timeout: float = 30.0

    def __post_init__(self) -> None:
        if not _RUN_ID_PATTERN.fullmatch(self.run_id):
            raise ValueError(
                "run_id must be 1-64 letters, numbers, dots, underscores, or hyphens"
            )
        if not isinstance(self.bucket, str) or not self.bucket.strip():
            raise ValueError("bucket must be a non-empty string")
        for name in ("object_count", "workers"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise ValueError("seed must be an integer")
        if not self.size_classes:
            raise ValueError("at least one size class is required")
        if not self.paths:
            raise ValueError("at least one benchmark path is required")
        path_names = [item.name for item in self.paths]
        if len(path_names) != len(set(path_names)):
            raise ValueError("benchmark path names must be unique")
        if self.faults not in {"none", "standard"}:
            raise ValueError("faults must be 'none' or 'standard'")
        if self.profile not in {"reduced", "full"}:
            raise ValueError("profile must be 'reduced' or 'full'")
        if self.profile == "full" and self.object_count != 1_000_000:
            raise ValueError("the full profile requires exactly 1,000,000 objects")
        if (
            self.profile == "full"
            and self.size_classes != FULL_QUALIFYING_SIZE_CLASSES
        ):
            raise ValueError(
                "the full profile requires the canonical mixed-size workload"
            )
        if self.profile == "full" and self.faults != "standard":
            raise ValueError("the full profile requires standard fault injection")
        if self.faults == "standard" and self.object_count < len(FAULT_KINDS):
            raise ValueError(
                f"standard fault qualification requires at least {len(FAULT_KINDS)} objects"
            )
        if self.faults == "standard" and self.max_attempts < 2:
            raise ValueError("standard fault qualification requires at least 2 attempts")
        RetryPolicy(
            max_attempts=self.max_attempts,
            base_delay=self.retry_base_delay,
            max_delay=self.retry_max_delay,
            jitter=0,
        )
        for name in ("lease_seconds", "worker_termination_timeout"):
            value = getattr(self, name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number")
        if self.lease_seconds >= self.retry_base_delay:
            raise ValueError("lease_seconds must be less than retry_base_delay")
        if self.catalog_path == Path(":memory:"):
            raise ValueError("qualification requires a persistent SQLite catalog")
        if self.catalog_path.expanduser().resolve() == self.output_path.expanduser().resolve():
            raise ValueError("catalog and output paths must be different")

    @property
    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            max_attempts=self.max_attempts,
            base_delay=self.retry_base_delay,
            max_delay=self.retry_max_delay,
            jitter=0,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "bucket": self.bucket,
            "object_count": self.object_count,
            "size_classes": [item.to_dict() for item in self.size_classes],
            "paths": [item.to_dict() for item in self.paths],
            "workers": self.workers,
            "seed": self.seed,
            "profile": self.profile,
            "faults": self.faults,
            "fault_execution_scope": (
                "direct Mover calls with production classify_job_error and "
                "RetryPolicy; JetStream DLQ/redrive is not evaluated by this report"
            ),
            "max_attempts": self.max_attempts,
            "retry_base_delay_seconds": self.retry_base_delay,
            "retry_max_delay_seconds": self.retry_max_delay,
            "lease_seconds": self.lease_seconds,
            "worker_termination_timeout_seconds": self.worker_termination_timeout,
            "drivers_path": str(self.drivers_path.expanduser().resolve()),
            "catalog_path": str(self.catalog_path),
        }


@dataclass(frozen=True)
class _MoveOutcome:
    index: int
    size: int
    latency_ms: float
    attempts: int


@dataclass(frozen=True)
class _ProcessStop:
    requested_actions: tuple[str, ...]
    exit_code: int | None
    signal_name: str | None


class _PhaseAccumulator:
    """Retain only compact latency samples and aggregate attempt counts."""

    def __init__(self) -> None:
        self.objects = 0
        self.attempts = 0
        self.latencies_ms = array("d")

    def add(self, outcome: _MoveOutcome) -> None:
        self.objects += 1
        self.attempts += outcome.attempts
        self.latencies_ms.append(outcome.latency_ms)


class _ObservedRetryAccumulator:
    """Aggregate unexpected failures without retaining a million rows."""

    def __init__(self, sample_limit: int = 20) -> None:
        self._sample_limit = sample_limit
        self._lock = threading.Lock()
        self._objects = 0
        self._recovered_objects = 0
        self._failed_attempts = 0
        self._source_retention_failures = 0
        self._categories: dict[str, int] = {}
        self._max_recovery_seconds = 0.0
        self._samples: list[dict[str, Any]] = []

    def add(self, evidence: dict[str, Any]) -> None:
        with self._lock:
            self._objects += 1
            self._recovered_objects += int(evidence["recovered"])
            failures = evidence["attempt_failures"]
            self._failed_attempts += len(failures)
            self._source_retention_failures += int(
                evidence["source_retained"] is not True
            )
            for failure in failures:
                category = failure["category"]
                self._categories[category] = self._categories.get(category, 0) + 1
            self._max_recovery_seconds = max(
                self._max_recovery_seconds,
                evidence["recovery_seconds"] or 0.0,
            )
            if len(self._samples) < self._sample_limit:
                self._samples.append(evidence)

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "objects_with_failures": self._objects,
                "recovered_objects": self._recovered_objects,
                "unrecovered_objects": self._objects - self._recovered_objects,
                "failed_attempts": self._failed_attempts,
                "source_retention_failures": self._source_retention_failures,
                "categories": dict(sorted(self._categories.items())),
                "max_recovery_seconds": self._max_recovery_seconds,
                "sample_limit": self._sample_limit,
                "samples": list(self._samples),
            }


class _InjectedThrottlingError(RuntimeError):
    status_code = 429


@dataclass
class _ArmedFault:
    kind: str
    phase: str
    remaining: int


class _FaultController:
    def __init__(self) -> None:
        self._faults: dict[tuple[str, str, str, str], _ArmedFault] = {}
        self._injected: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def arm(
        self,
        *,
        tier: str,
        operation: str,
        bucket: str,
        key: str,
        kind: str,
        phase: str = "before",
        failures: int = 1,
    ) -> None:
        if phase not in {"before", "after"}:
            raise ValueError("fault phase must be 'before' or 'after'")
        if not isinstance(failures, int) or isinstance(failures, bool) or failures < 1:
            raise ValueError("fault failures must be a positive integer")
        target = (tier, operation, bucket, key)
        with self._lock:
            if target in self._faults:
                raise ValueError(f"fault target is already armed: {target!r}")
            self._faults[target] = _ArmedFault(kind, phase, failures)

    def before(self, tier: str, operation: str, bucket: str, key: str) -> None:
        self._trigger("before", tier, operation, bucket, key)

    def after(self, tier: str, operation: str, bucket: str, key: str) -> None:
        self._trigger("after", tier, operation, bucket, key)

    def _trigger(
        self,
        phase: str,
        tier: str,
        operation: str,
        bucket: str,
        key: str,
    ) -> None:
        target = (tier, operation, bucket, key)
        with self._lock:
            fault = self._faults.get(target)
            if fault is None or fault.phase != phase:
                return
            fault.remaining -= 1
            if fault.remaining == 0:
                self._faults.pop(target)
            self._injected.append(
                {
                    "kind": fault.kind,
                    "tier": tier,
                    "operation": operation,
                    "phase": phase,
                    "bucket": bucket,
                    "key": key,
                    "injected_at": _utc_now(),
                }
            )
            kind = fault.kind
        if kind == "timeout":
            raise TimeoutError("injected storage timeout")
        if kind == "throttling":
            raise _InjectedThrottlingError("injected HTTP 429 throttling response")
        if kind == "backend_unavailable":
            raise ConnectionError("injected backend unavailability")
        raise RuntimeError(f"unsupported injected fault: {kind}")

    def injected(self, kind: str, key: str) -> bool:
        return self.injection_count(kind, key) > 0

    def injection_count(self, kind: str, key: str) -> int:
        with self._lock:
            return sum(
                item["kind"] == kind and item["key"] == key
                for item in self._injected
            )


class _FaultInjectingDriver(StorageDriver):
    def __init__(
        self,
        tier: str,
        delegate: StorageDriver,
        controller: _FaultController,
    ) -> None:
        self.tier = tier
        self.delegate = delegate
        self.controller = controller
        self.capabilities = delegate.capabilities
        self.chunk_size = getattr(delegate, "chunk_size", None)

    def put_object(
        self,
        bucket: str,
        key: str,
        data: bytes,
        range: str | None = None,
        overwrite: bool = True,
        **opts: Any,
    ) -> None:
        self.controller.before(self.tier, "put_object", bucket, key)
        self.delegate.put_object(
            bucket,
            key,
            data,
            range=range,
            overwrite=overwrite,
            **opts,
        )

    def get_object(
        self, bucket: str, key: str, range: str | None = None
    ) -> bytes:
        self.controller.before(self.tier, "get_object", bucket, key)
        return self.delegate.get_object(bucket, key, range=range)

    def open_object_reader(
        self,
        bucket: str,
        key: str,
        range: str | None = None,
    ) -> AbstractContextManager[ReadableStream]:
        self.controller.before(self.tier, "open_object_reader", bucket, key)
        return self.delegate.open_object_reader(bucket, key, range=range)

    def open_object_reader_if_generation(
        self,
        bucket: str,
        key: str,
        generation: str,
        range: str | None = None,
    ) -> AbstractContextManager[ReadableStream]:
        self.controller.before(self.tier, "open_object_reader", bucket, key)
        return self.delegate.open_object_reader_if_generation(
            bucket,
            key,
            generation,
            range=range,
        )

    def put_object_stream(
        self,
        bucket: str,
        key: str,
        source: ReadableStream,
        *,
        size: int,
        overwrite: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        self.controller.before(self.tier, "put_object_stream", bucket, key)
        transferred = self.delegate.put_object_stream(
            bucket,
            key,
            source,
            size=size,
            overwrite=overwrite,
            metadata=metadata,
        )
        self.controller.after(self.tier, "put_object_stream", bucket, key)
        return transferred

    def delete_object(self, bucket: str, key: str) -> None:
        self.controller.before(self.tier, "delete_object", bucket, key)
        self.delegate.delete_object(bucket, key)

    def delete_object_if_generation(
        self, bucket: str, key: str, generation: str
    ) -> bool:
        self.controller.before(self.tier, "delete_object_if_generation", bucket, key)
        return self.delegate.delete_object_if_generation(bucket, key, generation)

    def list_objects(
        self, bucket: str, prefix: str = ""
    ) -> Generator[str, None, None]:
        self.controller.before(self.tier, "list_objects", bucket, prefix)
        yield from self.delegate.list_objects(bucket, prefix)

    def stat_object(self, bucket: str, key: str) -> dict[str, Any]:
        self.controller.before(self.tier, "stat_object", bucket, key)
        return self.delegate.stat_object(bucket, key)

    def object_generation(self, bucket: str, key: str) -> str:
        self.controller.before(self.tier, "object_generation", bucket, key)
        return self.delegate.object_generation(bucket, key)

    def ensure_object_durable(self, bucket: str, key: str) -> None:
        self.controller.before(self.tier, "ensure_object_durable", bucket, key)
        self.delegate.ensure_object_durable(bucket, key)

    def same_backend(self, other: StorageDriver) -> bool:
        candidate = other.delegate if isinstance(other, _FaultInjectingDriver) else other
        return self.delegate.same_backend(candidate)


def parse_size_class(value: str) -> SizeClass:
    raw_bytes, separator, raw_weight = value.partition(":")
    try:
        size = int(raw_bytes)
        weight = int(raw_weight) if separator else 1
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "size must use BYTES or BYTES:WEIGHT with integers"
        ) from error
    try:
        return SizeClass(size, weight)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def parse_path(value: str) -> PathConfig:
    parts = value.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("path must use NAME:SOURCE_TIER:DESTINATION_TIER")
    try:
        return PathConfig(*parts)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _payload(seed: int, path_name: str, index: int, size: int) -> bytes:
    if size == 0:
        return b""
    block = hashlib.sha256(f"{seed}:{path_name}:{index}".encode()).digest()
    repeats = (size + len(block) - 1) // len(block)
    return (block * repeats)[:size]


def _size_for(index: int, size_classes: Sequence[SizeClass]) -> int:
    cycle = sum(item.weight for item in size_classes)
    offset = index % cycle
    for item in size_classes:
        if offset < item.weight:
            return item.bytes
        offset -= item.weight
    raise AssertionError("weighted size cycle is incomplete")


def _percentile(values: Sequence[float], percentile: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil((percentile / 100) * len(ordered)))
    return ordered[rank - 1]


def _latency_summary(values: Sequence[float]) -> dict[str, float]:
    return {
        "min": min(values, default=0.0),
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "p99": _percentile(values, 99),
        "max": max(values, default=0.0),
    }


def _configuration_sha256(configuration: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(configuration),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _bounded_results(
    function: Callable[[int], _T],
    indices: Iterable[int],
    *,
    workers: int,
    progress: Callable[[int], None] | None = None,
) -> Generator[_T, None, None]:
    iterator = iter(indices)
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending: set[Future[_T]] = set()
        for _ in range(workers * 2):
            try:
                item = next(iterator)
            except StopIteration:
                break
            pending.add(executor.submit(function, item))

        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                yield future.result()
                completed += 1
                if progress is not None:
                    progress(completed)
                try:
                    item = next(iterator)
                except StopIteration:
                    continue
                pending.add(executor.submit(function, item))


def _driver_description(driver: StorageDriver) -> dict[str, Any]:
    description: dict[str, Any] = {"type": type(driver).__name__}
    base = getattr(driver, "base", None)
    if isinstance(base, Path):
        description["path"] = str(base)
    endpoint = getattr(driver, "endpoint_url", None)
    if isinstance(endpoint, str):
        parsed = urlsplit(endpoint)
        safe_netloc = parsed.netloc.rsplit("@", 1)[-1]
        description["endpoint_url"] = urlunsplit(
            (parsed.scheme, safe_netloc, parsed.path, "", "")
        )
    region = getattr(driver, "region_name", None)
    if isinstance(region, str):
        description["region_name"] = region
    for name in (
        "partition",
        "addressing_style",
        "auto_create_bucket",
        "list_page_size",
        "chunk_size",
        "multipart_threshold",
    ):
        if not hasattr(driver, name):
            continue
        value = getattr(driver, name)
        if value is None or isinstance(value, (str, int, float, bool)):
            description[name] = value
    capabilities = getattr(driver, "capabilities", None)
    if capabilities is not None:
        description["capabilities"] = {
            name: bool(getattr(capabilities, name))
            for name in (
                "range_reads",
                "range_writes",
                "atomic_no_overwrite",
                "conditional_delete",
            )
        }
    return description


def _git_details() -> dict[str, Any]:
    details: dict[str, Any] = {"revision": None, "dirty": None, "source": None}
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        revision = os.environ.get("COGNISTORE_QUALIFICATION_GIT_REVISION", "").strip()
        dirty = os.environ.get("COGNISTORE_QUALIFICATION_GIT_DIRTY", "").strip().lower()
        if revision and dirty in {"true", "false"}:
            return {
                "revision": revision,
                "dirty": dirty == "true",
                "source": "environment",
            }
        return details
    return {"revision": revision or None, "dirty": bool(dirty), "source": "git"}


def _memory_bytes() -> int | None:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None
    if not isinstance(pages, int) or not isinstance(page_size, int):
        return None
    return pages * page_size


def _environment() -> dict[str, Any]:
    try:
        package_version = version("cognistore")
    except PackageNotFoundError:
        package_version = "uninstalled"
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "cognistore_version": package_version,
        "cpu_count": os.cpu_count(),
        "memory_bytes": _memory_bytes(),
        "git": _git_details(),
        "ci": {
            key: os.environ[key]
            for key in (
                "CI",
                "GITHUB_ACTIONS",
                "GITHUB_RUN_ID",
                "GITHUB_RUN_ATTEMPT",
                "GITHUB_SHA",
                "RUNNER_OS",
                "RUNNER_ARCH",
            )
            if key in os.environ
        },
    }


def _worker_termination_child(
    drivers_path: str,
    catalog_path: str,
    source_tier: str,
    destination_tier: str,
    bucket: str,
    key: str,
    idempotency_key: str,
    lease_seconds: float,
    checkpoint_reached: Any,
) -> None:
    drivers = load_drivers(drivers_path)
    catalog = SQLiteCatalog(catalog_path)

    def stop_at_transferred(job: Any) -> None:
        if job.state != MoveJobState.TRANSFERRED:
            return
        checkpoint_reached.set()
        while True:
            time.sleep(60)

    try:
        Mover(
            drivers,
            catalog,
            owner_id=f"terminated-worker:{os.getpid()}",
            lease_seconds=lease_seconds,
            transition_hook=stop_at_transferred,
        ).move(
            source_tier,
            destination_tier,
            bucket,
            key,
            idempotency_key=idempotency_key,
        )
    finally:
        catalog.close()


def _process_stop_result(
    process: Any,
    requested_actions: Sequence[str],
) -> _ProcessStop:
    exit_code = process.exitcode
    signal_name: str | None = None
    if isinstance(exit_code, int) and exit_code < 0:
        try:
            signal_name = signal.Signals(-exit_code).name
        except ValueError:
            signal_name = f"SIGNAL_{-exit_code}"
    return _ProcessStop(tuple(requested_actions), exit_code, signal_name)


def _stop_process(process: Any) -> _ProcessStop:
    """Bounded best-effort cleanup for a child that may be blocked forever."""

    requested_actions: list[str] = []
    for action_name, action in (
        ("kill", process.kill),
        ("terminate", process.terminate),
    ):
        if not process.is_alive():
            process.join(timeout=0)
            return _process_stop_result(process, requested_actions)
        try:
            action()
        except (OSError, ValueError):
            pass
        else:
            requested_actions.append(action_name)
        process.join(timeout=5)
    if process.is_alive():
        raise RuntimeError("injected worker process could not be terminated")
    process.join(timeout=0)
    return _process_stop_result(process, requested_actions)


class _QualificationRunner:
    def __init__(self, config: QualificationConfig) -> None:
        self.config = config
        self.drivers = load_drivers(str(config.drivers_path))
        self._validate_drivers()
        self.catalog: SQLiteCatalog | None = None
        driver_descriptions = {
            name: _driver_description(driver)
            for name, driver in self.drivers.items()
            if any(
                name in {path.source_tier, path.destination_tier}
                for path in config.paths
            )
        }
        self.report: dict[str, Any] = {
            "schema": "cognistore.move-qualification",
            "schema_version": SCHEMA_VERSION,
            "profile": config.profile,
            "acceptance_status": "not_evaluated",
            "status": "running",
            "started_at": _utc_now(),
            "finished_at": None,
            "environment": _environment(),
            "configuration": config.to_dict(),
            "drivers": driver_descriptions,
            "paths": [],
            "summary": {},
        }
        self.report["configuration"]["size_distribution"] = (
            self._size_distribution()
        )
        self.report["configuration"]["drivers_configuration_sha256"] = (
            _configuration_sha256(driver_descriptions)
        )

    def _validate_drivers(self) -> None:
        has_posix_path = False
        has_s3_path = False
        for path in self.config.paths:
            missing = sorted(
                {path.source_tier, path.destination_tier}.difference(self.drivers)
            )
            if missing:
                raise ValueError(
                    f"path {path.name!r} references unknown tiers: {', '.join(missing)}"
                )
            catalog = SQLiteCatalog(":memory:")
            try:
                Mover(self.drivers, catalog).validate_driver_pair(
                    path.source_tier, path.destination_tier
                )
            finally:
                catalog.close()
            driver_types = {
                type(self.drivers[path.source_tier]).__name__,
                type(self.drivers[path.destination_tier]).__name__,
            }
            has_posix_path = has_posix_path or driver_types == {"PosixDriver"}
            has_s3_path = has_s3_path or "S3Driver" in driver_types
        if self.config.profile == "full" and not (has_posix_path and has_s3_path):
            raise ValueError(
                "the full profile requires both a POSIX-only path and an S3 path"
            )

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        try:
            self._prepare_catalog()
            assert self.catalog is not None
            for path in self.config.paths:
                path_report = self._new_path_report(path)
                self.report["paths"].append(path_report)
                try:
                    self._run_path(path, path_report)
                except BaseException as error:
                    path_report["status"] = "failed"
                    path_report["error"] = {
                        "type": type(error).__name__,
                        "message": str(error),
                    }
                    raise
            self.report["status"] = "passed"
            self.report["acceptance_status"] = (
                "full_scale_passed"
                if self.config.profile == "full"
                else "reduced_scale_only"
            )
            self.report["summary"] = self._build_summary(
                time.monotonic() - started,
                completed=True,
            )
        except BaseException as error:
            self.report["status"] = "failed"
            self.report["acceptance_status"] = (
                "full_scale_failed"
                if self.config.profile == "full"
                else "reduced_scale_failed"
            )
            self.report["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
            self.report["summary"] = self._build_summary(
                time.monotonic() - started,
                completed=False,
            )
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                self._finish()
                raise
        finally:
            if self.catalog is not None:
                self.catalog.close()
                self.catalog = None
        self._finish()
        return self.report

    def _new_path_report(self, path: PathConfig) -> dict[str, Any]:
        prefix = f"qualification/{self.config.run_id}/{path.name}/"
        return {
            "name": path.name,
            "source_tier": path.source_tier,
            "destination_tier": path.destination_tier,
            "backend_path": (
                f"{type(self.drivers[path.source_tier]).__name__}->"
                f"{type(self.drivers[path.destination_tier]).__name__}"
            ),
            "prefix": prefix,
            "status": "running",
            "current_phase": "not_started",
            "setup": None,
            "forward": None,
            "reverse": None,
            "failures": [],
            "observed_retries": _ObservedRetryAccumulator().to_dict(),
            "idempotency": {
                "forward_replays": 0,
                "reverse_replays": 0,
                "new_transitions_on_replay": None,
            },
            "integrity": {
                "forward": None,
                "reverse": None,
                "silent_loss": None,
                "corruption": None,
            },
        }

    def _build_summary(
        self,
        elapsed_seconds: float,
        *,
        completed: bool,
    ) -> dict[str, Any]:
        paths = self.report["paths"]
        phase_reports = [
            path[direction]
            for path in paths
            for direction in ("forward", "reverse")
            if isinstance(path.get(direction), dict)
        ]
        failures = [item for path in paths for item in path["failures"]]
        observed = [path["observed_retries"] for path in paths]
        summary = {
            "paths_started": len(paths),
            "paths_completed": sum(path["status"] == "passed" for path in paths),
            "objects_per_path": self.config.object_count,
            "logical_moves": sum(phase["objects"] for phase in phase_reports),
            "payload_bytes_moved": sum(phase["bytes"] for phase in phase_reports),
            "fault_scenarios": len(failures),
            "recovered_fault_scenarios": sum(
                bool(item.get("recovered")) for item in failures
            ),
            "injected_failure_events": sum(
                int(item.get("injections", 0)) for item in failures
            ),
            "failed_attempts": (
                sum(len(item.get("attempt_failures", ())) for item in failures)
                + sum(item["failed_attempts"] for item in observed)
            ),
            "observed_failure_objects": sum(
                item["objects_with_failures"] for item in observed
            ),
            "silent_loss": 0 if completed else None,
            "corruption": 0 if completed else None,
            "elapsed_seconds": elapsed_seconds,
            "acceptance_criteria": self._acceptance_criteria(completed=completed),
        }
        return summary

    def _acceptance_criteria(self, *, completed: bool) -> dict[str, str]:
        if self.config.profile == "full":
            round_trip = "passed" if completed else "failed"
        else:
            round_trip = "not_evaluated_reduced_scale"

        if self.config.faults == "none":
            recovery = "not_evaluated_no_faults"
        elif not completed:
            recovery = "failed_or_incomplete"
        else:
            recovery = "passed" if self._standard_faults_passed() else "failed"

        return {
            "one_million_object_round_trip": round_trip,
            "injected_failure_idempotency_retry_and_source_retention": recovery,
            "environment_configuration_and_metrics_recorded": (
                "passed" if completed else "incomplete"
            ),
        }

    def _standard_faults_passed(self) -> bool:
        return all(
            self._path_standard_faults_passed(path)
            for path in self.report["paths"]
        )

    def _path_standard_faults_passed(self, path: Mapping[str, Any]) -> bool:
        if path["idempotency"]["new_transitions_on_replay"] != 0:
            return False
        failures = {item["kind"]: item for item in path["failures"]}
        if set(failures) != set(FAULT_KINDS):
            return False
        if not all(
            item["recovered"] and item["source_retained"]
            for item in failures.values()
        ):
            return False
        backend = failures["backend_unavailable"]
        if (
            not backend["retry_limit_reached"]
            or backend["bounded_retry_attempts"] != self.config.max_attempts
        ):
            return False
        return bool(failures["timeout"]["after_publication"])

    def _finish(self) -> None:
        self.report["finished_at"] = _utc_now()
        _write_json(self.config.output_path, self.report)

    def _prepare_catalog(self) -> None:
        if self.config.profile == "full":
            git = self.report["environment"]["git"]
            if git["revision"] is None or git["dirty"] is not False:
                raise RuntimeError(
                    "the full profile requires a clean, identifiable git revision"
                )
        catalog_path = self.config.catalog_path.expanduser().resolve()
        if catalog_path.exists():
            raise FileExistsError(
                f"qualification catalog already exists; use a fresh path: {catalog_path}"
            )
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        self.catalog = SQLiteCatalog(catalog_path)

    def _run_path(self, path: PathConfig, report: dict[str, Any]) -> None:
        catalog = self.catalog
        assert catalog is not None
        print(f"[{path.name}] checking namespace and seeding objects", file=sys.stderr)
        prefix = report["prefix"]
        report["current_phase"] = "namespace_check"
        self._assert_empty_namespace(path, prefix)
        report["current_phase"] = "seed"
        setup_started = time.monotonic()
        total_bytes = self._seed(path, prefix)
        setup_seconds = time.monotonic() - setup_started
        report["setup"] = {
            "objects": self.config.object_count,
            "bytes": total_bytes,
            "elapsed_seconds": setup_seconds,
        }

        controller = _FaultController()
        fault_indices: dict[int, str] = {}
        if self.config.faults == "standard":
            fault_indices = {
                0: "timeout",
                1: "throttling",
                2: "backend_unavailable",
                3: "worker_termination",
            }
            for index, kind in fault_indices.items():
                if kind == "worker_termination":
                    continue
                controller.arm(
                    tier=path.destination_tier,
                    operation="put_object_stream",
                    bucket=self.config.bucket,
                    key=self._key(prefix, index),
                    kind=kind,
                    phase="after" if kind == "timeout" else "before",
                    failures=(
                        self.config.max_attempts
                        if kind == "backend_unavailable"
                        else 1
                    ),
                )

        destination = _FaultInjectingDriver(
            path.destination_tier,
            self.drivers[path.destination_tier],
            controller,
        )
        forward_drivers = dict(self.drivers)
        forward_drivers[path.destination_tier] = destination
        failures: list[dict[str, Any]] = report["failures"]
        observed_retries = _ObservedRetryAccumulator()

        def preserve_observed(evidence: dict[str, Any]) -> None:
            observed_retries.add(evidence)

        print(f"[{path.name}] moving forward", file=sys.stderr)
        report["current_phase"] = "forward_moves"
        forward_started = time.monotonic()
        forward_stats = _PhaseAccumulator()
        ordinary_indices = range(self.config.object_count)
        if fault_indices:
            for index, kind in fault_indices.items():
                if kind == "worker_termination":
                    outcome, evidence = self._run_worker_termination(
                        path,
                        prefix,
                        index,
                        failure_sink=failures.append,
                    )
                else:
                    outcome, evidence = self._move_with_retry(
                        path,
                        prefix,
                        index,
                        forward=True,
                        drivers=forward_drivers,
                        expected_fault=kind,
                        controller=controller,
                        recover_after_exhaustion=(kind == "backend_unavailable"),
                        failure_sink=failures.append,
                    )
                forward_stats.add(outcome)
                failures.append(evidence)
            ordinary_indices = range(len(fault_indices), self.config.object_count)

        try:
            for outcome, evidence in _bounded_results(
                lambda index: self._move_with_retry(
                    path,
                    prefix,
                    index,
                    forward=True,
                    drivers=forward_drivers,
                    failure_sink=preserve_observed,
                ),
                ordinary_indices,
                workers=self.config.workers,
                progress=self._progress(
                    path.name, "forward", len(ordinary_indices)
                ),
            ):
                forward_stats.add(outcome)
                if evidence:
                    observed_retries.add(evidence)
        finally:
            report["observed_retries"] = observed_retries.to_dict()
        forward_seconds = time.monotonic() - forward_started
        forward_summary = self._phase_summary(
            forward_stats, forward_seconds, total_bytes
        )
        report["forward"] = forward_summary
        del forward_stats
        report["current_phase"] = "forward_audit"
        forward_audit = self._audit(path, prefix, at_destination=True)
        report["integrity"]["forward"] = forward_audit
        report["current_phase"] = "forward_idempotency_replay"
        forward_replays = self._verify_replays(path, prefix, forward=True)
        report["idempotency"]["forward_replays"] = forward_replays

        print(f"[{path.name}] moving reverse", file=sys.stderr)
        report["current_phase"] = "reverse_moves"
        reverse_started = time.monotonic()
        reverse_stats = _PhaseAccumulator()
        try:
            for outcome, evidence in _bounded_results(
                lambda index: self._move_with_retry(
                    path,
                    prefix,
                    index,
                    forward=False,
                    drivers=self.drivers,
                    failure_sink=preserve_observed,
                ),
                range(self.config.object_count),
                workers=self.config.workers,
                progress=self._progress(
                    path.name, "reverse", self.config.object_count
                ),
            ):
                reverse_stats.add(outcome)
                if evidence:
                    observed_retries.add(evidence)
        finally:
            report["observed_retries"] = observed_retries.to_dict()
        reverse_seconds = time.monotonic() - reverse_started
        reverse_summary = self._phase_summary(
            reverse_stats, reverse_seconds, total_bytes
        )
        report["reverse"] = reverse_summary
        report["current_phase"] = "reverse_audit"
        reverse_audit = self._audit(path, prefix, at_destination=False)
        report["integrity"]["reverse"] = reverse_audit
        report["current_phase"] = "reverse_idempotency_replay"
        reverse_replays = self._verify_replays(path, prefix, forward=False)
        report["idempotency"]["reverse_replays"] = reverse_replays
        report["idempotency"]["new_transitions_on_replay"] = 0
        report["integrity"]["silent_loss"] = 0
        report["integrity"]["corruption"] = 0
        report["observed_retries"] = observed_retries.to_dict()
        if report["observed_retries"]["source_retention_failures"]:
            raise AssertionError(
                "an ambient failure did not preserve a verifiable source"
            )
        if (
            self.config.faults == "standard"
            and not self._path_standard_faults_passed(report)
        ):
            raise AssertionError(
                "standard fault evidence did not satisfy recovery, retry-limit, "
                "idempotency, and source-retention requirements"
            )
        report["current_phase"] = "completed"
        report["status"] = "passed"

    def _assert_empty_namespace(self, path: PathConfig, prefix: str) -> None:
        for tier in (path.source_tier, path.destination_tier):
            first = next(
                self.drivers[tier].list_objects(self.config.bucket, prefix),
                None,
            )
            if first is not None:
                raise FileExistsError(
                    f"qualification namespace is not empty on tier {tier!r}: {first}"
                )

    def _seed(self, path: PathConfig, prefix: str) -> int:
        assert self.catalog is not None

        def seed_one(index: int) -> int:
            size = self._object_size(index)
            key = self._key(prefix, index)
            payload = _payload(self.config.seed, path.name, index, size)
            self.drivers[path.source_tier].put_object(
                self.config.bucket,
                key,
                payload,
                overwrite=False,
            )
            return size

        return sum(
            _bounded_results(
                seed_one,
                range(self.config.object_count),
                workers=self.config.workers,
                progress=self._progress(path.name, "seed", self.config.object_count),
            )
        )

    def _move_with_retry(
        self,
        path: PathConfig,
        prefix: str,
        index: int,
        *,
        forward: bool,
        drivers: Mapping[str, StorageDriver],
        expected_fault: str | None = None,
        controller: _FaultController | None = None,
        recover_after_exhaustion: bool = False,
        failure_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[_MoveOutcome, dict[str, Any]]:
        assert self.catalog is not None
        source = path.source_tier if forward else path.destination_tier
        destination = path.destination_tier if forward else path.source_tier
        direction = "forward" if forward else "reverse"
        key = self._key(prefix, index)
        idempotency_key = self._idempotency_key(path, direction, index)
        size = self._object_size(index)
        attempts = 0
        first_failure_at: float | None = None
        failure_records: list[dict[str, Any]] = []
        source_retained = True
        retry_limit_reached = False
        bounded_retry_attempts = 0
        recovery_attempts = 0
        started = time.monotonic()
        result: MoveVerificationResult | None = None
        last_error: Exception | None = None
        phases = [("bounded_retry", self.config.max_attempts)]
        if recover_after_exhaustion:
            phases.append(("post_exhaustion_resume", self.config.max_attempts))

        def evidence_for_outcome(*, recovered: bool) -> dict[str, Any]:
            elapsed_since_failure = (
                0.0
                if first_failure_at is None
                else time.monotonic() - first_failure_at
            )
            evidence: dict[str, Any] = {
                "kind": expected_fault or "observed_failure",
                "injected": expected_fault is not None,
                "object_index": index,
                "idempotency_key": idempotency_key,
                "attempts": attempts,
                "bounded_retry_attempts": bounded_retry_attempts,
                "recovery_attempts": recovery_attempts,
                "max_attempts": self.config.max_attempts,
                "retry_limit_reached": retry_limit_reached,
                "attempt_failures": list(failure_records),
                "source_retained": source_retained,
                "recovered": recovered,
                "recovery_seconds": elapsed_since_failure if recovered else None,
                "failure_duration_seconds": elapsed_since_failure,
            }
            if expected_fault is not None:
                evidence.update(
                    {
                        "mechanism": {
                            "timeout": (
                                "destination proxy timeout after publication"
                            ),
                            "throttling": (
                                "one-shot destination proxy before publication"
                            ),
                            "backend_unavailable": (
                                "destination proxy unavailable through bounded retry "
                                "limit, then same-key resume"
                            ),
                        }[expected_fault],
                        "injections": (
                            0
                            if controller is None
                            else controller.injection_count(expected_fault, key)
                        ),
                        "after_publication": expected_fault == "timeout",
                    }
                )
            return evidence

        def preserve_failed_outcome() -> None:
            if failure_sink is not None:
                failure_sink(evidence_for_outcome(recovered=False))

        for phase, phase_limit in phases:
            for phase_attempt in range(1, phase_limit + 1):
                attempts += 1
                if phase == "bounded_retry":
                    bounded_retry_attempts += 1
                else:
                    recovery_attempts += 1
                mover = Mover(
                    dict(drivers),
                    self.catalog,
                    owner_id=(
                        f"qualification:{path.name}:{direction}:{index}:"
                        f"{phase}:{phase_attempt}"
                    ),
                    lease_seconds=self.config.lease_seconds,
                )
                try:
                    result = mover.move(
                        source,
                        destination,
                        self.config.bucket,
                        key,
                        idempotency_key=idempotency_key,
                    )
                    self._assert_verification(result, size)
                    break
                except Exception as error:
                    last_error = error
                    if first_failure_at is None:
                        first_failure_at = time.monotonic()
                    classification = classify_job_error(error)
                    retained = self._object_matches(
                        self.drivers[source], path, key, index
                    )
                    source_retained = source_retained and retained
                    failure_records.append(
                        {
                            "attempt": attempts,
                            "phase": phase,
                            "error_type": type(error).__name__,
                            "category": classification.category.value,
                            "retryable": classification.retryable,
                            "source_retained": retained,
                        }
                    )
                    if not classification.retryable:
                        preserve_failed_outcome()
                        raise RuntimeError(
                            f"move {idempotency_key!r} failed terminally"
                        ) from error
                    if phase_attempt < phase_limit:
                        time.sleep(
                            self.config.retry_policy.delay_for(
                                phase_attempt, random_value=0.5
                            )
                        )
            if result is not None:
                break
            if phase == "bounded_retry" and recover_after_exhaustion:
                retry_limit_reached = True
                time.sleep(
                    self.config.retry_policy.delay_for(
                        self.config.max_attempts,
                        random_value=0.5,
                    )
                )
                continue
            if phase == "bounded_retry":
                retry_limit_reached = True
            preserve_failed_outcome()
            raise RuntimeError(
                f"move {idempotency_key!r} failed after {attempts} attempts"
            ) from last_error

        if result is None:
            preserve_failed_outcome()
            raise AssertionError("retry phases exited without a result")

        latency_ms = (time.monotonic() - started) * 1000
        outcome = _MoveOutcome(index, size, latency_ms, attempts)
        if expected_fault is None:
            if not failure_records:
                return outcome, {}
            return outcome, evidence_for_outcome(recovered=True)
        evidence = evidence_for_outcome(recovered=True)
        try:
            if controller is None or not controller.injected(expected_fault, key):
                raise AssertionError(
                    f"expected {expected_fault} fault was not injected"
                )
            if not failure_records:
                raise AssertionError(
                    f"expected {expected_fault} fault did not fail an attempt"
                )
            expected_category = {
                "timeout": "timeout",
                "throttling": "throttled",
                "backend_unavailable": "unavailable",
            }[expected_fault]
            if failure_records[0]["category"] != expected_category:
                raise AssertionError(
                    f"{expected_fault} classified as "
                    f"{failure_records[0]['category']}"
                )
            expected_injections = (
                self.config.max_attempts
                if recover_after_exhaustion
                else 1
            )
            injection_count = controller.injection_count(expected_fault, key)
            if injection_count != expected_injections:
                raise AssertionError(
                    f"expected {expected_injections} {expected_fault} injections, "
                    f"observed {injection_count}"
                )
        except BaseException:
            if failure_sink is not None:
                failure_sink(evidence)
            raise
        return outcome, evidence

    def _run_worker_termination(
        self,
        path: PathConfig,
        prefix: str,
        index: int,
        *,
        failure_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[_MoveOutcome, dict[str, Any]]:
        catalog = self.catalog
        assert catalog is not None
        key = self._key(prefix, index)
        idempotency_key = self._idempotency_key(path, "forward", index)
        context = multiprocessing.get_context("spawn")
        checkpoint_reached = context.Event()
        process = context.Process(
            target=_worker_termination_child,
            args=(
                str(self.config.drivers_path),
                str(self.config.catalog_path),
                path.source_tier,
                path.destination_tier,
                self.config.bucket,
                key,
                idempotency_key,
                self.config.lease_seconds,
                checkpoint_reached,
            ),
            name=f"cognistore-qualification-kill-{path.name}",
        )
        started = time.monotonic()
        process_started = False
        termination_requested = False
        termination_injected = False
        termination_actions: list[str] = []
        terminated_exit_code: int | None = None
        terminated_signal: str | None = None
        killed_at: float | None = None
        retained: bool | None = None
        resume_attempted = False
        try:
            process.start()
            process_started = True
            deadline = time.monotonic() + self.config.worker_termination_timeout
            while not checkpoint_reached.wait(timeout=0.05):
                if not process.is_alive():
                    process.join(timeout=5)
                    raise RuntimeError(
                        "injected worker exited before reaching the transferred "
                        f"checkpoint (exit code {process.exitcode})"
                    )
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "worker did not reach the injected checkpoint in time"
                    )

            termination_requested = True
            process_stop = _stop_process(process)
            termination_actions = list(process_stop.requested_actions)
            terminated_exit_code = process_stop.exit_code
            terminated_signal = process_stop.signal_name
            if not termination_actions:
                raise RuntimeError(
                    "worker exited before forced termination could be injected"
                )
            termination_injected = True
            killed_at = time.monotonic()
            interrupted = catalog.get_move_job(idempotency_key)
            if interrupted is None or interrupted.state != MoveJobState.TRANSFERRED:
                raise AssertionError("terminated worker did not persist TRANSFERRED state")
            retained = self._object_matches(
                self.drivers[path.source_tier], path, key, index
            )
            if not retained:
                raise AssertionError("worker termination did not retain the source")

            lease_expiry = datetime.fromisoformat(
                str(interrupted.lease_expires_at).replace("Z", "+00:00")
            )
            remaining = (lease_expiry - datetime.now(timezone.utc)).total_seconds()
            if remaining > 0:
                time.sleep(remaining + 0.01)

            mover = Mover(
                self.drivers,
                catalog,
                owner_id=f"recovery-worker:{path.name}:{index}",
                lease_seconds=self.config.lease_seconds,
            )
            resume_attempted = True
            result = mover.move(
                path.source_tier,
                path.destination_tier,
                self.config.bucket,
                key,
                idempotency_key=idempotency_key,
            )
            self._assert_verification(result, self._object_size(index))
            return (
                _MoveOutcome(
                    index,
                    self._object_size(index),
                    (time.monotonic() - started) * 1000,
                    2,
                ),
                {
                    "kind": "worker_termination",
                    "injected": True,
                    "mechanism": (
                        "forced process termination after durable TRANSFERRED "
                        "checkpoint"
                    ),
                    "object_index": index,
                    "idempotency_key": idempotency_key,
                    "checkpoint": MoveJobState.TRANSFERRED.value,
                    "termination_actions": termination_actions,
                    "terminated_exit_code": terminated_exit_code,
                    "terminated_signal": terminated_signal,
                    "attempts": 2,
                    "bounded_retry_attempts": 1,
                    "recovery_attempts": 1,
                    "max_attempts": self.config.max_attempts,
                    "retry_limit_reached": False,
                    "injections": 1,
                    "after_publication": True,
                    "attempt_failures": [
                        {
                            "attempt": 1,
                            "phase": "terminated_worker",
                            "error_type": "WorkerProcessTerminated",
                            "category": "worker_termination",
                            "retryable": True,
                            "source_retained": retained,
                        }
                    ],
                    "source_retained": retained,
                    "recovered": True,
                    "failure_duration_seconds": time.monotonic() - killed_at,
                    "recovery_seconds": time.monotonic() - killed_at,
                },
            )
        except BaseException as error:
            interrupted = catalog.get_move_job(idempotency_key)
            if retained is None:
                try:
                    retained = self._object_matches(
                        self.drivers[path.source_tier], path, key, index
                    )
                except Exception:
                    retained = None
            classification = classify_job_error(error)
            attempt_failures: list[dict[str, Any]] = []
            if termination_injected:
                attempt_failures.append(
                    {
                        "attempt": 1,
                        "phase": "terminated_worker",
                        "error_type": "WorkerProcessTerminated",
                        "category": "worker_termination",
                        "retryable": True,
                        "source_retained": retained,
                    }
                )
            if resume_attempted or not termination_injected:
                attempt_failures.append(
                    {
                        "attempt": 2 if resume_attempted else int(process_started),
                        "phase": (
                            "post_termination_resume"
                            if resume_attempted
                            else "worker_checkpoint"
                        ),
                        "error_type": type(error).__name__,
                        "category": classification.category.value,
                        "retryable": classification.retryable,
                        "source_retained": retained,
                    }
                )
            evidence = {
                "kind": "worker_termination",
                "injected": termination_injected,
                "mechanism": (
                    "forced process termination after durable TRANSFERRED checkpoint"
                    if termination_actions
                    else None
                ),
                "object_index": index,
                "idempotency_key": idempotency_key,
                "checkpoint": (
                    None if interrupted is None else interrupted.state.value
                ),
                "termination_actions": termination_actions,
                "terminated_exit_code": (
                    terminated_exit_code
                    if terminated_exit_code is not None
                    else process.exitcode if process_started else None
                ),
                "terminated_signal": (
                    terminated_signal
                    if terminated_signal is not None
                    else (
                        _process_stop_result(process, ()).signal_name
                        if process_started
                        else None
                    )
                ),
                "termination_requested": termination_requested,
                "attempts": int(process_started) + int(resume_attempted),
                "bounded_retry_attempts": int(process_started),
                "recovery_attempts": int(resume_attempted),
                "max_attempts": self.config.max_attempts,
                "retry_limit_reached": False,
                "injections": int(termination_injected),
                "after_publication": (
                    interrupted is not None
                    and interrupted.state != MoveJobState.PREPARED
                ),
                "attempt_failures": attempt_failures,
                "source_retained": retained,
                "recovered": False,
                "recovery_seconds": None,
                "failure_duration_seconds": time.monotonic() - started,
                "qualification_error": {
                    "type": type(error).__name__,
                    "category": classification.category.value,
                    "retryable": classification.retryable,
                },
            }
            if failure_sink is not None:
                failure_sink(evidence)
            raise
        finally:
            if process_started and process.is_alive():
                _stop_process(process)

    def _audit(
        self,
        path: PathConfig,
        prefix: str,
        *,
        at_destination: bool,
    ) -> dict[str, Any]:
        assert self.catalog is not None
        catalog = self.catalog
        present_tier = path.destination_tier if at_destination else path.source_tier
        absent_tier = path.source_tier if at_destination else path.destination_tier
        direction = "forward" if at_destination else "reverse"
        started = time.monotonic()

        def audit_one(index: int) -> None:
            key = self._key(prefix, index)
            if not self._object_matches(self.drivers[present_tier], path, key, index):
                raise AssertionError(
                    f"integrity mismatch on {present_tier}:{self.config.bucket}/{key}"
                )
            try:
                self.drivers[absent_tier].stat_object(self.config.bucket, key)
            except FileNotFoundError:
                pass
            else:
                raise AssertionError(
                    f"source-retention cleanup failed on {absent_tier}:"
                    f"{self.config.bucket}/{key}"
                )
            record = catalog.get(self.config.bucket, key)
            if record is None or record.tier != present_tier:
                raise AssertionError(
                    f"catalog placement mismatch for {self.config.bucket}/{key}"
                )
            move = catalog.get_move_job(
                self._idempotency_key(path, direction, index)
            )
            if move is None or move.state != MoveJobState.COMPLETED:
                raise AssertionError(
                    f"move journal is not completed for {self.config.bucket}/{key}"
                )

        for _ in _bounded_results(
            audit_one,
            range(self.config.object_count),
            workers=self.config.workers,
            progress=self._progress(
                path.name, f"audit-{direction}", self.config.object_count
            ),
        ):
            pass
        listed = sum(
            1
            for _ in self.drivers[present_tier].list_objects(
                self.config.bucket, prefix
            )
        )
        absent_listed = sum(
            1
            for _ in self.drivers[absent_tier].list_objects(
                self.config.bucket, prefix
            )
        )
        if listed != self.config.object_count or absent_listed != 0:
            raise AssertionError(
                f"namespace count mismatch: present={listed}, absent={absent_listed}, "
                f"expected={self.config.object_count}"
            )
        return {
            "verified_objects": self.config.object_count,
            "present_tier": present_tier,
            "present_objects": listed,
            "absent_tier": absent_tier,
            "absent_objects": absent_listed,
            "catalog_placements": self.config.object_count,
            "completed_move_jobs": self.config.object_count,
            "checksum_algorithm": "sha256",
            "elapsed_seconds": time.monotonic() - started,
        }

    def _verify_replays(
        self,
        path: PathConfig,
        prefix: str,
        *,
        forward: bool,
    ) -> int:
        assert self.catalog is not None
        direction = "forward" if forward else "reverse"
        source = path.source_tier if forward else path.destination_tier
        destination = path.destination_tier if forward else path.source_tier
        replay_indices = list(range(min(self.config.object_count, len(FAULT_KINDS) + 1)))
        for index in replay_indices:
            key = self._key(prefix, index)
            idempotency_key = self._idempotency_key(path, direction, index)
            before = self.catalog.list_move_job_transitions(idempotency_key)
            generation = self.drivers[destination].object_generation(
                self.config.bucket, key
            )
            result = Mover(
                self.drivers,
                self.catalog,
                owner_id=f"replay:{path.name}:{direction}:{index}",
                lease_seconds=self.config.lease_seconds,
            ).move(
                source,
                destination,
                self.config.bucket,
                key,
                idempotency_key=idempotency_key,
            )
            after = self.catalog.list_move_job_transitions(idempotency_key)
            if len(after) != len(before):
                raise AssertionError("idempotent replay created new transitions")
            if generation != self.drivers[destination].object_generation(
                self.config.bucket, key
            ):
                raise AssertionError("idempotent replay rewrote the destination")
            self._assert_verification(result, self._object_size(index))
        return len(replay_indices)

    def _object_matches(
        self,
        driver: StorageDriver,
        path: PathConfig,
        key: str,
        index: int,
    ) -> bool:
        expected = _payload(
            self.config.seed,
            path.name,
            index,
            self._object_size(index),
        )
        try:
            actual = driver.get_object(self.config.bucket, key)
        except FileNotFoundError:
            return False
        return actual == expected

    def _object_size(self, index: int) -> int:
        return _size_for(index, self.config.size_classes)

    def _size_distribution(self) -> list[dict[str, int]]:
        counts = {item.bytes: 0 for item in self.config.size_classes}
        for index in range(self.config.object_count):
            counts[self._object_size(index)] += 1
        return [
            {"bytes": size, "objects": count, "total_bytes": size * count}
            for size, count in sorted(counts.items())
        ]

    @staticmethod
    def _key(prefix: str, index: int) -> str:
        return f"{prefix}{index:0{_KEY_WIDTH}d}.bin"

    def _idempotency_key(
        self, path: PathConfig, direction: str, index: int
    ) -> str:
        return f"qualification:{self.config.run_id}:{path.name}:{direction}:{index}"

    @staticmethod
    def _assert_verification(result: MoveVerificationResult, size: int) -> None:
        if not result.verified:
            raise AssertionError("mover returned an unverified result")
        if result.source_size != size or result.destination_size != size:
            raise AssertionError("mover verification size does not match the workload")
        if result.source_checksum != result.destination_checksum:
            raise AssertionError("mover verification checksums do not match")

    @staticmethod
    def _phase_summary(
        statistics: _PhaseAccumulator, elapsed_seconds: float, total_bytes: int
    ) -> dict[str, Any]:
        attempts = statistics.attempts
        objects = statistics.objects
        return {
            "objects": objects,
            "bytes": total_bytes,
            "throughput_definition": "logical payload bytes completed per wall-clock second",
            "elapsed_seconds": elapsed_seconds,
            "objects_per_second": objects / elapsed_seconds if elapsed_seconds else 0.0,
            "mib_per_second": (
                total_bytes / (1024 * 1024) / elapsed_seconds
                if elapsed_seconds
                else 0.0
            ),
            "attempts": attempts,
            "retries": attempts - objects,
            "latency_ms": _latency_summary(statistics.latencies_ms),
            "latency_definition": (
                "logical move invocation through durable completion, including retries "
                "and retry backoff but excluding executor queue wait"
            ),
        }

    @staticmethod
    def _progress(
        path_name: str, phase: str, total: int
    ) -> Callable[[int], None] | None:
        if total < 100:
            return None
        interval = max(1, total // 20)

        def report(completed: int) -> None:
            if completed % interval == 0 or completed == total:
                print(
                    f"[{path_name}] {phase}: {completed}/{total}",
                    file=sys.stderr,
                )

        return report


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)


def run_qualification(config: QualificationConfig) -> dict[str, Any]:
    """Run one fresh round-trip campaign and persist its evidence report."""

    return _QualificationRunner(config).run()


def _default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Qualify deterministic POSIX/S3 round-trip moves, recovery, and integrity."
        )
    )
    parser.add_argument("--drivers", type=Path, required=True)
    parser.add_argument("--catalog-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", default=_default_run_id())
    parser.add_argument("--bucket", default="cognistore-qualification")
    parser.add_argument("--object-count", type=int, default=16)
    parser.add_argument(
        "--size",
        dest="size_classes",
        action="append",
        type=parse_size_class,
        help="repeatable BYTES or BYTES:WEIGHT size class",
    )
    parser.add_argument(
        "--path",
        dest="paths",
        action="append",
        type=parse_path,
        required=True,
        help="repeatable NAME:SOURCE_TIER:DESTINATION_TIER path",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=29)
    parser.add_argument("--profile", choices=("reduced", "full"), default="reduced")
    parser.add_argument("--faults", choices=("none", "standard"), default="standard")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retry-base-delay", type=float, default=0.1)
    parser.add_argument("--retry-max-delay", type=float, default=0.2)
    parser.add_argument("--lease-seconds", type=float, default=0.05)
    parser.add_argument("--worker-termination-timeout", type=float, default=30.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = QualificationConfig(
            drivers_path=args.drivers,
            catalog_path=args.catalog_db,
            output_path=args.output,
            run_id=args.run_id,
            bucket=args.bucket,
            object_count=args.object_count,
            size_classes=tuple(
                args.size_classes or FULL_QUALIFYING_SIZE_CLASSES
            ),
            paths=tuple(args.paths),
            workers=args.workers,
            seed=args.seed,
            profile=args.profile,
            faults=args.faults,
            max_attempts=args.max_attempts,
            retry_base_delay=args.retry_base_delay,
            retry_max_delay=args.retry_max_delay,
            lease_seconds=args.lease_seconds,
            worker_termination_timeout=args.worker_termination_timeout,
        )
        report = run_qualification(config)
    except (OSError, ValueError) as error:
        print(f"qualification configuration failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report["summary"], sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
