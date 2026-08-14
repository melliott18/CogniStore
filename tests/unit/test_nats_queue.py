from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from nats.js.api import ConsumerConfig, Header
from nats.js.errors import NotFoundError

from cognistore.cli.cognistore_cli import _enqueue_job
from cognistore.jobs.models import JobEnvelope, JobEnvelopeError
from cognistore.jobs.nats_queue import (
    CORRELATION_HEADER,
    JOB_TYPE_HEADER,
    NatsJetStreamConfig,
    NatsJetStreamQueue,
)


class FakeMessage:
    def __init__(self, data: bytes, attempt: int = 1) -> None:
        self.data = data
        self.metadata = SimpleNamespace(
            num_delivered=attempt,
            sequence=SimpleNamespace(stream=7, consumer=3),
        )
        self.acks = 0
        self.nacks = 0
        self.heartbeats = 0

    async def ack_sync(self, timeout: float) -> None:
        self.acks += 1

    async def nak(self) -> None:
        self.nacks += 1

    async def in_progress(self) -> None:
        self.heartbeats += 1

class FakeSubscription:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[FakeMessage] = asyncio.Queue()
        self.unsubscribed = 0

    async def fetch(self, batch: int, timeout: float):
        assert batch == 1
        return [await asyncio.wait_for(self.messages.get(), timeout)]

    async def unsubscribe(self) -> None:
        self.unsubscribed += 1


class FakeJetStream:
    def __init__(self, subscription: FakeSubscription) -> None:
        self.subscription = subscription
        self.stream_config = None
        self.consumer_config = None
        self.published = []
        self.stream_adds = 0
        self.consumer_creates = 0

    async def add_stream(self, config):
        self.stream_adds += 1
        self.stream_config = config
        return SimpleNamespace(config=config)

    async def create_consumer_response(self, subject, request, timeout):
        payload = json.loads(request)
        assert payload["action"] == "create"
        if self.consumer_config is not None:
            return {
                "error": {
                    "code": 400,
                    "err_code": 10148,
                    "description": "consumer already exists",
                }
            }
        self.consumer_creates += 1
        self.consumer_config = ConsumerConfig.from_response(
            dict(payload["config"])
        )
        return {"type": "io.nats.jetstream.api.v1.consumer_create_response"}

    async def pull_subscribe_bind(self, consumer, stream):
        return self.subscription

    async def publish(self, subject, payload, stream, headers):
        self.published.append((subject, payload, stream, headers))
        return SimpleNamespace(stream=stream, seq=11, duplicate=False)

    async def account_info(self):
        return SimpleNamespace()

    async def stream_info(self, stream):
        if self.stream_config is None:
            raise NotFoundError()
        return SimpleNamespace(config=self.stream_config)

    async def consumer_info(self, stream, consumer):
        if self.consumer_config is None:
            raise NotFoundError()
        return SimpleNamespace(
            config=self.consumer_config,
            num_pending=4,
            num_ack_pending=1,
            num_redelivered=2,
        )


class FakeConnection:
    def __init__(self, jetstream: FakeJetStream) -> None:
        self._jetstream = jetstream
        self.is_closed = False
        self.is_draining = False
        self.is_reconnecting = False
        self.is_connected = True
        self.connected_server_version = SimpleNamespace(major=2, minor=11, patch=8)
        self.flushes = 0
        self.drains = 0
        self.closes = 0
        self.close_error: Exception | None = None

    def jetstream(self, timeout: float):
        return self._jetstream

    async def flush(self, timeout: float) -> None:
        self.flushes += 1

    async def request(self, subject, payload, timeout: float):
        response = await self._jetstream.create_consumer_response(
            subject, payload, timeout
        )
        return SimpleNamespace(data=json.dumps(response).encode("utf-8"))

    async def drain(self) -> None:
        self.drains += 1
        self.is_closed = True
        self.is_connected = False

    async def close(self) -> None:
        self.closes += 1
        if self.close_error is not None:
            raise self.close_error
        self.is_closed = True
        self.is_connected = False


def _configured_queue(monkeypatch: pytest.MonkeyPatch, *, consume: bool = True):
    subscription = FakeSubscription()
    jetstream = FakeJetStream(subscription)
    connection = FakeConnection(jetstream)

    async def connect(**options):
        return connection

    monkeypatch.setattr("cognistore.jobs.nats_queue.nats.connect", connect)
    config = NatsJetStreamConfig(
        servers=("nats://test:4222",),
        stream="TEST_JOBS",
        subject="test.jobs",
        consumer="test-workers",
    )
    return (
        NatsJetStreamQueue(config, consume=consume),
        connection,
        jetstream,
        subscription,
    )


