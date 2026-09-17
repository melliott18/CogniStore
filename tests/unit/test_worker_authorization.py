from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from cognistore.auth.authorization import (
    RBACAuthorizer,
    RBACPolicy,
    authorization_context,
    current_authorizer,
)
from cognistore.auth.principal import PRINCIPAL_METADATA, Principal, principal_context
from cognistore.core.audit import AuditEventType, AuditOutcome, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.jobs.models import (
    STATUS_TRACKING_METADATA,
    DeadLetterReceipt,
    DeadLetterRecord,
    JobEnvelope,
)
from cognistore.jobs.runtime import AsyncWorker, WorkerConfig

PRINCIPAL = Principal("https://issuer.example", "worker-submitter")


class _Queue:
    def __init__(self) -> None:
        self.dead_letters: list[DeadLetterRecord] = []

    async def publish_dead_letter(self, record: DeadLetterRecord) -> DeadLetterReceipt:
        self.dead_letters.append(record)
        return DeadLetterReceipt(record.dead_letter_id, "DLQ", len(self.dead_letters))


@dataclass
class _Delivery:
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


def _policy(*roles: str) -> dict:
    return {
        "bindings": [{
            "issuer": PRINCIPAL.issuer,
            "subject": PRINCIPAL.subject,
            "roles": list(roles),
        }],
    }


def _job(
    job_type: str = "catalog.scan", *, principal: Principal | None = PRINCIPAL,
    payload: dict | None = None,
) -> JobEnvelope:
    metadata = {STATUS_TRACKING_METADATA: "1"}
    if principal is not None:
        metadata[PRINCIPAL_METADATA] = principal.to_json()
    return JobEnvelope.create(job_type, payload or {}, metadata=metadata)


@pytest.mark.parametrize(("job_type", "roles"), [
    ("catalog.scan", ("operator",)),
    ("policy.run", ("operator", "policy_manager")),
    ("policy.run", ("admin",)),
])
def test_worker_allows_granted_built_in_operations(job_type: str, roles: tuple[str, ...]) -> None:
    async def scenario() -> None:
        queue = _Queue()
        calls = []

        async def handler(job, context) -> None:
            calls.append(context.principal)

        worker = AsyncWorker(
            queue, {job_type: handler}, config=WorkerConfig(heartbeat_interval=0),
            authorization=RBACAuthorizer(RBACPolicy.from_dict(_policy(*roles))),
        )
        delivery = _Delivery(_job(job_type))
        await worker._process(delivery)
        assert calls == [PRINCIPAL]
        assert delivery.acked == 1
        assert delivery.nacked == 0
        assert queue.dead_letters == []

    asyncio.run(scenario())


def test_worker_checks_policy_and_persists_decision_outside_event_loop() -> None:
    async def scenario() -> None:
        loop_thread = threading.get_ident()
        decision_threads = []
        handler_threads = []

        class RecordingCatalog(Catalog):
            def append_audit_event(self, event):
                if event.event_type == AuditEventType.AUTHORIZATION_DECISION.value:
                    decision_threads.append(threading.get_ident())
                return super().append_audit_event(event)

        async def handler(job, context) -> None:
            handler_threads.append(threading.get_ident())

        worker = AsyncWorker(
            _Queue(), {"catalog.scan": handler}, config=WorkerConfig(heartbeat_interval=0),
            audit_catalog=RecordingCatalog(),
            authorization=RBACAuthorizer(RBACPolicy.from_dict(_policy("admin"))),
        )
        delivery = _Delivery(_job())
        await worker._process(delivery)
        assert delivery.acked == 1
        assert handler_threads == [loop_thread]
        assert len(decision_threads) == 1
        assert decision_threads[0] != loop_thread

    asyncio.run(scenario())


