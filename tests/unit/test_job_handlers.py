from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from cognistore.core.catalog import Catalog
from cognistore.core.move_jobs import MoveJobLeaseError, MoveJobState
from cognistore.core.mover import Mover
from cognistore.core.policy_runner import ActionResult, PolicyRunner
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.core.throughput import (
    ThroughputConfig,
    ThroughputController,
    TierLimits,
)
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.jobs.handlers import (
    CATALOG_SCAN_JOB,
    POLICY_RUN_JOB,
    _run_blocking_safely,
    build_handlers,
    policy_job_payload,
)
from cognistore.jobs.models import JobContext, JobEnvelope


async def _wait_for_thread_event(event: threading.Event) -> None:
    while not event.is_set():
        await asyncio.sleep(0)


def test_blocking_side_effect_ignores_repeated_cancellation_until_safe_boundary() -> None:
    async def scenario() -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_operation() -> str:
            started.set()
            assert release.wait(timeout=2)
            finished.set()
            return "completed"

        task = asyncio.create_task(_run_blocking_safely(blocking_operation))
        await asyncio.wait_for(_wait_for_thread_event(started), timeout=1)

        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        assert finished.is_set() is False

        release.set()
        assert await asyncio.wait_for(task, timeout=1) == "completed"
        assert finished.is_set() is True

    asyncio.run(scenario())


