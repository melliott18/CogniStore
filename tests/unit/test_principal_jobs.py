from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from cognistore import observability as telemetry
from cognistore.api.gateway import CogniStoreGateway
from cognistore.api.models import (
    CatalogScanRequest,
    ImportanceChangeRequest,
    PolicyConfig,
    PolicyRunRequest,
)
from cognistore.auth.authorization import RBACAuthorizer, RBACPolicy
from cognistore.auth.principal import (
    PRINCIPAL_METADATA,
    Principal,
    current_principal,
    principal_context,
)
from cognistore.core.audit import AuditEventType, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.jobs.handlers import _run_blocking_safely, build_handlers
from cognistore.jobs.models import (
    STATUS_TRACKING_METADATA,
    DeadLetterReceipt,
    DeadLetterRecord,
    EnqueueReceipt,
    JobEnvelope,
    JobEnvelopeError,
)
from cognistore.jobs.runtime import AsyncWorker, WorkerConfig


class _Queue:
    def __init__(self) -> None:
        self.jobs: list[JobEnvelope] = []
        self.dead_letters: list[DeadLetterRecord] = []

    async def enqueue(self, job: JobEnvelope, *, message_id=None) -> EnqueueReceipt:
        self.jobs.append(JobEnvelope.from_bytes(job.to_bytes()))
        return EnqueueReceipt(job.job_id, job.correlation_id, "JOBS", len(self.jobs))

    async def publish_dead_letter(self, record: DeadLetterRecord) -> DeadLetterReceipt:
        self.dead_letters.append(DeadLetterRecord.from_bytes(record.to_bytes()))
        return DeadLetterReceipt(record.dead_letter_id, "DLQ", len(self.dead_letters))


@dataclass
class _Delivery:
    job: JobEnvelope
    attempt: int = 1
    stream_sequence: int = 1
    consumer_sequence: int = 1
    source_stream: str = "JOBS"
    source_consumer: str = "worker"
    source_published_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    headers: dict[str, str] = field(default_factory=dict)
    acked: int = 0
    nacked: int = 0

    @property
    def raw_data(self) -> bytes:
        return self.job.to_bytes()

    async def ack(self) -> None:
        self.acked += 1

    async def nack(self, delay=None) -> None:
        self.nacked += 1

    async def in_progress(self) -> None:
        pass


def _job(principal: Principal | None) -> JobEnvelope:
    metadata = {STATUS_TRACKING_METADATA: "1"}
    if principal is not None:
        metadata[PRINCIPAL_METADATA] = principal.to_json()
    return JobEnvelope.create("catalog.scan", {}, metadata=metadata)


def _worker_authorization(*principals: Principal | None) -> RBACAuthorizer:
    return RBACAuthorizer(RBACPolicy.from_dict({
        "bindings": [
            {"issuer": principal.issuer, "subject": principal.subject, "roles": ["admin"]}
            for principal in principals
            if principal is not None
        ],
    }))


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        "null",
        "[]",
        "{}",
        '{"issuer":"https://issuer","subject":"","client_id":null}',
        '{"issuer":"https://issuer","subject":42,"client_id":null}',
        '{"issuer":"https://issuer","subject":"alice","client_id":null,"token":"secret"}',
    ],
)
def test_job_rejects_malformed_or_overbroad_principal_metadata(value: str) -> None:
    job = json.loads(_job(None).to_bytes())
    job["metadata"][PRINCIPAL_METADATA] = value
    with pytest.raises(JobEnvelopeError, match="invalid normalized principal metadata"):
        JobEnvelope.from_bytes(json.dumps(job).encode())


