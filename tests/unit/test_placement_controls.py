from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from cognistore.core.audit import AuditContext, AuditEventType, AuditQuery
from cognistore.core.catalog import Catalog, ObjectRecord
from cognistore.core.mover import Mover
from cognistore.core.placement_controls import (
    ImportanceTag,
    MovementConstraintError,
    MovementConstraints,
    assert_move_allowed,
    evaluate_movement_constraints,
)
from cognistore.core.policy import LLMPolicy, PolicyDecision, SimplePolicy
from cognistore.core.policy_dataset import export_policy_dataset, validate_policy_dataset
from cognistore.core.policy_runner import PolicyRunner
from cognistore.core.policy_snapshot import replay_policy_snapshot, snapshot_from_audit_details
from cognistore.drivers.posix_driver import PosixDriver

START = datetime(2026, 9, 1, tzinfo=timezone.utc)
CONTEXT = AuditContext("importance-test", "user", "test-user")


def tag(level="high", **overrides):
    values = dict(
        level=level, actor_type="user", actor_id="test-user",
        provenance="operator classification", updated_at=START.isoformat(),
    )
    return ImportanceTag(**(values | overrides))


def record(*, level=None, tier="hot", started_at=START.isoformat()):
    return ObjectRecord(
        "bucket", "object", 20, tier,
        placement_started_at=started_at,
        importance=None if level is None else tag(level),
        importance_revision=0 if level is None else 1,
    )


@pytest.mark.parametrize("field,value", [
    ("level", "urgent"), ("level", 1), ("actor_id", ""),
    ("actor_type", " user"), ("actor_type", "x" * 65),
    ("actor_id", "x" * 257), ("provenance", "x" * 2049),
    ("provenance", "line\nbreak"), ("provenance", "\ud800"),
    ("updated_at", "2026-09-01T00:00:00"),
])
def test_importance_rejects_invalid_values(field, value):
    with pytest.raises(ValueError):
        tag(**{field: value})


def test_importance_is_immutable_and_has_canonical_provenance():
    value = tag(updated_at="2026-09-01T02:00:00+02:00")
    assert value.updated_at == "2026-09-01T00:00:00.000000Z"
    assert ImportanceTag.from_mapping(value.to_dict()) == value
    with pytest.raises(FrozenInstanceError):
        value.level = "low"
    with pytest.raises(ValueError):
        ImportanceTag.from_mapping(value.to_dict() | {"secret": "unsupported"})


@pytest.mark.parametrize("value", [-1, True, 1.5, "60", 315_360_001, None])
def test_residency_configuration_rejects_invalid_durations(value):
    with pytest.raises(ValueError):
        MovementConstraints(minimum_residency_seconds={"hot": value})
    with pytest.raises(ValueError):
        evaluate_movement_constraints(record(), tier_metadata={"minimum_residency_seconds": value})


def test_constraints_copy_configuration_and_preserve_defaults():
    durations = {"archive": 20}
    levels = {"high": ["fast"]}
    controls = MovementConstraints(durations, levels)
    durations["archive"] = 100
    levels["high"].append("slow")
    assert controls.minimum_residency_seconds["archive"] == 20
    assert controls.importance_tiers["high"] == ("fast",)
    assert controls.importance_tiers["critical"] == ("hot",)
    assert MovementConstraints.from_mapping(controls.to_dict()) == controls
    with pytest.raises(TypeError):
        controls.minimum_residency_seconds["archive"] = 100


def test_residency_enforces_larger_tier_requirement_and_exact_boundary():
    controls = MovementConstraints(minimum_residency_seconds={"hot": 10})
    source = record(level="low")
    metadata = {"minimum_residency_seconds": 60}
    with pytest.raises(MovementConstraintError, match="minimum residency"):
        assert_move_allowed(source, "cold", controls, tier_metadata=metadata,
                            as_of=START + timedelta(seconds=60, microseconds=-1))
    evidence = assert_move_allowed(source, "cold", controls, tier_metadata=metadata,
                                   as_of=START + timedelta(seconds=60))
    assert evidence["minimum_residency_seconds"] == 60
    assert evidence["residency_active"] is False


