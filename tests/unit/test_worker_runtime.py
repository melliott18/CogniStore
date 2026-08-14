from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Callable

from cognistore.jobs.handlers import _run_blocking_safely
from cognistore.jobs.models import BusState, JobEnvelope, JobEnvelopeError, QueueHealth
from cognistore.jobs.runtime import AsyncWorker, WorkerConfig, WorkerState


async def _eventually(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=timeout)


@dataclass
class FakeDelivery:
    job: JobEnvelope
    attempt: int = 1
    stream_sequence: int = 1
    consumer_sequence: int = 1
    ack_error: Exception | None = None
    nack_error: Exception | None = None
    ack_count: int = 0
    nack_count: int = 0
    heartbeat_count: int = 0

    async def ack(self) -> None:
        self.ack_count += 1
        if self.ack_error:
            raise self.ack_error

    async def nack(self) -> None:
        self.nack_count += 1
        if self.nack_error:
            raise self.nack_error

    async def in_progress(self) -> None:
        self.heartbeat_count += 1

class FakeQueue:
    def __init__(self) -> None:
        self.deliveries: asyncio.Queue[FakeDelivery] = asyncio.Queue()
        self.connected = False
        self.ready = True
        self.closed = 0
        self.close_before_settlement = False

    async def connect(self) -> None:
        self.connected = True

    async def enqueue(self, job):  # pragma: no cover - worker-only fake
        raise NotImplementedError

    async def claim(self, timeout: float):
        try:
            return await asyncio.wait_for(self.deliveries.get(), timeout)
        except asyncio.TimeoutError:
            return None

    async def probe(self) -> QueueHealth:
        return QueueHealth(
            state=BusState.CONNECTED if self.connected else BusState.DISCONNECTED,
            ready=self.connected and self.ready,
            jetstream=self.connected and self.ready,
            stream="TEST_JOBS",
            consumer="test-workers",
        )

    async def close(self, *, graceful: bool = True) -> None:
        self.closed += 1
        self.connected = False


def _worker_config(**overrides) -> WorkerConfig:
    values = {
        "fetch_timeout": 0.01,
        "heartbeat_interval": 0,
        "shutdown_grace": 0.2,
        "settlement_timeout": 0.2,
    }
    values.update(overrides)
    return WorkerConfig(**values)


def test_success_is_acked_only_after_handler_returns() -> None:
    async def scenario() -> None:
        queue = FakeQueue()
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(job, context) -> None:
            started.set()
            await release.wait()

        delivery = FakeDelivery(JobEnvelope.create("test.block", {}))
        await queue.deliveries.put(delivery)
        worker = AsyncWorker(
            queue, {"test.block": handler}, config=_worker_config()
        )
        await worker.start()
        await started.wait()
        assert delivery.ack_count == 0
        assert delivery.nack_count == 0

        release.set()
        await _eventually(lambda: delivery.ack_count == 1)
        report = await worker.shutdown()

        assert report.graceful is True
        assert report.completed == 1
        assert delivery.nack_count == 0
        assert queue.closed == 1

    asyncio.run(scenario())


def test_handler_failure_nacks_for_redelivery_with_same_job_context() -> None:
    async def scenario() -> None:
        queue = FakeQueue()
        seen = []

        async def handler(job, context) -> None:
            seen.append((job.job_id, job.correlation_id, context.attempt, context.redelivered))
            raise RuntimeError("temporary failure")

        job = JobEnvelope.create(
            "test.fail", {}, correlation_id="correlation-from-request"
        )
        delivery = FakeDelivery(job, attempt=2)
        await queue.deliveries.put(delivery)
        worker = AsyncWorker(
            queue,
            {"test.fail": handler},
            config=_worker_config(stop_after_jobs=1),
        )
        await worker.start()
        await worker.wait_for_shutdown_request()
        report = await worker.shutdown()

        assert seen == [(job.job_id, "correlation-from-request", 2, True)]
        assert delivery.nack_count == 1
        assert delivery.ack_count == 0
        assert report.nacked == 1

    asyncio.run(scenario())


def test_graceful_shutdown_waits_for_in_flight_job_then_acks() -> None:
    async def scenario() -> None:
        queue = FakeQueue()
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(job, context) -> None:
            started.set()
            await release.wait()

        delivery = FakeDelivery(JobEnvelope.create("test.block", {}))
        await queue.deliveries.put(delivery)
        worker = AsyncWorker(queue, {"test.block": handler}, config=_worker_config())
        await worker.start()
        await started.wait()

        shutdown = asyncio.create_task(worker.shutdown(grace=0.5))
        await _eventually(
            lambda: worker.health_snapshot().state == WorkerState.DRAINING
        )
        assert queue.closed == 0
        assert worker.health_snapshot().state == WorkerState.DRAINING
        assert worker.health_snapshot().ready is False

        release.set()
        report = await shutdown
        assert report.graceful is True
        assert delivery.ack_count == 1
        assert delivery.nack_count == 0

    asyncio.run(scenario())


