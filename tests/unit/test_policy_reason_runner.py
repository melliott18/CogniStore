"""Reason persistence through batch failure, hard guards, and retries."""

import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

import pytest

from cognistore.core.audit import AuditEventType, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.core.mover import Mover
from cognistore.core.placement_controls import ImportanceTag, MovementConstraints, StabilityOverride
from cognistore.core.policy import ContentAwarePolicy, LLMPolicy, PolicyDecision, SimplePolicy
from cognistore.core.policy_dataset import export_policy_dataset, validate_policy_dataset
from cognistore.core.policy_reasons import reason_from_audit_details
from cognistore.core.policy_runner import PolicyRunner
from cognistore.drivers.posix_driver import PosixDriver

NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


def make_runner(tmp_path, *, policy=None, controls=None):
    catalog = Catalog()
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    for key, data in (("a-move", b"123456"), ("b-stay", b"1"), ("c-move", b"1234567")):
        drivers["hot"].put_object("bucket", key, data)
        catalog.upsert("bucket", key, len(data), "hot")
    return PolicyRunner(
        catalog, drivers, Mover(drivers, catalog), policy or SimplePolicy(5),
        idempotency_namespace="ticket-49", movement_constraints=controls,
        clock=lambda: NOW,
    )


def decisions(runner):
    return runner.catalog.list_audit_events(
        AuditQuery(event_types=frozenset({AuditEventType.POLICY_DECISION}))
    )


def test_batch_preflight_failure_retains_every_reason_without_moving(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)

    def fail(*args, **kwargs):
        raise ValueError("preflight collision")

    monkeypatch.setattr(runner.mover, "plan", fail)
    with pytest.raises(ValueError, match="preflight collision"):
        runner.run_once("bucket")
    events = decisions(runner)
    assert len(events) == 3
    reasons = [reason_from_audit_details(event.details) for event in events]
    assert {reason["disposition"] for reason in reasons} == {"move", "stay"}
    assert {reason["code"] for reason in reasons} == {"size_threshold"}
    assert all(record.tier == "hot" for record in runner.catalog.list("bucket"))
    assert sorted(runner.drivers["hot"].list_objects("bucket")) == [
        "a-move", "b-stay", "c-move",
    ]


@pytest.mark.parametrize("dry_run", [False, True])
def test_later_evaluator_failure_preserves_completed_decisions(tmp_path, dry_run):
    class FailingPolicy:
        calls = 0

        def evaluate(self, current_tier, size):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("evaluation failed")
            return PolicyDecision("move", "first decision", "warm")

    runner = make_runner(tmp_path, policy=FailingPolicy())
    with pytest.raises(RuntimeError, match="evaluation failed"):
        runner.run_once("bucket", dry_run=dry_run)
    events = decisions(runner)
    assert len(events) == (0 if dry_run else 1)
    if events:
        assert reason_from_audit_details(events[0].details)["disposition"] == "move"
    assert all(record.tier == "hot" for record in runner.catalog.list("bucket"))


def test_dry_run_does_not_persist_and_retry_freezes_original_reason(tmp_path):
    runner = make_runner(tmp_path)
    runner.plan_once("bucket", dry_run=True)
    assert decisions(runner) == []
    first = runner.plan_once("bucket")
    retained = {event.event_id: asdict(event) for event in decisions(runner)}
    runner.policy.size_threshold = 4  # Same actions, different decisive boundary.
    second = runner.plan_once("bucket")
    assert [action.decision_event_id for action in first] == [
        action.decision_event_id for action in second
    ]
    assert retained == {event.event_id: asdict(event) for event in decisions(runner)}
    for event in decisions(runner):
        assert reason_from_audit_details(event.details)["decisive_signals"][0]["threshold"] == 5


@pytest.mark.parametrize("guard", ["minimum_residency", "importance_restriction", "cooldown"])
def test_hard_guards_record_content_free_reasons_without_provider_call(tmp_path, guard):
    class NeverCalled:
        def decide(self, payload):
            pytest.fail("a hard guard must avoid the provider")

    config = {
        "minimum_residency": MovementConstraints(minimum_residency_seconds={"hot": 60}),
        "importance_restriction": MovementConstraints(),
        "cooldown": MovementConstraints(cooldown_seconds=60),
    }[guard]
    runner = make_runner(tmp_path, policy=LLMPolicy(NeverCalled()), controls=config)
    record = runner.catalog.get("bucket", "a-move")
    record = replace(record, placement_started_at=NOW.isoformat(), last_tier_move_at=NOW.isoformat())
    if guard == "importance_restriction":
        record = replace(record, importance=ImportanceTag(
            "critical", "user", "private actor", "private classification text", NOW.isoformat(),
        ))
    runner.reevaluate_record(record)
    reason = reason_from_audit_details(decisions(runner)[0].details)
    assert reason["code"] == guard
    assert reason["disposition"] == "suppressed"
    assert reason["decisive_signals"] == []
    assert "private" not in json.dumps(reason)


def test_hysteresis_keeps_baseline_and_effective_boundary(tmp_path):
    runner = make_runner(tmp_path, controls=MovementConstraints(size_hysteresis_bytes=2))
    assert runner.plan_once("bucket") == []
    event = next(event for event in decisions(runner) if event.object_key == "a-move")
    reason = reason_from_audit_details(event.details)
    assert reason["code"] == "hysteresis"
    assert reason["disposition"] == "suppressed"
    assert reason["constraints"]["candidate_destination_tier"] == "warm"
    assert reason["constraints"]["hysteresis_checks"] == [{
        "kind": "size", "configured_band": 2, "baseline_threshold": 5,
        "effective_threshold": 7, "value": 6, "rule_index": None,
    }]


