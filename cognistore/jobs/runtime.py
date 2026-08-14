from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping

from .models import (
    BusState,
    InvalidJobError,
    JobContext,
    JobEnvelope,
    JobEnvelopeError,
    QueueHealth,
)
from .protocols import JobDelivery, JobQueue


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


class AsyncWorker:
    """Claims and settles jobs while preserving at-least-once delivery.

    A handler return is acknowledged, a handler exception is negatively
    acknowledged, and a process death before settlement leaves the message for
    broker redelivery. Handlers must therefore treat duplicate delivery as a
    normal condition and use ``job.job_id`` as their idempotency key.
    """

    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[str, JobHandler],
        *,
        config: WorkerConfig | None = None,
    ) -> None:
        if not handlers:
            raise ValueError("at least one job handler is required")
        self.queue = queue
        self.handlers = dict(handlers)
        self.config = config or WorkerConfig()
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
                        # No quarantine store exists until ticket #24. Stop
                        # without settlement so the durable stream retains the
                        # malformed job for diagnosis and later recovery.
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
                        name=f"cognistore-job-{delivery.job.job_id}",
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

                    settled_jobs = self._completed + self._nacked
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
        job = delivery.job
        handler = self.handlers.get(job.job_type)
        heartbeat_stop = asyncio.Event()
        heartbeat_task: asyncio.Task[None] | None = None
        if self.config.heartbeat_interval > 0:
            heartbeat_task = asyncio.create_task(
                self._heartbeat(delivery, heartbeat_stop),
                name=f"cognistore-heartbeat-{job.job_id}",
            )

        context = JobContext(
            attempt=delivery.attempt,
            redelivered=delivery.attempt > 1,
            stream_sequence=delivery.stream_sequence,
            consumer_sequence=delivery.consumer_sequence,
            shutdown_requested=self._stop_requested,
        )
        try:
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
            self.last_error = (
                f"job {job.job_id} ({job.job_type}) failed: "
                f"{type(exc).__name__}: {exc}"
            )
            if isinstance(exc, InvalidJobError):
                LOGGER.exception(
                    "invalid job; worker is failing closed without settlement",
                    extra={"job_id": job.job_id, "correlation_id": job.correlation_id},
                )
            else:
                LOGGER.exception(
                    "job failed; requesting redelivery",
                    extra={"job_id": job.job_id, "correlation_id": job.correlation_id},
                )
            await self._stop_heartbeat(heartbeat_stop, heartbeat_task)
            try:
                if isinstance(exc, InvalidJobError):
                    # Without a quarantine store (ticket #24), fail closed and
                    # leave invalid work unacknowledged in JetStream.
                    self._set_failure(exc)
                else:
                    await delivery.nack()
                    self._nacked += 1
            except BaseException as settlement_error:
                self._set_failure(settlement_error)
            return

        await self._stop_heartbeat(heartbeat_stop, heartbeat_task)
        try:
            await delivery.ack()
            self._completed += 1
        except BaseException as exc:
            # ACK outcome is unknown. Never follow a failed double-ACK with NACK;
            # AckWait redelivery is the only safe recovery.
            self._set_failure(exc)

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
        active_ids = tuple(
            sorted(delivery.job.job_id for delivery in self._active.values())
        )
        return WorkerSnapshot(
            state=self.state,
            live=live,
            ready=ready,
            accepting_claims=self.accepting_claims,
            in_flight=len(self._active),
            active_job_ids=active_ids,
            completed=self._completed,
            nacked=self._nacked,
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
        )
