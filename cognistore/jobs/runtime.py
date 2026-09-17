from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
import traceback as traceback_module
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Protocol

from cognistore.auth.principal import principal_context
from cognistore.core.audit import (
    AuditContext,
    AuditEvent,
    AuditEventType,
    AuditOutcome,
    stable_audit_event_id,
)
from cognistore.core.catalog import CatalogStore
from cognistore.observability import (
    current_correlation_id,
    mark_current_span_error,
    observe,
    record_indexing_lag,
    record_job_event,
    record_job_queue_latency,
    request_context,
    set_job_queue_depth,
)

from .models import (
    ATTEMPT_OFFSET_METADATA,
    REDRIVE_COUNT_METADATA,
    STATUS_TRACKING_METADATA,
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
from .retry import ErrorClassification, RandomSource, RetryPolicy, classify_job_error
from .telemetry import job_operation

LOGGER = logging.getLogger(__name__)
JobHandler = Callable[[JobEnvelope, JobContext], Awaitable[None]]


class JobExecution(Protocol):
    """Optional lifecycle owned by one worker attempt."""

    execute: bool
    heartbeat_interval: float

    async def renew(self) -> None: ...

    async def retry(self) -> None: ...

    async def succeed(self) -> None: ...

    async def dead_letter(self, disposition: DeadLetterDisposition) -> None: ...


class JobCoordinator(Protocol):
    """Extension point for durable per-job execution coordination."""

    async def begin(self, job: JobEnvelope, context: JobContext) -> JobExecution | None: ...


class JobHeartbeatError(RuntimeError):
    """Fail a worker closed when delivery or execution lease renewal fails."""

    fail_worker = True


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
    max_in_flight: int = 1

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
        if (
            isinstance(self.max_in_flight, bool)
            or not isinstance(self.max_in_flight, int)
            or self.max_in_flight < 1
        ):
            raise ValueError("max_in_flight must be a positive integer")
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
    # Appended after the original fields to preserve positional construction.
    max_in_flight: int = 1
    available_capacity: int = 0
    saturated: bool = False
    saturation_events: int = 0
    saturation_seconds: float = 0.0
    throughput: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "worker": {
                "state": self.state.value,
                "live": self.live,
                "ready": self.ready,
                "accepting_claims": self.accepting_claims,
                "in_flight": self.in_flight,
                "max_in_flight": self.max_in_flight,
                "available_capacity": self.available_capacity,
                "saturated": self.saturated,
                "saturation_events": self.saturation_events,
                "saturation_seconds": self.saturation_seconds,
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
        if self.throughput is not None:
            payload["throughput"] = dict(self.throughput)
        return payload


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
        monotonic: Callable[[], float] | None = None,
        throughput: Any | None = None,
        coordinator: JobCoordinator | None = None,
        audit_catalog: CatalogStore | None = None,
    ) -> None:
        if not handlers:
            raise ValueError("at least one job handler is required")
        self.queue = queue
        self.handlers = dict(handlers)
        self.config = config or WorkerConfig()
        self._retry_policy = self.config.retry_policy
        self._random_source = random_source or random.random
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._throughput = throughput
        self._coordinator = coordinator
        self._audit_catalog = audit_catalog
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
        self._saturation_events = 0
        self._saturation_seconds = 0.0
        self._saturation_started_at: float | None = None
        self._stop_requested = asyncio.Event()
        self._claim_idle = asyncio.Event()
        self._claim_idle.set()
        self._capacity_changed = asyncio.Event()
        self._capacity_changed.set()
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
        self._update_saturation_state()
        self._stop_requested.set()
        self._capacity_changed.set()
        fetch_task = self._fetch_task
        if fetch_task is not None and not fetch_task.done():
            fetch_task.cancel()

    async def wait_for_shutdown_request(self) -> None:
        await self._stop_requested.wait()

    async def _supervise(self) -> None:
        try:
            while not self._stop_requested.is_set():
                if self._stop_limit_reached():
                    self.request_shutdown()
                    break
                if not self._can_claim():
                    await self._wait_for_capacity_change()
                    continue

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
                    self.last_error = "job claim failed"
                    LOGGER.error("job claim failed")
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
                    self._update_saturation_state()
                self._claim_idle.set()
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
        self._update_saturation_state()
        self._capacity_changed.set()
        if not task.cancelled():
            try:
                task.exception()
            except (asyncio.CancelledError, Exception):
                # Processing records settlement failures before returning/raising.
                pass
        if self._stop_limit_reached() and not self._stop_requested.is_set():
            self.request_shutdown()

    def _settled_jobs(self) -> int:
        return self._completed + self._nacked + self._dead_lettered

    def _stop_limit_reached(self) -> bool:
        return (
            self.config.stop_after_jobs is not None
            and self._settled_jobs() >= self.config.stop_after_jobs
        )

    def _can_claim(self) -> bool:
        if len(self._active) >= self.config.max_in_flight:
            return False
        if self.config.stop_after_jobs is None:
            return True
        # Active deliveries reserve the remaining stop budget so concurrent
        # claims can never overshoot the requested number of settlements.
        return (
            self._settled_jobs() + len(self._active)
            < self.config.stop_after_jobs
        )

    async def _wait_for_capacity_change(self) -> None:
        while not self._stop_requested.is_set() and not self._can_claim():
            self._capacity_changed.clear()
            if self._stop_requested.is_set() or self._can_claim():
                return
            await self._capacity_changed.wait()

    def _currently_saturated(self) -> bool:
        return (
            self.accepting_claims
            and len(self._active) >= self.config.max_in_flight
        )

    def _update_saturation_state(self) -> None:
        saturated = self._currently_saturated()
        if saturated and self._saturation_started_at is None:
            self._saturation_started_at = self._monotonic()
            self._saturation_events += 1
        elif not saturated and self._saturation_started_at is not None:
            elapsed = self._monotonic() - self._saturation_started_at
            self._saturation_seconds += max(0.0, elapsed)
            self._saturation_started_at = None

    def _current_saturation_seconds(self) -> float:
        total = self._saturation_seconds
        if self._saturation_started_at is not None:
            total += max(0.0, self._monotonic() - self._saturation_started_at)
        return total

    async def _process(self, delivery: JobDelivery) -> None:
        # Explicitly clear inherited request/task context for anonymous and
        # malformed deliveries, and restore it after every settlement path.
        with ExitStack() as contexts:
            contexts.enter_context(principal_context(None))
            try:
                job = delivery.job
            except Exception:
                job = None
            parent = job.metadata.get("traceparent") if job is not None else None
            operation = job_operation(job.job_type) if job is not None else "other"
            contexts.enter_context(request_context(
                correlation_id=job.correlation_id if job is not None else None,
                traceparent=parent,
            ))
            contexts.enter_context(observe("job", operation, kind="consumer"))
            record_job_event("started", operation=operation)
            published_at = getattr(delivery, "source_published_at", None)
            if (
                isinstance(published_at, datetime)
                and published_at.tzinfo is not None
                and published_at.utcoffset() is not None
            ):
                record_job_queue_latency(
                    max(0.0, (self._clock() - published_at).total_seconds()),
                    operation=operation,
                )
            LOGGER.info(
                "job delivery started",
                extra={
                    "job_id": job.job_id if job is not None else None,
                    "correlation_id": current_correlation_id(),
                    "operation": operation,
                    "attempt": delivery.attempt,
                },
            )
            await self._process_delivery(delivery, contexts)

    async def _process_delivery(self, delivery: JobDelivery, contexts: ExitStack) -> None:
        job: JobEnvelope | None = None
        execution: JobExecution | None = None
        observe_indexing = True
        heartbeat_stop = asyncio.Event()
        heartbeat_task: asyncio.Task[None] | None = None

        try:
            job = delivery.job
            principal = job.principal
            contexts.enter_context(principal_context(principal))
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
                principal=principal,
            )
            if self._coordinator is not None:
                execution = await self._coordinator.begin(job, context)
                # A terminal replay may represent a failed/dead-lettered scan.
                # Acknowledging it is not a fresh successful indexing attempt.
                observe_indexing = execution is None or execution.execute
            if execution is None or execution.execute:
                await self._record_status_audit(
                    delivery,
                    job,
                    event_type=AuditEventType.JOB_STARTED,
                    outcome=AuditOutcome.STARTED,
                )
                heartbeat_interval = self._execution_heartbeat_interval(execution)
                if heartbeat_interval is not None:
                    heartbeat_task = asyncio.create_task(
                        self._heartbeat(
                            delivery,
                            heartbeat_stop,
                            execution,
                            heartbeat_interval,
                        ),
                        name=f"cognistore-heartbeat-{delivery.stream_sequence}",
                    )
                handler = self.handlers.get(job.job_type)
                if handler is None:
                    raise InvalidJobError(
                        f"no handler is registered for job type {job.job_type!r}"
                    )
                result = handler(job, context)
                if not inspect.isawaitable(result):
                    raise TypeError(f"handler for {job.job_type!r} must be asynchronous")
                await self._await_handler(result, heartbeat_task, execution)
                await self._stop_heartbeat(
                    heartbeat_stop, heartbeat_task, propagate=True
                )
                heartbeat_task = None
                if execution is not None:
                    # Persist logical completion before the source ACK. If the ACK
                    # outcome is unknown, a redelivery observes the terminal run and
                    # skips already-completed side effects.
                    await execution.succeed()
                    execution = None
            await self._record_status_audit(
                delivery,
                job,
                event_type=AuditEventType.JOB_SUCCEEDED,
                outcome=AuditOutcome.SUCCEEDED,
            )
        except asyncio.CancelledError:
            if observe_indexing:
                self._record_indexing_completion(delivery, job, succeeded=False)
            record_job_event("cancelled", operation=job_operation(job.job_type) if job else "other")
            await self._stop_heartbeat(heartbeat_stop, heartbeat_task)
            try:
                if execution is not None and execution.execute:
                    await asyncio.shield(execution.retry())
                await asyncio.shield(delivery.nack())
                self._nacked += 1
            except BaseException as exc:
                self._set_failure(exc)
            raise
        except Exception as exc:
            if observe_indexing:
                self._record_indexing_completion(delivery, job, succeeded=False)
            await self._handle_job_failure(
                delivery,
                job,
                exc,
                execution,
                heartbeat_stop,
                heartbeat_task,
            )
            return

        if observe_indexing:
            self._record_indexing_completion(delivery, job, succeeded=True)
        await self._stop_heartbeat(heartbeat_stop, heartbeat_task)
        try:
            await delivery.ack()
            self._completed += 1
            record_job_event("succeeded", operation=job_operation(job.job_type) if job else "other")
            LOGGER.info(
                "job delivery succeeded",
                extra={"job_id": job.job_id if job else None, "attempt": delivery.attempt},
            )
        except BaseException as exc:
            # ACK outcome is unknown. Never follow a failed double-ACK with NACK;
            # AckWait redelivery is the only safe recovery.
            self._set_failure(exc)

    def _record_indexing_completion(
        self, delivery: JobDelivery, job: JobEnvelope | None, *, succeeded: bool,
    ) -> None:
        if job is None or job.job_type != "catalog.scan":
            return
        published_at = getattr(delivery, "source_published_at", None)
        seconds = None
        if (
            isinstance(published_at, datetime)
            and published_at.tzinfo is not None
            and published_at.utcoffset() is not None
        ):
            seconds = (self._clock() - published_at).total_seconds()
        record_indexing_lag(seconds, succeeded=succeeded)

    @staticmethod
    def _metadata_integer(job: JobEnvelope, key: str) -> int:
        value = job.metadata.get(key, "0")
        try:
            parsed = int(value)
        except ValueError:
            return 0
        return max(0, parsed)

    def _cumulative_attempt(
        self,
        delivery: JobDelivery,
        job: JobEnvelope | None,
    ) -> int:
        attempt_offset = (
            self._metadata_integer(job, ATTEMPT_OFFSET_METADATA)
            if job is not None
            else 0
        )
        return attempt_offset + delivery.attempt

    def _audit_delivery_details(
        self,
        delivery: JobDelivery,
        job: JobEnvelope | None,
        classification: ErrorClassification,
    ) -> dict[str, Any]:
        """Return secret-safe structured facts about one failed delivery.

        Exception messages, tracebacks, payloads, raw delivery bytes, and
        headers are deliberately excluded. Catalog implementations apply their
        normal recursive redaction to the remaining free-text fields.
        """

        source_stream = str(
            getattr(delivery, "source_stream", None)
            or self._last_health.stream
            or "unknown"
        )
        source_consumer = str(
            getattr(delivery, "source_consumer", None)
            or self._last_health.consumer
            or "worker"
        )
        return {
            "job_type": job.job_type if job is not None else None,
            "attempt": delivery.attempt,
            "cumulative_attempt": self._cumulative_attempt(delivery, job),
            "max_attempts": self._retry_policy.max_attempts,
            "redelivered": delivery.attempt > 1,
            "retryable": classification.retryable,
            "category": classification.category.value,
            "classification_reason": classification.reason,
            "source_stream": source_stream,
            "source_consumer": source_consumer,
            "stream_sequence": delivery.stream_sequence,
            "consumer_sequence": delivery.consumer_sequence,
        }

    async def _record_status_audit(
        self,
        delivery: JobDelivery,
        job: JobEnvelope,
        *,
        event_type: AuditEventType,
        outcome: AuditOutcome,
    ) -> AuditEvent | None:
        """Persist API-requested lifecycle status without changing legacy jobs."""

        if job.metadata.get(STATUS_TRACKING_METADATA) != "1":
            return None
        return await self._append_job_audit_event(
            delivery,
            job,
            event_type=event_type,
            outcome=outcome,
            details={
                "job_type": job.job_type,
                "attempt": delivery.attempt,
                "cumulative_attempt": self._cumulative_attempt(delivery, job),
                "redelivered": delivery.attempt > 1,
                "source_stream": str(
                    getattr(delivery, "source_stream", None)
                    or self._last_health.stream
                    or "unknown"
                ),
                "source_consumer": str(
                    getattr(delivery, "source_consumer", None)
                    or self._last_health.consumer
                    or "worker"
                ),
                "stream_sequence": delivery.stream_sequence,
                "consumer_sequence": delivery.consumer_sequence,
            },
        )

    async def _append_job_audit_event(
        self,
        delivery: JobDelivery,
        job: JobEnvelope | None,
        *,
        event_type: AuditEventType,
        outcome: AuditOutcome,
        details: Mapping[str, Any],
        causation_id: str | None = None,
    ) -> AuditEvent | None:
        """Persist one worker event before the corresponding settlement."""

        if self._audit_catalog is None:
            return None

        source_stream = str(
            getattr(delivery, "source_stream", None)
            or self._last_health.stream
            or "unknown"
        )
        source_consumer = str(
            getattr(delivery, "source_consumer", None)
            or self._last_health.consumer
            or "worker"
        )
        job_identity = job.job_id if job is not None else "malformed"
        event_id = stable_audit_event_id(
            "worker-delivery",
            event_type.value,
            job_identity,
            source_stream,
            str(delivery.stream_sequence),
            source_consumer,
            str(delivery.consumer_sequence),
            str(self._cumulative_attempt(delivery, job)),
        )
        correlation_id = (
            job.correlation_id
            if job is not None
            else stable_audit_event_id(
                "worker-malformed-correlation",
                source_stream,
                str(delivery.stream_sequence),
            )
        )
        occurred_at = self._clock()
        principal = job.principal if job is not None else None
        try:
            event = AuditEvent.create(
                event_type,
                outcome,
                AuditContext(
                    correlation_id=correlation_id,
                    causation_id=causation_id,
                    actor_type=principal.actor_type if principal is not None else "worker",
                    actor_id=principal.actor_id if principal is not None else source_consumer,
                    job_id=job.job_id if job is not None else None,
                ),
                event_id=event_id,
                occurred_at=occurred_at,
                recorded_at=occurred_at,
                details=details,
            )
            return await asyncio.to_thread(
                self._audit_catalog.append_audit_event,
                event,
            )
        except BaseException as audit_error:
            LOGGER.critical(
                "job audit persistence failed; leaving delivery unsettled",
                extra={
                    "job_id": job.job_id if job is not None else None,
                    "attempt": delivery.attempt,
                    "event_type": event_type.value,
                    "stream_sequence": delivery.stream_sequence,
                },
            )
            self._set_failure(audit_error)
            raise

    async def _record_failure_audit(
        self,
        delivery: JobDelivery,
        job: JobEnvelope | None,
        exc: Exception,
        classification: ErrorClassification,
    ) -> AuditEvent | None:
        details = self._audit_delivery_details(delivery, job, classification)
        details["exception_type"] = (
            f"{type(exc).__module__}.{type(exc).__qualname__}"
        )
        return await self._append_job_audit_event(
            delivery,
            job,
            event_type=AuditEventType.JOB_FAILURE,
            outcome=AuditOutcome.FAILED,
            details=details,
        )

    async def _record_retry_audit(
        self,
        delivery: JobDelivery,
        job: JobEnvelope | None,
        classification: ErrorClassification,
        failure_event: AuditEvent | None,
        *,
        delay: float,
        deferred_without_exhaustion: bool,
        throughput_saturated: bool,
    ) -> AuditEvent | None:
        details = self._audit_delivery_details(delivery, job, classification)
        details.update(
            {
                "next_attempt": details["cumulative_attempt"] + 1,
                "retry_delay_seconds": delay,
                "deferred_without_exhaustion": deferred_without_exhaustion,
                "throughput_saturated": throughput_saturated,
            }
        )
        return await self._append_job_audit_event(
            delivery,
            job,
            event_type=AuditEventType.JOB_RETRY,
            outcome=AuditOutcome.RETRYING,
            causation_id=(failure_event.event_id if failure_event is not None else None),
            details=details,
        )

    async def _settle_retry(
        self,
        delivery: JobDelivery,
        job: JobEnvelope | None,
        classification: ErrorClassification,
        failure_event: AuditEvent | None,
        execution: JobExecution | None,
        heartbeat_stop: asyncio.Event,
        heartbeat_task: asyncio.Task[None] | None,
        *,
        delay: float,
        deferred_without_exhaustion: bool = False,
        throughput_saturated: bool = False,
    ) -> None:
        try:
            await self._stop_heartbeat(heartbeat_stop, heartbeat_task)
            if execution is not None and execution.execute:
                await execution.retry()
            await self._record_retry_audit(
                delivery,
                job,
                classification,
                failure_event,
                delay=delay,
                deferred_without_exhaustion=deferred_without_exhaustion,
                throughput_saturated=throughput_saturated,
            )
            await delivery.nack(delay=delay)
            self._nacked += 1
            self._retried += 1
            record_job_event("retried", operation=job_operation(job.job_type) if job else "other")
        except BaseException as settlement_error:
            self._set_failure(settlement_error)

    async def _handle_job_failure(
        self,
        delivery: JobDelivery,
        job: JobEnvelope | None,
        exc: Exception,
        execution: JobExecution | None,
        heartbeat_stop: asyncio.Event,
        heartbeat_task: asyncio.Task[None] | None,
    ) -> None:
        classification = classify_job_error(exc)
        mark_current_span_error()
        record_job_event("failed", operation=job_operation(job.job_type) if job else "other")
        self.last_error = f"job delivery failed ({classification.category.value})"
        log_context = {
            "job_id": job.job_id if job is not None else None,
            "correlation_id": current_correlation_id(),
            "attempt": delivery.attempt,
            "category": classification.category.value,
        }
        try:
            failure_event = await self._record_failure_audit(
                delivery,
                job,
                exc,
                classification,
            )
        except BaseException:
            await self._stop_heartbeat(heartbeat_stop, heartbeat_task)
            if execution is not None and execution.execute:
                try:
                    # The handler is already at a safe side-effect boundary.
                    # Release its attempt owner while leaving the source delivery
                    # unsettled so another worker can retry audit persistence.
                    await execution.retry()
                except BaseException as release_error:
                    LOGGER.critical(
                        "job audit persistence failed and its durable owner could "
                        "not be released; leaving delivery unsettled",
                        extra=log_context,
                    )
                    self._set_failure(release_error)
            return

        if getattr(exc, "fail_worker", False):
            await self._stop_heartbeat(heartbeat_stop, heartbeat_task)
            if execution is not None and execution.execute:
                try:
                    # _await_handler does not surface a heartbeat failure until
                    # cancellation has drained the handler to its safe side-effect
                    # boundary. Clear the durable owner now so fail-closed running
                    # state does not quarantine an occurrence that is safe to retry.
                    await execution.retry()
                except BaseException as release_error:
                    LOGGER.critical(
                        "job coordination failed and its durable owner could not be "
                        "released; leaving delivery unsettled",
                        extra=log_context,
                    )
                    self._set_failure(release_error)
                    return
            LOGGER.critical(
                "job coordination state is unavailable; leaving delivery unsettled",
                extra=log_context,
            )
            self._set_failure(exc)
            return

        if getattr(exc, "defer_without_exhaustion", False):
            delay = self._retry_policy.delay_for(
                delivery.attempt, random_value=self._random_source()
            )
            LOGGER.warning(
                "job coordination is busy; deferring delivery in %.3f seconds",
                delay,
                extra={**log_context, "retry_delay": delay},
            )
            await self._settle_retry(
                delivery,
                job,
                classification,
                failure_event,
                execution,
                heartbeat_stop,
                heartbeat_task,
                delay=delay,
                deferred_without_exhaustion=True,
            )
            return

        if getattr(exc, "throughput_saturated", False):
            # Local admission pressure means the job has not started. Defer it
            # without ever converting healthy backlog into an exhausted/DLQ
            # record merely because capacity remained busy for several pulls.
            delay = self._retry_policy.delay_for(
                delivery.attempt, random_value=self._random_source()
            )
            LOGGER.warning(
                "local movement capacity is saturated; deferring delivery in "
                "%.3f seconds",
                delay,
                extra={**log_context, "retry_delay": delay},
            )
            await self._settle_retry(
                delivery,
                job,
                classification,
                failure_event,
                execution,
                heartbeat_stop,
                heartbeat_task,
                delay=delay,
                throughput_saturated=True,
            )
            return

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
            )
            await self._settle_retry(
                delivery,
                job,
                classification,
                failure_event,
                execution,
                heartbeat_stop,
                heartbeat_task,
                delay=delay,
            )
            return

        disposition = (
            DeadLetterDisposition.EXHAUSTED
            if classification.retryable
            else DeadLetterDisposition.TERMINAL
        )
        LOGGER.error(
            "job is terminal or exhausted; publishing diagnostic to dead-letter stream",
            extra={**log_context, "disposition": disposition.value},
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
            await self._stop_heartbeat(heartbeat_stop, heartbeat_task)
            dead_letter_details = self._audit_delivery_details(
                delivery,
                job,
                classification,
            )
            dead_letter_details.update(
                {
                    "disposition": disposition.value,
                    "dead_letter_id": receipt.dead_letter_id,
                    "dead_letter_stream": receipt.stream,
                    "dead_letter_sequence": receipt.sequence,
                }
            )
            await self._append_job_audit_event(
                delivery,
                job,
                event_type=AuditEventType.JOB_DEAD_LETTERED,
                outcome=AuditOutcome.FAILED,
                causation_id=(
                    failure_event.event_id if failure_event is not None else None
                ),
                details=dead_letter_details,
            )
            if execution is not None and execution.execute:
                await execution.dead_letter(disposition)
        except BaseException as dead_letter_error:
            await self._stop_heartbeat(heartbeat_stop, heartbeat_task)
            # The original remains unsettled unless durable DLQ publication was
            # confirmed. The handler is already at a safe side-effect boundary,
            # so release its attempt owner before another delivery retries the
            # transfer. If the fenced transition is unavailable, retain the
            # running quarantine rather than allowing an overlapping takeover.
            if execution is not None and execution.execute:
                try:
                    await execution.retry()
                except BaseException as release_error:
                    LOGGER.critical(
                        "dead-letter handling failed and its durable owner could "
                        "not be released; leaving delivery unsettled",
                        extra=log_context,
                    )
                    self._set_failure(release_error)
                    return
            self._set_failure(dead_letter_error)
            return

        try:
            await delivery.ack()
            self._dead_lettered += 1
            record_job_event(
                "dead_lettered", operation=job_operation(job.job_type) if job else "other"
            )
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

    def _execution_heartbeat_interval(
        self, execution: JobExecution | None
    ) -> float | None:
        intervals: list[float] = []
        if self.config.heartbeat_interval > 0:
            intervals.append(self.config.heartbeat_interval)
        if (
            execution is not None
            and execution.execute
            and execution.heartbeat_interval > 0
        ):
            intervals.append(execution.heartbeat_interval)
        return min(intervals) if intervals else None

    @staticmethod
    async def _await_handler(
        awaitable: Awaitable[None],
        heartbeat_task: asyncio.Task[None] | None,
        execution: JobExecution | None,
    ) -> None:
        if heartbeat_task is None:
            await awaitable
            return

        handler_task = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait(
                {handler_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                if heartbeat_task.cancelled():
                    heartbeat_error: BaseException = asyncio.CancelledError()
                else:
                    heartbeat_error = heartbeat_task.exception() or RuntimeError(
                        "job heartbeat stopped before the handler completed"
                    )
                if not handler_task.done():
                    handler_task.cancel()
                    await AsyncWorker._drain_cancelled_handler(
                        handler_task, execution
                    )
                raise heartbeat_error
            await handler_task
        except BaseException:
            if not handler_task.done():
                handler_task.cancel()
                # Renew the execution lease directly throughout the safe drain.
                # The combined delivery heartbeat may fail after this branch is
                # entered, so observing its state only once would be unsafe.
                await AsyncWorker._drain_cancelled_handler(
                    handler_task, execution
                )
            raise

    @staticmethod
    async def _drain_cancelled_handler(
        handler_task: asyncio.Future[None], execution: JobExecution | None
    ) -> None:
        # Give ordinary async handlers one cycle to observe cancellation. A
        # thread-backed handler deliberately suppresses cancellation until its
        # side effects reach a safe boundary, so keep renewing its scheduled
        # execution lease during that drain.
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        while not handler_task.done():
            if execution is not None and execution.execute:
                try:
                    await execution.renew()
                except BaseException:
                    LOGGER.error(
                        "scheduled execution lease renewal failed while draining handler"
                    )
                interval = max(0.001, execution.heartbeat_interval)
            else:
                interval = 0.1
            try:
                await asyncio.wait_for(
                    asyncio.shield(handler_task), timeout=interval
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                # Shutdown may cancel the owning _process task again while a
                # thread-backed handler is draining. Do not let that second
                # cancellation stop lease renewal before the safe boundary.
                if handler_task.done():
                    break
                continue
            except BaseException:
                break
        await asyncio.gather(handler_task, return_exceptions=True)

    async def _heartbeat(
        self,
        delivery: JobDelivery,
        stop_event: asyncio.Event,
        execution: JobExecution | None,
        interval: float,
    ) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                try:
                    if self.config.heartbeat_interval > 0:
                        await delivery.in_progress()
                    if execution is not None and execution.execute:
                        await execution.renew()
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    heartbeat_error = JobHeartbeatError(
                        f"job heartbeat failed: {type(exc).__name__}: {exc}"
                    )
                    self._set_failure(heartbeat_error)
                    raise heartbeat_error from exc

    @staticmethod
    async def _stop_heartbeat(
        stop_event: asyncio.Event,
        task: asyncio.Task[None] | None,
        *,
        propagate: bool = False,
    ) -> None:
        if task is None:
            return
        stop_event.set()
        results = await asyncio.gather(task, return_exceptions=True)
        if propagate and results and isinstance(results[0], BaseException):
            raise results[0]

    def _set_failure(self, exc: BaseException) -> None:
        mark_current_span_error()
        self.last_error = "worker operation failed"
        self.state = WorkerState.FAILED
        self.accepting_claims = False
        self._update_saturation_state()
        self._stop_requested.set()
        self._capacity_changed.set()
        fetch_task = self._fetch_task
        if fetch_task is not None and not fetch_task.done():
            fetch_task.cancel()

    def _record_probe(self, health: QueueHealth) -> None:
        self._last_health = health
        # Broker consumer statistics, rather than this process's active tasks,
        # account for backlog and all workers sharing the durable consumer.
        for state, value in (("pending", health.pending), ("in_flight", health.ack_pending)):
            set_job_queue_depth(
                value if health.ready and value is not None else float("nan"),
                state=state,
            )
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
        in_flight = len(self._active)
        available_capacity = max(0, self.config.max_in_flight - in_flight)
        throughput_snapshot: Mapping[str, Any] | None = None
        if self._throughput is not None:
            throughput_snapshot = self._throughput.snapshot().to_dict()
        return WorkerSnapshot(
            state=self.state,
            live=live,
            ready=ready,
            accepting_claims=self.accepting_claims,
            in_flight=in_flight,
            max_in_flight=self.config.max_in_flight,
            available_capacity=available_capacity,
            saturated=self._currently_saturated(),
            saturation_events=self._saturation_events,
            saturation_seconds=self._current_saturation_seconds(),
            active_job_ids=tuple(sorted(active_ids)),
            completed=self._completed,
            nacked=self._nacked,
            retried=self._retried,
            dead_lettered=self._dead_lettered,
            last_error=self.last_error,
            last_bus_probe=self._last_probe_at,
            bus=self._last_health,
            throughput=throughput_snapshot,
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
