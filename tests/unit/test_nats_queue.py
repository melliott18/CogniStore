from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from nats.js.api import (
    ConsumerConfig,
    DiscardPolicy,
    Header,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.errors import APIError, NotFoundError

from cognistore.cli.cognistore_cli import _enqueue_job
from cognistore.jobs.models import (
    DEAD_LETTER_CHAIN_METADATA,
    DeadLetterDisposition,
    DeadLetterRecord,
    DeadLetterRedriveError,
    JobEnvelope,
    JobEnvelopeError,
    QueueSaturatedError,
)
from cognistore.jobs.nats_queue import (
    CORRELATION_HEADER,
    DEAD_LETTER_ID_HEADER,
    JOB_TYPE_HEADER,
    NatsJetStreamConfig,
    NatsJetStreamQueue,
)


class FakeMessage:
    def __init__(self, data: bytes, attempt: int = 1, headers=None) -> None:
        self.data = data
        self.headers = headers or {}
        self.metadata = SimpleNamespace(
            num_delivered=attempt,
            sequence=SimpleNamespace(stream=7, consumer=3),
            stream="TEST_JOBS",
            consumer="test-workers",
            timestamp=datetime(2026, 8, 17, tzinfo=timezone.utc),
        )
        self.acks = 0
        self.nacks = 0
        self.nack_delays = []
        self.heartbeats = 0

    async def ack_sync(self, timeout: float) -> None:
        self.acks += 1

    async def nak(self, delay=None) -> None:
        self.nacks += 1
        self.nack_delays.append(delay)

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
        self.stream_configs = {}
        self.stream_states = {}
        self.consumer_config = None
        self.published = []
        self.publish_error: APIError | None = None
        self.max_payload: int | None = None
        self.stream_adds = 0
        self.consumer_creates = 0

    async def add_stream(self, config):
        self.stream_adds += 1
        self.stream_configs[config.name] = config
        state = SimpleNamespace(messages=0, bytes=0)
        self.stream_states[config.name] = state
        return SimpleNamespace(config=config, state=state)

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
        if self.publish_error is not None:
            raise self.publish_error
        if self.max_payload is not None:
            encoded_headers = dict(headers)
            encoded_headers[Header.EXPECTED_STREAM] = stream
            wire_size = len(payload) + len(b"NATS/1.0\r\n\r\n")
            for key, value in encoded_headers.items():
                wire_size += len(key.strip().encode("utf-8"))
                wire_size += len(b": ")
                wire_size += len(value.strip().encode("utf-8"))
                wire_size += len(b"\r\n")
            assert wire_size <= self.max_payload
        self.published.append((subject, payload, stream, headers))
        state = self.stream_states.setdefault(
            stream, SimpleNamespace(messages=0, bytes=0)
        )
        state.messages += 1
        state.bytes += len(payload)
        return SimpleNamespace(stream=stream, seq=11, duplicate=False)

    async def account_info(self):
        return SimpleNamespace()

    async def stream_info(self, stream):
        if stream not in self.stream_configs:
            raise NotFoundError()
        state = self.stream_states.setdefault(
            stream, SimpleNamespace(messages=0, bytes=0)
        )
        return SimpleNamespace(config=self.stream_configs[stream], state=state)

    async def get_last_msg(self, stream, subject):
        for index, published in reversed(list(enumerate(self.published, start=1))):
            published_subject, payload, published_stream, headers = published
            if published_stream == stream and published_subject == subject:
                return SimpleNamespace(
                    data=payload,
                    headers=headers,
                    seq=index,
                    subject=subject,
                )
        raise NotFoundError()

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
        self.max_payload = 1024 * 1024
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


def test_enqueue_carries_producer_context_without_changing_job_contract(monkeypatch) -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import SpanKind

    from cognistore import observability

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(observability, "_tracer", provider.get_tracer("jobs-test"))

    async def scenario() -> None:
        queue, _, jetstream, _ = _configured_queue(monkeypatch, consume=False)
        await queue.connect()
        try:
            with observability.request_context(correlation_id="sensitive-request-id"):
                with observability.observe("api", "request") as request:
                    job = JobEnvelope.create("catalog.scan", {"content": "private-content"})
                    original = job.to_bytes()
                    await queue.enqueue(job)
                    assert job.to_bytes() == original
                    published = JobEnvelope.from_bytes(jetstream.published[-1][1])
                    assert published.schema_version == 1
                    assert published.correlation_id == observability.current_correlation_id()
                    assert published.correlation_id != "sensitive-request-id"
                    assert published.payload == job.payload
                    assert set(published.metadata) == {"traceparent"}
                    assert jetstream.published[-1][3]["traceparent"] == published.metadata["traceparent"]
            spans = exporter.get_finished_spans()
            producer = next(span for span in spans if span.name == "queue.enqueue")
            assert producer.kind == SpanKind.PRODUCER
            assert producer.parent.span_id == request.get_span_context().span_id
            assert published.metadata["traceparent"].split("-")[2] == format(producer.context.span_id, "016x")
            assert "private-content" not in repr([dict(span.attributes) for span in spans])
            assert observability.current_correlation_id() is None
        finally:
            await queue.close()

    try:
        asyncio.run(scenario())
    finally:
        provider.shutdown()


def test_job_transport_headers_do_not_forward_untrusted_trace_metadata() -> None:
    job = JobEnvelope.create(
        "catalog.scan", {}, metadata={"traceparent": "secret\r\nAuthorization: secret", "baggage": "secret"}
    )

    headers = NatsJetStreamQueue._job_headers(job, job.job_id)

    assert "traceparent" not in headers
    assert "baggage" not in headers


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
        assert queue.config.stream in jetstream.stream_configs
        stream_config = jetstream.stream_configs[queue.config.stream]
        assert stream_config.max_msgs == queue.config.stream_max_messages
        assert stream_config.max_bytes == queue.config.stream_max_bytes
        assert stream_config.discard == DiscardPolicy.NEW
        assert jetstream.consumer_config is None
        assert health.ready is True
        assert health.pending is None
        assert health.stored_messages == 1
        assert health.stored_bytes == len(job.to_bytes())
        assert health.max_messages == queue.config.stream_max_messages
        assert health.max_bytes == queue.config.stream_max_bytes
        assert health.utilization is not None
        assert health.saturated is False

        state = jetstream.stream_states[queue.config.stream]
        state.messages = queue.config.stream_max_messages
        saturated_health = await queue.probe()
        assert saturated_health.utilization == pytest.approx(1.0)
        assert saturated_health.saturated is True
        assert saturated_health.to_dict()["saturated"] is True
        with pytest.raises(RuntimeError, match="not configured for claims"):
            await queue.claim(timeout=0.1)

        await queue.close()
        assert subscription.unsubscribed == 0
        assert connection.drains == 1

    asyncio.run(scenario())


def test_dead_letter_publish_lookup_and_redrive_preserve_logical_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        queue, _, jetstream, _ = _configured_queue(monkeypatch)
        await queue.connect()
        job = JobEnvelope.create(
            "policy.run",
            {"bucket": "documents"},
            correlation_id="request-24",
        )
        record = DeadLetterRecord.create(
            failed_at=datetime(2026, 8, 17, 12, tzinfo=timezone.utc),
            disposition=DeadLetterDisposition.EXHAUSTED,
            retryable=True,
            category="timeout",
            classification_reason="operation timed out",
            attempt=3,
            max_attempts=3,
            cumulative_attempt=3,
            source_stream=queue.config.stream,
            source_published_at=datetime(2026, 8, 17, 11, tzinfo=timezone.utc),
            source_consumer=queue.config.consumer,
            stream_sequence=77,
            consumer_sequence=12,
            exception_type="builtins.TimeoutError",
            exception_message="backend timed out",
            traceback="timeout traceback",
            raw_data=job.to_bytes(),
            headers={CORRELATION_HEADER: job.correlation_id},
            job=job,
        )

        dead_letter_receipt = await queue.publish_dead_letter(record)
        assert dead_letter_receipt.dead_letter_id == record.dead_letter_id
        dlq_publish = jetstream.published[-1]
        assert dlq_publish[0].endswith(record.dead_letter_id)
        assert dlq_publish[2] == queue.config.resolved_dead_letter_stream
        assert dlq_publish[3][DEAD_LETTER_ID_HEADER] == record.dead_letter_id
        assert await queue.get_dead_letter(record.dead_letter_id) == record

        # Simulate a source ACK with an unknown outcome followed by a later
        # redelivery. The original immutable diagnostic wins even though the
        # new observation has a later timestamp and attempt number.
        duplicate_record = DeadLetterRecord.create(
            failed_at=datetime(2026, 8, 17, 12, 5, tzinfo=timezone.utc),
            disposition=DeadLetterDisposition.EXHAUSTED,
            retryable=True,
            category="timeout",
            classification_reason="operation timed out",
            attempt=4,
            max_attempts=3,
            cumulative_attempt=4,
            source_stream=queue.config.stream,
            source_published_at=datetime(2026, 8, 17, 11, tzinfo=timezone.utc),
            source_consumer=queue.config.consumer,
            stream_sequence=77,
            consumer_sequence=13,
            exception_type="builtins.TimeoutError",
            exception_message="backend timed out again",
            traceback="later timeout traceback",
            raw_data=job.to_bytes(),
            headers={CORRELATION_HEADER: job.correlation_id},
            job=job,
        )
        original_publish = jetstream.publish

        async def reject_existing_entry(subject, payload, stream, headers):
            if ".entry." in subject:
                raise APIError(
                    code=400,
                    err_code=10071,
                    description="wrong last sequence",
                )
            return await original_publish(subject, payload, stream, headers)

        jetstream.publish = reject_existing_entry
        publish_count_before_duplicate = len(jetstream.published)
        duplicate_receipt = await queue.publish_dead_letter(duplicate_record)
        assert duplicate_record.dead_letter_id == record.dead_letter_id
        assert duplicate_receipt.duplicate is True
        assert len(jetstream.published) == publish_count_before_duplicate
        assert await queue.get_dead_letter(record.dead_letter_id) == record

        original_publish_job = queue._publish_job

        async def fail_after_intent(
            job, *, transport_id, reserve_redrive_headroom
        ):
            raise ConnectionError("main stream unavailable after intent PubAck")

        queue._publish_job = fail_after_intent  # type: ignore[method-assign]
        with pytest.raises(ConnectionError, match="after intent PubAck"):
            await queue.redrive_dead_letter(record.dead_letter_id)
        queue._publish_job = original_publish_job  # type: ignore[method-assign]

        # The durable intent is self-contained. It can finish even if the
        # older diagnostic entry and chunks expire during an enqueue outage.
        intent_subject = queue._redrive_intent_subject(record.dead_letter_id)
        jetstream.published[:] = [
            item
            for item in jetstream.published
            if item[0] == intent_subject
            or (
                f".entry.{record.dead_letter_id}" not in item[0]
                and f".chunk.{record.dead_letter_id}." not in item[0]
            )
        ]

        first_redrive = await queue.redrive_dead_letter(record.dead_letter_id)
        redrive_publish = next(
            item
            for item in jetstream.published
            if item[2] == queue.config.stream
            and item[3][Header.MSG_ID] == f"redrive:{record.dead_letter_id}"
        )
        redriven_job = JobEnvelope.from_bytes(redrive_publish[1])
        assert redriven_job.job_id == job.job_id
        assert redriven_job.correlation_id == job.correlation_id
        assert json.loads(redriven_job.metadata[DEAD_LETTER_CHAIN_METADATA]) == [
            record.dead_letter_id
        ]
        assert first_redrive.dead_letter_id == record.dead_letter_id
        assert first_redrive.job_id == job.job_id
        assert first_redrive.redrive_count == 1
        assert first_redrive.duplicate is False

        audit_subject = queue._redrive_audit_subject(record.dead_letter_id)
        published_subjects = [item[0] for item in jetstream.published]
        assert published_subjects.count(intent_subject) == 1
        assert published_subjects.index(intent_subject) < published_subjects.index(
            queue.config.subject
        )
        assert published_subjects.index(queue.config.subject) < published_subjects.index(
            audit_subject
        )

        publish_count = len(jetstream.published)
        second_redrive = await queue.redrive_dead_letter(record.dead_letter_id)
        assert len(jetstream.published) == publish_count
        assert second_redrive.job_id == job.job_id
        assert second_redrive.duplicate is True
        await queue.close()

    asyncio.run(scenario())


def test_enqueue_reserves_payload_headroom_for_first_redrive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        queue, connection, jetstream, _ = _configured_queue(monkeypatch)
        connection.max_payload = 1024
        jetstream.max_payload = connection.max_payload
        await queue.connect()
        safe_job = JobEnvelope.create("policy.run", {"payload": ""})

        receipt = await queue.enqueue(safe_job)
        assert receipt.job_id == safe_job.job_id

        too_large = JobEnvelope.create("policy.run", {"payload": "x" * 200})
        before_rejection = len(jetstream.published)
        with pytest.raises(JobEnvelopeError, match="cannot reserve a first redrive"):
            await queue.enqueue(too_large)
        assert len(jetstream.published) == before_rejection

        record = DeadLetterRecord.create(
            failed_at=datetime(2026, 8, 17, 12, tzinfo=timezone.utc),
            disposition=DeadLetterDisposition.EXHAUSTED,
            retryable=True,
            category="timeout",
            classification_reason="operation timed out",
            attempt=1,
            max_attempts=1,
            cumulative_attempt=1,
            source_stream=queue.config.stream,
            source_published_at=datetime(2026, 8, 17, 11, tzinfo=timezone.utc),
            source_consumer=queue.config.consumer,
            stream_sequence=99,
            consumer_sequence=99,
            exception_type="builtins.TimeoutError",
            exception_message="backend timed out",
            traceback="timeout traceback",
            raw_data=safe_job.to_bytes(),
            headers={},
            job=safe_job,
        )
        await queue.publish_dead_letter(record)
        redrive = await queue.redrive_dead_letter(record.dead_letter_id)
        assert redrive.job_id == safe_job.job_id
        await queue.close()

    asyncio.run(scenario())


def test_publish_wire_limit_includes_utf8_headers_and_expected_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        queue, connection, jetstream, _ = _configured_queue(monkeypatch)
        await queue.connect()
        job = JobEnvelope.create(
            "test.écho", {}, correlation_id="追跡-id"
        )
        transport_id = "custom-" + "x" * 80
        headers = queue._job_headers(job, transport_id)
        exact_size = queue._nats_wire_size(
            job.to_bytes(), headers, stream=queue.config.stream
        )
        with pytest.raises(ValueError, match="must not contain CR or LF"):
            queue._nats_wire_size(
                b"payload",
                {"Trace": "safe\r\nNats-Msg-Id: forged"},
                stream=queue.config.stream,
            )
        with pytest.raises(ValueError, match="must be valid UTF-8"):
            queue._nats_wire_size(
                b"payload", {"Trace": "\ud800"}, stream=queue.config.stream
            )
        connection.max_payload = exact_size
        jetstream.max_payload = exact_size

        receipt = await queue._publish_job(
            job,
            transport_id=transport_id,
            reserve_redrive_headroom=False,
        )
        assert receipt.job_id == job.job_id

        connection.max_payload = exact_size - 1
        jetstream.max_payload = exact_size - 1
        before_rejection = len(jetstream.published)
        with pytest.raises(JobEnvelopeError, match="bytes on the wire"):
            await queue._publish_job(
                job,
                transport_id=transport_id,
                reserve_redrive_headroom=False,
            )
        assert len(jetstream.published) == before_rejection
        assert connection.is_connected is True
        await queue.close()

    asyncio.run(scenario())


def test_legacy_oversized_redrive_is_rejected_before_any_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        queue, connection, jetstream, _ = _configured_queue(monkeypatch)
        await queue.connect()
        job = JobEnvelope.create("policy.run", {"payload": "x" * 200})
        record = DeadLetterRecord.create(
            failed_at=datetime(2026, 8, 17, 12, tzinfo=timezone.utc),
            disposition=DeadLetterDisposition.EXHAUSTED,
            retryable=True,
            category="timeout",
            classification_reason="operation timed out",
            attempt=1,
            max_attempts=1,
            cumulative_attempt=1,
            source_stream=queue.config.stream,
            source_published_at=datetime(2026, 8, 17, 11, tzinfo=timezone.utc),
            source_consumer=queue.config.consumer,
            stream_sequence=100,
            consumer_sequence=100,
            exception_type="builtins.TimeoutError",
            exception_message="backend timed out",
            traceback="timeout traceback",
            raw_data=job.to_bytes(),
            headers={},
            job=job,
        )
        await queue.publish_dead_letter(record)
        before_redrive = len(jetstream.published)
        connection.max_payload = 800
        jetstream.max_payload = 800

        with pytest.raises(DeadLetterRedriveError, match="projected redrive"):
            await queue.redrive_dead_letter(record.dead_letter_id)
        assert len(jetstream.published) == before_redrive
        assert connection.is_connected is True
        await queue.close()

    asyncio.run(scenario())


def test_main_stream_max_message_size_limits_redrive_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        queue, _, jetstream, _ = _configured_queue(monkeypatch, consume=False)
        jetstream.stream_configs[queue.config.stream] = StreamConfig(
            name=queue.config.stream,
            subjects=[queue.config.subject],
            retention=RetentionPolicy.WORK_QUEUE,
            storage=StorageType.FILE,
            max_msgs=queue.config.stream_max_messages,
            max_bytes=queue.config.stream_max_bytes,
            discard=DiscardPolicy.NEW,
            duplicate_window=queue.config.duplicate_window,
            max_msg_size=500,
        )
        await queue.connect()
        job = JobEnvelope.create("policy.run", {})

        with pytest.raises(JobEnvelopeError, match="message limit is 500"):
            await queue.enqueue(job)
        assert jetstream.published == []
        await queue.close()

    asyncio.run(scenario())


def test_large_dead_letter_is_chunked_below_server_payload_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        queue, connection, jetstream, _ = _configured_queue(monkeypatch)
        connection.max_payload = 1024
        jetstream.max_payload = connection.max_payload
        await queue.connect()
        job = JobEnvelope.create("policy.run", {"payload": "x" * 8_000})
        record = DeadLetterRecord.create(
            failed_at=datetime(2026, 8, 17, 12, tzinfo=timezone.utc),
            disposition=DeadLetterDisposition.EXHAUSTED,
            retryable=True,
            category="unavailable",
            classification_reason="HTTP 503",
            attempt=7,
            max_attempts=7,
            cumulative_attempt=7,
            source_stream=queue.config.stream,
            source_published_at=datetime(2026, 8, 17, 11, tzinfo=timezone.utc),
            source_consumer=queue.config.consumer,
            stream_sequence=90,
            consumer_sequence=20,
            exception_type="builtins.ConnectionError",
            exception_message="backend unavailable",
            traceback="connection traceback",
            raw_data=job.to_bytes(),
            headers={},
            job=job,
        )

        await queue.publish_dead_letter(record)

        dlq_messages = [
            published
            for published in jetstream.published
            if published[2] == queue.config.resolved_dead_letter_stream
        ]
        assert len(dlq_messages) > 2
        assert max(len(published[1]) for published in dlq_messages) <= 1024
        assert await queue.get_dead_letter(record.dead_letter_id) == record
        await queue.close()

    asyncio.run(scenario())


def test_dead_letter_retention_cannot_be_shorter_than_deduplication() -> None:
    with pytest.raises(ValueError, match="at least duplicate_window"):
        NatsJetStreamConfig(duplicate_window=120, dead_letter_max_age=60)


@pytest.mark.parametrize("field_name", ["stream_max_messages", "stream_max_bytes"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_main_stream_capacity_must_be_a_positive_integer(
    field_name: str, value: object
) -> None:
    with pytest.raises(ValueError, match=f"{field_name} must be a positive integer"):
        NatsJetStreamConfig(**{field_name: value})


def test_new_stream_limits_preserve_legacy_positional_config_fields() -> None:
    config = NatsJetStreamConfig(
        ("nats://legacy:4222",),
        "LEGACY",
        "legacy.jobs",
        "legacy-workers",
        30.0,
        2,
        90.0,
        3.0,
        4.0,
        5,
        True,
        True,
        6.0,
        7.0,
        "legacy-client",
        None,
        None,
        180.0,
    )

    assert config.duplicate_window == 90.0
    assert config.client_name == "legacy-client"
    assert config.dead_letter_max_age == 180.0
    assert config.stream_max_messages == 10_000
    assert config.stream_max_bytes == 1024 * 1024 * 1024


def test_worker_rejects_incompatible_unbounded_main_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        queue, connection, jetstream, _ = _configured_queue(monkeypatch)
        jetstream.stream_configs[queue.config.stream] = StreamConfig(
            name=queue.config.stream,
            subjects=[queue.config.subject],
            retention=RetentionPolicy.WORK_QUEUE,
            storage=StorageType.FILE,
            max_msgs=-1,
            max_bytes=-1,
            discard=DiscardPolicy.OLD,
            duplicate_window=queue.config.duplicate_window,
        )

        with pytest.raises(RuntimeError, match="incompatible configuration"):
            await queue.connect()
        assert connection.closes == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("max_age", 60.0),
        ("max_msgs_per_subject", 10),
        ("discard_new_per_subject", True),
        ("no_ack", True),
        ("sealed", True),
        ("allow_rollup_hdrs", True),
        ("allow_msg_ttl", True),
        ("subject_transform", object()),
        ("mirror", object()),
        ("sources", [object()]),
        ("persist_mode", "async"),
    ],
)
def test_worker_rejects_hidden_unsafe_main_stream_settings(
    monkeypatch: pytest.MonkeyPatch, field_name: str, value: object
) -> None:
    async def scenario() -> None:
        queue, connection, jetstream, _ = _configured_queue(monkeypatch)
        config = StreamConfig(
            name=queue.config.stream,
            subjects=[queue.config.subject],
            retention=RetentionPolicy.WORK_QUEUE,
            storage=StorageType.FILE,
            max_msgs=queue.config.stream_max_messages,
            max_bytes=queue.config.stream_max_bytes,
            max_age=0,
            max_msgs_per_subject=-1,
            discard=DiscardPolicy.NEW,
            duplicate_window=queue.config.duplicate_window,
        )
        setattr(config, field_name, value)
        jetstream.stream_configs[queue.config.stream] = config

        with pytest.raises(RuntimeError, match="incompatible configuration"):
            await queue.connect()
        assert connection.closes == 1

    asyncio.run(scenario())


def test_probe_reports_effective_capacity_and_rejects_live_topology_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        queue, _, jetstream, _ = _configured_queue(monkeypatch)
        await queue.connect()
        stream_config = jetstream.stream_configs[queue.config.stream]
        stream_config.max_msgs = 17
        stream_config.discard = DiscardPolicy.OLD

        health = await queue.probe()

        assert health.ready is False
        assert health.jetstream is True
        assert health.max_messages == 17
        assert health.max_bytes == queue.config.stream_max_bytes
        assert health.error is not None
        assert "stream" in health.error
        assert "incompatible configuration" in health.error
        await queue.close()

    asyncio.run(scenario())


def test_probe_rejects_live_consumer_topology_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        queue, _, jetstream, _ = _configured_queue(monkeypatch)
        await queue.connect()
        jetstream.consumer_config.headers_only = True

        health = await queue.probe()

        assert health.ready is False
        assert health.jetstream is True
        assert health.error is not None
        assert "consumer" in health.error
        assert "incompatible configuration" in health.error
        await queue.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "reason", ["maximum messages exceeded", "maximum bytes exceeded"]
)
def test_enqueue_propagates_bounded_stream_saturation(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    async def scenario() -> None:
        queue, _, jetstream, _ = _configured_queue(monkeypatch, consume=False)
        await queue.connect()
        jetstream.publish_error = APIError(
            code=503,
            err_code=10077,
            description=reason,
        )

        with pytest.raises(QueueSaturatedError, match=reason) as raised:
            await queue.enqueue(JobEnvelope.create("test.saturated", {}))
        assert raised.value.retryable is True
        assert raised.value.stream == queue.config.stream
        await queue.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("err_code", "reason"),
    [
        (10077, "storage is temporarily unavailable"),
        (10999, "maximum messages exceeded"),
    ],
)
def test_enqueue_does_not_overmatch_stream_store_failure(
    monkeypatch: pytest.MonkeyPatch, err_code: int, reason: str
) -> None:
    async def scenario() -> None:
        queue, _, jetstream, _ = _configured_queue(monkeypatch, consume=False)
        await queue.connect()
        error = APIError(
            code=503,
            err_code=err_code,
            description=reason,
        )
        jetstream.publish_error = error

        with pytest.raises(APIError) as raised:
            await queue.enqueue(JobEnvelope.create("test.store-error", {}))
        assert raised.value is error
        await queue.close()

    asyncio.run(scenario())


