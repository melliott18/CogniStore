from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.core.audit import AuditContext, AuditEvent, AuditEventType, AuditOutcome
from cognistore.core.catalog import Catalog
from cognistore.core.mover import Mover
from cognistore.core.policy import SimplePolicy
from cognistore.core.policy_runner import PolicyRunner
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.sdk import (
    CogniStoreClient,
    DecisionExecution,
    DecisionExplanation,
    DecisionPlacement,
    DecisiveSignal,
    JobStatus,
    NotFoundError,
    PolicyConfig,
    PolicyDecision,
    PolicyDecisionPage,
    PolicyDecisionResource,
    PolicyEvaluationRequest,
    PolicyReason,
    ReasonConstraints,
    ResponseContractError,
)


def test_sdk_placement_decisions_round_trip_from_preview_to_completed_move(tmp_path: Path) -> None:
    catalog = Catalog()
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    keys = ["folder/first ?#&.txt", "folder/second.txt"]
    for key in keys:
        drivers["hot"].put_object("documents", key, b"123456")
        catalog.upsert("documents", key, 6, "hot")
    gateway = CogniStoreGateway(catalog, drivers)
    job_id, correlation_id = uuid4(), uuid4()
    context = AuditContext(
        str(correlation_id), "system", "sdk-test", job_id=str(job_id),
    )
    with TestClient(create_app(gateway)) as http_client:
        with CogniStoreClient("http://testserver", http_client=http_client) as sdk:
            preview = sdk.preview_policy_decision(PolicyEvaluationRequest(
                bucket="documents", key=keys[0], config=PolicyConfig(threshold=5),
            ))
            assert isinstance(preview, PolicyDecision)
            assert PolicyDecisionResource is PolicyDecision
            assert preview.decision_id is None
            assert isinstance(preview.current, DecisionPlacement)
            assert preview.current.tier == "hot" and preview.proposed.tier == "warm"
            assert preview.changed_fields == ["tier"]
            assert isinstance(preview.execution, DecisionExecution)
            assert preview.execution.state == "dry_run" and preview.execution.job is None
            assert isinstance(preview.explanation, DecisionExplanation)
            reason = preview.explanation.structured_reason
            assert isinstance(reason, PolicyReason)
            assert isinstance(reason.decisive_signals[0], DecisiveSignal)
            assert reason.decisive_signals[0].value == 6
            assert isinstance(reason.constraints, ReasonConstraints)
            assert reason.constraints.residency_active is False
            assert catalog.list_audit_events() == []
            assert catalog.get("documents", keys[0]).tier == "hot"

            catalog.append_audit_event(AuditEvent.create(
                AuditEventType.JOB_QUEUED, AuditOutcome.REQUESTED, context,
                details={"job_type": "policy.run"},
            ))
            runner = PolicyRunner(
                catalog, drivers, Mover(drivers, catalog), SimplePolicy(5),
                audit_context=context,
            )
            runner.run_once("documents")
            catalog.append_audit_event(AuditEvent.create(
                AuditEventType.JOB_SUCCEEDED, AuditOutcome.SUCCEEDED, context,
                details={"job_type": "policy.run"},
            ))
            first = sdk.list_policy_decisions(job_id=job_id, correlation_id=correlation_id, limit=1)
            assert isinstance(first, PolicyDecisionPage)
            assert len(first.items) == 1
            assert first.page.next_cursor is not None
            second = sdk.list_policy_decisions(
                job_id=job_id, correlation_id=correlation_id,
                limit=1, cursor=first.page.next_cursor,
            )
            assert len(second.items) == 1 and second.page.next_cursor is None
            assert {first.items[0].key, second.items[0].key} == set(keys)
            history = sdk.list_policy_decisions(bucket="documents", key=keys[0])
            assert len(history.items) == 1
            decision_id = history.items[0].decision_id
            assert decision_id is not None
            executed = sdk.get_policy_decision(decision_id)
            assert executed == history.items[0]
            assert executed.current.tier == "hot" and executed.proposed.tier == "warm"
            assert executed.execution.state == "completed"
            assert isinstance(executed.execution.job, JobStatus)
            assert executed.execution.job.status == "succeeded"
            assert executed.execution.job.job_id == job_id
            assert executed.explanation.structured_reason.code == reason.code
            assert executed.explanation.structured_reason.decisive_signals == reason.decisive_signals
            with pytest.raises(NotFoundError):
                sdk.get_policy_decision(str(uuid4()))


def test_sdk_decision_history_preserves_opaque_values_and_missing_evidence() -> None:
    observed: list[httpx.Request] = []
    payload = {
        "schema_version": 1, "decision_id": "decision", "bucket": "documents",
        "key": "folder/object", "evaluated_at": "2026-09-11T00:00:00Z",
        "action": None, "disposition": "unavailable",
        "current": {"tier": None, "future_field": True}, "proposed": {"tier": None},
        "changed_fields": [],
        "explanation": {
            "state": "legacy", "structured_reason": None, "model_details": "unavailable",
        },
        "execution": {"mode": "persisted", "state": "unavailable"},
        "future_field": {"added": "later"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        body = (
            {"items": [payload], "page": {"limit": 2, "next_cursor": "opaque/+=?"}}
            if request.url.path == "/v1/policy-decisions" else payload
        )
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        with CogniStoreClient("http://example.test", http_client=http_client) as sdk:
            page = sdk.list_policy_decisions(
                bucket="docs ?&", key="folder/# ?&", job_id="job/+=?",
                correlation_id="trace/+=?", limit=2, cursor="opaque/+=?",
            )
            assert page.page.next_cursor == "opaque/+=?"
            assert page.items[0].explanation.structured_reason is None
            assert page.items[0].execution.state == "unavailable"
            assert dict(observed[0].url.params) == {
                "bucket": "docs ?&", "key": "folder/# ?&", "job_id": "job/+=?",
                "correlation_id": "trace/+=?", "limit": "2", "cursor": "opaque/+=?",
            }
            sdk.get_policy_decision("../reserved ?#")
            assert observed[1].url.raw_path == b"/v1/policy-decisions/..%2Freserved%20%3F%23"
            payload["execution"] = {"mode": "preview", "state": "unknown"}
            with pytest.raises(ResponseContractError):
                sdk.get_policy_decision("malformed")
