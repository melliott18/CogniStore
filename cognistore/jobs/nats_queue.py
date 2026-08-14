from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import nats
from nats.errors import NoRespondersError, TimeoutError as NatsTimeoutError
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DEFAULT_PREFIX,
    DeliverPolicy,
    Header,
    ReplayPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.errors import APIError, NotFoundError, ServiceUnavailableError

from .models import BusState, EnqueueReceipt, JobEnvelope, QueueHealth


CORRELATION_HEADER = "CogniStore-Correlation-Id"
JOB_TYPE_HEADER = "CogniStore-Job-Type"
CONSUMER_ALREADY_EXISTS = 10148
MINIMUM_NATS_SERVER = (2, 10)


@dataclass(frozen=True)
class NatsJetStreamConfig:
    servers: tuple[str, ...] = ("nats://127.0.0.1:4222",)
    stream: str = "COGNISTORE_JOBS"
    subject: str = "cognistore.jobs"
    consumer: str = "cognistore-workers"
    ack_wait: float = 30.0
    max_ack_pending: int = 1
    duplicate_window: float = 120.0
    connect_timeout: float = 2.0
    request_timeout: float = 5.0
    drain_timeout: float = 30.0
    client_name: str = "cognistore"

    def __post_init__(self) -> None:
        if not self.servers or any(not value.strip() for value in self.servers):
            raise ValueError("at least one non-empty NATS server URL is required")
        for field_name in ("stream", "subject", "consumer", "client_name"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must be non-empty")
        for field_name in (
            "ack_wait",
            "duplicate_window",
            "connect_timeout",
            "request_timeout",
            "drain_timeout",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be greater than zero")
        if self.max_ack_pending < 1:
            raise ValueError("max_ack_pending must be at least one")


class NatsJobDelivery:
    """A single JetStream delivery with serialized terminal settlement."""

    def __init__(self, message: Any, job: JobEnvelope, ack_timeout: float) -> None:
        metadata = message.metadata
        self._message = message
        self._ack_timeout = ack_timeout
        self._lock = asyncio.Lock()
        self._settled = False
        self.job = job
        self.attempt = metadata.num_delivered
        self.stream_sequence = metadata.sequence.stream
        self.consumer_sequence = metadata.sequence.consumer

    @property
    def settled(self) -> bool:
        return self._settled

    async def ack(self) -> None:
        async with self._lock:
            if self._settled:
                return
            # Double acknowledgement means success is reported only after the
            # server confirms it recorded the ACK.
            await self._message.ack_sync(timeout=self._ack_timeout)
            self._settled = True

    async def nack(self) -> None:
        async with self._lock:
            if self._settled:
                return
            await self._message.nak()
            self._settled = True

    async def in_progress(self) -> None:
        async with self._lock:
            if not self._settled:
                await self._message.in_progress()


class NatsJetStreamQueue:
    """Durable, at-least-once job queue backed by NATS JetStream."""

    def __init__(self, config: NatsJetStreamConfig, *, consume: bool = True) -> None:
        self.config = config
        self._consume = consume
        self._connection: Any | None = None
        self._jetstream: Any | None = None
        self._subscription: Any | None = None

    async def connect(self) -> None:
        if self._connection is not None and not self._connection.is_closed:
            return
        connection = await nats.connect(
            servers=list(self.config.servers),
            name=self.config.client_name,
            connect_timeout=self.config.connect_timeout,
            drain_timeout=self.config.drain_timeout,
        )
        self._connection = connection
        self._jetstream = connection.jetstream(timeout=self.config.request_timeout)
        try:
            await self._ensure_stream()
            if self._consume:
                await self._ensure_consumer()
                self._subscription = await self._jetstream.pull_subscribe_bind(
                    consumer=self.config.consumer,
                    stream=self.config.stream,
                )
        except BaseException:
            await connection.close()
            self._connection = None
            self._jetstream = None
            self._subscription = None
            raise

    async def _ensure_stream(self) -> None:
        assert self._jetstream is not None
        try:
            stream_info = await self._jetstream.stream_info(self.config.stream)
        except NotFoundError:
            stream_info = await self._jetstream.add_stream(
                config=StreamConfig(
                    name=self.config.stream,
                    description="Durable CogniStore background jobs",
                    subjects=[self.config.subject],
                    retention=RetentionPolicy.WORK_QUEUE,
                    storage=StorageType.FILE,
                    duplicate_window=self.config.duplicate_window,
                )
            )
        stream_config = stream_info.config
        if (
            stream_config.name != self.config.stream
            or stream_config.retention != RetentionPolicy.WORK_QUEUE
            or stream_config.storage != StorageType.FILE
            or set(stream_config.subjects or ()) != {self.config.subject}
            or stream_config.duplicate_window is None
            or abs(
                stream_config.duplicate_window - self.config.duplicate_window
            )
            > 0.001
        ):
            raise RuntimeError(
                f"NATS stream {self.config.stream!r} has incompatible configuration"
            )

    async def _ensure_consumer(self) -> None:
        assert self._jetstream is not None
        assert self._connection is not None
        version = self._connection.connected_server_version
        if (version.major, version.minor) < MINIMUM_NATS_SERVER:
            raise RuntimeError(
                "CogniStore workers require NATS Server 2.10 or newer for "
                "atomic durable-consumer creation"
            )

        desired = ConsumerConfig(
            name=self.config.consumer,
            durable_name=self.config.consumer,
            description="Shared CogniStore worker consumer",
            filter_subject=self.config.subject,
            deliver_policy=DeliverPolicy.ALL,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=self.config.ack_wait,
            max_deliver=-1,
            max_ack_pending=self.config.max_ack_pending,
            replay_policy=ReplayPolicy.INSTANT,
        )
        # nats-py currently exposes only create-or-update. Use JetStream's
        # server-side create action so two first-starting workers cannot race
        # and rewrite each other's AckWait while work is in flight.
        request = {
            "stream_name": self.config.stream,
            "config": desired.as_dict(),
            "action": "create",
        }
        subject = (
            f"{DEFAULT_PREFIX}.CONSUMER.CREATE."
            f"{self.config.stream}.{self.config.consumer}.{self.config.subject}"
        )
        try:
            try:
                response_message = await self._connection.request(
                    subject,
                    json.dumps(request).encode("utf-8"),
                    timeout=self.config.request_timeout,
                )
            except NoRespondersError as exc:
                raise ServiceUnavailableError() from exc
            response = json.loads(response_message.data)
            if not isinstance(response, dict):
                raise RuntimeError("NATS consumer-create response must be an object")
            error = response.get("error")
            if error is not None:
                if not isinstance(error, dict):
                    raise RuntimeError("NATS consumer-create error must be an object")
                APIError.from_error(error)
        except APIError as exc:
            if exc.err_code != CONSUMER_ALREADY_EXISTS:
                raise

        consumer_info = await self._jetstream.consumer_info(
            self.config.stream, self.config.consumer
        )
        consumer_config = consumer_info.config
        if (
            consumer_config.deliver_policy != DeliverPolicy.ALL
            or consumer_config.ack_policy != AckPolicy.EXPLICIT
            or consumer_config.filter_subject != self.config.subject
            or consumer_config.deliver_subject is not None
            or consumer_config.max_ack_pending != self.config.max_ack_pending
            or consumer_config.max_deliver != -1
            or bool(consumer_config.backoff)
            or consumer_config.replay_policy != ReplayPolicy.INSTANT
            or consumer_config.durable_name != self.config.consumer
            or consumer_config.name != self.config.consumer
            or consumer_config.ack_wait is None
            or abs(consumer_config.ack_wait - self.config.ack_wait) > 0.001
        ):
            raise RuntimeError(
                f"NATS consumer {self.config.consumer!r} has incompatible configuration"
            )

    def _require_connected(self) -> tuple[Any, Any]:
        if self._connection is None or self._jetstream is None:
            raise RuntimeError("NATS queue is not connected")
        return self._connection, self._jetstream

    def _require_consumer(self) -> tuple[Any, Any, Any]:
        connection, jetstream = self._require_connected()
        if self._subscription is None:
            raise RuntimeError("NATS queue is not configured for claims")
        return connection, jetstream, self._subscription

    async def enqueue(self, job: JobEnvelope) -> EnqueueReceipt:
        _, jetstream = self._require_connected()
        acknowledgement = await jetstream.publish(
            self.config.subject,
            job.to_bytes(),
            stream=self.config.stream,
            headers={
                Header.MSG_ID: job.job_id,
                CORRELATION_HEADER: job.correlation_id,
                JOB_TYPE_HEADER: job.job_type,
            },
        )
        return EnqueueReceipt(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            stream=acknowledgement.stream,
            sequence=acknowledgement.seq,
            duplicate=bool(acknowledgement.duplicate),
        )

    async def claim(self, timeout: float) -> NatsJobDelivery | None:
        _, _, subscription = self._require_consumer()
        try:
            messages = await subscription.fetch(batch=1, timeout=timeout)
        except NatsTimeoutError:
            return None
        message = messages[0]
        job = JobEnvelope.from_bytes(message.data)
        return NatsJobDelivery(message, job, self.config.request_timeout)

    def _bus_state(self) -> BusState:
        connection = self._connection
        if connection is None:
            return BusState.DISCONNECTED
        if connection.is_closed:
            return BusState.CLOSED
        if connection.is_draining:
            return BusState.DRAINING
        if connection.is_reconnecting:
            return BusState.RECONNECTING
        if connection.is_connected:
            return BusState.CONNECTED
        return BusState.DISCONNECTED

    async def probe(self) -> QueueHealth:
        state = self._bus_state()
        if state != BusState.CONNECTED or self._jetstream is None:
            return QueueHealth(
                state=state,
                ready=False,
                jetstream=False,
                stream=self.config.stream,
                consumer=self.config.consumer,
                error="NATS is not connected",
            )
        try:
            assert self._connection is not None
            await self._connection.flush(timeout=self.config.request_timeout)
            await self._jetstream.account_info()
            await self._jetstream.stream_info(self.config.stream)
            consumer = None
            if self._consume:
                consumer = await self._jetstream.consumer_info(
                    self.config.stream, self.config.consumer
                )
        except Exception as exc:
            return QueueHealth(
                state=self._bus_state(),
                ready=False,
                jetstream=False,
                stream=self.config.stream,
                consumer=self.config.consumer,
                error=f"{type(exc).__name__}: {exc}",
            )
        return QueueHealth(
            state=self._bus_state(),
            ready=self._bus_state() == BusState.CONNECTED,
            jetstream=True,
            stream=self.config.stream,
            consumer=self.config.consumer,
            pending=consumer.num_pending if consumer is not None else None,
            ack_pending=consumer.num_ack_pending if consumer is not None else None,
            redelivered=consumer.num_redelivered if consumer is not None else None,
        )

    async def close(self, *, graceful: bool = True) -> None:
        connection = self._connection
        subscription = self._subscription
        self._subscription = None
        self._jetstream = None
        self._connection = None
        if connection is None or connection.is_closed:
            return
        if graceful:
            if subscription is not None:
                await subscription.unsubscribe()
            await connection.drain()
        else:
            await connection.close()