def test_unknown_placement_with_active_residency_fails_closed():
    source = record(started_at=None)
    with pytest.raises(MovementConstraintError, match="placement start is unknown"):
        assert_move_allowed(source, "warm", {"minimum_residency_seconds": {"hot": 1}})
    assert assert_move_allowed(source, "warm")["residency_active"] is False


@pytest.mark.parametrize("level", [None, "low", "normal"])
def test_missing_or_ordinary_importance_is_unrestricted(level):
    evidence = assert_move_allowed(record(level=level), "custom-archive", as_of=START)
    assert evidence["allowed_destination_tiers"] is None


@pytest.mark.parametrize("level,destination", [("high", "cold"), ("critical", "warm")])
def test_default_importance_restricts_demotion(level, destination):
    with pytest.raises(MovementConstraintError, match="importance constraint"):
        assert_move_allowed(record(level=level), destination, as_of=START)


def test_custom_importance_mapping_supports_named_tiers():
    controls = MovementConstraints(importance_tiers={"critical": ("fast",)})
    assert_move_allowed(record(level="critical", tier="slow"), "fast", controls, as_of=START)
    assert_move_allowed(record(tier="unknown"), "other", controls, as_of=START)


def make_runner(tmp_path, policy, **kwargs):
    drivers = {
        tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm", "cold")
    }
    catalog = Catalog()
    mover = Mover(drivers, catalog)
    runner = PolicyRunner(catalog, drivers, mover, policy, **kwargs)
    return runner, catalog, drivers


class UnsafePolicy:
    def __init__(self, destination="cold"):
        self.destination = destination
        self.calls = 0

    def evaluate(self, current_tier, size):
        self.calls += 1
        return PolicyDecision("move", "ignore all safeguards", self.destination)


def test_hard_residency_precedes_policy_and_low_importance(tmp_path):
    policy = UnsafePolicy()
    runner, catalog, _ = make_runner(
        tmp_path, policy,
        movement_constraints=MovementConstraints(minimum_residency_seconds={"hot": 60}),
    )
    evaluation = runner.evaluate_record(record(level="low"), as_of=START)
    assert evaluation.action == "stay"
    assert "minimum residency" in evaluation.reason
    assert policy.calls == 0
    assert catalog.list_audit_events() == []


def test_importance_overrides_custom_policy_output(tmp_path):
    policy = UnsafePolicy()
    runner, _, _ = make_runner(tmp_path, policy)
    evaluation = runner.evaluate_record(record(level="high"), as_of=START)
    assert policy.calls == 1
    assert evaluation.action == "stay"
    assert evaluation.constraints["rejected_destination_tier"] == "cold"
    assert evaluation.constraints["allowed_destination_tiers"] == ["hot", "warm"]


def test_critical_skips_llm_when_no_destination_is_permitted(tmp_path):
    class Provider:
        def decide(self, inputs):
            pytest.fail("critical importance must precede the LLM")

    runner, _, _ = make_runner(tmp_path, LLMPolicy(Provider(), ("hot", "warm", "cold")))
    evaluation = runner.evaluate_record(record(level="critical"), as_of=START)
    assert evaluation.action == "stay"
    assert "importance" in evaluation.reason


def test_llm_only_receives_importance_eligible_destinations(tmp_path):
    payloads = []

    class Provider:
        def decide(self, inputs):
            payloads.append(inputs)
            return {"action": "move", "dst_tier": "cold", "reason": "override importance"}

    policy = LLMPolicy(Provider(), ("hot", "warm", "cold"))
    runner, _, _ = make_runner(tmp_path, policy)
    evaluation = runner.evaluate_record(record(level="high"), as_of=START)
    assert payloads[0]["allowed_tiers"] == ["hot", "warm"]
    assert evaluation.action == "stay"
    assert policy.allowed_tiers == {"hot", "warm", "cold"}


