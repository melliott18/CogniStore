from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from uuid import uuid4

import pytest
from nats.js.errors import NotFoundError

from cognistore.jobs.models import (
    ATTEMPT_OFFSET_METADATA,
    DEAD_LETTER_CHAIN_METADATA,
    REDRIVE_COUNT_METADATA,
    JobEnvelope,
    QueueSaturatedError,
)
from cognistore.jobs.nats_queue import NatsJetStreamConfig, NatsJetStreamQueue
from cognistore.jobs.runtime import AsyncWorker, WorkerConfig, WorkerState

NATS_URL = os.environ.get("COGNISTORE_NATS_URL")
TEST_STREAM_MAX_BYTES = 16 * 1024 * 1024
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not NATS_URL,
        reason="set COGNISTORE_NATS_URL to an isolated JetStream-enabled NATS server",
    ),
]


def _config(*, ack_wait: float = 0.4) -> NatsJetStreamConfig:
    token = uuid4().hex[:12]
    return NatsJetStreamConfig(
        servers=(NATS_URL or "nats://127.0.0.1:4222",),
        stream=f"CS_TEST_{token.upper()}",
        subject=f"cognistore.test.{token}",
        consumer=f"workers-{token}",
        ack_wait=ack_wait,
        duplicate_window=2.0,
        connect_timeout=1.0,
        request_timeout=1.0,
        drain_timeout=1.0,
        # Each test owns a durable stream. Keep its reservation small so the
        # suite is independent of the CI runner's remaining container disk.
        stream_max_bytes=TEST_STREAM_MAX_BYTES,
    )


def test_enqueue_ack_and_explicit_nack_redelivery() -> None:
    async def scenario() -> None:
        config = _config()
        publisher = NatsJetStreamQueue(config, consume=False)
        consumer = NatsJetStreamQueue(config)
        await publisher.connect()
        await consumer.connect()
        try:
            first_job = JobEnvelope.create(
                "test.echo", {"value": 1}, correlation_id="integration-correlation"
            )
            receipt = await publisher.enqueue(first_job)
            assert receipt.job_id == first_job.job_id
            first = await consumer.claim(timeout=1.0)
            assert first is not None
            assert first.attempt == 1
            assert first.job.correlation_id == "integration-correlation"
            await first.nack()

            redelivery = await consumer.claim(timeout=1.0)
            assert redelivery is not None
            assert redelivery.job.job_id == first_job.job_id
            assert redelivery.job.correlation_id == first_job.correlation_id
            assert redelivery.attempt >= 2
            await redelivery.ack()

            second_job = JobEnvelope.create("test.echo", {"value": 2})
            await publisher.enqueue(second_job)
            second = await consumer.claim(timeout=1.0)
            assert second is not None
            assert second.job.job_id == second_job.job_id
            await second.ack()
            assert await consumer.claim(timeout=0.1) is None
        finally:
            await consumer.close()
            await publisher.close()

    asyncio.run(scenario())


def test_bounded_stream_rejects_without_evicting_and_recovers_after_ack() -> None:
    async def scenario() -> None:
        config = replace(_config(), stream_max_messages=1)
        publisher = NatsJetStreamQueue(config, consume=False)
        consumer = NatsJetStreamQueue(config)
        await publisher.connect()
        await consumer.connect()
        try:
            first_job = JobEnvelope.create("test.capacity", {"value": 1})
            second_job = JobEnvelope.create("test.capacity", {"value": 2})
            await publisher.enqueue(first_job)

            health = await consumer.probe()
            assert health.stored_messages == 1
            assert health.max_messages == 1
            assert health.saturated is True
            with pytest.raises(QueueSaturatedError, match="maximum messages"):
                await publisher.enqueue(second_job)

            # DiscardNew must preserve the older durable job.
            first = await consumer.claim(timeout=1.0)
            assert first is not None
            assert first.job.job_id == first_job.job_id
            await first.ack()

            async def wait_for_capacity() -> None:
                while (await consumer.probe()).stored_messages != 0:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_capacity(), timeout=1.0)
            await publisher.enqueue(second_job)
            second = await consumer.claim(timeout=1.0)
            assert second is not None
            assert second.job.job_id == second_job.job_id
            await second.ack()
        finally:
            await consumer.close()
            await publisher.close()

    asyncio.run(scenario())