@pytest.mark.parametrize("by_name", [False, True])
def test_builtin_suppression_retains_the_destination_removed_by_importance(tmp_path, by_name):
    policy = (
        ContentAwarePolicy(allowed_tiers=("hot", "warm", "cold"))
        if by_name else SimplePolicy(100, allowed_tiers=("hot", "warm", "cold"))
    )
    if by_name:
        policy.cold_name_patterns = ["a-*"]
    runner = make_runner(tmp_path, policy=policy, controls=MovementConstraints(
        importance_tiers={"high": ["hot", "warm"] if by_name else ["warm", "cold"]},
    ))
    record = replace(
        runner.catalog.get("bucket", "a-move"), tier="hot" if by_name else "warm",
        importance=ImportanceTag("high", "user", "actor", "classification", NOW.isoformat()),
    )
    runner.reevaluate_record(record)
    reason = reason_from_audit_details(decisions(runner)[0].details)
    assert reason["code"] == "importance_restriction"
    assert reason["disposition"] == "suppressed"
    assert reason["constraints"]["rejected_destination_tier"] == ("cold" if by_name else "hot")
    assert reason["decisive_signals"][0]["name"] == ("name_match" if by_name else "size_bytes")


def test_override_retains_kind_without_free_text(tmp_path):
    runner = make_runner(tmp_path, controls=MovementConstraints(
        size_hysteresis_bytes=10,
        stability_override=StabilityOverride("emergency", "Private incident content"),
    ))
    assert len(runner.plan_once("bucket")) == 2
    reason = reason_from_audit_details(decisions(runner)[0].details)
    assert reason["constraints"]["stability_override_kind"] == "emergency"
    assert reason["constraints"]["hysteresis_checks"] == []
    assert "Private incident content" not in json.dumps(reason)


@pytest.mark.parametrize("custom", [False, True])
def test_provider_prose_and_arbitrary_signals_never_enter_reason_projections(tmp_path, custom):
    private_text = "Patient private narrative with no credential-shaped tokens"

    class Provider:
        provider_id = "reason-test"
        model = "inferred-model"
        model_version = "actual-revision"
        version = "adapter-v1"

        def complete(self, prompt, schema, timeout_seconds):
            return json.dumps({"action": "stay", "dst_tier": None, "reason": private_text})

        def evaluate(self, current_tier, size):
            return PolicyDecision("stay", private_text,
                                  hysteresis={"suppressed": True, "raw": private_text},
                                  reason_code=private_text,
                                  decisive_signals=[{"raw": private_text}])

    runner = make_runner(tmp_path, policy=Provider() if custom else LLMPolicy(Provider()))
    runner.model_identity = "model-49"
    runner.model_version = "revision-2"
    assert runner.plan_once("bucket") == []
    for event in decisions(runner):
        reason = reason_from_audit_details(event.details)
        assert reason["code"] == ("custom_policy" if custom else "provider_decision")
        assert reason["decisive_signals"] == []
        assert reason["policy"]["model"] == {"identity": "model-49", "version": "revision-2"}
        assert reason["confidence"] == {"value": None, "source": "not_reported"}
        assert private_text not in json.dumps({
            name: event.details[name] for name in ("structured_reason", "reason", "dataset")
        })


def test_native_inference_model_provenance_and_privacy_survive_export(tmp_path):
    from cognistore.core.placement_llm import CallablePlacementProvider

    provider = CallablePlacementProvider(
        lambda *args: json.dumps({
            "action": "stay", "dst_tier": None, "reason": "private inference prose",
        }),
        provider_id="test", model="actual-model", model_version="actual-version",
    )
    runner = make_runner(tmp_path, policy=LLMPolicy(provider))
    runner.plan_once("bucket")
    events = decisions(runner)
    latest = max(event.details["dataset"]["decision_at"] for event in events)
    cutoff = datetime.fromisoformat(latest.replace("Z", "+00:00")) + timedelta(hours=1)
    dataset = export_policy_dataset(runner.catalog, as_of=cutoff, observation_seconds=60)
    assert validate_policy_dataset(dataset) == []
    for row in dataset["rows"]:
        reason = row["structured_reason"]
        assert reason["code"] == "provider_decision"
        assert reason["policy"]["model"] == row["snapshot"]["policy"]["model"] == {
            "identity": "actual-model", "version": "actual-version",
        }
        assert reason["confidence"]["value"] is None
        assert "private inference prose" not in json.dumps(row)
        assert "llm_audit" not in row


@pytest.mark.parametrize("destination,code", [
    ("unknown", "destination_not_allowed"), (None, "destination_missing"),
    ("hot", "already_in_tier"),
])
def test_rejected_proposals_have_structured_codes(tmp_path, destination, code):
    class RejectedPolicy:
        def evaluate(self, current_tier, size):
            return PolicyDecision("move", "proposal", destination)

    runner = make_runner(tmp_path, policy=RejectedPolicy())
    assert runner.plan_once("bucket") == []
    assert all(reason_from_audit_details(event.details)["code"] == code
               for event in decisions(runner))
    assert all(reason_from_audit_details(event.details)["disposition"] == "rejected"
               for event in decisions(runner))