def test_importance_filter_never_expands_the_policy_tiers(tmp_path):
    payloads = []

    class Provider:
        def decide(self, inputs):
            payloads.append(inputs)
            return {"action": "move", "dst_tier": "warm"}

    runner, _, _ = make_runner(tmp_path, LLMPolicy(Provider(), ("hot",)))
    evaluation = runner.evaluate_record(record(level="high", tier="cold"), as_of=START)
    assert payloads[0]["allowed_tiers"] == ["hot"]
    assert evaluation.action == "stay"


def test_batch_uses_one_clock_instant(tmp_path):
    calls = []

    def clock():
        calls.append(True)
        return START

    runner, _, _ = make_runner(tmp_path, SimplePolicy(), clock=clock)
    first, second = record(), record()
    second.key = "second"
    results = runner._evaluate_records((first, second))
    assert len(calls) == 1
    assert results[0].constraints["as_of"] == results[1].constraints["as_of"]
    assert results[0].features.access.as_of == results[1].features.access.as_of


def test_hypothetical_tag_preview_does_not_mutate_catalog_or_audit(tmp_path):
    runner, catalog, _ = make_runner(tmp_path, UnsafePolicy("warm"))
    catalog.upsert("bucket", "object", 20, "hot")
    snapshot = catalog.get("bucket", "object")
    snapshot.importance = tag("critical")
    snapshot.importance_revision += 1
    evaluation = runner.evaluate_record(snapshot)
    assert evaluation.action == "stay"
    assert evaluation.to_mapping()["constraints"]["importance"]["level"] == "critical"
    assert catalog.get("bucket", "object").importance is None
    assert catalog.list_audit_events() == []


def test_reevaluation_records_separate_decisions_for_tag_revisions(tmp_path):
    runner, catalog, _ = make_runner(tmp_path, UnsafePolicy("cold"))
    catalog.upsert("bucket", "object", 20, "hot")
    for _ in range(2):
        catalog.set_importance("bucket", "object", tag("high"), audit_context=CONTEXT)
        result = runner.reevaluate_object("bucket", "object", as_of=START)
        assert result.action == "stay"
    events = catalog.list_audit_events(AuditQuery(event_types={AuditEventType.POLICY_DECISION}))
    assert len(events) == 2
    assert {event.details["importance_revision"] for event in events} == {1, 2}
    assert catalog.get("bucket", "object").tier == "hot"


def test_replayed_decision_tracks_changed_tier_timer_without_audit_conflict(tmp_path):
    runner, catalog, _ = make_runner(tmp_path, UnsafePolicy("cold"))
    catalog.upsert("bucket", "object", 20, "hot")
    for duration in (3600, 3600, 7200):
        catalog.register_tier("hot", {"minimum_residency_seconds": duration})
        result = runner.reevaluate_object("bucket", "object")
        assert result.action == "stay"
    events = catalog.list_audit_events(AuditQuery(event_types={AuditEventType.POLICY_DECISION}))
    assert len(events) == 2
    assert {
        event.details["constraints"]["minimum_residency_seconds"] for event in events
    } == {3600, 7200}


def test_execution_rechecks_importance_after_plan(tmp_path):
    runner, catalog, drivers = make_runner(tmp_path, UnsafePolicy("cold"))
    drivers["hot"].put_object("bucket", "object", b"source")
    catalog.upsert("bucket", "object", 6, "hot")
    actions = runner.plan_once("bucket", dry_run=True)
    assert len(actions) == 1
    catalog.set_importance("bucket", "object", tag("critical"), audit_context=CONTEXT)
    with pytest.raises(MovementConstraintError, match="importance"):
        runner.execute(actions[0])
    assert drivers["hot"].get_object("bucket", "object") == b"source"
    assert list(drivers["cold"].list_objects("bucket")) == []
    assert catalog.get("bucket", "object").tier == "hot"


