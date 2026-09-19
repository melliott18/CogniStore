"""Executable evidence for the action-to-event matrix in docs/audit_coverage.md."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path

import pytest

from cognistore.auth.principal import PRINCIPAL_METADATA, Principal
from cognistore.core.audit import AuditContext, AuditEventType, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.core.mover import Mover
from cognistore.core.policy import SimplePolicy
from cognistore.core.policy_runner import PolicyRunner
from cognistore.core.scanner import scan_catalog
from cognistore.db.catalog import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.jobs.handlers import CATALOG_SCAN_JOB, build_handlers
from cognistore.jobs.models import JobContext, JobEnvelope
from cognistore.jobs.runtime import AsyncWorker, WorkerConfig


@contextmanager
def _catalog(backend: str, tmp_path: Path):
    if backend == "memory":
        yield Catalog()
    else:
        with SQLCatalog(tmp_path / "coverage.sqlite3") as catalog:
            yield catalog


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_scan_emits_correlated_intent_and_outcome_in_own_tenant(backend, tmp_path):
    driver = PosixDriver(str(tmp_path / "hot"))
    driver.put_object("bucket", "objects/example.txt", b"content never in audit")
    context = AuditContext("scan-63", "operator", "tester", job_id="scan-job-63")
    with _catalog(backend, tmp_path) as root:
        catalog = root.for_tenant("tenant-a")
        results = scan_catalog(
            tier="hot", bucket="bucket", prefix="objects/", driver=driver,
            catalog=catalog, audit_context=context,
        )
        events = catalog.list_audit_events(AuditQuery(correlation_id=context.correlation_id))
        assert [event.event_type for event in events] == [
            AuditEventType.SCAN_STARTED, AuditEventType.SCAN_COMPLETED,
        ]
        started, completed = events
        assert completed.causation_id == started.event_id
        assert {event.job_id for event in events} == {context.job_id}
        assert {event.actor_id for event in events} == {context.actor_id}
        assert {event.details["bucket"] for event in events} == {"bucket"}
        assert completed.details["objects_observed"] == len(results) == 1
        assert "content never in audit" not in str(events)
        assert root.for_tenant("tenant-b").list_audit_events() == []


def test_scan_failure_is_correlated_and_excludes_exception_content(tmp_path, monkeypatch):
    catalog = Catalog()
    driver = PosixDriver(str(tmp_path / "hot"))

    def fail(*args, **kwargs):
        raise OSError("password=private-exception-content")

    monkeypatch.setattr(driver, "list_objects", fail)
    with pytest.raises(OSError):
        scan_catalog(tier="hot", bucket="bucket", driver=driver, catalog=catalog)
    started, failed = catalog.list_audit_events()
    assert started.event_type == AuditEventType.SCAN_STARTED
    assert failed.event_type == AuditEventType.SCAN_FAILED
    assert failed.correlation_id == started.correlation_id
    assert failed.causation_id == started.event_id
    assert failed.outcome == "failed"
    assert failed.details["exception_type"] == "builtins.OSError"
    assert "private-exception-content" not in str(failed.details)


def test_scan_audit_failure_prevents_storage_access_and_dry_run_never_audits(tmp_path, monkeypatch):
    driver = PosixDriver(str(tmp_path / "hot"))
    driver.put_object("bucket", "example.txt", b"example")
    catalog = Catalog()
    observed = []
    original_listing = driver.list_objects

    def listing(*args, **kwargs):
        observed.append(True)
        return original_listing(*args, **kwargs)

    def fail(event):
        raise OSError("audit unavailable")

    monkeypatch.setattr(driver, "list_objects", listing)
    monkeypatch.setattr(catalog, "append_audit_event", fail)
    with pytest.raises(OSError, match="audit unavailable"):
        scan_catalog(tier="hot", bucket="bucket", driver=driver, catalog=catalog)
    assert observed == []
    assert len(scan_catalog(
        tier="hot", bucket="bucket", driver=driver, catalog=catalog, dry_run=True,
    )) == 1
    assert catalog.get("bucket", "example.txt") is None
    assert catalog.list_audit_events() == []


def test_worker_scan_preserves_verified_actor_job_and_request(tmp_path):
    async def scenario():
        catalog = Catalog()
        driver = PosixDriver(str(tmp_path / "hot"))
        driver.put_object("bucket", "example.txt", b"example")
        principal = Principal("https://issuer.example", "worker-user")
        job = JobEnvelope.create(
            CATALOG_SCAN_JOB, {"tier": "hot", "bucket": "bucket", "prefix": ""},
            correlation_id="worker-scan-63", metadata={PRINCIPAL_METADATA: principal.to_json()},
        )
        context = JobContext(
            attempt=1, redelivered=False, stream_sequence=1, consumer_sequence=1,
            shutdown_requested=asyncio.Event(), principal=principal,
        )
        await build_handlers({"hot": driver}, catalog)[CATALOG_SCAN_JOB](job, context)
        events = catalog.list_audit_events(AuditQuery(job_id=job.job_id))
        assert {event.event_type for event in events} == {
            AuditEventType.SCAN_STARTED, AuditEventType.SCAN_COMPLETED,
        }
        assert {event.correlation_id for event in events} == {job.correlation_id}
        assert {event.actor_id for event in events} == {principal.actor_id}
        assert {event.actor_type for event in events} == {principal.actor_type}

    asyncio.run(scenario())


def test_worker_success_is_audited_without_api_status_metadata_before_ack():
    async def scenario():
        catalog = Catalog()
        job = JobEnvelope.create("catalog.scan", {}, correlation_id="local-job-63")
        observed = []

        class Delivery:
            attempt = 1
            stream_sequence = 1
            consumer_sequence = 1
            source_stream = "TEST"
            source_consumer = "worker"

            async def ack(self):
                events = catalog.list_audit_events(AuditQuery(job_id=job.job_id))
                assert {event.event_type for event in events} == {
                    AuditEventType.JOB_STARTED, AuditEventType.JOB_SUCCEEDED,
                }
                assert {event.correlation_id for event in events} == {job.correlation_id}
                observed.append("ack")

        delivery = Delivery()
        delivery.job = job

        async def handler(delivered, context):
            assert [event.event_type for event in catalog.list_audit_events()] == [
                AuditEventType.JOB_STARTED,
            ]
            observed.append("handler")

        worker = AsyncWorker(
            object(), {job.job_type: handler}, audit_catalog=catalog,
            config=WorkerConfig(heartbeat_interval=0),
        )
        await worker._process(delivery)
        assert observed == ["handler", "ack"]

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_policy_storage_move_has_complete_correlated_causal_path(backend, tmp_path):
    drivers = {tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm")}
    drivers["hot"].put_object("bucket", "large.txt", b"large object")
    context = AuditContext("policy-storage-63", "operator", "tester", job_id="policy-job")
    with _catalog(backend, tmp_path) as root:
        catalog = root.for_tenant("tenant-a")
        catalog.upsert("bucket", "large.txt", 12, tier="hot")
        runner = PolicyRunner(
            catalog, drivers, Mover(drivers, catalog, audit_context=context),
            SimplePolicy(size_threshold=5), audit_context=context,
        )
        runner.run_once("bucket")
        events = catalog.list_audit_events(AuditQuery(correlation_id=context.correlation_id))
        by_id = {event.event_id: event for event in events}
        terminal = next(event for event in events if event.event_type == AuditEventType.MOVE_COMPLETED)
        chain = [terminal]
        while chain[-1].causation_id is not None:
            chain.append(by_id[chain[-1].causation_id])
        assert chain[-1].event_type == AuditEventType.POLICY_DECISION
        assert {AuditEventType.MOVE_PREPARED, AuditEventType.MOVE_TRANSITIONED} <= {
            event.event_type for event in chain
        }
        assert {event.job_id for event in chain} == {context.job_id}
        assert {event.actor_id for event in chain} == {context.actor_id}
        assert {event.bucket for event in chain} == {"bucket"}
        assert {event.object_key for event in chain} == {"large.txt"}
        assert drivers["warm"].get_object("bucket", "large.txt") == b"large object"
        assert list(drivers["hot"].list_objects("bucket")) == []
        assert root.for_tenant("tenant-b").list_audit_events() == []


def test_worker_cancellation_is_audited_before_nack():
    async def scenario():
        catalog = Catalog()
        job = JobEnvelope.create("catalog.scan", {}, correlation_id="cancelled-job-63")
        observed = []

        class Delivery:
            attempt = 1
            stream_sequence = 1
            consumer_sequence = 1
            source_stream = "TEST"
            source_consumer = "worker"

            async def nack(self):
                events = catalog.list_audit_events(AuditQuery(job_id=job.job_id))
                assert {event.event_type for event in events} == {
                    AuditEventType.JOB_STARTED, AuditEventType.JOB_RETRY,
                }
                retry = next(event for event in events if event.event_type == AuditEventType.JOB_RETRY)
                assert retry.correlation_id == job.correlation_id
                assert retry.details["reason"] == "worker_cancelled"
                observed.append("nack")

        delivery = Delivery()
        delivery.job = job

        async def handler(delivered, context):
            raise asyncio.CancelledError()

        worker = AsyncWorker(
            object(), {job.job_type: handler}, audit_catalog=catalog,
            config=WorkerConfig(heartbeat_interval=0),
        )
        with pytest.raises(asyncio.CancelledError):
            await worker._process(delivery)
        assert observed == ["nack"]

    asyncio.run(scenario())


def test_api_queue_failure_has_correlated_submission_evidence(tmp_path):
    from cognistore.api.gateway import CogniStoreGateway
    from cognistore.api.models import CatalogScanRequest

    async def scenario():
        catalog = Catalog()
        published = []

        class FailingQueue:
            async def enqueue(self, job, *, message_id):
                published.append(job)
                assert [event.event_type for event in catalog.list_audit_events()] == [
                    AuditEventType.JOB_QUEUED,
                ]
                raise TimeoutError("private queue diagnostic")

        gateway = CogniStoreGateway(
            catalog, {"hot": PosixDriver(str(tmp_path / "hot"))}, queue=FailingQueue(),
        )
        with pytest.raises(TimeoutError):
            await gateway.submit_catalog_scan(CatalogScanRequest(tier="hot", bucket="bucket"))
        job = published[0]
        events = catalog.list_audit_events(AuditQuery(job_id=job.job_id))
        assert [event.event_type for event in events] == [
            AuditEventType.JOB_QUEUED, AuditEventType.JOB_SUBMISSION_FAILED,
        ]
        assert {event.correlation_id for event in events} == {job.correlation_id}
        assert {event.actor_id for event in events} == {"cognistore-rest-api"}
        assert "private queue diagnostic" not in str(events)

    asyncio.run(scenario())


def test_worker_terminal_replay_does_not_invent_success():
    async def scenario():
        from types import SimpleNamespace

        from cognistore.core.audit import AuditEvent, AuditOutcome

        catalog = Catalog()
        job = JobEnvelope.create("catalog.scan", {}, correlation_id="terminal-replay-63")
        catalog.append_audit_event(AuditEvent.create(
            AuditEventType.JOB_DEAD_LETTERED, AuditOutcome.FAILED,
            AuditContext(job.correlation_id, "worker", "worker", job_id=job.job_id),
        ))
        acknowledged = []

        class Delivery:
            attempt = 2
            stream_sequence = 1
            consumer_sequence = 2

            async def ack(self):
                acknowledged.append(True)

        class Coordinator:
            async def begin(self, job, context):
                return SimpleNamespace(execute=False)

        delivery = Delivery()
        delivery.job = job

        async def handler(delivered, context):
            raise AssertionError("terminal replay must not execute")

        worker = AsyncWorker(
            object(), {job.job_type: handler}, audit_catalog=catalog,
            coordinator=Coordinator(), config=WorkerConfig(heartbeat_interval=0),
        )
        await worker._process(delivery)
        assert acknowledged == [True]
        assert [event.event_type for event in catalog.list_audit_events()] == [
            AuditEventType.JOB_DEAD_LETTERED,
        ]

    asyncio.run(scenario())