def test_bounded_stream_rejects_a_publication_over_its_byte_capacity() -> None:
    async def scenario() -> None:
        config = replace(
            _config(),
            stream_max_messages=100,
            stream_max_bytes=1024,
        )
        publisher = NatsJetStreamQueue(config, consume=False)
        consumer = NatsJetStreamQueue(config)
        await publisher.connect()
        await consumer.connect()
        try:
            oversized = JobEnvelope.create(
                "test.byte-capacity", {"value": "x" * 8192}
            )
            with pytest.raises(QueueSaturatedError, match="maximum bytes"):
                await publisher.enqueue(oversized)

            health = await consumer.probe()
            assert health.ready is True
            assert health.stored_messages == 0
            assert health.max_bytes == 1024
        finally:
            await consumer.close()
            await publisher.close()

    asyncio.run(scenario())


def test_claimed_job_survives_worker_connection_restart() -> None:
    async def scenario() -> None:
        config = _config(ack_wait=0.25)
        publisher = NatsJetStreamQueue(config, consume=False)
        worker_one = NatsJetStreamQueue(config)
        await publisher.connect()
        await worker_one.connect()
        job = JobEnvelope.create("test.restart", {"value": "durable"})
        await publisher.enqueue(job)

        first = await worker_one.claim(timeout=1.0)
        assert first is not None
        assert first.job.job_id == job.job_id
        # Simulate a worker process losing its connection before ACK/NAK.
        await worker_one.close(graceful=False)

        await asyncio.sleep(config.ack_wait + 0.1)
        worker_two = NatsJetStreamQueue(config)
        await worker_two.connect()
        try:
            redelivery = await worker_two.claim(timeout=1.0)
            assert redelivery is not None
            assert redelivery.job.job_id == job.job_id
            assert redelivery.job.correlation_id == job.correlation_id
            assert redelivery.attempt >= 2
            await redelivery.ack()
        finally:
            await worker_two.close()
            await publisher.close()

    asyncio.run(scenario())


def test_worker_graceful_shutdown_acks_completed_in_flight_job() -> None:
    async def scenario() -> None:
        config = _config()
        publisher = NatsJetStreamQueue(config, consume=False)
        await publisher.connect()
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(job, context) -> None:
            started.set()
            await release.wait()

        worker = AsyncWorker(
            NatsJetStreamQueue(config),
            {"test.block": handler},
            config=WorkerConfig(
                fetch_timeout=0.05,
                heartbeat_interval=0.1,
                shutdown_grace=1.0,
                settlement_timeout=0.5,
            ),
        )
        await worker.start()
        job = JobEnvelope.create("test.block", {})
        await publisher.enqueue(job)
        await asyncio.wait_for(started.wait(), timeout=1.0)

        shutdown = asyncio.create_task(worker.shutdown(grace=1.0))
        await asyncio.sleep(0.05)
        release.set()
        report = await shutdown
        assert report.graceful is True
        assert report.completed == 1

        verifier = NatsJetStreamQueue(config)
        await verifier.connect()
        try:
            assert await verifier.claim(timeout=0.2) is None
        finally:
            await verifier.close()
            await publisher.close()

    asyncio.run(scenario())


def test_misconfigured_worker_cannot_rewrite_live_consumer_ack_wait() -> None:
    async def scenario() -> None:
        config = _config(ack_wait=2.0)
        publisher = NatsJetStreamQueue(config, consume=False)
        worker_one = NatsJetStreamQueue(config)
        await publisher.connect()
        await worker_one.connect()
        job = JobEnvelope.create("test.lease", {})
        await publisher.enqueue(job)
        delivery = await worker_one.claim(timeout=1.0)
        assert delivery is not None

        worker_two = NatsJetStreamQueue(
            replace(config, ack_wait=0.2, client_name="misconfigured-worker")
        )
        try:
            with pytest.raises(RuntimeError, match="incompatible configuration"):
                await worker_two.connect()

            assert worker_one._jetstream is not None
            info = await worker_one._jetstream.consumer_info(
                config.stream, config.consumer
            )
            assert info.config.ack_wait == pytest.approx(2.0)
            assert await worker_one.claim(timeout=0.35) is None
            await delivery.ack()
        finally:
            await worker_two.close(graceful=False)
            await worker_one.close()
            await publisher.close()

    asyncio.run(scenario())