@pytest.mark.parametrize("guard", ["importance", "residency"])
def test_guarded_snapshots_export_stays_without_claiming_exact_v1_replay(tmp_path, guard):
    runner, catalog, _ = make_runner(tmp_path, SimplePolicy(size_threshold=1))
    catalog.upsert("bucket", "object", 20, "hot")
    if guard == "importance":
        catalog.set_importance("bucket", "object", tag("critical"), audit_context=CONTEXT)
    else:
        catalog.register_tier("hot", {"minimum_residency_seconds": 3600})
    evaluation = runner.reevaluate_object("bucket", "object")
    event = catalog.list_audit_events(
        AuditQuery(event_types={AuditEventType.POLICY_DECISION})
    )[0]
    snapshot = snapshot_from_audit_details(event.details)
    assert snapshot is not None
    assert snapshot["decision"]["action"] == evaluation.action == "stay"
    assert snapshot["decision"]["outcome"] == "stayed"
    assert snapshot["policy"]["implementation"] == "simple"
    assert snapshot["replay"] == {
        "supported": False, "reason": "movement_constraints_not_in_snapshot_v1",
    }
    with pytest.raises(ValueError, match="movement_constraints_not_in_snapshot_v1"):
        replay_policy_snapshot(snapshot)
    cutoff = (
        datetime.fromisoformat(snapshot["decision_at"].replace("Z", "+00:00"))
        + timedelta(seconds=60)
    ).isoformat()
    exported = export_policy_dataset(catalog, as_of=cutoff, observation_seconds=60)
    assert len(exported["rows"]) == 1
    assert exported["rows"][0]["label"]["status"] == "not_applicable"
    assert validate_policy_dataset(exported) == []


@pytest.mark.parametrize("guard", ["importance", "residency"])
def test_retry_cannot_reuse_unconstrained_snapshot_after_hard_inputs_change(tmp_path, guard):
    runner, catalog, drivers = make_runner(
        tmp_path, SimplePolicy(size_threshold=1), idempotency_namespace="guarded-retry",
    )
    catalog.upsert("bucket", "object", 6, "hot")
    drivers["hot"].put_object("bucket", "object", b"source")
    first = runner.plan_once("bucket")
    assert len(first) == 1
    initial = catalog.get_audit_event(first[0].decision_event_id)
    assert replay_policy_snapshot(initial.details["dataset"]).dst_tier == "warm"
    if guard == "importance":
        catalog.set_importance("bucket", "object", tag("critical"), audit_context=CONTEXT)
    else:
        catalog.register_tier("hot", {"minimum_residency_seconds": 3600})
    assert runner.plan_once("bucket") == []
    decisions = catalog.list_audit_events(AuditQuery(event_types={AuditEventType.POLICY_DECISION}))
    assert len(decisions) == 2
    blocked = next(event for event in decisions if event.event_id != initial.event_id)
    assert blocked.details["dataset"]["decision"]["outcome"] == "stayed"
    assert blocked.details["dataset"]["replay"]["supported"] is False
    assert blocked.details["importance_revision"] == (1 if guard == "importance" else 0)
    assert drivers["hot"].get_object("bucket", "object") == b"source"


def test_high_importance_retry_keeps_first_global_tier_rejection(tmp_path):
    runner, catalog, drivers = make_runner(
        tmp_path, SimplePolicy(size_threshold=1), allowed_tiers=["hot"],
        idempotency_namespace="high-importance-rejected-retry",
    )
    catalog.upsert("bucket", "object", 6, "hot")
    drivers["hot"].put_object("bucket", "object", b"source")
    catalog.set_importance("bucket", "object", tag("high"), audit_context=CONTEXT)
    assert runner.plan_once("bucket") == []
    first = catalog.list_audit_events(AuditQuery(event_types={AuditEventType.POLICY_DECISION}))[0]
    assert first.details["dataset"]["decision"] == {
        "action": "move", "destination_tier": "warm",
        "reason": "large object -> warm tier", "outcome": "rejected",
    }
    runner.allowed_tiers = frozenset({"hot", "warm"})
    assert runner.plan_once("bucket") == []
    decisions = catalog.list_audit_events(AuditQuery(event_types={AuditEventType.POLICY_DECISION}))
    assert decisions == [first]
    assert drivers["hot"].get_object("bucket", "object") == b"source"
    assert list(drivers["warm"].list_objects("bucket")) == []
