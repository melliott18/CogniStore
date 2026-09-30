#!/usr/bin/env python3
"""Bounded open-loop request recording core for the M5 load adapter.

The built-in CLI executes a deterministic synthetic transport fixture only. It
does not contact or qualify staging. An operational adapter must select objects
from the retained corpus, coordinate the mutable key pool, verify full responses,
and supply background work, reconciliation and private telemetry separately.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

OPERATION_CYCLE = (
    *("get",) * 20, *("head",) * 20, *("catalog",) * 20,
    *("ask",) * 15, *("put_replace",) * 10, *("put_create",) * 5,
    *("delete",) * 5, *("policy_preview",) * 5,
)
SIZE_CYCLE = (*(4096,) * 60, *(65536,) * 30, *(1048576,) * 9, 16777216)
OUTCOMES = frozenset({
    "ok", "http_error", "timeout", "transport_error", "missed",
    "concurrency_limit", "adapter_error",
})
TRANSPORT_OUTCOMES = OUTCOMES - {"missed", "concurrency_limit"}
PROFILES = {"nominal": (10, 16), "burst": (30, 32), "capacity": (10, 16)}
NORMALIZED_CYCLE = tuple("put" if item.startswith("put_") else item
                         for item in OPERATION_CYCLE)
OPERATION_COUNTS = Counter(NORMALIZED_CYCLE)
OCCURRENCE_IN_CYCLE = tuple(NORMALIZED_CYCLE[:i].count(operation)
                            for i, operation in enumerate(NORMALIZED_CYCLE))


@dataclass(frozen=True)
class Request:
    """Public workload selection; no endpoint, token, response or object key."""

    sequence: int
    operation: str
    tenant: str
    size_bytes: int
    put_kind: str | None
    hotspot: bool
    selection: float


def request_for_sequence(sequence: int, seed: str = "m5-pilot-v1") -> Request:
    """Return the deterministic descriptor for an offered arrival.

    Size uses each operation's occurrence count rather than the overall slot:
    every operation sees every size. PUT kinds share one size counter. Selection
    is a stable fraction within the adapter's chosen size/hotspot eligible pool;
    the adapter owns corpus lookup and leases. It must never silently substitute
    another size, tenant or hot/cold cohort when no eligible key exists.
    """
    if type(sequence) is not int or sequence < 0 or not isinstance(seed, str):
        raise ValueError("invalid workload descriptor input")
    cycle, slot = divmod(sequence, len(OPERATION_CYCLE))
    selected = OPERATION_CYCLE[slot]
    operation = NORMALIZED_CYCLE[slot]
    occurrence = cycle * OPERATION_COUNTS[operation] + OCCURRENCE_IN_CYCLE[slot]
    size = SIZE_CYCLE[(occurrence * 37) % len(SIZE_CYCLE)]
    hotspot = (occurrence + occurrence // 100) % 5 != 4
    digest = hashlib.sha256(f"{seed}:{sequence}".encode()).digest()
    # Fifty-three bits keep the fraction strictly below one in IEEE doubles.
    selection = (int.from_bytes(digest[:8], "big") >> 11) / (1 << 53)
    return Request(sequence, operation, f"pilot-{'ab'[(occurrence + occurrence // 100) % 2]}",
                   size, selected.removeprefix("put_") if operation == "put" else None,
                   hotspot, selection)


@dataclass(frozen=True)
class Result:
    success: bool
    outcome: str


@dataclass(frozen=True)
class Settings:
    cohort: str
    duration_seconds: float
    seed: str = "m5-pilot-v1"

    def __post_init__(self) -> None:
        if (self.cohort not in PROFILES or type(self.duration_seconds) not in (int, float)
                or not math.isfinite(self.duration_seconds) or self.duration_seconds <= 0
                or not isinstance(self.seed, str) or len(self.seed.encode()) > 1024):
            raise ValueError("invalid workload settings")

    @property
    def rate(self) -> int:
        return PROFILES[self.cohort][0]

    @property
    def outstanding_limit(self) -> int:
        return PROFILES[self.cohort][1]

    @property
    def offered_count(self) -> int:
        # Exactly the arrivals in [0, duration); the final response may drain later.
        return math.ceil(self.duration_seconds * self.rate)


Transport = Callable[[Request], Awaitable[Result]]
Recorder = Callable[[dict], None]


async def run(
    settings: Settings, transport: Transport, record: Recorder, *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    start_time: float | None = None,
) -> dict:
    """Offer fixed-rate arrivals without queueing or retrying rejected requests.

    The transport is called once for admitted slots and must complete only after
    the entire response and its expected result have been checked. It must obey
    cancellation; transports that block the event loop are unsupported. Each
    attempt expires 30 seconds after its scheduled arrival, including client
    wait. Every slot not admitted is still recorded as
    bad. Records are streamed in completion order and contain no transport data.
    A failed recorder aborts the runner instead of silently discarding evidence.
    ``start_time`` optionally anchors arrivals to an existing monotonic schedule;
    late invocation then records missed slots instead of shifting the window.
    """
    if start_time is not None and (type(start_time) not in (float, int)
                                   or not math.isfinite(start_time) or start_time < 0):
        raise ValueError("invalid workload start time")
    start = clock() if start_time is None else start_time
    started_at = datetime.now(timezone.utc).isoformat()
    pending: dict[asyncio.Task, Request] = {}
    counters: Counter = Counter()
    max_outstanding = 0
    max_puts = 0

    def emit(request: Request, result: Result, elapsed: float,
             actual: float | None = None) -> None:
        row = {
            "sequence": request.sequence,
            "scheduled_seconds": request.sequence / settings.rate,
            "elapsed_seconds": max(0.0, elapsed),
            "operation": request.operation,
            "tenant": request.tenant,
            "cohort": settings.cohort,
            "success": result.success,
            "outcome": result.outcome,
        }
        if request.operation in {"put", "get", "head"}:
            row["size_bytes"] = request.size_bytes
        if request.put_kind:
            row["put_kind"] = request.put_kind
        if actual is not None:
            row["actual_seconds"] = max(0.0, actual)
        record(row)
        counters["recorded"] += 1
        counters["successes" if result.success else "failures"] += 1
        counters[result.outcome] += 1

    async def attempt(request: Request, scheduled: float) -> None:
        began = clock()
        remaining = 30 - (began - scheduled)
        if remaining <= 0:
            emit(request, Result(False, "timeout"), began - scheduled)
            return
        try:
            result = await asyncio.wait_for(transport(request), timeout=remaining)
            if (not isinstance(result, Result) or type(result.success) is not bool
                    or result.outcome not in TRANSPORT_OUTCOMES
                    or result.success != (result.outcome == "ok")):
                result = Result(False, "adapter_error")
        except (TimeoutError, asyncio.TimeoutError):
            result = Result(False, "timeout")
        except Exception:
            # Exception text can contain bearer tokens, URLs or response bodies.
            result = Result(False, "transport_error")
        finished = clock()
        if finished - scheduled >= 30:
            result = Result(False, "timeout")
        emit(request, result, finished - scheduled, finished - began)

    def reap() -> None:
        for task in tuple(pending):
            if task.done():
                del pending[task]
                task.result()  # In particular, never suppress a failed evidence write.

    try:
        for sequence in range(settings.offered_count):
            request = request_for_sequence(sequence, settings.seed)
            scheduled = start + sequence / settings.rate
            remaining = scheduled - clock()
            if remaining > 0:
                await sleep(remaining)
            reap()
            now = clock()
            if now >= scheduled + 1 / settings.rate:
                emit(request, Result(False, "missed"), now - scheduled)
                continue
            put_count = sum(item.operation == "put" for item in pending.values())
            if (len(pending) >= settings.outstanding_limit
                    or (request.operation == "put" and put_count >= 4)):
                emit(request, Result(False, "concurrency_limit"), now - scheduled)
                continue
            pending[asyncio.create_task(attempt(request, scheduled))] = request
            counters["issued"] += 1
            max_outstanding = max(max_outstanding, len(pending))
            max_puts = max(max_puts, put_count + int(request.operation == "put"))
            # Let the transport start even if an injected clock's sleep is immediate.
            await asyncio.sleep(0)
        if pending:
            await asyncio.gather(*pending)
        reap()
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    return {
        "schema_version": 1,
        "scope": "open-loop-workload-component",
        "started_at": started_at,
        "generator_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "seed": settings.seed,
        "seed_sha256": hashlib.sha256(settings.seed.encode()).hexdigest(),
        "cohort": settings.cohort,
        "duration_seconds": settings.duration_seconds,
        "offered_requests_per_second": settings.rate,
        "offered": settings.offered_count,
        "counts": dict(counters),
        "max_outstanding_observed": max_outstanding,
        "max_puts_observed": max_puts,
        "production_qualified": False,
    }


async def fixture_transport(request: Request) -> Result:
    """Predictable local failures exercise denominators; no request is sent."""
    return Result(False, "http_error") if request.sequence % 31 == 30 else Result(True, "ok")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-synthetic-fixture", action="store_true", required=True)
    parser.add_argument("--cohort", choices=PROFILES, default="nominal")
    parser.add_argument("--duration-seconds", type=float, default=10)
    parser.add_argument("--seed", default="m5-pilot-v1")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.resolve() == args.summary.resolve():
        parser.error("output paths must differ")
    try:
        settings = Settings(args.cohort, args.duration_seconds, args.seed)
        if settings.duration_seconds > 60:
            raise ValueError("synthetic fixture duration must be at most 60 seconds")
        # Exclusive creation preserves existing raw evidence. Keep partial data on failure.
        with args.summary.open("x") as summary_file, args.output.open("x") as output:
            def record(row: dict) -> None:
                output.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
                output.flush()

            report = asyncio.run(run(settings, fixture_transport, record))
            report.update({
                "execution_scope": "synthetic-fixture",
                "qualification_eligible": False,
                "limitations": [
                    "No service requests were made; timings describe the fixture runner only.",
                    "A staging adapter, key-pool coordinator and campaign orchestration are required.",
                    "No background jobs, fault injection, integrity, telemetry or alert delivery proof.",
                ],
            })
            summary_file.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError):
        parser.exit(2, "fixture inputs/output unavailable; partial evidence may remain\n")
    print("synthetic fixture completed; production qualification remains incomplete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