def test_concurrent_first_workers_cannot_race_consumer_configuration() -> None:
    async def scenario() -> None:
        config = _config(ack_wait=2.0)
        publisher = NatsJetStreamQueue(config, consume=False)
        await publisher.connect()
        long_lease = NatsJetStreamQueue(config)
        short_lease = NatsJetStreamQueue(
            replace(config, ack_wait=0.2, client_name="short-lease-worker")
        )
        try:
            results = await asyncio.gather(
                long_lease.connect(), short_lease.connect(), return_exceptions=True
            )
            successes = [result for result in results if result is None]
            failures = [result for result in results if isinstance(result, Exception)]
            assert len(successes) == 1
            assert len(failures) == 1
            assert isinstance(failures[0], RuntimeError)
            assert "incompatible configuration" in str(failures[0])

            connected = long_lease if results[0] is None else short_lease
            assert connected._jetstream is not None
            info = await connected._jetstream.consumer_info(
                config.stream, config.consumer
            )
            expected = 2.0 if connected is long_lease else 0.2
            assert info.config.ack_wait == pytest.approx(expected)
        finally:
            await long_lease.close(graceful=False)
            await short_lease.close(graceful=False)
            await publisher.close()

    asyncio.run(scenario())


def test_delayed_retry_exhaustion_dead_letter_and_idempotent_redrive() -> None:
    async def scenario() -> None:
        config = _config(ack_wait=0.5)
        publisher = NatsJetStreamQueue(config, consume=False)
        attempts: list[tuple[float, int, int, str, str]] = []

        async def unavailable(job, context) -> None:
            attempts.append(
                (
                    asyncio.get_running_loop().time(),
                    context.attempt,
                    context.cumulative_attempt,
                    job.job_id,
                    job.correlation_id,
                )
            )
            raise TimeoutError("integration backend timeout")

        worker = AsyncWorker(
            NatsJetStreamQueue(config),
            {"test.retry": unavailable},
            config=WorkerConfig(
                fetch_timeout=0.02,
                heartbeat_interval=0.1,
                shutdown_grace=1.0,
                settlement_timeout=0.5,
                max_attempts=3,
                retry_base_delay=0.05,
                retry_max_delay=0.05,
                retry_jitter=0,
            ),
        )
        verifier: NatsJetStreamQueue | None = None
        try:
            await publisher.connect()
            await worker.start()
            job = JobEnvelope.create(
                "test.retry", {}, correlation_id="integration-retry-correlation"
            )
            await publisher.enqueue(job)

            async def wait_for_dead_letter() -> None:
                while worker.health_snapshot().dead_lettered < 1:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_dead_letter(), timeout=3.0)
            report = await worker.shutdown()
            assert report.retried == 2
            assert report.dead_lettered == 1
            assert [item[1] for item in attempts] == [1, 2, 3]
            assert [item[2] for item in attempts] == [1, 2, 3]
            assert {item[3] for item in attempts} == {job.job_id}
            assert {item[4] for item in attempts} == {job.correlation_id}
            assert attempts[-1][0] - attempts[0][0] >= 0.07

            assert publisher._jetstream is not None
            info = await publisher._jetstream.stream_info(
                config.resolved_dead_letter_stream
            )
            dead_letter_id = None
            for sequence in range(info.state.first_seq, info.state.last_seq + 1):
                try:
                    raw = await publisher._jetstream.get_msg(
                        config.resolved_dead_letter_stream, seq=sequence
                    )
                except NotFoundError:
                    continue
                marker = f"{config.resolved_dead_letter_subject}.entry."
                if raw.subject.startswith(marker):
                    dead_letter_id = raw.subject.removeprefix(marker)
                    break
            assert dead_letter_id is not None

            record = await publisher.get_dead_letter(dead_letter_id)
            assert record.disposition.value == "exhausted"
            assert record.attempt == 3
            assert record.max_attempts == 3
            assert record.job == job

            first_redrive = await publisher.redrive_dead_letter(dead_letter_id)
            repeated_redrive = await publisher.redrive_dead_letter(dead_letter_id)
            assert first_redrive.job_id == job.job_id
            assert first_redrive.duplicate is False
            assert repeated_redrive.duplicate is True
            assert repeated_redrive.sequence == first_redrive.sequence

            verifier = NatsJetStreamQueue(config)
            await verifier.connect()
            delivery = await verifier.claim(timeout=1.0)
            assert delivery is not None
            assert delivery.job.job_id == job.job_id
            assert delivery.job.correlation_id == job.correlation_id
            assert delivery.job.metadata[ATTEMPT_OFFSET_METADATA] == "3"
            assert delivery.job.metadata[REDRIVE_COUNT_METADATA] == "1"
            assert json.loads(
                delivery.job.metadata[DEAD_LETTER_CHAIN_METADATA]
            ) == [dead_letter_id]
            await delivery.ack()
            assert await verifier.claim(timeout=0.15) is None
        finally:
            if worker.state not in {WorkerState.STOPPED, WorkerState.FAILED}:
                await worker.shutdown()
            if verifier is not None:
                await verifier.close()
            await publisher.close()

    asyncio.run(scenario())