def test_worker_rejects_dead_letter_stream_that_can_evict_live_record_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        queue, connection, jetstream, _ = _configured_queue(monkeypatch)
        jetstream.stream_configs[queue.config.resolved_dead_letter_stream] = StreamConfig(
            name=queue.config.resolved_dead_letter_stream,
            subjects=[f"{queue.config.resolved_dead_letter_subject}.>"],
            retention=RetentionPolicy.LIMITS,
            storage=StorageType.FILE,
            duplicate_window=queue.config.duplicate_window,
            max_age=queue.config.dead_letter_max_age,
            max_msgs=1,
            max_bytes=-1,
            max_msgs_per_subject=-1,
            max_msg_size=-1,
        )

        with pytest.raises(RuntimeError, match="incompatible configuration"):
            await queue.connect()
        assert connection.closes == 1

    asyncio.run(scenario())


def test_malformed_dead_letter_cannot_be_automatically_redriven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        queue, _, _, _ = _configured_queue(monkeypatch)
        await queue.connect()
        record = DeadLetterRecord.create(
            failed_at=datetime(2026, 8, 17, 12, tzinfo=timezone.utc),
            disposition=DeadLetterDisposition.TERMINAL,
            retryable=False,
            category="invalid",
            classification_reason="malformed envelope",
            attempt=1,
            max_attempts=3,
            cumulative_attempt=1,
            source_stream=queue.config.stream,
            source_published_at=datetime(2026, 8, 17, 11, tzinfo=timezone.utc),
            source_consumer=queue.config.consumer,
            stream_sequence=78,
            consumer_sequence=13,
            exception_type="cognistore.jobs.models.JobEnvelopeError",
            exception_message="malformed envelope",
            traceback="parser traceback",
            raw_data=b"not-json",
            headers={},
            job=None,
        )
        await queue.publish_dead_letter(record)

        with pytest.raises(DeadLetterRedriveError, match="no valid job envelope"):
            await queue.redrive_dead_letter(record.dead_letter_id)
        await queue.close()

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


