"""Real service operations compose grants without reauthorizing internal projections."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cognistore.api.gateway import CogniStoreGateway
from cognistore.api.models import ImportanceChangeRequest, PolicyRunRequest
from cognistore.auth.authorization import AuthorizationError, RBACAuthorizer, RBACPolicy
from cognistore.auth.principal import Principal, principal_context
from cognistore.core.audit import AuditContext, AuditEventType, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.core.mover import Mover
from cognistore.core.policy import SimplePolicy
from cognistore.core.policy_runner import PolicyRunner
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.jobs.models import EnqueueReceipt, JobEnvelope

PRINCIPAL = Principal("https://issuer.example", "service-user-57")


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[JobEnvelope] = []

    async def enqueue(self, job: JobEnvelope, *, message_id=None) -> EnqueueReceipt:
        self.jobs.append(JobEnvelope.from_bytes(job.to_bytes()))
        return EnqueueReceipt(job.job_id, job.correlation_id, "JOBS", len(self.jobs))


def _authorization(*roles: str) -> RBACAuthorizer:
    return RBACAuthorizer(RBACPolicy({(PRINCIPAL.issuer, PRINCIPAL.subject): roles}))


@pytest.fixture
def service(tmp_path: Path):
    catalog = Catalog()
    drivers = {tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm")}
    drivers["hot"].put_object("documents", "report.txt", b"report")
    catalog.upsert("documents", "report.txt", 6, "hot")
    queue = RecordingQueue()
    return CogniStoreGateway(catalog, drivers, queue=queue), drivers, queue


def test_policy_submission_requires_policy_and_movement_before_enqueue(service) -> None:
    gateway, drivers, queue = service
    gateway.authorization = _authorization("policy_manager")
    request = PolicyRunRequest(bucket="documents")
    with principal_context(PRINCIPAL), pytest.raises(AuthorizationError):
        asyncio.run(gateway.submit_policy_run(request))
    assert queue.jobs == []
    assert (
        gateway.catalog.list_audit_events(
            AuditQuery(event_types=frozenset({AuditEventType.JOB_QUEUED.value}))
        )
        == []
    )
    assert drivers["hot"].get_object("documents", "report.txt") == b"report"

    gateway.authorization = _authorization("policy_manager", "operator")
    with principal_context(PRINCIPAL):
        status = asyncio.run(gateway.submit_policy_run(request))
    assert status.status == "queued"
    assert len(queue.jobs) == 1
    assert queue.jobs[0].job_id == str(status.job_id)
    assert queue.jobs[0].principal == PRINCIPAL
    assert queue.jobs[0].job_type == "policy.run"


@pytest.mark.parametrize("incomplete_role", ["writer", "policy_manager"])
def test_importance_update_composes_write_and_policy_grants(service, incomplete_role: str) -> None:
    gateway, drivers, _ = service
    request = ImportanceChangeRequest(
        bucket="documents",
        key="report.txt",
        level="high",
        actor_id="untrusted-actor",
        provenance="Requested importance update",
    )
    gateway.authorization = _authorization(incomplete_role)
    with principal_context(PRINCIPAL), pytest.raises(AuthorizationError):
        gateway.set_importance(request, correlation_id="importance-57")
    assert gateway.catalog.get("documents", "report.txt").importance is None

    gateway.authorization = _authorization("writer", "policy_manager")
    with principal_context(PRINCIPAL):
        response = gateway.set_importance(request, correlation_id="importance-57")
    record = gateway.catalog.get("documents", "report.txt")
    assert record.importance.level == "high"
    assert record.importance.actor_id == PRINCIPAL.actor_id
    assert response.bucket == "documents" and response.key == "report.txt"
    assert drivers["hot"].get_object("documents", "report.txt") == b"report"
    assert gateway.catalog.list_audit_events(
        AuditQuery(event_types=frozenset({AuditEventType.IMPORTANCE_CHANGED.value}))
    )


def test_auditor_projects_policy_decision_and_nested_job_without_read_permission(service) -> None:
    gateway, drivers, queue = service
    gateway.authorization = _authorization("policy_manager", "operator")
    with principal_context(PRINCIPAL):
        status = asyncio.run(gateway.submit_policy_run(PolicyRunRequest(bucket="documents")))
    job = queue.jobs[0]
    runner = PolicyRunner(
        gateway.catalog,
        drivers,
        Mover(drivers, gateway.catalog),
        SimplePolicy(5),
        audit_context=AuditContext(
            correlation_id=job.correlation_id,
            job_id=job.job_id,
            actor_type=PRINCIPAL.actor_type,
            actor_id=PRINCIPAL.actor_id,
        ),
    )
    action = runner.plan_once("documents")[0]
    gateway.authorization = _authorization("auditor")
    with principal_context(PRINCIPAL):
        decision = gateway.get_policy_decision(action.decision_event_id)
        page = gateway.list_policy_decisions(job_id=job.job_id)
        with pytest.raises(AuthorizationError):
            gateway.get_job(job.job_id)
        with pytest.raises(AuthorizationError):
            gateway.get_catalog_object("documents", "report.txt")
    assert decision.explanation.state == "available"
    assert decision.current.tier == "hot" and decision.proposed.tier == "warm"
    assert decision.execution.state == "planned"
    assert decision.execution.job == status
    assert page.items == [decision]


def test_authenticated_direct_gateway_denies_without_policy_then_allows_configured_writer(service):
    gateway, drivers, _ = service
    kwargs = {"overwrite": False, "content_type": "text/plain"}
    with principal_context(PRINCIPAL), pytest.raises(AuthorizationError):
        gateway.put_object("hot", "documents", "new.txt", b"new", **kwargs)
    assert gateway.catalog.get("documents", "new.txt") is None
    assert list(drivers["hot"].list_objects("documents")) == ["report.txt"]

    gateway.authorization = _authorization("writer")
    with principal_context(PRINCIPAL):
        resource = gateway.put_object("hot", "documents", "new.txt", b"new", **kwargs)
    assert resource.size == 3
    assert gateway.catalog.get("documents", "new.txt").size == 3
    assert drivers["hot"].get_object("documents", "new.txt") == b"new"
