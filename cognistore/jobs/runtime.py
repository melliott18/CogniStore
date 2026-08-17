from __future__ import annotations

import asyncio
import inspect
import logging
import random
import traceback as traceback_module
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping

from .models import (
    ATTEMPT_OFFSET_METADATA,
    REDRIVE_COUNT_METADATA,
    BusState,
    DeadLetterDisposition,
    DeadLetterRecord,
    InvalidJobError,
    JobContext,
    JobEnvelope,
    JobEnvelopeError,
    QueueHealth,
)
from .protocols import JobDelivery, JobQueue
from .retry import RandomSource, RetryPolicy, classify_job_error

LOGGER = logging.getLogger(__name__)
JobHandler = Callable[[JobEnvelope, JobContext], Awaitable[None]]


class WorkerState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DRAINING = "draining"
    FAILED = "failed"


@dataclass(frozen=True)
class WorkerConfig:
    fetch_timeout: float = 1.0
    heartbeat_interval: float = 10.0
    shutdown_grace: float = 30.0
    settlement_timeout: float = 5.0
    stop_after_jobs: int | None = None
    max_attempts: int = 7
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0
    retry_jitter: float = 0.2

    def __post_init__(self) -> None:
        for field_name in (
            "fetch_timeout",
            "shutdown_grace",
            "settlement_timeout",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be greater than zero")
        if self.heartbeat_interval < 0:
            raise ValueError("heartbeat_interval cannot be negative")
        if self.stop_after_jobs is not None and self.stop_after_jobs < 1:
            raise ValueError("stop_after_jobs must be at least one")
        RetryPolicy(
            max_attempts=self.max_attempts,
            base_delay=self.retry_base_delay,
            max_delay=self.retry_max_delay,
            jitter=self.retry_jitter,
        )

    @property
    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            max_attempts=self.max_attempts,
            base_delay=self.retry_base_delay,
            max_delay=self.retry_max_delay,
            jitter=self.retry_jitter,
        )


@dataclass(frozen=True)
class WorkerSnapshot:
    state: WorkerState
    live: bool
    ready: bool
    accepting_claims: bool
    in_flight: int
    active_job_ids: tuple[str, ...]
    completed: int
    nacked: int
    retried: int
    dead_lettered: int
    last_error: str | None
    last_bus_probe: str | None
    bus: QueueHealth

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker": {
                "state": self.state.value,
                "live": self.live,
                "ready": self.ready,
                "accepting_claims": self.accepting_claims,
                "in_flight": self.in_flight,
                "active_job_ids": list(self.active_job_ids),
                "completed": self.completed,
                "nacked": self.nacked,
                "retried": self.retried,
                "dead_lettered": self.dead_lettered,
                "last_error": self.last_error,
                "last_bus_probe": self.last_bus_probe,
            },
            "bus": self.bus.to_dict(),
        }


@dataclass(frozen=True)
class ShutdownReport:
    completed: int
    nacked: int
    unsettled: int
    graceful: bool
    retried: int = 0
    dead_lettered: int = 0


