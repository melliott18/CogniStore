"""Index freshness measures completion, retry delay, and failure outcomes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from cognistore import observability as telemetry
from cognistore.jobs.models import JobEnvelope
from cognistore.jobs.runtime import AsyncWorker, WorkerConfig
from tests.unit.test_worker_runtime import FakeCoordinator, FakeDelivery, FakeExecution, FakeQueue


def _sample(name, labels=None):
    return telemetry.REGISTRY.get_sample_value(name, labels or {}) or 0


def test_scan_completion_includes_queue_wait_handler_and_retry_delay():
    published = datetime(2026, 9, 17, tzinfo=timezone.utc)
    now = published + timedelta(seconds=200)
    job = JobEnvelope.create("catalog.scan", {})
    before_success = _sample("cognistore_indexing_lag_seconds_sum", {"outcome": "success"})
    before_error = _sample("cognistore_indexing_lag_seconds_sum", {"outcome": "error"})
    before_fast = _sample(
        "cognistore_indexing_lag_seconds_bucket", {"outcome": "success", "le": "300.0"},
    )
    calls = 0

    async def handler(job, context):
        nonlocal calls, now
        calls += 1
        now += timedelta(seconds=40)
        if calls == 1:
            raise TimeoutError("retryable scan")

    async def run():
        nonlocal now
        worker = AsyncWorker(
            FakeQueue(), {"catalog.scan": handler}, clock=lambda: now,
            config=WorkerConfig(heartbeat_interval=0),
        )
        first = FakeDelivery(job, source_published_at=published)
        await worker._process(first)
        assert first.nack_count == 1
        now += timedelta(seconds=60)
        second = FakeDelivery(job, attempt=2, source_published_at=published)
        await worker._process(second)
        assert second.ack_count == 1

    asyncio.run(run())
    assert _sample("cognistore_indexing_lag_seconds_sum", {"outcome": "error"}) == (
        before_error + 240
    )
    assert _sample("cognistore_indexing_lag_seconds_sum", {"outcome": "success"}) == (
        before_success + 340
    )
    assert _sample(
        "cognistore_indexing_lag_seconds_bucket", {"outcome": "success", "le": "300.0"},
    ) == before_fast


@pytest.mark.parametrize("publication", [None, datetime(2026, 9, 17), "not-a-time", "future"])
def test_missing_or_future_scan_timestamp_is_unknown_not_fast(publication):
    now = datetime(2026, 9, 17, tzinfo=timezone.utc)
    if publication == "future":
        publication = now + timedelta(seconds=1)
    before = _sample("cognistore_indexing_lag_unknown_total")
    count = _sample("cognistore_indexing_lag_seconds_count", {"outcome": "success"})

    async def handler(job, context):
        pass

    async def run():
        worker = AsyncWorker(
            FakeQueue(), {"catalog.scan": handler}, clock=lambda: now,
            config=WorkerConfig(heartbeat_interval=0),
        )
        delivery = FakeDelivery(JobEnvelope.create("catalog.scan", {}))
        delivery.source_published_at = publication
        await worker._process(delivery)
        assert delivery.ack_count == 1

    asyncio.run(run())
    assert _sample("cognistore_indexing_lag_unknown_total") == before + 1
    assert _sample("cognistore_indexing_lag_seconds_count", {"outcome": "success"}) == count


def test_cancellation_is_bad_and_non_scan_jobs_are_excluded():
    now = datetime(2026, 9, 17, tzinfo=timezone.utc)
    before = _sample("cognistore_indexing_lag_seconds_count", {"outcome": "error"})
    unknown = _sample("cognistore_indexing_lag_unknown_total")

    async def cancelled(job, context):
        raise asyncio.CancelledError()

    async def run():
        worker = AsyncWorker(
            FakeQueue(), {"catalog.scan": cancelled, "object.move": cancelled},
            clock=lambda: now, config=WorkerConfig(heartbeat_interval=0),
        )
        for job_type in ("catalog.scan", "object.move"):
            delivery = FakeDelivery(
                JobEnvelope.create(job_type, {}), source_published_at=now,
            )
            with pytest.raises(asyncio.CancelledError):
                await worker._process(delivery)
            assert delivery.nack_count == 1

    asyncio.run(run())
    assert _sample("cognistore_indexing_lag_seconds_count", {"outcome": "error"}) == before + 1
    assert _sample("cognistore_indexing_lag_unknown_total") == unknown


def test_index_metric_rejects_nonfinite_values():
    before = _sample("cognistore_indexing_lag_unknown_total")
    for value in (float("inf"), float("nan"), -1):
        telemetry.record_indexing_lag(value, succeeded=True)
    assert _sample("cognistore_indexing_lag_unknown_total") == before + 3


def test_terminal_scan_replay_does_not_claim_successful_indexing():
    # execute=False can represent a terminal dead-lettered run, not only success.
    now = datetime(2026, 9, 17, tzinfo=timezone.utc)
    counts = {
        outcome: _sample("cognistore_indexing_lag_seconds_count", {"outcome": outcome})
        for outcome in ("success", "error")
    }
    events = []

    async def handler(job, context):
        pytest.fail("terminal replay must skip indexing")

    async def run():
        worker = AsyncWorker(
            FakeQueue(), {"catalog.scan": handler}, clock=lambda: now,
            config=WorkerConfig(heartbeat_interval=0),
            coordinator=FakeCoordinator(FakeExecution(events, execute=False), events),
        )
        delivery = FakeDelivery(JobEnvelope.create("catalog.scan", {}), source_published_at=now)
        await worker._process(delivery)
        assert delivery.ack_count == 1

    asyncio.run(run())
    for outcome, count in counts.items():
        assert _sample("cognistore_indexing_lag_seconds_count", {"outcome": outcome}) == count
