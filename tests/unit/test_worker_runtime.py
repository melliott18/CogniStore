from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from cognistore.core.audit import (
    AuditEventType,
    AuditQuery,
    AuditRetentionPolicy,
    stable_audit_event_id,
)
from cognistore.core.catalog import Catalog
from cognistore.core.throughput import ThroughputSaturatedError
from cognistore.db.catalog import SQLCatalog
from cognistore.jobs.handlers import _run_blocking_safely
from cognistore.jobs.models import (
    DEAD_LETTER_CHAIN_METADATA,
    BusState,
    DeadLetterDisposition,
    DeadLetterReceipt,
    DeadLetterRecord,
    JobContext,
    JobEnvelope,
    JobEnvelopeError,
    QueueHealth,
)
from cognistore.jobs.runtime import (
    AsyncWorker,
    ShutdownReport,
    WorkerConfig,
    WorkerState,
)
from cognistore.jobs.scheduler import (
    SCHEDULE_ID_METADATA,
    SCHEDULE_SCOPE_METADATA,
    SCHEDULED_FOR_METADATA,
    ScheduledRunCoordinator,
    ScheduledRunLockedError,
    SQLiteScheduleStore,
)


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
    source_stream: str = "TEST_JOBS"
    source_published_at: datetime = field(
        default_factory=lambda: datetime(2026, 8, 17, tzinfo=timezone.utc)
    )
    source_consumer: str = "test-workers"
    raw_data: bytes | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    nack_delays: list[float | None] = field(default_factory=list)
    events: list[str] | None = None

    def __post_init__(self) -> None:
        if self.raw_data is None:
            self.raw_data = self.job.to_bytes()

    async def ack(self) -> None:
        if self.events is not None:
            self.events.append("ack")
        self.ack_count += 1
        if self.ack_error:
            raise self.ack_error

    async def nack(self, delay: float | None = None) -> None:
        if self.events is not None:
            self.events.append(f"nack:{delay}")
        self.nack_count += 1
        self.nack_delays.append(delay)
        if self.nack_error:
            raise self.nack_error

    async def in_progress(self) -> None:
        self.heartbeat_count += 1


@dataclass
class FakeMalformedDelivery:
    raw_data: bytes
    headers: Mapping[str, str] = field(default_factory=dict)
    attempt: int = 1
    source_stream: str = "TEST_JOBS"
    source_published_at: datetime = field(
        default_factory=lambda: datetime(2026, 8, 17, tzinfo=timezone.utc)
    )
    source_consumer: str = "test-workers"
    stream_sequence: int = 9
    consumer_sequence: int = 9
    ack_count: int = 0
    nack_count: int = 0
    heartbeat_count: int = 0

    @property
    def job(self) -> JobEnvelope:
        return JobEnvelope.from_bytes(self.raw_data)

    async def ack(self) -> None:
        self.ack_count += 1

    async def nack(self, delay: float | None = None) -> None:
        self.nack_count += 1

    async def in_progress(self) -> None:
        self.heartbeat_count += 1


class FakeQueue:
    def __init__(self, events: list[str] | None = None) -> None:
        self.deliveries: asyncio.Queue[FakeDelivery] = asyncio.Queue()
        self.events = events
        self.connected = False
        self.ready = True
        self.closed = 0
        self.close_before_settlement = False
        self.dead_letters: list[DeadLetterRecord] = []
        self.dead_letter_error: Exception | None = None

    async def connect(self) -> None:
        self.connected = True

    async def enqueue(self, job, *, message_id=None):  # pragma: no cover - worker-only fake
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

    async def publish_dead_letter(self, record: DeadLetterRecord) -> DeadLetterReceipt:
        if self.dead_letter_error is not None:
            raise self.dead_letter_error
        if self.events is not None:
            self.events.append("publish_dead_letter")
        self.dead_letters.append(record)
        return DeadLetterReceipt(
            dead_letter_id=record.dead_letter_id,
            stream="TEST_JOBS_DLQ",
            sequence=len(self.dead_letters),
        )

    async def get_dead_letter(self, dead_letter_id):  # pragma: no cover - not used
        raise NotImplementedError

    async def redrive_dead_letter(self, dead_letter_id):  # pragma: no cover - not used
        raise NotImplementedError

    async def close(self, *, graceful: bool = True) -> None:
        self.closed += 1
        self.connected = False