class AsyncWorker:
    """Claims and settles jobs while preserving at-least-once delivery.

    A handler return is acknowledged. Retryable failures are negatively
    acknowledged with bounded delay; terminal and exhausted failures are
    durably dead-lettered before the source delivery is acknowledged. Process
    death before settlement leaves the message for broker redelivery. Handlers
    must therefore treat duplicate delivery as a normal condition and use
    ``job.job_id`` as their idempotency key.
    """

    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[str, JobHandler],
        *,
        config: WorkerConfig | None = None,
        random_source: RandomSource | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not handlers:
            raise ValueError("at least one job handler is required")
        self.queue = queue
        self.handlers = dict(handlers)
        self.config = config or WorkerConfig()
        self._retry_policy = self.config.retry_policy
        self._random_source = random_source or random.random
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.state = WorkerState.STOPPED
        self.accepting_claims = False
        self.last_error: str | None = None
        self._last_health = QueueHealth(
            state=BusState.DISCONNECTED,
            ready=False,
            jetstream=False,
            stream="",
            consumer="",
            error="worker has not started",
        )
        self._last_probe_at: str | None = None
        self._completed = 0
        self._nacked = 0
        self._retried = 0
        self._dead_lettered = 0
        self._stop_requested = asyncio.Event()
        self._claim_idle = asyncio.Event()
        self._claim_idle.set()
        self._fetch_task: asyncio.Task[JobDelivery | None] | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._active: dict[asyncio.Task[None], JobDelivery] = {}
        self._shutdown_task: asyncio.Task[ShutdownReport] | None = None

    async def start(self) -> None:
        if self.state != WorkerState.STOPPED or self._supervisor is not None:
            raise RuntimeError("worker has already been started")
        self.state = WorkerState.STARTING
        try:
            await self.queue.connect()
            health = await self.queue.probe()
            self._record_probe(health)
            if not health.ready:
                raise RuntimeError(health.error or "job queue is not ready")
        except BaseException as exc:
            self._set_failure(exc)
            try:
                await self.queue.close(graceful=False)
            except Exception:
                pass
            raise
        self.state = WorkerState.RUNNING
        self.accepting_claims = True
        self._supervisor = asyncio.create_task(
            self._supervise(), name="cognistore-worker-supervisor"
        )

    def request_shutdown(self) -> None:
        if self.state in (WorkerState.STOPPED, WorkerState.FAILED):
            self._stop_requested.set()
            return
        self.state = WorkerState.DRAINING
        self.accepting_claims = False
        self._stop_requested.set()
        fetch_task = self._fetch_task
        if fetch_task is not None and not fetch_task.done():
            fetch_task.cancel()

    async def wait_for_shutdown_request(self) -> None:
        await self._stop_requested.wait()

    async def _supervise(self) -> None:
        try:
            while not self._stop_requested.is_set():
                delivery: JobDelivery | None = None
                self._claim_idle.clear()
                self._fetch_task = asyncio.create_task(
                    self.queue.claim(self.config.fetch_timeout),
                    name="cognistore-job-claim",
                )
                try:
                    delivery = await self._fetch_task
                except asyncio.CancelledError:
                    if not self._stop_requested.is_set():
                        raise
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    LOGGER.exception("job claim failed")
                    if isinstance(exc, JobEnvelopeError):
                        # A legacy/custom queue may parse before returning its
                        # delivery handle. Without that handle the worker cannot
                        # safely publish-and-ACK, so retain the source message.
                        self._set_failure(exc)
                        break
                    health = await self.queue.probe()
                    self._record_probe(health)
                    if not self._stop_requested.is_set():
                        await asyncio.sleep(min(self.config.fetch_timeout, 0.25))
                finally:
                    self._fetch_task = None

                if delivery is not None:
                    task = asyncio.create_task(
                        self._process(delivery),
                        name=f"cognistore-job-{delivery.stream_sequence}",
                    )
                    self._active[task] = delivery
                    task.add_done_callback(self._active_done)
                self._claim_idle.set()

                if delivery is not None:
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        if not self._stop_requested.is_set():
                            raise

                    settled_jobs = self._completed + self._nacked + self._dead_lettered
                    if (
                        self.config.stop_after_jobs is not None
                        and settled_jobs >= self.config.stop_after_jobs
                    ):
                        self.request_shutdown()
        except asyncio.CancelledError:
            pass
        except BaseException as exc:
            self._set_failure(exc)
        finally:
            self.accepting_claims = False
            self._claim_idle.set()
            if not self._stop_requested.is_set():
                self._set_failure(RuntimeError("worker supervisor stopped unexpectedly"))

    def _active_done(self, task: asyncio.Task[None]) -> None:
        self._active.pop(task, None)
        if task.cancelled():
            return
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            # Processing records settlement failures before returning/raising.
            pass

    async def _process(self, delivery: JobDelivery) -> None:
        job: JobEnvelope | None = None
        heartbeat_stop = asyncio.Event()
        heartbeat_task: asyncio.Task[None] | None = None
        if self.config.heartbeat_interval > 0:
            heartbeat_task = asyncio.create_task(
                self._heartbeat(delivery, heartbeat_stop),
                name=f"cognistore-heartbeat-{delivery.stream_sequence}",
            )

        try:
            job = delivery.job
            attempt_offset = self._metadata_integer(job, ATTEMPT_OFFSET_METADATA)
            redrive_count = self._metadata_integer(job, REDRIVE_COUNT_METADATA)
            context = JobContext(
                attempt=delivery.attempt,
                redelivered=delivery.attempt > 1,
                stream_sequence=delivery.stream_sequence,
                consumer_sequence=delivery.consumer_sequence,
                shutdown_requested=self._stop_requested,
                cumulative_attempt=attempt_offset + delivery.attempt,
                redrive_count=redrive_count,
            )
            handler = self.handlers.get(job.job_type)
            if handler is None:
                raise InvalidJobError(
                    f"no handler is registered for job type {job.job_type!r}"
                )
            result = handler(job, context)
            if not inspect.isawaitable(result):
                raise TypeError(f"handler for {job.job_type!r} must be asynchronous")
            await result
        except asyncio.CancelledError:
            await self._stop_heartbeat(heartbeat_stop, heartbeat_task)
            try:
                await asyncio.shield(delivery.nack())
                self._nacked += 1
            except BaseException as exc:
                self._set_failure(exc)
            raise
        except Exception as exc:
            await self._stop_heartbeat(heartbeat_stop, heartbeat_task)
            await self._handle_job_failure(delivery, job, exc)
            return

        await self._stop_heartbeat(heartbeat_stop, heartbeat_task)
        try:
            await delivery.ack()
            self._completed += 1
        except BaseException as exc:
            # ACK outcome is unknown. Never follow a failed double-ACK with NACK;
            # AckWait redelivery is the only safe recovery.
            self._set_failure(exc)

    @staticmethod
    def _metadata_integer(job: JobEnvelope, key: str) -> int:
        value = job.metadata.get(key, "0")
        try:
            parsed = int(value)
        except ValueError:
            return 0
        return max(0, parsed)

    async def _handle_job_failure(
        self,
        delivery: JobDelivery,
        job: JobEnvelope | None,
        exc: Exception,
    ) -> None:
        classification = classify_job_error(exc)
        job_label = (
            f"job {job.job_id} ({job.job_type})"
            if job is not None
            else f"malformed delivery {delivery.source_stream}:{delivery.stream_sequence}"
        )
        self.last_error = f"{job_label} failed: {type(exc).__name__}: {exc}"
        log_context = {
            "job_id": job.job_id if job is not None else None,
            "correlation_id": job.correlation_id if job is not None else None,
            "attempt": delivery.attempt,
            "category": classification.category.value,
        }

        if classification.retryable and delivery.attempt < self._retry_policy.max_attempts:
            delay = self._retry_policy.delay_for(
                delivery.attempt, random_value=self._random_source()
            )
            LOGGER.warning(
                "job failed; scheduling attempt %s/%s in %.3f seconds",
                delivery.attempt + 1,
                self._retry_policy.max_attempts,
                delay,
                extra={**log_context, "retry_delay": delay},
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            try:
                await delivery.nack(delay=delay)
                self._nacked += 1
                self._retried += 1
            except BaseException as settlement_error:
                self._set_failure(settlement_error)
            return

        disposition = (
            DeadLetterDisposition.EXHAUSTED
            if classification.retryable
            else DeadLetterDisposition.TERMINAL
        )
        LOGGER.error(
            "job is terminal or exhausted; publishing diagnostic to dead-letter stream",
            extra={**log_context, "disposition": disposition.value},
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        try:
            record = self._dead_letter_record(
                delivery,
                job,
                exc,
                disposition=disposition,
                retryable=classification.retryable,
                category=classification.category.value,
                classification_reason=classification.reason,
            )
            receipt = await self.queue.publish_dead_letter(record)
        except BaseException as dead_letter_error:
            # The original remains unsettled unless durable DLQ publication was
            # confirmed. Another worker can therefore retry the transfer.
            self._set_failure(dead_letter_error)
            return

        try:
            await delivery.ack()
            self._dead_lettered += 1
            LOGGER.error(
                "job moved to dead-letter stream dead_letter_id=%s sequence=%s",
                receipt.dead_letter_id,
                receipt.sequence,
                extra={
                    **log_context,
                    "dead_letter_id": receipt.dead_letter_id,
                    "dead_letter_sequence": receipt.sequence,
                    "disposition": disposition.value,
                },
            )
        except BaseException as settlement_error:
            # A deterministic DLQ ID suppresses a duplicate transfer if this
            # source ACK outcome is unknown and the message is redelivered.
            self._set_failure(settlement_error)

    def _dead_letter_record(
        self,
        delivery: JobDelivery,
        job: JobEnvelope | None,
        exc: Exception,
        *,
        disposition: DeadLetterDisposition,
        retryable: bool,
        category: str,
        classification_reason: str,
    ) -> DeadLetterRecord:
        raw_data = getattr(delivery, "raw_data", None)
        if not isinstance(raw_data, bytes):
            raw_data = job.to_bytes() if job is not None else b""
        try:
            diagnostic_job = JobEnvelope.from_bytes(raw_data)
        except JobEnvelopeError:
            diagnostic_job = None
        raw_headers = getattr(delivery, "headers", {})
        if not isinstance(raw_headers, Mapping):
            raw_headers = {}
        headers = {str(key): str(value) for key, value in raw_headers.items()}
        source_stream = getattr(delivery, "source_stream", None) or self._last_health.stream
        source_consumer = (
            getattr(delivery, "source_consumer", None) or self._last_health.consumer
        )
        failed_at = self._clock()
        source_published_at = getattr(delivery, "source_published_at", None)
        if (
            not isinstance(source_published_at, datetime)
            or source_published_at.tzinfo is None
            or source_published_at.utcoffset() is None
        ):
            source_published_at = failed_at
        attempt_offset = (
            self._metadata_integer(diagnostic_job, ATTEMPT_OFFSET_METADATA)
            if diagnostic_job is not None
            else 0
        )
        return DeadLetterRecord.create(
            failed_at=failed_at,
            disposition=disposition,
            retryable=retryable,
            category=category,
            classification_reason=classification_reason,
            attempt=delivery.attempt,
            max_attempts=self._retry_policy.max_attempts,
            cumulative_attempt=attempt_offset + delivery.attempt,
            source_stream=source_stream,
            source_published_at=source_published_at,
            source_consumer=source_consumer,
            stream_sequence=delivery.stream_sequence,
            consumer_sequence=delivery.consumer_sequence,
            exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
            exception_message=str(exc),
            traceback="".join(
                traceback_module.format_exception(type(exc), exc, exc.__traceback__)
            ),
            raw_data=raw_data,
            headers=headers,
            job=diagnostic_job,
        )

    async def _heartbeat(
        self, delivery: JobDelivery, stop_event: asyncio.Event
    ) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.config.heartbeat_interval
                )
            except asyncio.TimeoutError:
                try:
                    await delivery.in_progress()
                except BaseException as exc:
                    self._set_failure(exc)
                    return

    @staticmethod
    async def _stop_heartbeat(
        stop_event: asyncio.Event, task: asyncio.Task[None] | None
    ) -> None:
        if task is None:
            return
        stop_event.set()
        await asyncio.gather(task, return_exceptions=True)

    def _set_failure(self, exc: BaseException) -> None:
        self.last_error = f"{type(exc).__name__}: {exc}"
        self.state = WorkerState.FAILED
        self.accepting_claims = False
        self._stop_requested.set()
        fetch_task = self._fetch_task
        if fetch_task is not None and not fetch_task.done():
            fetch_task.cancel()

    def _record_probe(self, health: QueueHealth) -> None:
        self._last_health = health
        self._last_probe_at = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )

    def health_snapshot(self) -> WorkerSnapshot:
        live = self.state in {
            WorkerState.STARTING,
            WorkerState.RUNNING,
            WorkerState.DRAINING,
        }
        ready = (
            self.state == WorkerState.RUNNING
            and self.accepting_claims
            and self._last_health.ready
        )
        active_ids: list[str] = []
        for delivery in self._active.values():
            try:
                active_ids.append(delivery.job.job_id)
            except JobEnvelopeError:
                active_ids.append(
                    f"malformed:{delivery.source_stream}:{delivery.stream_sequence}"
                )
        return WorkerSnapshot(
            state=self.state,
            live=live,
            ready=ready,
            accepting_claims=self.accepting_claims,
            in_flight=len(self._active),
            active_job_ids=tuple(sorted(active_ids)),
            completed=self._completed,
            nacked=self._nacked,
            retried=self._retried,
            dead_lettered=self._dead_lettered,
            last_error=self.last_error,
            last_bus_probe=self._last_probe_at,
            bus=self._last_health,
        )

    async def check_readiness(self) -> WorkerSnapshot:
        health = await self.queue.probe()
        self._record_probe(health)
        return self.health_snapshot()

    async def shutdown(self, *, grace: float | None = None) -> ShutdownReport:
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(
                self._shutdown_impl(
                    self.config.shutdown_grace if grace is None else grace
                ),
                name="cognistore-worker-shutdown",
            )
        return await asyncio.shield(self._shutdown_task)

    async def _shutdown_impl(self, grace: float) -> ShutdownReport:
        if grace < 0:
            raise ValueError("shutdown grace cannot be negative")
        self.request_shutdown()
        try:
            await asyncio.wait_for(
                self._claim_idle.wait(), timeout=self.config.settlement_timeout
            )
        except asyncio.TimeoutError:
            pass

        active = tuple(self._active)
        pending: set[asyncio.Task[None]] = set()
        if active:
            _, pending = await asyncio.wait(active, timeout=grace)
        for task in pending:
            task.cancel()
        if pending:
            _, pending = await asyncio.wait(
                pending, timeout=self.config.settlement_timeout
            )

        if pending:
            self.last_error = (
                f"{len(pending)} handler(s) exceeded the shutdown deadlines; "
                "waiting for a safe side-effect boundary"
            )
            # Python cannot forcibly stop a storage operation already running
            # in a thread. Keep the connection/heartbeats alive and do not
            # return until it reaches its settlement boundary; closing early
            # would permit an overlapping redelivery.
            await asyncio.gather(*pending, return_exceptions=True)
            pending = set()

        supervisor = self._supervisor
        if supervisor is not None and not supervisor.done():
            supervisor.cancel()
        if supervisor is not None:
            await asyncio.gather(supervisor, return_exceptions=True)

        graceful = self.state != WorkerState.FAILED
        try:
            await self.queue.close(graceful=True)
        except BaseException as exc:
            graceful = False
            self._set_failure(exc)

        if self.state != WorkerState.FAILED:
            self.state = WorkerState.STOPPED
        self.accepting_claims = False
        return ShutdownReport(
            completed=self._completed,
            nacked=self._nacked,
            unsettled=len(pending),
            graceful=graceful,
            retried=self._retried,
            dead_lettered=self._dead_lettered,
        )
