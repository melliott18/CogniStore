"""Contract, retained evidence, and causal boundaries for placement resources."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.api.models import PolicyEvaluationRequest
from cognistore.api.pagination import encode_cursor
from cognistore.core.audit import AuditContext, AuditEvent, AuditEventType, AuditOutcome, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.core.mover import Mover
from cognistore.core.policy import SimplePolicy
from cognistore.core.policy_runner import PolicyRunner
from cognistore.drivers.posix_driver import PosixDriver

NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)


@pytest.fixture
def service(tmp_path):
    catalog = Catalog()
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    drivers["hot"].put_object("bucket", "folder/object", b"123456")
    catalog.upsert("bucket", "folder/object", 6, "hot")
    return CogniStoreGateway(catalog, drivers), drivers


def decision_event(service, *, details=None, move_id=None, job_id=None, event_id=None):
    gateway, _ = service
    return gateway.catalog.append_audit_event(AuditEvent.create(
        AuditEventType.POLICY_DECISION, AuditOutcome.SELECTED,
        AuditContext(actor_type="system", actor_id="test", correlation_id="placement-test", job_id=job_id),
        event_id=event_id, occurred_at=NOW,
        bucket="bucket", object_key="folder/object", move_id=move_id,
        details=details or {
            "current_tier": "hot", "destination_tier": "warm", "action": "move",
        },
    ))


def test_preview_uses_the_exact_retained_reason_and_does_not_write(service):
    gateway, drivers = service
    runner = PolicyRunner(
        gateway.catalog, drivers, Mover(drivers, gateway.catalog), SimplePolicy(5),
        idempotency_namespace="placement-test", clock=lambda: NOW,
    )
    snapshot, reason = runner.preview_decision("bucket", "folder/object")
    assert gateway.catalog.list_audit_events() == []
    action = runner.plan_once("bucket")[0]
    stored = gateway.catalog.get_audit_event(action.decision_event_id)
    assert stored.details["structured_reason"] == reason
    assert stored.details["dataset"]["decision"] == snapshot["decision"]
    assert gateway.catalog.get("bucket", "folder/object").tier == "hot"
    assert gateway.get_policy_decision(stored.event_id).execution.state == "planned"


def test_legacy_record_has_explicit_unavailable_reason_and_frozen_placement(service):
    gateway, _ = service
    event = decision_event(service)
    gateway.catalog.upsert("bucket", "folder/object", 6, "warm")
    with TestClient(create_app(gateway)) as client:
        body = client.get(f"/v1/policy-decisions/{event.event_id}").json()
        assert body["current"]["tier"] == "hot"
        assert body["proposed"]["tier"] == "warm"
        assert body["explanation"] == {
            "state": "legacy", "structured_reason": None, "model_details": "unavailable",
        }
        assert body["execution"]["state"] == "not_requested"


def test_corrupt_or_future_reason_does_not_hide_valid_snapshot(service):
    gateway, drivers = service
    runner = PolicyRunner(gateway.catalog, drivers, Mover(drivers, gateway.catalog), SimplePolicy(5))
    snapshot, reason = runner.preview_decision("bucket", "folder/object")
    reason["schema_version"] = 999
    event = decision_event(service, details={"dataset": snapshot, "structured_reason": reason})
    body = gateway.get_policy_decision(event.event_id)
    assert body.explanation.state == "unavailable"
    assert body.explanation.structured_reason is None
    assert body.current.tier == "hot" and body.proposed.tier == "warm"


def test_corrupt_snapshot_does_not_hide_valid_guardrail_reason(service):
    gateway, drivers = service
    runner = PolicyRunner(gateway.catalog, drivers, Mover(drivers, gateway.catalog), SimplePolicy(5))
    snapshot, reason = runner.preview_decision("bucket", "folder/object")
    snapshot["schema_version"] = 999
    event = decision_event(service, details={"dataset": snapshot, "structured_reason": reason})
    body = gateway.get_policy_decision(event.event_id)
    assert body.current.tier is None and body.proposed.tier is None
    assert body.explanation.state == "available"
    assert body.explanation.structured_reason.code == "size_threshold"


def test_preview_provider_metadata_unavailable_retains_rule_evidence(service, monkeypatch):
    gateway, drivers = service
    runner = PolicyRunner(gateway.catalog, drivers, Mover(drivers, gateway.catalog), SimplePolicy(5))
    snapshot, reason = runner.preview_decision("bucket", "folder/object")
    reason["policy"]["model"] = None
    reason["confidence"]["source"] = "not_reported"
    monkeypatch.setattr(gateway, "_policy_runner", lambda config: runner)
    monkeypatch.setattr(runner, "preview_decision", lambda *args: (snapshot, reason))
    body = gateway.preview_policy(PolicyEvaluationRequest(bucket="bucket", key="folder/object"))
    assert body.explanation.model_details == "unavailable"
    assert body.explanation.structured_reason.code == "size_threshold"
    assert body.explanation.structured_reason.decisive_signals[0].name == "size_bytes"
    assert body.execution.state == "dry_run"


def test_causal_chain_rejects_another_decision_and_survives_missing_ancestors(service):
    gateway, _ = service
    original = decision_event(service, move_id="same-move")
    other = decision_event(service, move_id="same-move")
    completion = gateway.catalog.append_audit_event(AuditEvent.create(
        AuditEventType.MOVE_COMPLETED, AuditOutcome.SUCCEEDED,
        AuditContext(actor_type="system", actor_id="test", correlation_id="placement-test", causation_id=other.event_id),
        bucket="bucket", object_key="folder/object", move_id="same-move",
    ))
    # The audit move chain may include both decisions, but completion belongs
    # to its nearest decision only.
    assert gateway.get_policy_decision(original.event_id).execution.state == "unavailable"
    assert gateway.get_policy_decision(other.event_id).execution.state == "completed"
    gateway.catalog._audit_events.pop(other.event_id)
    result = gateway.get_policy_decision(original.event_id)
    assert result.execution.state == "unavailable"
    assert result.execution.event_id != completion.event_id


@pytest.mark.parametrize("has_started", [False, True])
def test_successful_parent_job_does_not_claim_a_completed_move(service, has_started):
    gateway, _ = service
    job_id, correlation_id = str(uuid4()), str(uuid4())
    context = AuditContext(actor_type="system", actor_id="test", correlation_id=correlation_id, job_id=job_id)
    for event_type, outcome in (
        (AuditEventType.JOB_QUEUED, AuditOutcome.REQUESTED),
        (AuditEventType.JOB_SUCCEEDED, AuditOutcome.SUCCEEDED),
    ):
        gateway.catalog.append_audit_event(AuditEvent.create(
            event_type, outcome, context, details={"job_type": "policy.run"},
        ))
    event = gateway.catalog.append_audit_event(AuditEvent.create(
        AuditEventType.POLICY_DECISION, AuditOutcome.SELECTED, context,
        bucket="bucket", object_key="folder/object", move_id="missing-move",
        details={"action": "move", "current_tier": "hot", "destination_tier": "warm"},
    ))
    if has_started:
        prepared = gateway.catalog.append_audit_event(AuditEvent.create(
            AuditEventType.MOVE_PREPARED, AuditOutcome.STARTED,
            AuditContext(correlation_id, "system", "test", job_id=job_id, causation_id=event.event_id),
            bucket="bucket", object_key="folder/object", move_id="missing-move",
        ))
        middle = gateway.catalog.append_audit_event(AuditEvent.create(
            AuditEventType.MOVE_TRANSITIONED, AuditOutcome.SUCCEEDED,
            AuditContext(correlation_id, "system", "test", job_id=job_id, causation_id=prepared.event_id),
            bucket="bucket", object_key="folder/object", move_id="missing-move",
        ))
        gateway.catalog.append_audit_event(AuditEvent.create(
            AuditEventType.MOVE_COMPLETED, AuditOutcome.SUCCEEDED,
            AuditContext(correlation_id, "system", "test", job_id=job_id, causation_id=middle.event_id),
            bucket="bucket", object_key="folder/object", move_id="missing-move",
        ))
        assert gateway.get_policy_decision(event.event_id).execution.state == "completed"
        # Retention can remove a middle transition while retaining the final
        # event. An older connected preparation cannot imply ongoing work.
        gateway.catalog._audit_events.pop(middle.event_id)
    result = gateway.get_policy_decision(event.event_id)
    assert result.execution.job.status == "succeeded"
    assert result.execution.state == "unavailable"
    assert result.execution.event_id == (prepared.event_id if has_started else None)


def test_execution_chain_cannot_cross_move_identities(service):
    gateway, _ = service
    event = decision_event(service)
    prepared = gateway.catalog.append_audit_event(AuditEvent.create(
        AuditEventType.MOVE_PREPARED, AuditOutcome.STARTED,
        AuditContext("placement-test", "system", "test", causation_id=event.event_id),
        bucket="bucket", object_key="folder/object", move_id="first-move",
    ))
    gateway.catalog.append_audit_event(AuditEvent.create(
        AuditEventType.MOVE_COMPLETED, AuditOutcome.SUCCEEDED,
        AuditContext("placement-test", "system", "test", causation_id=prepared.event_id),
        bucket="bucket", object_key="folder/object", move_id="different-move",
    ))
    result = gateway.get_policy_decision(event.event_id)
    assert result.execution.state == "running"
    assert result.execution.event_id == prepared.event_id


def test_pagination_is_query_bound_and_retains_timestamp_ties(service):
    gateway, _ = service
    expected = [decision_event(service).event_id for _ in range(5)]
    with TestClient(create_app(gateway)) as client:
        params = {"bucket": "bucket", "key": "folder/object", "limit": 2}
        page = client.get("/v1/policy-decisions", params=params).json()
        actual = [item["decision_id"] for item in page["items"]]
        cursor = page["page"]["next_cursor"]
        wrong_filter = client.get("/v1/policy-decisions", params={**params, "job_id": "other", "cursor": cursor})
        assert wrong_filter.status_code == 422
        assert wrong_filter.json()["error"]["code"] == "invalid_cursor"
        while cursor is not None:
            page = client.get("/v1/policy-decisions", params={**params, "cursor": cursor}).json()
            actual.extend(item["decision_id"] for item in page["items"])
            cursor = page["page"]["next_cursor"]
        assert actual == sorted(expected, reverse=True)


@pytest.mark.parametrize("params", [
    {"key": "folder/object"}, {"limit": 201}, {"limit": 0}, {"cursor": "invalid"},
])
def test_invalid_history_query_uses_public_validation_envelope(service, params):
    gateway, _ = service
    with TestClient(create_app(gateway)) as client:
        response = client.get("/v1/policy-decisions", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] in {"validation_error", "invalid_cursor"}


def test_bad_boundary_inside_well_formed_cursor_is_rejected(service):
    gateway, _ = service
    cursor = encode_cursor(
        resource="policy-decisions",
        filters={"bucket": None, "key": None, "job_id": None, "correlation_id": None},
        last_key='["bad timestamp", "bad event id"]',
    )
    with TestClient(create_app(gateway)) as client:
        response = client.get("/v1/policy-decisions", params={"cursor": cursor})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_cursor"


def test_nondecision_and_missing_resource_are_404(service):
    gateway, _ = service
    event = gateway.catalog.append_audit_event(AuditEvent.create(
        AuditEventType.JOB_STARTED, AuditOutcome.STARTED, AuditContext("placement-test", "system", "test"),
    ))
    with TestClient(create_app(gateway)) as client:
        for identifier in (event.event_id, str(uuid4())):
            assert client.get(f"/v1/policy-decisions/{identifier}").status_code == 404


def test_legacy_rejection_does_not_render_effective_change(service):
    gateway, _ = service
    event = gateway.catalog.append_audit_event(AuditEvent.create(
        AuditEventType.POLICY_DECISION, AuditOutcome.REJECTED, AuditContext("placement-test", "system", "test"),
        bucket="bucket", object_key="folder/object",
        details={"action": "move", "current_tier": "hot", "destination_tier": "unknown"},
    ))
    result = gateway.get_policy_decision(event.event_id)
    assert result.disposition == "rejected"
    assert result.changed_fields == []
    assert result.current == result.proposed


@pytest.mark.parametrize("redact_correlation", [False, True])
def test_retained_long_identifiers_are_queryable_without_public_pseudonym_bypass(
    service, redact_correlation,
):
    gateway, _ = service
    key = "/".join(["segment" * 29] * 3)
    correlation_id = "trace" * 140 if redact_correlation else "placement-test"
    context = AuditContext(correlation_id, "system", "test")
    event = gateway.catalog.append_audit_event(AuditEvent.create(
        AuditEventType.POLICY_DECISION, AuditOutcome.SELECTED, context,
        bucket="bucket", object_key=key, move_id="long-key-move",
        details={"action": "move", "current_tier": "hot", "destination_tier": "warm"},
    ))
    completed = gateway.catalog.append_audit_event(AuditEvent.create(
        AuditEventType.MOVE_COMPLETED, AuditOutcome.SUCCEEDED,
        AuditContext(correlation_id, "system", "test", causation_id=event.event_id),
        bucket="bucket", object_key=key, move_id="long-key-move",
    ))
    assert event.object_key.startswith("[REDACTED:sha256:")
    with pytest.raises(ValueError, match="reserved pseudonym"):
        AuditQuery(bucket="bucket", object_key=event.object_key)
    with TestClient(create_app(gateway)) as client:
        detail = client.get(f"/v1/policy-decisions/{event.event_id}")
        page = client.get("/v1/policy-decisions", params={
            "bucket": "bucket", "key": key, "correlation_id": correlation_id,
        })
        reserved_query = client.get("/v1/policy-decisions", params={
            "bucket": "bucket", "key": event.object_key,
        })
    assert detail.status_code == page.status_code == 200
    assert reserved_query.status_code == 422
    assert detail.json()["execution"]["state"] == "completed"
    assert detail.json()["execution"]["event_id"] == completed.event_id
    assert page.json()["items"] == [detail.json()]