class FakeExecution:
    def __init__(
        self,
        events: list[str],
        *,
        execute: bool = True,
        heartbeat_interval: float = 0.0,
        renew_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.execute = execute
        self.heartbeat_interval = heartbeat_interval
        self.renew_error = renew_error

    async def renew(self) -> None:
        self.events.append("renew")
        if self.renew_error is not None:
            error = self.renew_error
            self.renew_error = None
            raise error

    async def retry(self) -> None:
        self.events.append("retry")

    async def succeed(self) -> None:
        self.events.append("succeed")

    async def dead_letter(self, disposition: DeadLetterDisposition) -> None:
        self.events.append(f"dead_letter:{disposition.value}")


class FakeCoordinator:
    def __init__(self, execution: FakeExecution, events: list[str]) -> None:
        self.execution = execution
        self.events = events
        self.begin_calls: list[tuple[JobEnvelope, int]] = []

    async def begin(self, job: JobEnvelope, context: JobContext) -> FakeExecution:
        self.events.append("begin")
        self.begin_calls.append((job, context.attempt))
        return self.execution


def _worker_config(**overrides) -> WorkerConfig:
    values = {
        "fetch_timeout": 0.01,
        "heartbeat_interval": 0,
        "shutdown_grace": 0.2,
        "settlement_timeout": 0.2,
        "retry_jitter": 0,
    }
    values.update(overrides)
    return WorkerConfig(**values)


def test_worker_config_defaults_to_one_in_flight_and_rejects_invalid_capacity() -> None:
    assert WorkerConfig().max_in_flight == 1

    for invalid in (0, -1, 1.5, True):
        try:
            WorkerConfig(max_in_flight=invalid)  # type: ignore[arg-type]
        except ValueError as exc:
            assert str(exc) == "max_in_flight must be a positive integer"
        else:  # pragma: no cover - defensive assertion
            raise AssertionError(f"accepted invalid max_in_flight={invalid!r}")


def test_worker_runs_up_to_capacity_and_replenishes_after_completion() -> None:
    async def scenario() -> None:
        queue = FakeQueue()
        releases = [asyncio.Event() for _ in range(4)]
        started: list[int] = []

        async def handler(job, context) -> None:
            index = job.payload["index"]
            assert isinstance(index, int)
            started.append(index)
            await releases[index].wait()

        deliveries = [
            FakeDelivery(
                JobEnvelope.create("test.concurrent", {"index": index}),
                stream_sequence=index + 1,
                consumer_sequence=index + 1,
            )
            for index in range(4)
        ]
        for delivery in deliveries:
            await queue.deliveries.put(delivery)

        worker = AsyncWorker(
            queue,
            {"test.concurrent": handler},
            config=_worker_config(max_in_flight=2, stop_after_jobs=4),
        )
        await worker.start()
        await _eventually(lambda: len(started) == 2)

        snapshot = worker.health_snapshot()
        assert started == [0, 1]
        assert queue.deliveries.qsize() == 2
        assert snapshot.in_flight == 2
        assert snapshot.max_in_flight == 2
        assert snapshot.available_capacity == 0
        assert snapshot.saturated is True
        assert snapshot.saturation_events == 1

        releases[0].set()
        await _eventually(lambda: len(started) == 3)
        assert started == [0, 1, 2]
        assert worker.health_snapshot().in_flight == 2
        assert worker.health_snapshot().saturation_events == 2

        for release in releases:
            release.set()
        await worker.wait_for_shutdown_request()
        report = await worker.shutdown()

        assert report.completed == 4
        assert all(delivery.ack_count == 1 for delivery in deliveries)
        assert queue.deliveries.empty()

    asyncio.run(scenario())


def test_stop_after_jobs_reserves_capacity_without_overclaiming() -> None:
    async def scenario() -> None:
        queue = FakeQueue()
        release = asyncio.Event()
        started: list[str] = []

        async def handler(job, context) -> None:
            started.append(job.job_id)
            await release.wait()

        deliveries = [
            FakeDelivery(
                JobEnvelope.create("test.bounded-stop", {}),
                stream_sequence=index + 1,
                consumer_sequence=index + 1,
            )
            for index in range(4)
        ]
        for delivery in deliveries:
            await queue.deliveries.put(delivery)

        worker = AsyncWorker(
            queue,
            {"test.bounded-stop": handler},
            config=_worker_config(max_in_flight=4, stop_after_jobs=2),
        )
        await worker.start()
        await _eventually(lambda: len(started) == 2)
        await asyncio.sleep(0)

        assert queue.deliveries.qsize() == 2
        assert worker.health_snapshot().in_flight == 2
        assert worker.health_snapshot().saturated is False

        release.set()
        await worker.wait_for_shutdown_request()
        report = await worker.shutdown()

        assert report.completed == 2
        assert [delivery.ack_count for delivery in deliveries] == [1, 1, 0, 0]
        assert queue.deliveries.qsize() == 2

    asyncio.run(scenario())


def test_snapshot_accumulates_time_spent_at_capacity() -> None:
    async def scenario() -> None:
        queue = FakeQueue()
        started = asyncio.Event()
        release = asyncio.Event()
        now = [10.0]

        async def handler(job, context) -> None:
            started.set()
            await release.wait()

        delivery = FakeDelivery(JobEnvelope.create("test.saturation", {}))
        await queue.deliveries.put(delivery)
        worker = AsyncWorker(
            queue,
            {"test.saturation": handler},
            config=_worker_config(max_in_flight=1),
            monotonic=lambda: now[0],
        )
        await worker.start()
        await started.wait()

        now[0] = 12.5
        saturated = worker.health_snapshot()
        assert saturated.saturated is True
        assert saturated.saturation_seconds == 2.5
        assert saturated.to_dict()["worker"]["saturation_seconds"] == 2.5

        release.set()
        await _eventually(lambda: worker.health_snapshot().in_flight == 0)
        snapshot = worker.health_snapshot()
        assert snapshot.saturated is False
        assert snapshot.available_capacity == 1
        assert snapshot.saturation_events == 1
        assert snapshot.saturation_seconds == 2.5
        await worker.shutdown()

    asyncio.run(scenario())


def test_large_backlog_is_never_prefetched_beyond_worker_capacity() -> None:
    class LazyBacklogQueue(FakeQueue):
        def __init__(self, backlog: int) -> None:
            super().__init__()
            self.remaining = backlog
            self.claims = 0

        async def claim(self, timeout: float):
            if self.remaining <= 0:
                await asyncio.sleep(timeout)
                return None
            self.remaining -= 1
            self.claims += 1
            return FakeDelivery(
                JobEnvelope.create("test.block", {"index": self.claims}),
                stream_sequence=self.claims,
                consumer_sequence=self.claims,
            )

    async def scenario() -> None:
        queue = LazyBacklogQueue(10_000)
        started = 0
        never_release = asyncio.Event()

        async def handler(job, context) -> None:
            nonlocal started
            started += 1
            await never_release.wait()

        worker = AsyncWorker(
            queue,
            {"test.block": handler},
            config=_worker_config(max_in_flight=8),
        )
        await worker.start()
        await _eventually(lambda: started == 8)
        await asyncio.sleep(0)

        snapshot = worker.health_snapshot()
        assert queue.claims == 8
        assert queue.remaining == 9_992
        assert snapshot.in_flight == 8
        assert snapshot.available_capacity == 0
        assert snapshot.saturated is True

        report = await worker.shutdown(grace=0)
        assert report.nacked == 8

    asyncio.run(scenario())


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


async def _run_coordinator_case(
    *,
    execute: bool = True,
    error: Exception | None = None,
    attempt: int = 1,
) -> tuple[list[str], JobEnvelope, FakeDelivery, FakeCoordinator, ShutdownReport]:
    events: list[str] = []
    queue = FakeQueue(events)
    job = JobEnvelope.create("test.coordinated", {})
    delivery = FakeDelivery(job, attempt=attempt, events=events)
    coordinator = FakeCoordinator(FakeExecution(events, execute=execute), events)

    async def handler(delivered_job, context) -> None:
        assert delivered_job == job
        events.append("handler")
        if not execute:
            raise AssertionError("settled scheduled run must skip its handler")
        if error is not None:
            raise error

    await queue.deliveries.put(delivery)
    worker = AsyncWorker(
        queue,
        {job.job_type: handler},
        config=_worker_config(stop_after_jobs=1),
        coordinator=coordinator,
    )
    await worker.start()
    await worker.wait_for_shutdown_request()
    report = await worker.shutdown()
    return events, job, delivery, coordinator, report


def test_coordinator_succeeds_before_source_ack() -> None:
    events, job, _, coordinator, report = asyncio.run(_run_coordinator_case())

    assert coordinator.begin_calls == [(job, 1)]
    assert events == ["begin", "handler", "succeed", "ack"]
    assert report.completed == 1


def test_coordinator_retries_before_delayed_nack() -> None:
    events, _, delivery, _, report = asyncio.run(
        _run_coordinator_case(error=RuntimeError("retry me"))
    )

    assert events == ["begin", "handler", "retry", "nack:1.0"]
    assert delivery.nack_delays == [1.0]
    assert report.retried == 1


def test_coordinator_dead_letters_before_source_ack() -> None:
    events, _, _, _, report = asyncio.run(
        _run_coordinator_case(error=ValueError("terminal job"))
    )

    assert events == [
        "begin",
        "handler",
        "publish_dead_letter",
        "dead_letter:terminal",
        "ack",
    ]
    assert report.dead_lettered == 1


def test_coordinator_completed_redelivery_skips_handler_and_acks() -> None:
    events, job, _, coordinator, report = asyncio.run(
        _run_coordinator_case(execute=False, attempt=2)
    )

    assert coordinator.begin_calls == [(job, 2)]
    assert events == ["begin", "ack"]
    assert report.completed == 1


def test_missing_scheduled_state_fails_closed_without_source_settlement(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        queue = FakeQueue()
        store = SQLiteScheduleStore(tmp_path / "wrong-scheduler.db")
        handled = False
        job = JobEnvelope.create(
            "test.scheduled",
            {},
            metadata={
                SCHEDULE_ID_METADATA: "missing-schedule",
                SCHEDULE_SCOPE_METADATA: "missing-scope",
                SCHEDULED_FOR_METADATA: "2026-08-23T12:00:00.000000Z",
            },
        )
        delivery = FakeDelivery(job)

        async def handler(delivered_job, context) -> None:
            nonlocal handled
            handled = True

        await queue.deliveries.put(delivery)
        worker = AsyncWorker(
            queue,
            {job.job_type: handler},
            config=_worker_config(stop_after_jobs=1),
            coordinator=ScheduledRunCoordinator(store, lease_seconds=30),
        )
        try:
            await worker.start()
            await asyncio.wait_for(worker.wait_for_shutdown_request(), timeout=1)
            report = await worker.shutdown(grace=0)
        finally:
            store.close()

        assert worker.state == WorkerState.FAILED
        assert handled is False
        assert delivery.ack_count == 0
        assert delivery.nack_count == 0
        assert queue.dead_letters == []
        assert report.completed == 0
        assert report.nacked == 0
        assert report.dead_lettered == 0

    asyncio.run(scenario())


def test_execution_renew_failure_cancels_handler_without_settling_source() -> None:
    async def scenario() -> None:
        events: list[str] = []
        queue = FakeQueue(events)
        job = JobEnvelope.create("test.coordinated", {})
        delivery = FakeDelivery(job, events=events)
        execution = FakeExecution(
            events,
            heartbeat_interval=0.01,
            renew_error=RuntimeError("execution lease was lost"),
        )
        coordinator = FakeCoordinator(execution, events)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def handler(delivered_job, context) -> None:
            events.append("handler")
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("handler_cancelled")
                cancelled.set()
                raise

        await queue.deliveries.put(delivery)
        worker = AsyncWorker(
            queue,
            {job.job_type: handler},
            config=_worker_config(),
            coordinator=coordinator,
        )
        await worker.start()
        await started.wait()
        await asyncio.wait_for(worker.wait_for_shutdown_request(), timeout=1)
        try:
            await asyncio.wait_for(cancelled.wait(), timeout=0.5)
            await _eventually(lambda: worker.health_snapshot().in_flight == 0)
        finally:
            report = await worker.shutdown(grace=0)

        assert worker.state == WorkerState.FAILED
        assert events == [
            "begin",
            "handler",
            "renew",
            "handler_cancelled",
            "retry",
        ]
        assert delivery.ack_count == 0
        assert delivery.nack_count == 0
        assert queue.dead_letters == []
        assert report.completed == 0
        assert report.nacked == 0
        assert report.dead_lettered == 0

    asyncio.run(scenario())


def test_blocking_handler_keeps_renewing_after_shutdown_cancels_its_drain() -> None:
    async def scenario() -> None:
        events: list[str] = []
        queue = FakeQueue(events)
        job = JobEnvelope.create("test.blocking-coordinated", {})
        delivery = FakeDelivery(job, events=events)
        execution = FakeExecution(
            events,
            heartbeat_interval=0.01,
            renew_error=RuntimeError("injected heartbeat failure"),
        )
        coordinator = FakeCoordinator(execution, events)
        blocking_started = threading.Event()
        release_boundary = threading.Event()
        boundary_reached = threading.Event()

        def blocking_operation() -> None:
            blocking_started.set()
            assert release_boundary.wait(timeout=2)
            boundary_reached.set()

        async def handler(delivered_job, context) -> None:
            events.append("handler")
            await _run_blocking_safely(blocking_operation)

        await queue.deliveries.put(delivery)
        worker = AsyncWorker(
            queue,
            {job.job_type: handler},
            config=_worker_config(),
            coordinator=coordinator,
        )
        await worker.start()
        await _eventually(blocking_started.is_set)
        await asyncio.wait_for(worker.wait_for_shutdown_request(), timeout=1)
        await _eventually(lambda: events.count("renew") >= 2)

        renewals_before_shutdown = events.count("renew")
        shutdown = asyncio.create_task(worker.shutdown(grace=0))
        try:
            await asyncio.sleep(0.05)
            assert shutdown.done() is False
            assert boundary_reached.is_set() is False
            assert events.count("renew") >= renewals_before_shutdown + 2
            assert delivery.ack_count == 0
            assert delivery.nack_count == 0
            assert queue.dead_letters == []
        finally:
            release_boundary.set()

        report = await asyncio.wait_for(shutdown, timeout=1)
        assert boundary_reached.is_set() is True
        assert worker.state == WorkerState.FAILED
        assert delivery.ack_count == 0
        assert delivery.nack_count == 0
        assert queue.dead_letters == []
        assert report.completed == 0
        assert report.nacked == 0
        assert report.dead_lettered == 0

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
        assert delivery.nack_delays == [2.0]
        assert delivery.ack_count == 0
        assert report.nacked == 1
        assert report.retried == 1

    asyncio.run(scenario())


def test_failure_and_retry_audits_are_durable_before_nack_and_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "worker-audit.db"
    audit_retention = AuditRetentionPolicy(max_age_seconds=None)
    occurred_at = datetime(2026, 8, 29, 12, 30, tzinfo=timezone.utc)

    async def scenario() -> tuple[str, set[str]]:
        audit_catalog = SQLCatalog(
            database,
            audit_retention=audit_retention,
        )
        queue = FakeQueue()
        job = JobEnvelope.create(
            "test.audited-retry",
            {"password": "payload-secret-must-not-be-copied"},
            correlation_id="request-audited-retry",
        )
        delivery = FakeDelivery(job, stream_sequence=41, consumer_sequence=17)
        original_nack = delivery.nack
        observed_before_nack: set[str] = set()

        async def handler(delivered_job, context) -> None:
            raise TimeoutError(
                "password=raw-exception-secret "
                "https://worker:credential@example.invalid/private"
            )

        async def nack_after_audit(delay: float | None = None) -> None:
            events = audit_catalog.list_audit_events(AuditQuery(job_id=job.job_id))
            observed_before_nack.update(event.event_type for event in events)
            assert observed_before_nack == {
                AuditEventType.JOB_FAILURE.value,
                AuditEventType.JOB_RETRY.value,
            }
            await original_nack(delay)

        delivery.nack = nack_after_audit  # type: ignore[method-assign]
        await queue.deliveries.put(delivery)
        worker = AsyncWorker(
            queue,
            {job.job_type: handler},
            config=_worker_config(stop_after_jobs=1),
            clock=lambda: occurred_at,
            audit_catalog=audit_catalog,
        )
        try:
            await worker.start()
            await worker.wait_for_shutdown_request()
            report = await worker.shutdown()

            assert report.retried == 1
            assert delivery.nack_count == 1
            assert delivery.ack_count == 0
        finally:
            audit_catalog.close()
        return job.job_id, observed_before_nack

    job_id, observed_before_nack = asyncio.run(scenario())
    assert observed_before_nack == {
        AuditEventType.JOB_FAILURE.value,
        AuditEventType.JOB_RETRY.value,
    }

    with SQLCatalog(
        database,
        migrate=False,
        audit_retention=audit_retention,
    ) as restarted_catalog:
        events = restarted_catalog.list_audit_events(AuditQuery(job_id=job_id))

    assert len(events) == 2
    by_type = {event.event_type: event for event in events}
    failure = by_type[AuditEventType.JOB_FAILURE.value]
    retry = by_type[AuditEventType.JOB_RETRY.value]
    expected_failure_id = stable_audit_event_id(
        "worker-delivery",
        AuditEventType.JOB_FAILURE.value,
        job_id,
        "TEST_JOBS",
        "41",
        "test-workers",
        "17",
        "1",
    )
    expected_retry_id = stable_audit_event_id(
        "worker-delivery",
        AuditEventType.JOB_RETRY.value,
        job_id,
        "TEST_JOBS",
        "41",
        "test-workers",
        "17",
        "1",
    )
    assert failure.event_id == expected_failure_id
    assert retry.event_id == expected_retry_id
    assert failure.correlation_id == "request-audited-retry"
    assert retry.correlation_id == failure.correlation_id
    assert retry.causation_id == failure.event_id
    assert failure.outcome == "failed"
    assert retry.outcome == "retrying"
    assert failure.expires_at is None
    assert retry.expires_at is None
    assert failure.details["exception_type"] == "builtins.TimeoutError"
    assert failure.details["category"] == "timeout"
    assert retry.details["retry_delay_seconds"] == 1.0
    assert retry.details["next_attempt"] == 2
    persisted_details = json.dumps(
        [failure.details, retry.details],
        sort_keys=True,
    )
    assert "payload-secret-must-not-be-copied" not in persisted_details
    assert "raw-exception-secret" not in persisted_details
    assert "credential" not in persisted_details
    assert "exception_message" not in failure.details
    assert "traceback" not in failure.details


def test_dead_letter_audit_is_durable_before_source_ack() -> None:
    async def scenario() -> None:
        audit_catalog = Catalog(
            audit_retention=AuditRetentionPolicy(max_age_seconds=None)
        )
        queue = FakeQueue()
        occurred_at = datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc)
        job = JobEnvelope.create(
            "test.audited-terminal",
            {},
            correlation_id="request-audited-terminal",
        )
        delivery = FakeDelivery(job, stream_sequence=52, consumer_sequence=23)

        async def handler(delivered_job, context) -> None:
            raise ValueError(
                "token=terminal-exception-secret and traceback must stay private"
            )

        async def ack_after_audit() -> None:
            events = audit_catalog.list_audit_events(AuditQuery(job_id=job.job_id))
            assert {event.event_type for event in events} == {
                AuditEventType.JOB_FAILURE.value,
                AuditEventType.JOB_DEAD_LETTERED.value,
            }
            assert len(queue.dead_letters) == 1
            delivery.ack_count += 1

        delivery.ack = ack_after_audit  # type: ignore[method-assign]
        await queue.deliveries.put(delivery)
        worker = AsyncWorker(
            queue,
            {job.job_type: handler},
            config=_worker_config(stop_after_jobs=1),
            clock=lambda: occurred_at,
            audit_catalog=audit_catalog,
        )
        await worker.start()
        await worker.wait_for_shutdown_request()
        report = await worker.shutdown()

        assert report.dead_lettered == 1
        assert delivery.ack_count == 1
        assert delivery.nack_count == 0
        events = audit_catalog.list_audit_events(AuditQuery(job_id=job.job_id))
        by_type = {event.event_type: event for event in events}
        failure = by_type[AuditEventType.JOB_FAILURE.value]
        dead_lettered = by_type[AuditEventType.JOB_DEAD_LETTERED.value]
        assert dead_lettered.causation_id == failure.event_id
        assert dead_lettered.details["disposition"] == "terminal"
        assert dead_lettered.details["dead_letter_id"] == (
            queue.dead_letters[0].dead_letter_id
        )
        assert dead_lettered.details["dead_letter_stream"] == "TEST_JOBS_DLQ"
        assert dead_lettered.details["dead_letter_sequence"] == 1
        persisted_details = json.dumps(
            [failure.details, dead_lettered.details],
            sort_keys=True,
        )
        assert "terminal-exception-secret" not in persisted_details
        assert "exception_message" not in failure.details
        assert "traceback" not in failure.details

    asyncio.run(scenario())


def test_audit_failure_leaves_source_unsettled_and_fails_worker_closed() -> None:
    class FailingAuditCatalog:
        def append_audit_event(self, event):
            raise OSError("audit store unavailable")

    async def scenario() -> None:
        queue = FakeQueue()
        job = JobEnvelope.create("test.audit-unavailable", {})
        delivery = FakeDelivery(job)
        coordinator_events: list[str] = []
        coordinator = FakeCoordinator(
            FakeExecution(coordinator_events),
            coordinator_events,
        )

        async def handler(delivered_job, context) -> None:
            raise TimeoutError("handler failed")

        await queue.deliveries.put(delivery)
        worker = AsyncWorker(
            queue,
            {job.job_type: handler},
            config=_worker_config(),
            coordinator=coordinator,
            audit_catalog=FailingAuditCatalog(),  # type: ignore[arg-type]
        )
        await worker.start()
        await worker.wait_for_shutdown_request()
        report = await worker.shutdown()

        assert worker.state == WorkerState.FAILED
        assert report.graceful is False
        assert delivery.ack_count == 0
        assert delivery.nack_count == 0
        assert queue.dead_letters == []
        assert coordinator_events == ["begin", "retry"]

    asyncio.run(scenario())


def test_local_saturation_never_exhausts_a_healthy_delivery() -> None:
    async def scenario() -> None:
        queue = FakeQueue()

        async def handler(job, context) -> None:
            raise ThroughputSaturatedError("local admission queue is full")

        delivery = FakeDelivery(
            JobEnvelope.create("test.saturated", {}),
            attempt=20,
        )
        await queue.deliveries.put(delivery)
        worker = AsyncWorker(
            queue,
            {"test.saturated": handler},
            config=_worker_config(max_attempts=1, stop_after_jobs=1),
        )
        await worker.start()
        await worker.wait_for_shutdown_request()
        report = await worker.shutdown()

        assert delivery.nack_count == 1
        assert delivery.ack_count == 0
        assert delivery.nack_delays == [30.0]
        assert report.retried == 1
        assert report.dead_lettered == 0
        assert queue.dead_letters == []

    asyncio.run(scenario())


def test_deferred_coordination_never_exhausts_a_delivery() -> None:
    async def scenario() -> None:
        queue = FakeQueue()

        async def handler(job, context) -> None:
            raise ScheduledRunLockedError("scheduled scope is still leased")

        delivery = FakeDelivery(
            JobEnvelope.create("test.deferred", {}),
            attempt=20,
        )
        await queue.deliveries.put(delivery)
        worker = AsyncWorker(
            queue,
            {"test.deferred": handler},
            config=_worker_config(max_attempts=1, stop_after_jobs=1),
        )
        await worker.start()
        await worker.wait_for_shutdown_request()
        report = await worker.shutdown()

        assert delivery.nack_count == 1
        assert delivery.nack_delays == [30.0]
        assert delivery.ack_count == 0
        assert queue.dead_letters == []
        assert report.retried == 1
        assert report.dead_lettered == 0

    asyncio.run(scenario())


def test_transient_retry_keeps_job_identity_and_cumulative_attempt() -> None:
    async def scenario() -> None:
        queue = FakeQueue()
        seen = []
        job = JobEnvelope.create(
            "test.retry",
            {"move": "hot-to-warm"},
            correlation_id="request-retry",
        )

        async def handler(delivered_job, context) -> None:
            seen.append(
                (
                    delivered_job.job_id,
                    delivered_job.correlation_id,
                    context.attempt,
                    context.cumulative_attempt,
                )
            )
            if context.attempt == 1:
                raise TimeoutError("one-shot backend timeout")

        first = FakeDelivery(job, attempt=1, consumer_sequence=1)
        second = FakeDelivery(job, attempt=2, consumer_sequence=2)
        await queue.deliveries.put(first)
        await queue.deliveries.put(second)
        worker = AsyncWorker(
            queue,
            {"test.retry": handler},
            config=_worker_config(stop_after_jobs=2),
        )
        await worker.start()
        await worker.wait_for_shutdown_request()
        report = await worker.shutdown()

        assert seen == [
            (job.job_id, "request-retry", 1, 1),
            (job.job_id, "request-retry", 2, 2),
        ]
        assert first.nack_delays == [1.0]
        assert first.ack_count == 0
        assert second.ack_count == 1
        assert report.retried == 1
        assert report.completed == 1
        assert queue.dead_letters == []

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


def test_unknown_job_type_is_dead_lettered_and_worker_keeps_running() -> None:
    async def scenario() -> None:
        queue = FakeQueue()
        delivery = FakeDelivery(JobEnvelope.create("unknown.type", {}))
        await queue.deliveries.put(delivery)
        async def handler(job, context) -> None:
            return None

        worker = AsyncWorker(
            queue,
            {"known.type": handler},
            config=_worker_config(stop_after_jobs=1),
        )
        await worker.start()
        await worker.wait_for_shutdown_request()
        report = await worker.shutdown()

        assert worker.state == WorkerState.STOPPED
        assert delivery.ack_count == 1
        assert delivery.nack_count == 0
        assert report.graceful is True
        assert report.dead_lettered == 1
        assert queue.dead_letters[0].job == delivery.job
        assert queue.dead_letters[0].disposition.value == "terminal"

    asyncio.run(scenario())


def test_retry_exhaustion_dead_letters_complete_diagnostics_before_ack() -> None:
    async def scenario() -> None:
        queue = FakeQueue()

        async def handler(job, context) -> None:
            raise TimeoutError("backend timed out")

        job = JobEnvelope.create(
            "test.timeout",
            {"bucket": "documents"},
            correlation_id="request-24",
        )
        delivery = FakeDelivery(job, attempt=3, stream_sequence=24)

        async def ack_after_publish() -> None:
            assert len(queue.dead_letters) == 1
            delivery.ack_count += 1

        delivery.ack = ack_after_publish  # type: ignore[method-assign]
        await queue.deliveries.put(delivery)
        worker = AsyncWorker(
            queue,
            {"test.timeout": handler},
            config=_worker_config(max_attempts=3, stop_after_jobs=1),
        )
        await worker.start()
        await worker.wait_for_shutdown_request()
        report = await worker.shutdown()

        assert report.dead_lettered == 1
        assert delivery.ack_count == 1
        assert delivery.nack_count == 0
        record = queue.dead_letters[0]
        assert record.disposition.value == "exhausted"
        assert record.retryable is True
        assert record.category == "timeout"
        assert record.attempt == 3
        assert record.cumulative_attempt == 3
        assert record.job == job
        assert record.raw_data == job.to_bytes()
        assert record.exception_type == "builtins.TimeoutError"
        assert record.exception_message == "backend timed out"
        assert "raise TimeoutError" in record.traceback
        assert record.source_stream == "TEST_JOBS"
        assert record.stream_sequence == 24

    asyncio.run(scenario())


def test_malformed_raw_delivery_is_quarantined_and_next_job_runs() -> None:
    async def scenario() -> None:
        queue = FakeQueue()
        malformed = FakeMalformedDelivery(
            b"\xffnot-json\x00",
            headers={"CogniStore-Correlation-Id": "request-malformed"},
        )
        valid = FakeDelivery(JobEnvelope.create("known.type", {}), stream_sequence=10)
        await queue.deliveries.put(malformed)  # type: ignore[arg-type]
        await queue.deliveries.put(valid)
        handled = 0

        async def handler(job, context) -> None:
            nonlocal handled
            handled += 1

        worker = AsyncWorker(
            queue,
            {"known.type": handler},
            config=_worker_config(stop_after_jobs=2),
        )
        await worker.start()
        await worker.wait_for_shutdown_request()
        report = await worker.shutdown()

        assert report.dead_lettered == 1
        assert report.completed == 1
        assert malformed.ack_count == 1
        assert malformed.nack_count == 0
        assert valid.ack_count == 1
        assert handled == 1
        record = queue.dead_letters[0]
        assert record.job is None
        assert record.raw_data == b"\xffnot-json\x00"
        assert record.headers["CogniStore-Correlation-Id"] == "request-malformed"
        assert record.disposition.value == "terminal"
        assert record.category == "invalid"

    asyncio.run(scenario())


def test_unencodable_header_field_is_quarantined_losslessly() -> None:
    async def scenario() -> None:
        queue = FakeQueue()
        value = json.loads(JobEnvelope.create("known.type", {}).to_bytes())
        value["job_type"] = "\ud800"
        raw_data = json.dumps(value).encode("utf-8")
        delivery = FakeMalformedDelivery(raw_data)
        await queue.deliveries.put(delivery)  # type: ignore[arg-type]

        async def handler(job, context) -> None:
            raise AssertionError("malformed envelope must not reach a handler")

        worker = AsyncWorker(
            queue,
            {"known.type": handler},
            config=_worker_config(stop_after_jobs=1),
        )
        await worker.start()
        await worker.wait_for_shutdown_request()
        report = await worker.shutdown()

        assert report.dead_lettered == 1
        assert delivery.ack_count == 1
        record = queue.dead_letters[0]
        assert record.job is None
        assert record.raw_data == raw_data

    asyncio.run(scenario())


def test_dead_letter_publish_failure_leaves_source_unsettled_and_fails_closed() -> None:
    async def scenario() -> None:
        queue = FakeQueue()
        queue.dead_letter_error = ConnectionError("DLQ unavailable")
        delivery = FakeDelivery(JobEnvelope.create("unknown.type", {}))
        await queue.deliveries.put(delivery)
        events: list[str] = []
        execution = FakeExecution(events)
        coordinator = FakeCoordinator(execution, events)

        async def handler(job, context) -> None:
            return None

        worker = AsyncWorker(
            queue,
            {"known.type": handler},
            config=_worker_config(),
            coordinator=coordinator,
        )
        await worker.start()
        await worker.wait_for_shutdown_request()
        report = await worker.shutdown()

        assert worker.state == WorkerState.FAILED
        assert report.graceful is False
        assert delivery.ack_count == 0
        assert delivery.nack_count == 0
        assert events == ["begin", "retry"]

    asyncio.run(scenario())


def test_handler_mutation_cannot_change_the_recorded_original_envelope() -> None:
    async def scenario() -> None:
        queue = FakeQueue()
        job = JobEnvelope.create("test.mutate", {"value": "original"})
        delivery = FakeDelivery(job)
        original = bytes(delivery.raw_data or b"")
        await queue.deliveries.put(delivery)

        async def handler(received, context) -> None:
            received.payload["value"] = object()  # type: ignore[index,assignment]
            raise ValueError("mutated invalid request")

        worker = AsyncWorker(
            queue,
            {"test.mutate": handler},
            config=_worker_config(stop_after_jobs=1),
        )
        await worker.start()
        await worker.wait_for_shutdown_request()
        await worker.shutdown()

        record = queue.dead_letters[0]
        assert record.raw_data == original
        assert record.job == JobEnvelope.from_bytes(original)
        assert record.job.payload == {"value": "original"}

    asyncio.run(scenario())


def test_malformed_reserved_audit_metadata_cannot_block_quarantine() -> None:
    async def scenario() -> None:
        queue = FakeQueue()
        job = JobEnvelope.create(
            "unknown.type",
            {},
            metadata={
                DEAD_LETTER_CHAIN_METADATA: ("[" * 1_100) + ("]" * 1_100)
            },
        )
        delivery = FakeDelivery(job)
        await queue.deliveries.put(delivery)

        async def handler(received, context) -> None:
            return None

        worker = AsyncWorker(
            queue,
            {"known.type": handler},
            config=_worker_config(stop_after_jobs=1),
        )
        await worker.start()
        await worker.wait_for_shutdown_request()
        await worker.shutdown()

        record = queue.dead_letters[0]
        assert record.audit_chain == (record.dead_letter_id,)
        assert delivery.ack_count == 1

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
