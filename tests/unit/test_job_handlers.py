from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from cognistore.core.sqlite_catalog import SQLiteCatalog
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
