from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from cognistore.auth.authorization import RBACAuthorizer, RBACPolicy
from cognistore.auth.principal import PRINCIPAL_METADATA, Principal
from cognistore.auth.tenancy import TenantResolver, current_tenant_id, tenant_context
from cognistore.core.audit import AuditEventType, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.drivers.tenancy import TenantStorageDriver
from cognistore.jobs.handlers import _run_blocking_safely, build_handlers
from cognistore.jobs.models import (
    STATUS_TRACKING_METADATA,
    TENANT_METADATA,
    DeadLetterReceipt,
    DeadLetterRecord,
    JobEnvelope,
    JobEnvelopeError,
)
from cognistore.jobs.retry import RetryableJobError
from cognistore.jobs.runtime import AsyncWorker, WorkerConfig

ALICE = Principal("https://issuer.example", "alice")
BOB = Principal("https://issuer.example", "bob")
RESOLVER = TenantResolver({(ALICE.issuer, ALICE.subject): "alpha", (BOB.issuer, BOB.subject): "beta"})
AUTHORIZATION = RBACAuthorizer(RBACPolicy.from_dict({"bindings": [
    {"issuer": principal.issuer, "subject": principal.subject, "roles": ["admin"]}
    for principal in (ALICE, BOB)
]}))


class Queue:
    def __init__(self) -> None:
        self.dead_letters: list[DeadLetterRecord] = []

    async def publish_dead_letter(self, record: DeadLetterRecord) -> DeadLetterReceipt:
        self.dead_letters.append(DeadLetterRecord.from_bytes(record.to_bytes()))
        return DeadLetterReceipt(record.dead_letter_id, "DLQ", len(self.dead_letters))


@dataclass
class Delivery:
    job: JobEnvelope
    attempt: int = 1
    stream_sequence: int = 1
    consumer_sequence: int = 1
    source_stream: str = "JOBS"
    source_consumer: str = "worker"
    acked: int = 0
    nacked: int = 0

    @property
    def raw_data(self) -> bytes:
        return self.job.to_bytes()

    @property
    def headers(self) -> dict[str, str]:
        return {}

    async def ack(self) -> None:
        self.acked += 1

    async def nack(self, delay=None) -> None:
        self.nacked += 1

    async def in_progress(self) -> None:
        pass


def job(principal: Principal = ALICE, owner: str | None = "alpha", **kwargs) -> JobEnvelope:
    return JobEnvelope.create(
        "catalog.scan", kwargs.pop("payload", {}), tenant_id=owner,
        metadata={PRINCIPAL_METADATA: principal.to_json(), STATUS_TRACKING_METADATA: "1"},
        **kwargs,
    )


def worker(queue, handlers, **kwargs) -> AsyncWorker:
    return AsyncWorker(
        queue, handlers, config=WorkerConfig(heartbeat_interval=0),
        authorization=AUTHORIZATION, tenant_resolver=RESOLVER, **kwargs,
    )


@pytest.mark.parametrize("owner", ["", "../beta", "alpha/beta", "alpha\nbeta", "alpha beta"])
def test_tenant_envelope_rejects_invalid_owner(owner: str) -> None:
    encoded = json.loads(job().to_bytes())
    encoded["metadata"][TENANT_METADATA] = owner
    with pytest.raises(JobEnvelopeError, match="tenant ownership"):
        JobEnvelope.from_bytes(json.dumps(encoded).encode())


def test_job_owner_is_immutable_and_context_cannot_be_overridden() -> None:
    with tenant_context("alpha"):
        envelope = JobEnvelope.create("catalog.scan", {})
        assert envelope.tenant_id == "alpha"
        with pytest.raises(JobEnvelopeError, match="tenant context"):
            JobEnvelope.create("catalog.scan", {}, metadata={TENANT_METADATA: "beta"})
    envelope.metadata[TENANT_METADATA] = "beta"
    assert envelope.tenant_id == "alpha"
    assert JobEnvelope.from_bytes(envelope.to_bytes()).tenant_id == "alpha"
    del envelope.metadata[TENANT_METADATA]
    assert JobEnvelope.from_bytes(envelope.to_bytes()).tenant_id == "alpha"