def test_grace_expiry_cancels_handler_and_nacks_before_close() -> None:
    async def scenario() -> None:
        queue = FakeQueue()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def handler(job, context) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        delivery = FakeDelivery(JobEnvelope.create("test.block", {}))

        async def nack() -> None:
            assert queue.closed == 0
            delivery.nack_count += 1

        delivery.nack = nack  # type: ignore[method-assign]
        await queue.deliveries.put(delivery)
        worker = AsyncWorker(queue, {"test.block": handler}, config=_worker_config())
        await worker.start()
        await started.wait()

        report = await worker.shutdown(grace=0)

        assert cancelled.is_set()
        assert delivery.nack_count == 1
        assert delivery.ack_count == 0
        assert report.unsettled == 0
        assert queue.closed == 1

    asyncio.run(scenario())


def test_shutdown_waits_past_deadline_for_thread_side_effect_boundary() -> None:
    async def scenario() -> None:
        queue = FakeQueue()
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_operation() -> None:
            started.set()
            assert release.wait(timeout=2)
            finished.set()

        async def handler(job, context) -> None:
            await _run_blocking_safely(blocking_operation)

        delivery = FakeDelivery(JobEnvelope.create("test.thread", {}))
        await queue.deliveries.put(delivery)
        worker = AsyncWorker(
            queue,
            {"test.thread": handler},
            config=_worker_config(settlement_timeout=0.02),
        )
        await worker.start()
        await _eventually(started.is_set)

        shutdown = asyncio.create_task(worker.shutdown(grace=0))
        await _eventually(lambda: worker.last_error is not None)
        assert shutdown.done() is False
        assert queue.closed == 0
        assert delivery.ack_count == 0
        assert delivery.nack_count == 0
        assert finished.is_set() is False

        release.set()
        report = await asyncio.wait_for(shutdown, timeout=1)
        assert finished.is_set() is True
        assert delivery.ack_count == 1
        assert delivery.nack_count == 0
        assert queue.closed == 1
        assert report.unsettled == 0

    asyncio.run(scenario())


def test_duplicate_delivery_is_visible_and_not_suppressed() -> None:
    async def scenario() -> None:
        queue = FakeQueue()
        seen = []

        async def handler(job, context) -> None:
            seen.append((job.job_id, context.attempt, context.redelivered))

        job = JobEnvelope.create("test.duplicate", {})
        first = FakeDelivery(job, attempt=1, consumer_sequence=1)
        second = FakeDelivery(job, attempt=2, consumer_sequence=2)
        await queue.deliveries.put(first)
        await queue.deliveries.put(second)
        worker = AsyncWorker(
            queue,
            {"test.duplicate": handler},
            config=_worker_config(stop_after_jobs=2),
        )
        await worker.start()
        await worker.wait_for_shutdown_request()
        await worker.shutdown()

        assert seen == [(job.job_id, 1, False), (job.job_id, 2, True)]
        assert first.ack_count == 1
        assert second.ack_count == 1

    asyncio.run(scenario())


def test_unknown_ack_outcome_is_not_followed_by_nack() -> None:
    async def scenario() -> None:
        queue = FakeQueue()

        async def handler(job, context) -> None:
            return None

        delivery = FakeDelivery(
            JobEnvelope.create("test.ack", {}), ack_error=TimeoutError("unknown")
        )
        await queue.deliveries.put(delivery)
        worker = AsyncWorker(queue, {"test.ack": handler}, config=_worker_config())
        await worker.start()
        await worker.wait_for_shutdown_request()
        report = await worker.shutdown()

        assert worker.state == WorkerState.FAILED
        assert delivery.ack_count == 1
        assert delivery.nack_count == 0
        assert report.graceful is False

    asyncio.run(scenario())


def test_unknown_job_type_fails_closed_without_settling_delivery() -> None:
    async def scenario() -> None:
        queue = FakeQueue()
        delivery = FakeDelivery(JobEnvelope.create("unknown.type", {}))
        await queue.deliveries.put(delivery)
        worker = AsyncWorker(queue, {"known.type": lambda *_: None}, config=_worker_config())
        await worker.start()
        await worker.wait_for_shutdown_request()
        report = await worker.shutdown()

        assert worker.state == WorkerState.FAILED
        assert delivery.ack_count == 0
        assert delivery.nack_count == 0
        assert report.graceful is False

    asyncio.run(scenario())


def test_malformed_claim_fails_worker_closed_instead_of_hot_looping() -> None:
    async def scenario() -> None:
        class MalformedQueue(FakeQueue):
            def __init__(self) -> None:
                super().__init__()
                self.claims = 0

            async def claim(self, timeout: float):
                self.claims += 1
                raise JobEnvelopeError("malformed envelope")

        queue = MalformedQueue()

        async def handler(job, context) -> None:
            return None

        worker = AsyncWorker(queue, {"known.type": handler}, config=_worker_config())
        await worker.start()
        await worker.wait_for_shutdown_request()
        report = await worker.shutdown()

        assert queue.claims == 1
        assert worker.state == WorkerState.FAILED
        assert report.graceful is False

    asyncio.run(scenario())


def test_readiness_requires_running_worker_and_ready_bus() -> None:
    async def scenario() -> None:
        queue = FakeQueue()

        async def handler(job, context) -> None:
            return None

        worker = AsyncWorker(queue, {"test": handler}, config=_worker_config())
        await worker.start()
        assert (await worker.check_readiness()).ready is True

        queue.ready = False
        snapshot = await worker.check_readiness()
        assert snapshot.live is True
        assert snapshot.ready is False
        assert snapshot.bus.ready is False

        await worker.shutdown()
        assert worker.health_snapshot().live is False
        assert worker.health_snapshot().ready is False

    asyncio.run(scenario())
