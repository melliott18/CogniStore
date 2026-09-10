"""Inference evidence survives runner guardrails without authorizing unsafe moves."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cognistore.core.audit import AuditContext
from cognistore.core.catalog import Catalog, ObjectRecord
from cognistore.core.mover import Mover
from cognistore.core.placement_controls import ImportanceTag
from cognistore.core.placement_llm import CallablePlacementProvider, FakePlacementProvider
from cognistore.core.policy import LLMPolicy, PolicyDecision
from cognistore.core.policy_factory import ThresholdProvider
from cognistore.core.policy_runner import PolicyRunner
from cognistore.core.policy_snapshot import replay_policy_snapshot
from cognistore.drivers.posix_driver import PosixDriver


class EvidencePolicy:
    """Supply explicit evidence to exercise the runner independently of transport."""

    def __init__(self, destination="warm", *, fallback_reason=None):
        self.destination = destination
        self.fallback_reason = fallback_reason
        self.calls = 0

    def evaluate(self, current_tier, size):
        self.calls += 1
        action = "move" if self.destination else "stay"
        return PolicyDecision(
            action,
            "placement recommendation",
            self.destination,
            llm_audit={
                "provider": "test-provider",
                "provider_version": "transport-v1",
                "model": "placement-model-v2",
                "prompt": "authorization: Bearer private-prompt-credential",
                "response": {"api_key": "private-response-credential"},
                "attempts": [{"attempt": self.calls}],
                "fallback_reason": self.fallback_reason,
                "final_proposal": {"action": action, "dst_tier": self.destination},
            },
        )


def make_runner(tmp_path: Path, policy, **kwargs):
    drivers = {
        tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm", "cold")
    }
    catalog = Catalog()
    catalog.upsert("bucket", "object", size=7, tier="hot")
    drivers["hot"].put_object("bucket", "object", b"content")
    runner = PolicyRunner(
        catalog, drivers, Mover(drivers, catalog), policy,
        audit_context=AuditContext("llm-audit-test", "system", "test-runner"),
        policy_name="llm", **kwargs,
    )
    return runner, catalog, drivers


@pytest.mark.parametrize("destination", ["warm", None])
def test_llm_preview_and_evaluation_expose_redacted_evidence_without_writes(
    tmp_path, destination,
):
    runner, catalog, drivers = make_runner(tmp_path, EvidencePolicy(destination))

    preview = runner.preview_once("bucket")[0]
    evaluation = runner.evaluate_once("bucket", "object")
    actions = runner.run_once("bucket", dry_run=True)

    for result in (preview, evaluation):
        assert result.llm_audit["provider"] == "test-provider"
        assert result.llm_audit["final_decision"]["action"] == (
            "move" if destination else "stay"
        )
        assert result.to_mapping()["llm_audit"] == result.llm_audit
        assert "private-prompt-credential" not in str(result.llm_audit)
        assert "private-response-credential" not in str(result.llm_audit)
    if destination:
        assert actions[0].llm_audit["final_decision"]["destination_tier"] == destination
    else:
        assert actions == []
    assert catalog.list_audit_events() == []
    assert catalog.get("bucket", "object").tier == "hot"
    assert drivers["hot"].get_object("bucket", "object") == b"content"
    assert list(drivers["warm"].list_objects("bucket")) == []


def test_llm_evidence_retains_proposal_when_importance_suppresses_move(tmp_path):
    runner, catalog, _ = make_runner(tmp_path, EvidencePolicy("cold"))
    record = ObjectRecord(
        "bucket", "object", 7, "hot",
        importance=ImportanceTag(
            level="high", actor_type="user", actor_id="operator",
            provenance="classification", updated_at="2026-09-01T00:00:00Z",
        ),
        importance_revision=1,
    )

    evaluation = runner.reevaluate_record(record)

    assert evaluation.action == "stay"
    assert evaluation.llm_audit["final_proposal"]["dst_tier"] == "cold"
    assert evaluation.llm_audit["final_decision"] == {
        "action": "stay", "destination_tier": None,
        "reason": "movement constraints forbid policy destination", "outcome": "stayed",
    }
    event = catalog.list_audit_events()[0]
    assert event.details["llm_audit"] == evaluation.llm_audit
    assert event.details["dataset"]["decision"]["action"] == "stay"


def test_llm_audit_retries_preserve_first_evidence_and_snapshot_model(tmp_path):
    runner, catalog, _ = make_runner(tmp_path, EvidencePolicy())

    first = runner.plan_once("bucket")[0]
    second = runner.plan_once("bucket")[0]

    assert first.decision_event_id == second.decision_event_id
    assert first.llm_audit == second.llm_audit
    assert second.llm_audit["attempts"] == [{"attempt": 1}]
    events = catalog.list_audit_events()
    assert len(events) == 1
    assert events[0].details["llm_audit"] == first.llm_audit
    assert events[0].details["dataset"]["policy"]["model"] == {
        "identity": "placement-model-v2", "version": None,
    }
    assert events[0].details["dataset"]["replay"]["supported"] is False


@pytest.mark.parametrize("fallback_reason", ["malformed_response", "unavailable", "late_response"])
def test_failed_current_inference_cannot_replay_selected_snapshot(
    tmp_path, monkeypatch, fallback_reason,
):
    policy = EvidencePolicy()
    runner, catalog, drivers = make_runner(tmp_path, policy)
    first = runner.plan_once("bucket")[0]
    selected_event = catalog.get_audit_event(first.decision_event_id)
    policy.destination = None
    policy.fallback_reason = fallback_reason
    # Even if retry persistence returns a previously selected decision, current
    # failed inference must fence execution before that snapshot is consumed.
    original_get = catalog.get_audit_event
    returned_selected = False

    def stale_first_lookup(event_id):
        nonlocal returned_selected
        if not returned_selected:
            returned_selected = True
            return selected_event
        return original_get(event_id)

    monkeypatch.setattr(catalog, "get_audit_event", stale_first_lookup)

    assert runner.run_once("bucket") == []

    assert catalog.get("bucket", "object").tier == "hot"
    assert drivers["hot"].get_object("bucket", "object") == b"content"
    assert list(drivers["warm"].list_objects("bucket")) == []
    failure_event = next(
        event for event in catalog.list_audit_events()
        if event.event_id != first.decision_event_id
    )
    assert failure_event.details["inference_retry_of"] == first.decision_event_id
    assert failure_event.details["llm_audit"]["fallback_reason"] == fallback_reason
    assert failure_event.details["llm_audit"]["final_decision"]["action"] == "stay"


def test_invalid_retry_after_selected_inference_persists_its_stay(tmp_path):
    provider = FakePlacementProvider(
        '{"action":"move","dst_tier":"warm","reason":"eligible destination"}'
    )
    runner, catalog, drivers = make_runner(tmp_path, LLMPolicy(provider))
    selected = runner.plan_once("bucket")[0]
    provider.response = "invalid-json"

    assert runner.run_once("bucket") == []

    events = catalog.list_audit_events()
    assert len(events) == 2
    failed = next(event for event in events if event.event_id != selected.decision_event_id)
    assert failed.details["llm_audit"]["response"] == "[response omitted: invalid json]"
    assert failed.details["llm_audit"]["validation_errors"]
    assert failed.details["llm_audit"]["final_decision"]["action"] == "stay"
    assert catalog.get("bucket", "object").tier == "hot"
    assert drivers["hot"].get_object("bucket", "object") == b"content"


def test_distinct_retry_failures_are_auditable_and_identical_failures_deduplicate(tmp_path):
    first_response = '{"action":"move","dst_tier":"warm","extra":"first"}'
    second_response = '{"action":"move","dst_tier":"warm","extra":"second"}'
    provider = FakePlacementProvider(first_response)
    runner, catalog, _ = make_runner(tmp_path, LLMPolicy(provider))
    assert runner.plan_once("bucket") == []
    first_event = catalog.list_audit_events()[0]
    provider.response = second_response

    assert runner.plan_once("bucket") == []
    assert runner.plan_once("bucket") == []

    events = catalog.list_audit_events()
    assert len(events) == 2
    retry = next(event for event in events if event.event_id != first_event.event_id)
    assert retry.details["inference_retry_of"] == first_event.event_id
    assert retry.details["llm_audit"]["response"] == second_response
    assert retry.details["llm_audit"]["final_decision"]["action"] == "stay"
    assert first_event.details["llm_audit"]["response"] == first_response


def test_concurrent_distinct_inference_failure_is_retained_after_audit_conflict(
    tmp_path, monkeypatch,
):
    current_response = '{"action":"move","dst_tier":"warm","extra":"current"}'
    racing_response = '{"action":"move","dst_tier":"warm","extra":"racing"}'
    runner, catalog, _ = make_runner(tmp_path, LLMPolicy(FakePlacementProvider(current_response)))
    original_append = catalog.append_audit_event
    raced = False

    def append_with_racing_failure(event):
        nonlocal raced
        if not raced:
            raced = True
            details = dict(event.details)
            details["llm_audit"] = {
                **details["llm_audit"], "response": racing_response,
            }
            original_append(replace(event, details=details))
            raise ValueError("concurrent audit conflict")
        return original_append(event)

    monkeypatch.setattr(catalog, "append_audit_event", append_with_racing_failure)

    assert runner.plan_once("bucket") == []

    events = catalog.list_audit_events()
    assert len(events) == 2
    assert {event.details["llm_audit"]["response"] for event in events} == {
        racing_response, current_response,
    }
    assert all(event.details["llm_audit"]["final_decision"]["action"] == "stay" for event in events)


def test_llm_residency_blocks_inference_before_provider_call(tmp_path):
    policy = EvidencePolicy()
    instant = datetime.now(timezone.utc)
    runner, catalog, _ = make_runner(
        tmp_path, policy, movement_constraints={"minimum_residency_seconds": {"hot": 3600}},
    )

    evaluation = runner.evaluate_once("bucket", "object", as_of=instant)

    assert evaluation.action == "stay"
    assert policy.calls == 0
    assert evaluation.llm_audit is None
    assert catalog.list_audit_events() == []


@pytest.mark.parametrize("response,action", [
    ('{"action":"move","dst_tier":"warm","reason":"lower storage cost"}', "move"),
    ('{"action":"stay","dst_tier":null,"reason":"uncertain"}', "stay"),
    ('{"action":"move","dst_tier":"warm"}', "stay"),
])
def test_inference_to_execution_persists_proposal_and_final_decision(tmp_path, response, action):
    policy = LLMPolicy(FakePlacementProvider(response), ("hot", "warm"))
    runner, catalog, drivers = make_runner(tmp_path, policy)

    evaluation = runner.evaluate_once("bucket", "object")
    assert catalog.list_audit_events() == []
    results = runner.run_once("bucket")

    assert evaluation.action == action
    event = next(event for event in catalog.list_audit_events() if "llm_audit" in event.details)
    audit = event.details["llm_audit"]
    assert audit["provider"] == "fake"
    assert audit["prompt_hash"] and audit["schema_hash"]
    assert audit["response"] == response
    assert audit["final_decision"]["action"] == action
    assert audit["final_proposal"]["action"] == action
    if action == "move":
        assert results[0].llm_audit == audit
        assert catalog.get("bucket", "object").tier == "warm"
        assert drivers["warm"].get_object("bucket", "object") == b"content"
    else:
        assert results == []
        assert catalog.get("bucket", "object").tier == "hot"
        assert drivers["hot"].get_object("bucket", "object") == b"content"


def test_inference_receives_intersection_of_runner_policy_and_importance_tiers(tmp_path):
    prompts = []

    def complete(prompt, schema, timeout_seconds):
        prompts.append(prompt)
        return '{"action":"move","dst_tier":"warm","reason":"eligible destination"}'

    provider = CallablePlacementProvider(complete, provider_id="test", model="placement")
    policy = LLMPolicy(provider, ("hot", "warm", "cold"))
    runner, catalog, _ = make_runner(tmp_path, policy, allowed_tiers=("warm", "cold"))
    record = ObjectRecord(
        "bucket", "object", 7, "hot",
        importance=ImportanceTag(
            level="high", actor_type="user", actor_id="operator",
            provenance="classification", updated_at="2026-09-01T00:00:00Z",
        ),
    )

    evaluation = runner.evaluate_record(record)

    assert json.loads(prompts[0].splitlines()[-1])["allowed_tiers"] == ["warm"]
    assert evaluation.destination_tier == "warm"
    assert policy.allowed_tiers == {"hot", "warm", "cold"}
    assert catalog.list_audit_events() == []


def test_legacy_threshold_snapshot_preserves_runner_tier_restriction(tmp_path):
    policy = LLMPolicy(ThresholdProvider(0, ("hot", "warm")), ("hot", "warm"))
    runner, catalog, _ = make_runner(tmp_path, policy, allowed_tiers=("hot",))

    assert runner.plan_once("bucket") == []

    snapshot = catalog.list_audit_events()[0].details["dataset"]
    assert snapshot["decision"]["action"] == "stay"
    assert replay_policy_snapshot(snapshot).action == "stay"