@pytest.mark.parametrize("submission", ["scan", "policy"])
def test_gateway_submission_carries_only_normalized_identity(
    tmp_path: Path, submission: str
) -> None:
    queue = _Queue()
    catalog = Catalog()
    gateway = CogniStoreGateway(
        catalog,
        {tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm")},
        queue=queue,
    )
    principal = Principal("https://issuer.example", "service-account", "batch-client")
    gateway.authorization = _worker_authorization(principal)
    with principal_context(principal):
        if submission == "scan":
            status = asyncio.run(
                gateway.submit_catalog_scan(CatalogScanRequest(tier="hot", bucket="bucket"))
            )
        else:
            status = asyncio.run(gateway.submit_policy_run(PolicyRunRequest(bucket="bucket")))

    job = queue.jobs[0]
    assert current_principal() is None
    assert job.principal == principal
    assert json.loads(job.metadata[PRINCIPAL_METADATA]) == {
        "issuer": principal.issuer,
        "subject": principal.subject,
        "client_id": principal.client_id,
    }
    assert set(job.metadata) == {STATUS_TRACKING_METADATA, PRINCIPAL_METADATA}
    events = catalog.list_audit_events(AuditQuery(job_id=str(status.job_id)))
    assert len([event for event in events if event.event_type == AuditEventType.JOB_QUEUED]) == 1
    assert events[0].actor_type == principal.actor_type
    assert events[0].actor_id == principal.actor_id


def test_worker_context_isolated_across_concurrent_jobs_and_threads() -> None:
    async def scenario() -> None:
        principals = [
            Principal("https://issuer.example", "alice"),
            None,
            Principal("https://other.example", "alice"),
        ]
        outer = Principal("https://ambient.example", "unrelated")
        jobs = [_job(principal) for principal in principals]
        seen = {}
        ready = asyncio.Event()

        async def handler(job, context) -> None:
            seen[job.job_id] = [context.principal, current_principal()]
            if len(seen) == len(jobs):
                ready.set()
            await ready.wait()
            seen[job.job_id].append(await _run_blocking_safely(current_principal))
            seen[job.job_id].append(current_principal())

        worker = AsyncWorker(
            _Queue(), {"catalog.scan": handler}, config=WorkerConfig(heartbeat_interval=0),
            authorization=_worker_authorization(*principals),
        )
        anonymous_worker = AsyncWorker(
            _Queue(), {"catalog.scan": handler}, config=WorkerConfig(heartbeat_interval=0),
        )
        with principal_context(outer):
            await asyncio.gather(*(
                (worker if job.principal is not None else anonymous_worker)._process(_Delivery(job))
                for job in jobs
            ))
            assert current_principal() == outer
        assert current_principal() is None
        for job, principal in zip(jobs, principals):
            assert seen[job.job_id] == [principal] * 4
        assert principals[0].actor_id != principals[2].actor_id

    asyncio.run(scenario())


def test_worker_retry_failure_and_redrive_preserve_identity() -> None:
    async def scenario() -> None:
        principal = Principal("https://issuer.example", "alice")
        job = _job(principal)
        queue = _Queue()
        catalog = Catalog()
        seen = []

        async def handler(job, context) -> None:
            seen.append((context.principal, current_principal()))
            if context.redrive_count == 0:
                raise TimeoutError("retry this operation")

        worker = AsyncWorker(
            queue,
            {"catalog.scan": handler},
            audit_catalog=catalog,
            authorization=_worker_authorization(principal),
            config=WorkerConfig(heartbeat_interval=0, max_attempts=2),
        )
        first = _Delivery(job)
        await worker._process(first)
        assert first.nacked == 1
        second = _Delivery(JobEnvelope.from_bytes(first.raw_data), attempt=2, consumer_sequence=2)
        await worker._process(second)
        assert second.acked == 1
        assert len(queue.dead_letters) == 1
        redriven = queue.dead_letters[0].job_for_redrive()
        assert redriven.principal == principal
        assert redriven.metadata[PRINCIPAL_METADATA] == job.metadata[PRINCIPAL_METADATA]
        third = _Delivery(redriven, stream_sequence=2, consumer_sequence=3)
        await worker._process(third)
        assert third.acked == 1
        assert seen == [(principal, principal)] * 3
        assert current_principal() is None
        events = catalog.list_audit_events(AuditQuery(job_id=job.job_id))
        assert {
            AuditEventType.JOB_STARTED.value,
            AuditEventType.JOB_FAILURE.value,
            AuditEventType.JOB_RETRY.value,
            AuditEventType.JOB_DEAD_LETTERED.value,
            AuditEventType.JOB_SUCCEEDED.value,
        }.issubset({event.event_type for event in events})
        assert all(event.actor_id == principal.actor_id for event in events)
        assert all(event.actor_type == principal.actor_type for event in events)

    asyncio.run(scenario())


def test_trace_and_principal_coexist_across_threads_retry_and_redrive(monkeypatch) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_tracer", provider.get_tracer("principal-test"))

    async def scenario() -> None:
        principal = Principal("https://issuer.example", "alice")
        correlation_id = "db9b49f8-6281-47ec-8352-edb1701592cb"
        with telemetry.request_context(correlation_id), principal_context(principal):
            with telemetry.observe("api", "request", kind="server") as submission:
                job = _job(principal)
                parent_span = submission.get_span_context()
        assert job.correlation_id == correlation_id
        assert "traceparent" in job.metadata
        assert job.principal == principal
        observations = []
        queue = _Queue()
        catalog = Catalog()

        def snapshot():
            return (
                current_principal(),
                telemetry.current_correlation_id(),
                trace.get_current_span().get_span_context().trace_id,
            )

        async def handler(job, context) -> None:
            assert context.principal == principal
            observations.append(snapshot())
            observations.append(await _run_blocking_safely(snapshot))
            if context.redrive_count == 0:
                raise TimeoutError("retry this operation")

        worker = AsyncWorker(
            queue,
            {"catalog.scan": handler},
            audit_catalog=catalog,
            authorization=_worker_authorization(principal),
            config=WorkerConfig(heartbeat_interval=0, max_attempts=2),
        )
        ambient = Principal("https://other.example", "unrelated")
        with telemetry.request_context(), principal_context(ambient):
            with telemetry.observe("api", "request"):
                ambient_snapshot = snapshot()
                await worker._process(_Delivery(JobEnvelope.from_bytes(job.to_bytes())))
                await worker._process(_Delivery(job, attempt=2, consumer_sequence=2))
                redriven = queue.dead_letters[0].job_for_redrive()
                assert redriven.metadata["traceparent"] == job.metadata["traceparent"]
                assert redriven.principal == principal
                await worker._process(_Delivery(redriven, stream_sequence=2, consumer_sequence=3))
                assert snapshot() == ambient_snapshot
        assert observations == [(principal, correlation_id, parent_span.trace_id)] * 6
        assert current_principal() is None
        assert telemetry.current_correlation_id() is None
        consumer_spans = [
            span for span in exporter.get_finished_spans() if span.kind == SpanKind.CONSUMER
        ]
        assert len(consumer_spans) == 3
        assert all(span.parent.span_id == parent_span.span_id for span in consumer_spans)
        events = catalog.list_audit_events(AuditQuery(job_id=job.job_id))
        assert all(event.actor_id == principal.actor_id for event in events)

    try:
        asyncio.run(scenario())
    finally:
        provider.shutdown()


@pytest.mark.parametrize("authenticated", [True, False])
def test_handler_cannot_rewrite_submitting_identity(authenticated: bool) -> None:
    async def scenario() -> None:
        principal = Principal("https://issuer.example", "alice") if authenticated else None
        replacement = Principal("https://issuer.example", "forged")
        job = _job(principal)
        catalog = Catalog()
        queue = _Queue()

        async def handler(job, context) -> None:
            job.metadata[PRINCIPAL_METADATA] = replacement.to_json()
            assert job.principal == principal
            assert JobEnvelope.from_bytes(job.to_bytes()).principal == principal
            raise ValueError("terminal failure")

        worker = AsyncWorker(
            queue,
            {"catalog.scan": handler},
            audit_catalog=catalog,
            authorization=_worker_authorization(principal) if principal is not None else None,
            config=WorkerConfig(heartbeat_interval=0),
        )
        await worker._process(_Delivery(job))
        events = catalog.list_audit_events(AuditQuery(job_id=job.job_id))
        actor_id = principal.actor_id if principal is not None else "worker"
        assert all(event.actor_id == actor_id for event in events)
        assert queue.dead_letters[0].job_for_redrive().principal == principal

    asyncio.run(scenario())


def test_authenticated_policy_moves_and_importance_use_verified_actor(tmp_path: Path) -> None:
    async def scenario() -> None:
        principal = Principal("https://issuer.example", "alice")
        catalog = Catalog()
        queue = _Queue()
        drivers = {tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm")}
        drivers["warm"].put_object("bucket", "one.txt", b"small")
        catalog.upsert("bucket", "one.txt", 5, "warm")
        gateway = CogniStoreGateway(catalog, drivers, queue=queue, authorization=_worker_authorization(principal))
        with principal_context(principal):
            gateway.set_importance(
                ImportanceChangeRequest(
                    bucket="bucket",
                    key="one.txt",
                    level="normal",
                    actor_id="spoofed-actor",
                    provenance="operator request",
                ),
                correlation_id="importance-change",
            )
            await gateway.submit_policy_run(PolicyRunRequest(bucket="bucket"))
        record = catalog.get("bucket", "one.txt")
        assert record.importance.actor_id == principal.actor_id
        worker = AsyncWorker(
            queue,
            build_handlers(drivers, catalog),
            audit_catalog=catalog,
            authorization=_worker_authorization(principal),
            config=WorkerConfig(heartbeat_interval=0),
        )
        delivery = _Delivery(queue.jobs[0])
        await worker._process(delivery)
        assert delivery.acked == 1
        assert drivers["hot"].get_object("bucket", "one.txt") == b"small"
        events = catalog.list_audit_events()
        assert {
            AuditEventType.IMPORTANCE_CHANGED.value,
            AuditEventType.POLICY_DECISION.value,
            AuditEventType.MOVE_COMPLETED.value,
        }.issubset({event.event_type for event in events})
        assert all(event.actor_id == principal.actor_id for event in events)
        assert all(event.actor_type == principal.actor_type for event in events)
        assert "spoofed-actor" not in repr(events)

    asyncio.run(scenario())


def test_anonymous_gateway_and_worker_keep_legacy_actor_identity(tmp_path: Path) -> None:
    async def scenario() -> None:
        catalog = Catalog()
        queue = _Queue()
        gateway = CogniStoreGateway(catalog, {"hot": PosixDriver(str(tmp_path))}, queue=queue)
        await gateway.submit_catalog_scan(CatalogScanRequest(tier="hot", bucket="bucket"))
        job = queue.jobs[0]
        assert job.principal is None
        assert job.metadata == {STATUS_TRACKING_METADATA: "1"}
        worker = AsyncWorker(
            queue,
            build_handlers(gateway.drivers, catalog),
            audit_catalog=catalog,
            config=WorkerConfig(heartbeat_interval=0),
        )
        await worker._process(_Delivery(job))
        events = catalog.list_audit_events(AuditQuery(job_id=job.job_id))
        assert {(event.actor_type, event.actor_id) for event in events} == {
            ("api", "cognistore-rest-api"),
            ("worker", "worker"),
        }
        assert (
            gateway._policy_runner(PolicyConfig(allowed_tiers=["hot"])).audit_context.actor_type
            == "system"
        )

    asyncio.run(scenario())