@pytest.mark.parametrize("case", ["changed", "missing", "missing_principal", "unknown_principal"])
def test_worker_rejects_owner_before_handler_coordinator_or_status(case: str) -> None:
    async def scenario() -> None:
        queue, catalog, calls = Queue(), Catalog(), []
        envelope = json.loads(job().to_bytes())
        if case == "changed":
            envelope["metadata"][TENANT_METADATA] = "beta"
        elif case == "missing":
            del envelope["metadata"][TENANT_METADATA]
        elif case == "missing_principal":
            del envelope["metadata"][PRINCIPAL_METADATA]
        else:
            envelope["metadata"][PRINCIPAL_METADATA] = Principal(ALICE.issuer, "unknown").to_json()

        async def handler(job, context):
            calls.append("handler")

        class Coordinator:
            async def begin(self, job, context):
                calls.append("coordinator")

        delivery = Delivery(JobEnvelope.from_bytes(json.dumps(envelope).encode()), attempt=2)
        with tenant_context("unrelated"):
            await worker(queue, {"catalog.scan": handler}, audit_catalog=catalog,
                         coordinator=Coordinator())._process(delivery)
            assert current_tenant_id() == "unrelated"
        assert calls == []
        assert delivery.acked == 1
        assert queue.dead_letters[0].category == "authorization"
        for owner in ("default", "alpha", "beta"):
            assert catalog.for_tenant(owner).list_audit_events(AuditQuery()) == []

    asyncio.run(scenario())


def test_concurrent_worker_context_and_status_are_tenant_scoped() -> None:
    async def scenario() -> None:
        catalog, queue, seen = Catalog(), Queue(), {}
        ready = asyncio.Event()
        first = job()
        second = job(BOB, "beta", job_id=first.job_id)

        async def handler(envelope, context):
            owner = envelope.tenant_id
            seen[owner] = [context.tenant_id, current_tenant_id()]
            if len(seen) == 2:
                ready.set()
            await ready.wait()
            seen[owner].append(await _run_blocking_safely(current_tenant_id))

        runtime = worker(queue, {"catalog.scan": handler}, audit_catalog=catalog)
        with tenant_context("unrelated"):
            await asyncio.gather(runtime._process(Delivery(first)), runtime._process(Delivery(second)))
            assert current_tenant_id() == "unrelated"
        assert seen == {"alpha": ["alpha"] * 3, "beta": ["beta"] * 3}
        for owner, principal in (("alpha", ALICE), ("beta", BOB)):
            events = catalog.for_tenant(owner).list_audit_events(AuditQuery(job_id=first.job_id))
            assert any(event.event_type == AuditEventType.JOB_SUCCEEDED for event in events)
            assert {event.actor_id for event in events} == {principal.actor_id}
        assert catalog.list_audit_events(AuditQuery()) == []
        assert queue.dead_letters == []

    asyncio.run(scenario())


def test_tenant_membership_is_rechecked_on_retry_and_redrive(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "tenants.json"
        def policy(owner):
            path.write_text(json.dumps({"bindings": [{
                "issuer": ALICE.issuer, "subject": ALICE.subject, "tenant_id": owner,
            }]}))
        policy("alpha")
        resolver = TenantResolver(policy_path=path)
        queue, calls = Queue(), []

        async def handler(envelope, context):
            calls.append(current_tenant_id())
            raise RetryableJobError("retry")

        runtime = AsyncWorker(
            queue, {"catalog.scan": handler}, authorization=AUTHORIZATION,
            tenant_resolver=resolver, config=WorkerConfig(heartbeat_interval=0),
        )
        envelope = job()
        first = Delivery(envelope)
        await runtime._process(first)
        assert first.nacked == 1
        policy("beta")
        await runtime._process(Delivery(envelope, attempt=2))
        assert calls == ["alpha"]
        record = queue.dead_letters[0]
        assert record.job.tenant_id == "alpha"
        redriven = record.job_for_redrive()
        assert redriven.tenant_id == "alpha"
        await runtime._process(Delivery(redriven, stream_sequence=2))
        assert calls == ["alpha"]
        assert len(queue.dead_letters) == 2

    asyncio.run(scenario())


def test_scan_handler_uses_tenant_storage_and_catalog(tmp_path: Path) -> None:
    async def scenario() -> None:
        catalog, driver, queue = Catalog(), PosixDriver(str(tmp_path)), Queue()
        for owner, data in (("alpha", b"alpha bytes"), ("beta", b"beta")):
            TenantStorageDriver(driver, owner).put_object("bucket", "same-key", data)
        runtime = worker(queue, build_handlers({"hot": driver}, catalog), audit_catalog=catalog)
        payload = {"tier": "hot", "bucket": "bucket", "prefix": ""}
        await asyncio.gather(
            runtime._process(Delivery(job(payload=payload))),
            runtime._process(Delivery(job(BOB, "beta", payload=payload), stream_sequence=2)),
        )
        assert queue.dead_letters == []
        assert catalog.for_tenant("alpha").get("bucket", "same-key").size == len(b"alpha bytes")
        assert catalog.for_tenant("beta").get("bucket", "same-key").size == len(b"beta")
        assert catalog.get("bucket", "same-key") is None

    asyncio.run(scenario())