def test_publisher_connects_without_creating_worker_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        queue, connection, jetstream, subscription = _configured_queue(
            monkeypatch, consume=False
        )
        await queue.connect()

        job = JobEnvelope.create("test.publish", {"value": 18})
        receipt = await queue.enqueue(job)
        health = await queue.probe()

        assert receipt.job_id == job.job_id
        assert jetstream.stream_config is not None
        assert jetstream.consumer_config is None
        assert health.ready is True
        assert health.pending is None
        with pytest.raises(RuntimeError, match="not configured for claims"):
            await queue.claim(timeout=0.1)

        await queue.close()
        assert subscription.unsubscribed == 0
        assert connection.drains == 1

    asyncio.run(scenario())


def test_cli_enqueue_helper_uses_publisher_only_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        queue, connection, jetstream, subscription = _configured_queue(
            monkeypatch, consume=False
        )
        job = JobEnvelope.create("test.cli-publish", {})

        receipt = await _enqueue_job(queue.config, job)

        assert receipt.job_id == job.job_id
        assert jetstream.consumer_config is None
        assert subscription.unsubscribed == 0
        assert connection.closes == 1

    asyncio.run(scenario())


def test_cli_enqueue_helper_preserves_puback_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        queue, connection, _, _ = _configured_queue(monkeypatch, consume=False)
        connection.close_error = RuntimeError("connection dropped after PubAck")
        job = JobEnvelope.create("test.cli-publish", {})

        receipt = await _enqueue_job(queue.config, job)

        assert receipt.job_id == job.job_id
        assert receipt.sequence == 11
        assert connection.closes == 1

    asyncio.run(scenario())


def test_existing_consumer_is_validated_without_being_updated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        queue, _, jetstream, _ = _configured_queue(monkeypatch)
        await queue.connect()
        assert jetstream.consumer_creates == 1

        incompatible = NatsJetStreamConfig(
            servers=queue.config.servers,
            stream=queue.config.stream,
            subject=queue.config.subject,
            consumer=queue.config.consumer,
            ack_wait=0.2,
        )
        second = NatsJetStreamQueue(incompatible)
        with pytest.raises(RuntimeError, match="incompatible configuration"):
            await second.connect()

        assert jetstream.consumer_creates == 1
        assert jetstream.consumer_config.ack_wait == queue.config.ack_wait

    asyncio.run(scenario())


def test_worker_rejects_server_without_atomic_consumer_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        queue, connection, jetstream, _ = _configured_queue(monkeypatch)
        connection.connected_server_version = SimpleNamespace(
            major=2, minor=9, patch=24
        )

        with pytest.raises(RuntimeError, match="NATS Server 2.10 or newer"):
            await queue.connect()

        assert jetstream.consumer_creates == 0
        assert connection.closes == 1

    asyncio.run(scenario())


def test_enqueue_claim_ack_and_health(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        queue, connection, jetstream, subscription = _configured_queue(monkeypatch)
        await queue.connect()
        job = JobEnvelope.create(
            "test.echo", {"message": "hello"}, correlation_id="trace-123"
        )

        receipt = await queue.enqueue(job)
        published = jetstream.published[0]
        assert JobEnvelope.from_bytes(published[1]) == job
        assert published[3][Header.MSG_ID] == job.job_id
        assert published[3][CORRELATION_HEADER] == "trace-123"
        assert published[3][JOB_TYPE_HEADER] == "test.echo"
        assert receipt.sequence == 11

        message = FakeMessage(job.to_bytes(), attempt=2)
        await subscription.messages.put(message)
        delivery = await queue.claim(timeout=0.1)
        assert delivery is not None
        assert delivery.job == job
        assert delivery.attempt == 2
        assert delivery.stream_sequence == 7
        await delivery.in_progress()
        await delivery.ack()
        await delivery.ack()
        assert message.heartbeats == 1
        assert message.acks == 1
        assert message.nacks == 0

        health = await queue.probe()
        assert health.ready is True
        assert health.jetstream is True
        assert (health.pending, health.ack_pending, health.redelivered) == (4, 1, 2)

        await queue.close(graceful=True)
        assert subscription.unsubscribed == 1
        assert connection.drains == 1

    asyncio.run(scenario())


def test_negative_ack_prevents_a_later_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        queue, _, _, subscription = _configured_queue(monkeypatch)
        await queue.connect()
        message = FakeMessage(JobEnvelope.create("test.fail", {}).to_bytes())
        await subscription.messages.put(message)

        delivery = await queue.claim(timeout=0.1)
        assert delivery is not None
        await delivery.nack()
        await delivery.ack()

        assert message.nacks == 1
        assert message.acks == 0
        await queue.close()

    asyncio.run(scenario())


def test_malformed_delivery_is_left_unsettled_for_operator_diagnosis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        queue, _, _, subscription = _configured_queue(monkeypatch)
        await queue.connect()
        message = FakeMessage(b"not-json")
        await subscription.messages.put(message)

        with pytest.raises(JobEnvelopeError):
            await queue.claim(timeout=0.1)

        assert message.nacks == 0
        assert message.acks == 0
        await queue.close()

    asyncio.run(scenario())