def test_blocking_side_effect_can_propagate_cancellation_after_safe_boundary() -> None:
    async def scenario() -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_operation() -> None:
            started.set()
            assert release.wait(timeout=2)
            finished.set()

        task = asyncio.create_task(
            _run_blocking_safely(
                blocking_operation,
                _propagate_cancellation=True,
            )
        )
        await asyncio.wait_for(_wait_for_thread_event(started), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert finished.is_set() is True

    asyncio.run(scenario())


def _context() -> JobContext:
    return JobContext(
        attempt=1,
        redelivered=False,
        stream_sequence=1,
        consumer_sequence=1,
        shutdown_requested=asyncio.Event(),
    )


def test_catalog_scan_handler_indexes_into_worker_catalog(tmp_path: Path) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    hot.put_object("bucket", "reports/one.txt", b"hello worker")
    catalog = SQLiteCatalog(tmp_path / "catalog.db")
    try:
        handler = build_handlers({"hot": hot}, catalog)[CATALOG_SCAN_JOB]
        job = JobEnvelope.create(
            CATALOG_SCAN_JOB,
            {"tier": "hot", "bucket": "bucket", "prefix": "reports/"},
        )

        asyncio.run(handler(job, _context()))

        record = catalog.get("bucket", "reports/one.txt")
        assert record is not None
        assert record.tier == "hot"
        assert record.metadata["mime"] == "text/plain"
        assert record.metadata["sample_len"] == len(b"hello worker")
    finally:
        catalog.close()


def test_policy_handler_moves_and_tolerates_duplicate_delivery(tmp_path: Path) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    data = b"small"
    warm.put_object("bucket", "one.txt", data)
    catalog = SQLiteCatalog(tmp_path / "catalog.db")
    catalog.upsert("bucket", "one.txt", len(data), "warm")
    try:
        handler = build_handlers({"hot": hot, "warm": warm}, catalog)[POLICY_RUN_JOB]
        job = JobEnvelope.create(
            POLICY_RUN_JOB,
            policy_job_payload(
                bucket="bucket",
                prefix="",
                policy="simple",
                threshold=len(data),
                llm_threshold=None,
                allowed_tiers=("hot", "warm"),
                hot_name_patterns=(),
                warm_name_patterns=(),
                cold_name_patterns=(),
                hot_mime_prefixes=(),
                warm_mime_prefixes=(),
                cold_mime_prefixes=(),
            ),
        )

        asyncio.run(handler(job, _context()))
        # At-least-once delivery is expected. The updated placement makes the
        # same policy pass a no-op rather than attempting the move twice.
        asyncio.run(handler(job, _context()))

        assert hot.get_object("bucket", "one.txt") == data
        assert list(warm.list_objects("bucket")) == []
        record = catalog.get("bucket", "one.txt")
        assert record is not None
        assert record.tier == "hot"
    finally:
        catalog.close()


def test_policy_handler_exposes_unrelated_lanes_from_one_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        drivers = {
            tier: PosixDriver(str(tmp_path / tier))
            for tier in ("hot", "warm", "cold", "archive")
        }
        catalog = Catalog()
        hot_started = threading.Event()
        cold_started = threading.Event()
        release_hot = threading.Event()
        starts: list[str] = []
        actions = [
            ActionResult("bucket", "hot-1", "hot", "warm", "test"),
            ActionResult("bucket", "hot-2", "hot", "warm", "test"),
            ActionResult("bucket", "cold-1", "cold", "archive", "test"),
        ]

        monkeypatch.setattr(
            PolicyRunner,
            "plan_once",
            lambda self, bucket, prefix="", dry_run=False: actions,
        )

        def execute(self, action: ActionResult) -> None:
            starts.append(action.key)
            if action.key.startswith("hot"):
                hot_started.set()
                assert release_hot.wait(timeout=2)
            else:
                cold_started.set()

        monkeypatch.setattr(PolicyRunner, "execute", execute)
        throughput = ThroughputController(
            ThroughputConfig(
                max_queue_depth=4,
                tiers={tier: TierLimits() for tier in drivers},
            )
        )
        handler = build_handlers(
            drivers,
            catalog,
            throughput=throughput,
        )[POLICY_RUN_JOB]
        job = JobEnvelope.create(
            POLICY_RUN_JOB,
            policy_job_payload(
                bucket="bucket",
                prefix="",
                policy="simple",
                threshold=1,
                llm_threshold=None,
                allowed_tiers=tuple(drivers),
                hot_name_patterns=(),
                warm_name_patterns=(),
                cold_name_patterns=(),
                hot_mime_prefixes=(),
                warm_mime_prefixes=(),
                cold_mime_prefixes=(),
            ),
        )

        task = asyncio.create_task(handler(job, _context()))
        try:
            await asyncio.wait_for(_wait_for_thread_event(hot_started), timeout=1)
            await asyncio.wait_for(_wait_for_thread_event(cold_started), timeout=1)
            assert set(starts) == {"hot-1", "cold-1"}
            assert "hot-2" not in starts
        finally:
            release_hot.set()
        await asyncio.wait_for(task, timeout=1)
        assert starts[-1] == "hot-2"

    asyncio.run(scenario())


def test_concurrent_duplicate_policy_deliveries_use_distinct_move_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        hot = PosixDriver(str(tmp_path / "hot"), chunk_size=2)
        warm = PosixDriver(str(tmp_path / "warm"), chunk_size=2)
        data = b"concurrent"
        warm.put_object("bucket", "one.txt", data)
        catalog = SQLiteCatalog(tmp_path / "catalog.db")
        catalog.upsert("bucket", "one.txt", len(data), "warm")
        started = threading.Event()
        release = threading.Event()
        original_put = hot.put_object_stream

        def blocking_put(*args, **kwargs):
            started.set()
            assert release.wait(timeout=2)
            return original_put(*args, **kwargs)

        monkeypatch.setattr(hot, "put_object_stream", blocking_put)
        handler = build_handlers({"hot": hot, "warm": warm}, catalog)[
            POLICY_RUN_JOB
        ]
        job = JobEnvelope.create(
            POLICY_RUN_JOB,
            policy_job_payload(
                bucket="bucket",
                prefix="",
                policy="simple",
                threshold=len(data),
                llm_threshold=None,
                allowed_tiers=("hot", "warm"),
                hot_name_patterns=(),
                warm_name_patterns=(),
                cold_name_patterns=(),
                hot_mime_prefixes=(),
                warm_mime_prefixes=(),
                cold_mime_prefixes=(),
            ),
        )

        first = asyncio.create_task(handler(job, _context()))
        await asyncio.wait_for(_wait_for_thread_event(started), timeout=1)
        second = asyncio.create_task(handler(job, _context()))
        try:
            with pytest.raises(MoveJobLeaseError):
                await asyncio.wait_for(second, timeout=1)
        finally:
            release.set()
        await asyncio.wait_for(first, timeout=1)

        assert hot.get_object("bucket", "one.txt") == data
        assert list(warm.list_objects("bucket")) == []
        catalog.close()

    asyncio.run(scenario())


def test_duplicate_with_published_destination_retries_live_move_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        hot = PosixDriver(str(tmp_path / "hot"), chunk_size=2)
        warm = PosixDriver(str(tmp_path / "warm"), chunk_size=2)
        data = b"published-before-transition"
        warm.put_object("bucket", "one.txt", data)
        catalog = SQLiteCatalog(tmp_path / "catalog.db")
        catalog.upsert("bucket", "one.txt", len(data), "warm")
        transition_started = threading.Event()
        release_transition = threading.Event()
        original_transition = Mover._transition

        def block_first_transfer_transition(
            mover, move, to_state, reason, *, updates=None
        ):
            if move.state == MoveJobState.PREPARED:
                transition_started.set()
                assert release_transition.wait(timeout=2)
            return original_transition(
                mover, move, to_state, reason, updates=updates
            )

        monkeypatch.setattr(Mover, "_transition", block_first_transfer_transition)
        handler = build_handlers({"hot": hot, "warm": warm}, catalog)[
            POLICY_RUN_JOB
        ]
        job = JobEnvelope.create(
            POLICY_RUN_JOB,
            policy_job_payload(
                bucket="bucket",
                prefix="",
                policy="simple",
                threshold=len(data),
                llm_threshold=None,
                allowed_tiers=("hot", "warm"),
                hot_name_patterns=(),
                warm_name_patterns=(),
                cold_name_patterns=(),
                hot_mime_prefixes=(),
                warm_mime_prefixes=(),
                cold_mime_prefixes=(),
            ),
        )

        first = asyncio.create_task(handler(job, _context()))
        await asyncio.wait_for(_wait_for_thread_event(transition_started), timeout=1)
        assert hot.get_object("bucket", "one.txt") == data

        second = asyncio.create_task(handler(job, _context()))
        try:
            with pytest.raises(MoveJobLeaseError):
                await asyncio.wait_for(second, timeout=1)
        finally:
            release_transition.set()
        await asyncio.wait_for(first, timeout=1)

        record = catalog.get("bucket", "one.txt")
        assert record is not None
        assert record.tier == "hot"
        catalog.close()

    asyncio.run(scenario())