def test_existing_headers_only_consumer_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        first, _, jetstream, _ = _configured_queue(monkeypatch)
        await first.connect()
        jetstream.consumer_config.headers_only = True

        second = NatsJetStreamQueue(first.config)
        with pytest.raises(RuntimeError, match="incompatible configuration"):
            await second.connect()

        assert jetstream.consumer_creates == 1

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
        await delivery.nack(delay=1.5)
        await delivery.ack()

        assert message.nacks == 1
        assert message.nack_delays == [1.5]
        assert message.acks == 0
        await queue.close()

    asyncio.run(scenario())


def test_malformed_delivery_retains_raw_message_and_settlement_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        queue, _, _, subscription = _configured_queue(monkeypatch)
        await queue.connect()
        message = FakeMessage(b"not-json")
        await subscription.messages.put(message)

        delivery = await queue.claim(timeout=0.1)
        assert delivery is not None
        assert delivery.raw_data == b"not-json"
        with pytest.raises(JobEnvelopeError):
            _ = delivery.job

        assert message.nacks == 0
        assert message.acks == 0
        await queue.close()

    asyncio.run(scenario())


def test_nats_connection_error_callback_redacts_driver_exception(caplog) -> None:
    from cognistore.jobs.nats_queue import _report_connection_error

    asyncio.run(_report_connection_error(ConnectionError("nats://secret:password@host/private")))

    assert "NATS connection failed" in caplog.text
    assert "secret" not in caplog.text
    assert "password" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
