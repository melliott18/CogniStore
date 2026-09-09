"""Reproducible local SQLite access-history qualification.

Run ``python -m tests.perf.access_benchmark --output report.json``. The fixture is
project-authored synthetic access history; timings measure the catalog DAL, not
storage, HTTP, or PostgreSQL. No production database is accepted or modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sqlite3
import statistics
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cognistore.core.access import (
    ACCESS_KINDS,
    AccessConfig,
    AccessEvent,
    AccessSnapshot,
    AccessWindow,
    access_timestamp,
)
from cognistore.db.catalog import SQLCatalog

DAY = 86400
AS_OF = datetime(2026, 9, 1, tzinfo=timezone.utc)
BUCKET = "access-qualification"


@dataclass(frozen=True)
class BenchmarkConfig:
    event_count: int = 10000
    object_count: int = 100
    query_runs: int = 3
    prune_batch_size: int = 500
    replay_every: int = 10
    history_seconds: int = 60 * DAY
    retention_seconds: int = 30 * DAY

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.event_count < 8:
            raise ValueError("event_count must be at least 8 for boundary fixtures")
        if self.history_seconds <= self.retention_seconds or self.retention_seconds < 7 * DAY:
            raise ValueError("history must exceed retention, which must span at least 7 days")
        if self.prune_batch_size > 10000:
            raise ValueError("prune_batch_size must not exceed 10000")

    @property
    def access_config(self) -> AccessConfig:
        return AccessConfig(
            windows_seconds=tuple(sorted({DAY, 7 * DAY, self.retention_seconds})),
            retention_seconds=self.retention_seconds,
        )


def fixture_events(config: BenchmarkConfig) -> tuple[AccessEvent, ...]:
    """Cover exact window boundaries, future exclusion, samples, and namespaces."""
    boundary_ages = (config.history_seconds, config.retention_seconds, 7 * DAY, DAY, 0, -1)
    events = []
    for index in range(config.event_count):
        age = (
            boundary_ages[index]
            if index < len(boundary_ages)
            else (index * config.history_seconds) // (config.event_count - 1)
        )
        kind = ACCESS_KINDS[index % len(ACCESS_KINDS)]
        events.append(
            AccessEvent.create(
                kind=kind,
                bucket=BUCKET,
                key=None if kind == "list" else f"object-{index // 4 % config.object_count:06d}",
                tier="hot",
                source="api",
                operation_id=f"qualification:{index:08d}",
                correlation_id=f"request:{index:08d}",
                occurred_at=AS_OF - timedelta(seconds=age),
                sample_rate=0.25 if index % 10 == 0 else 1.0,
            )
        )
    return tuple(events)


def reference_snapshot(
    events: tuple[AccessEvent, ...], key: str | None, config: AccessConfig
) -> AccessSnapshot:
    """Independent Python oracle with explicit event-time interval comparisons."""
    oldest = AS_OF - timedelta(seconds=config.retention_seconds)
    relevant = [
        event
        for event in events
        if event.bucket == BUCKET
        and event.key == key
        and oldest < datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00")) <= AS_OF
    ]
    windows = []
    for seconds in config.windows_seconds:
        counts: Counter[str] = Counter()
        estimates: Counter[str] = Counter()
        lower = AS_OF - timedelta(seconds=seconds)
        for event in relevant:
            if datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00")) > lower:
                counts[event.kind] += 1
                estimates[event.kind] += 1 / event.sample_rate
        windows.append(
            AccessWindow(
                seconds,
                tuple(counts[kind] for kind in ACCESS_KINDS),
                tuple(float(estimates[kind]) for kind in ACCESS_KINDS),
            )
        )
    return AccessSnapshot(
        windows=tuple(windows),
        observed_events=len(relevant),
        observed_since=min((event.occurred_at for event in relevant), default=None),
        last_access_at=max((event.occurred_at for event in relevant), default=None),
        minimum_sample_rate=min((event.sample_rate for event in relevant), default=None),
    )


def latency_summary(samples: list[float]) -> dict[str, float | int]:
    if not samples or any(not math.isfinite(value) or value < 0 for value in samples):
        raise ValueError("latency samples must be nonempty, finite, and nonnegative")
    ordered = sorted(samples)
    return {
        "sample_count": len(samples),
        "min": ordered[0],
        "mean": statistics.mean(samples),
        "p50": ordered[math.ceil(0.5 * len(ordered)) - 1],
        "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "max": ordered[-1],
    }


def _database_stats(path: Path) -> dict[str, int | str]:
    with sqlite3.connect(path) as connection:
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        return {
            "events": connection.execute("SELECT COUNT(*) FROM access_events").fetchone()[0],
            "active_events": connection.execute(
                "SELECT COUNT(*) FROM access_events WHERE expired = 0"
            ).fetchone()[0],
            "page_size_bytes": page_size,
            "allocated_bytes": page_size * page_count,
            "free_pages": connection.execute("PRAGMA freelist_count").fetchone()[0],
            "file_bytes": path.stat().st_size,
            "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
            "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
        }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"access qualification failed: {message}")


def _prune_until_idle(
    catalog: SQLCatalog, path: Path, cutoff: str, config: BenchmarkConfig
) -> dict[str, Any]:
    samples = []
    batches = []
    while True:
        before = _database_stats(path)
        started = time.perf_counter()
        processed = catalog.prune_access_events(
            cutoff, limit=config.prune_batch_size, retention_seconds=config.retention_seconds
        )
        samples.append((time.perf_counter() - started) * 1000)
        after = _database_stats(path)
        purged = int(before["events"]) - int(after["events"])
        expired = processed - purged
        _require(0 <= expired <= config.prune_batch_size, "expiration exceeded the batch bound")
        _require(0 <= purged <= config.prune_batch_size, "purge exceeded the batch bound")
        batches.append({"expired": expired, "purged": purged})
        if expired == 0 and purged == 0:
            break
        _require(len(batches) <= 2 * config.event_count, "prune did not make progress")
    return {
        "cutoff": cutoff,
        "expired_events": sum(batch["expired"] for batch in batches),
        "purged_events": sum(batch["purged"] for batch in batches),
        "batches": batches,
        "latency_ms": latency_summary(samples),
    }


def run_benchmark(path: Path, config: BenchmarkConfig | None = None) -> dict[str, Any]:
    """Measure a fresh, disposable SQLite file; refuse to overwrite existing data."""
    config = config or BenchmarkConfig()
    if path.exists():
        raise ValueError("benchmark database path must not already exist")
    events = fixture_events(config)
    payload = "".join(
        json.dumps(asdict(event), sort_keys=True, separators=(",", ":")) + "\n" for event in events
    ).encode("utf-8")
    keys: list[str | None] = [f"object-{index:06d}" for index in range(config.object_count)]
    keys.extend([None, "missing-object"])
    query_config = config.access_config
    wide_config = AccessConfig(
        windows_seconds=(config.history_seconds,), retention_seconds=config.history_seconds
    )
    insert_samples: list[float] = []
    query_samples: list[float] = []
    with SQLCatalog(path) as catalog:
        baseline = _database_stats(path)
        start = time.perf_counter()
        for event in events:
            started = time.perf_counter()
            catalog.append_access_event(event)
            insert_samples.append((time.perf_counter() - started) * 1000)
        insert_seconds = time.perf_counter() - start
        inserted = _database_stats(path)
        _require(inserted["events"] == config.event_count, "unique event row count")
        start = time.perf_counter()
        replay_events = events[:: config.replay_every]
        for event in replay_events:
            replay = AccessEvent.create(
                kind=event.kind,
                bucket=event.bucket,
                key=event.key,
                tier="cold",
                source="driver",
                operation_id=event.operation_id,
                correlation_id="retried-request",
                occurred_at=AS_OF + timedelta(seconds=1),
                sample_rate=event.sample_rate,
            )
            stored = catalog.append_access_event(replay)
            _require(stored == event, "replay must return the first event")
        replay_seconds = time.perf_counter() - start
        _require(_database_stats(path)["events"] == len(events), "replays added rows")
        snapshots = {}
        for key in keys:
            expected = reference_snapshot(events, key, query_config)
            for _ in range(config.query_runs):
                started = time.perf_counter()
                observed = catalog.aggregate_access_events(
                    BUCKET, key, config=query_config, as_of=AS_OF
                )
                query_samples.append((time.perf_counter() - started) * 1000)
                _require(observed == expected, f"window oracle mismatch for {key!r}")
            snapshots[key] = observed
        wide_before = {
            key: catalog.aggregate_access_events(BUCKET, key, config=wide_config, as_of=AS_OF)
            for key in keys
        }
        for key in keys:
            _require(
                wide_before[key] == reference_snapshot(events, key, wide_config),
                "pre-prune wide-window oracle mismatch",
            )
        cutoff = access_timestamp(AS_OF - timedelta(seconds=config.retention_seconds))
        expected_retained = tuple(event for event in events if event.occurred_at >= cutoff)
        expiration = _prune_until_idle(catalog, path, cutoff, config)
        after_prune = _database_stats(path)
        _require(
            expiration["expired_events"] == len(events) - len(expected_retained),
            "expired row count",
        )
        _require(after_prune["active_events"] == len(expected_retained), "active row count")
        # Expired identities remain retry-safe during the additional dedup horizon.
        for event in replay_events:
            _require(catalog.append_access_event(event) == event, "expired replay changed identity")
        _require(_database_stats(path) == after_prune, "expired retries resurrected events")
        wide_after = {}
        for key in keys:
            observed = catalog.aggregate_access_events(
                BUCKET, key, config=query_config, as_of=AS_OF
            )
            _require(observed == snapshots[key], "in-retention aggregates changed after prune")
            wide_after[key] = catalog.aggregate_access_events(
                BUCKET, key, config=wide_config, as_of=AS_OF
            )
            _require(
                wide_after[key] == reference_snapshot(expected_retained, key, wide_config),
                "post-prune wide-window oracle mismatch",
            )
        # Advance the maintenance cutoff by one retention horizon. Old expired
        # identities are now physically removable, while newer history expires.
        purge = _prune_until_idle(catalog, path, access_timestamp(AS_OF), config)
        after_purge = _database_stats(path)
        _require(after_purge["events"] == len(expected_retained), "dedup-horizon purge row count")
        _require(
            after_purge["active_events"]
            == sum(event.occurred_at >= access_timestamp(AS_OF) for event in events),
            "advanced retention active row count",
        )
    growth = int(inserted["allocated_bytes"]) - int(baseline["allocated_bytes"])
    return {
        "schema_version": 1,
        "benchmark": "sqlite-access-history",
        "status": "passed",
        "generated_at": access_timestamp(),
        "environment": {
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
            "sqlite": sqlite3.sqlite_version,
        },
        "workload": {
            **asdict(config),
            "as_of": access_timestamp(AS_OF),
            "windows_seconds": list(query_config.windows_seconds),
            "fixture_sha256": hashlib.sha256(payload).hexdigest(),
            "kind_counts": dict(Counter(event.kind for event in events)),
            "declared_sampled_rows": sum(event.sample_rate < 1 for event in events),
            "sampled_row_rate": 0.25,
            "coordinates_queried": len(keys),
            "future_rows": sum(event.occurred_at > access_timestamp(AS_OF) for event in events),
        },
        "volume": {
            "serialized_jsonl_bytes": len(payload),
            "allocated_growth_bytes": growth,
            "allocated_growth_bytes_per_event": growth / len(events),
            "baseline": baseline,
            "after_insert": inserted,
            "after_prune": after_prune,
            "after_dedup_horizon": after_purge,
        },
        "insert": {
            "seconds": insert_seconds,
            "events_per_second": len(events) / insert_seconds,
            "latency_ms": latency_summary(insert_samples),
            "definition": "one committed DAL append per event, single writer; migration excluded",
        },
        "replays": {
            "attempts_before_prune": len(replay_events),
            "attempts_after_prune": len(replay_events),
            "seconds_before_prune": replay_seconds,
            "added_rows": 0,
            "expired_rows_resurrected": 0,
        },
        "aggregation": {
            "latency_ms": latency_summary(query_samples),
            "definition": "DAL aggregate and detached result; fixture oracle excluded; no warmup",
            "oracle_comparisons": len(query_samples) + 3 * len(keys),
            "in_retention_unchanged_after_prune": True,
            "wide_window_events_before": sum(item.observed_events for item in wide_before.values()),
            "wide_window_events_after": sum(item.observed_events for item in wide_after.values()),
            "example_object": asdict(snapshots[keys[0]]),
            "namespace_listing": asdict(snapshots[None]),
            "missing_object": asdict(snapshots["missing-object"]),
        },
        "retention": {
            "predicate": "occurred_at < cutoff",
            "expiration": expiration,
            "advanced_dedup_horizon": purge,
            "vacuum_performed": False,
        },
        "limitations": [
            "Local SQLite DAL only; no PostgreSQL, concurrent writers, network, or HTTP measurements.",
            "Sampled rows are injected with declared weights; recorder sampling cost is excluded.",
            "Allocated growth includes access indexes and SQLite page slack; JSONL is logical volume.",
            "Pruning frees reusable pages; file truncation and VACUUM are not measured.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--events", type=int, default=10000)
    parser.add_argument("--objects", type=int, default=100)
    parser.add_argument("--query-runs", type=int, default=3)
    parser.add_argument("--prune-batch-size", type=int, default=500)
    args = parser.parse_args()
    config = BenchmarkConfig(
        event_count=args.events,
        object_count=args.objects,
        query_runs=args.query_runs,
        prune_batch_size=args.prune_batch_size,
    )
    with tempfile.TemporaryDirectory(prefix="cognistore-access-benchmark-") as directory:
        report = run_benchmark(Path(directory) / "catalog.sqlite", config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(f"Access qualification passed: {args.output}")


if __name__ == "__main__":
    main()