@pytest.mark.parametrize("case", [
    "no_grants", "no_identity", "unknown_operation", "unregistered_move", "unmapped_issuer",
])
def test_worker_denies_before_coordinator_or_handler_and_dead_letters(case: str) -> None:
    async def scenario() -> None:
        queue = _Queue()
        catalog = Catalog()
        calls = []

        async def handler(job, context) -> None:
            calls.append("handler")

        class Coordinator:
            async def begin(self, job, context):
                calls.append("coordinator")
                return None

        job_type = {
            "unknown_operation": "plugin.unknown", "unregistered_move": "object.move",
        }.get(case, "catalog.scan")
        principal = (
            None if case == "no_identity"
            else Principal("https://other.example", PRINCIPAL.subject)
            if case == "unmapped_issuer" else PRINCIPAL
        )
        worker = AsyncWorker(
            queue, {job_type: handler}, coordinator=Coordinator(), audit_catalog=catalog,
            config=WorkerConfig(heartbeat_interval=0),
            authorization=(
                None if case == "no_grants"
                else RBACAuthorizer(RBACPolicy.from_dict(_policy("admin")))
            ),
        )
        job = _job(job_type, principal=principal)
        job.metadata["permissions"] = '["administration"]'
        delivery = _Delivery(job)
        # An ambient identity never authenticates an anonymous queued job.
        ambient_authorization = RBACAuthorizer(RBACPolicy.from_dict(_policy("admin")))
        with principal_context(PRINCIPAL), authorization_context(ambient_authorization):
            await worker._process(delivery)
            assert current_authorizer() is ambient_authorization
        assert calls == []
        assert delivery.acked == 1
        assert delivery.nacked == 0
        assert len(queue.dead_letters) == 1
        assert queue.dead_letters[0].category == "authorization"
        events = catalog.list_audit_events(AuditQuery(job_id=job.job_id))
        assert AuditEventType.JOB_STARTED.value not in {event.event_type for event in events}
        assert AuditEventType.JOB_SUCCEEDED.value not in {event.event_type for event in events}
        decisions = [
            event for event in events
            if event.event_type == AuditEventType.AUTHORIZATION_DECISION.value
        ]
        assert len(decisions) == 1
        assert decisions[0].outcome == AuditOutcome.DENIED.value

    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [{}, {"dry_run": True}, {"dry_run": "true"}])
@pytest.mark.parametrize("role", ["policy_manager", "operator"])
def test_policy_job_requires_both_policy_and_movement_for_actual_handler(
    payload: dict, role: str,
) -> None:
    async def scenario() -> None:
        queue = _Queue()
        calls = []

        async def handler(job, context) -> None:
            calls.append(job.job_id)

        worker = AsyncWorker(
            queue, {"policy.run": handler}, config=WorkerConfig(heartbeat_interval=0),
            authorization=RBACAuthorizer(RBACPolicy.from_dict(_policy(role))),
        )
        job = _job("policy.run", payload=payload)
        job.metadata["permissions"] = '["policy"]'
        job.metadata["dry_run"] = "true"
        delivery = _Delivery(job)
        await worker._process(delivery)
        assert calls == []
        assert delivery.acked == 1
        assert delivery.nacked == 0
        assert queue.dead_letters[0].category == "authorization"

    asyncio.run(scenario())


def test_retry_and_redrive_recheck_revoked_file_bindings(tmp_path: Path) -> None:
    async def scenario() -> None:
        policy_path = tmp_path / "authorization.json"
        policy_path.write_text(json.dumps(_policy("admin")))
        queue = _Queue()
        catalog = Catalog()
        calls = []

        async def handler(job, context) -> None:
            calls.append(context.cumulative_attempt)
            raise TimeoutError("retryable backend failure")

        worker = AsyncWorker(
            queue, {"catalog.scan": handler}, audit_catalog=catalog,
            config=WorkerConfig(heartbeat_interval=0, max_attempts=4),
            authorization=RBACAuthorizer(policy_path=policy_path),
        )
        job = _job()
        first = _Delivery(job)
        await worker._process(first)
        assert first.nacked == 1
        assert calls == [1]
        policy_path.write_text('{"bindings": []}')
        retry = _Delivery(JobEnvelope.from_bytes(first.raw_data), attempt=2, consumer_sequence=2)
        await worker._process(retry)
        assert retry.acked == 1
        assert retry.nacked == 0
        assert queue.dead_letters[0].category == "authorization"
        redrive = _Delivery(
            queue.dead_letters[0].job_for_redrive(), stream_sequence=2, consumer_sequence=3,
        )
        await worker._process(redrive)
        assert redrive.acked == 1
        assert redrive.nacked == 0
        assert len(queue.dead_letters) == 2
        assert queue.dead_letters[1].category == "authorization"
        assert calls == [1]

    asyncio.run(scenario())


@pytest.mark.parametrize("replacement", [None, "invalid: [", '{"bindings": []}'])
def test_worker_fails_closed_when_configured_policy_disappears_or_changes(
    tmp_path: Path, replacement: str | None,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "rbac.yaml"
        path.write_text(json.dumps(_policy("admin")))
        authorizer = RBACAuthorizer(policy_path=path)
        if replacement is None:
            path.unlink()
        else:
            path.write_text(replacement)
        queue = _Queue()
        calls = []

        async def handler(job, context) -> None:
            calls.append(job.job_id)

        worker = AsyncWorker(
            queue, {"catalog.scan": handler}, authorization=authorizer,
            config=WorkerConfig(heartbeat_interval=0),
        )
        delivery = _Delivery(_job())
        await worker._process(delivery)
        assert calls == []
        assert delivery.acked == 1
        assert queue.dead_letters[0].category == "authorization"

    asyncio.run(scenario())
