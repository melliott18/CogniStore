"""Durable cooldowns, explicit overrides, and clock/noise regression tests."""

import logging
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from cognistore.core.audit import AuditContext, AuditEventType, AuditQuery
from cognistore.core.catalog import Catalog, ObjectRecord
from cognistore.core.move_jobs import MoveJobConflictError, MoveJobState
from cognistore.core.mover import Mover
from cognistore.core.placement_controls import (
    ImportanceTag,
    MovementConstraintError,
    MovementConstraints,
    StabilityOverride,
    assert_move_allowed,
    evaluate_movement_constraints,
)
from cognistore.core.policy import PolicyDecision, SimplePolicy
from cognistore.core.policy_factory import build_policy
from cognistore.core.policy_runner import PolicyRunner
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver

START = datetime(2026, 9, 10, tzinfo=timezone.utc)


def moved_record(**changes):
    return replace(ObjectRecord(
        "bucket", "key", 101, "hot", placement_started_at=START.isoformat(),
        last_tier_move_at=START.isoformat(),
    ), **changes)


@pytest.mark.parametrize("field,value", [
    ("cooldown_seconds", True), ("cooldown_seconds", -1),
    ("cooldown_seconds", 315360001), ("cooldown_seconds", 1.5),
    ("size_hysteresis_bytes", -1), ("size_hysteresis_bytes", 2**63),
    ("size_hysteresis_bytes", True), ("size_hysteresis_bytes", "1"),
    ("similarity_hysteresis", True), ("similarity_hysteresis", float("nan")),
    ("similarity_hysteresis", float("inf")), ("similarity_hysteresis", -0.1),
    ("similarity_hysteresis", 2.1), ("similarity_hysteresis", "0.1"),
    ("stability_override", {}), ("stability_override", {"kind": "emergency"}),
    ("stability_override", {"kind": "automatic", "reason": "test"}),
    ("stability_override", {"kind": "emergency", "reason": " "}),
    ("stability_override", {"kind": "emergency", "reason": "x\ny"}),
])
def test_invalid_stability_controls_fail_closed(field, value):
    with pytest.raises(ValueError):
        MovementConstraints.from_mapping({field: value})


@pytest.mark.parametrize("offset,blocked", [
    (timedelta(days=-1), True), (timedelta(0), True),
    (timedelta(seconds=60, microseconds=-1), True),
    (timedelta(seconds=60), False), (timedelta(seconds=60, microseconds=1), False),
])
def test_cooldown_exact_expiry_and_backwards_clock(offset, blocked):
    controls = MovementConstraints(cooldown_seconds=60)
    evidence = evaluate_movement_constraints(moved_record(), controls, as_of=START + offset)
    assert evidence["cooldown_active"] is blocked
    assert evidence["cooldown_expires_at"] == "2026-09-10T00:01:00.000000Z"
    if blocked:
        with pytest.raises(MovementConstraintError, match="cooldown"):
            assert_move_allowed(moved_record(), "warm", controls, as_of=START + offset)
    else:
        assert_move_allowed(moved_record(), "warm", controls, as_of=START + offset)


def test_cooldown_timezone_overflow_and_first_placement():
    controls = MovementConstraints(cooldown_seconds=60)
    offset_record = moved_record(last_tier_move_at="2026-09-09T17:00:00-07:00")
    assert not evaluate_movement_constraints(
        offset_record, controls, as_of=START + timedelta(seconds=60),
    )["cooldown_active"]
    assert not evaluate_movement_constraints(
        moved_record(last_tier_move_at=None), controls, as_of=START,
    )["cooldown_active"]
    overflow = moved_record(last_tier_move_at="9999-12-31T23:59:59Z")
    with pytest.raises(MovementConstraintError, match="timestamp range"):
        assert_move_allowed(overflow, "warm", controls, as_of=START)


@pytest.mark.parametrize("kind", ["emergency", "compliance"])
def test_override_is_explicit_and_cannot_disable_hard_controls(kind):
    override = StabilityOverride(kind, "Incident 45 approved recovery")
    controls = MovementConstraints(cooldown_seconds=60, stability_override=override)
    assert MovementConstraints.from_mapping(controls.to_dict()) == controls
    assert_move_allowed(moved_record(), "warm", controls, as_of=START)
    with pytest.raises(MovementConstraintError, match="minimum residency"):
        assert_move_allowed(moved_record(), "warm", controls,
                            tier_metadata={"minimum_residency_seconds": 60}, as_of=START)
    important = moved_record(importance=ImportanceTag(
        "critical", "user", "operator", "protected data", START.isoformat(),
    ))
    with pytest.raises(MovementConstraintError, match="importance"):
        assert_move_allowed(important, "warm", controls, as_of=START)


def runner_for(catalog, drivers, *, clock=START, controls=None, threshold=100, **kwargs):
    mover = Mover(drivers, catalog, clock=lambda: clock)
    return PolicyRunner(catalog, drivers, mover, SimplePolicy(threshold),
                        movement_constraints=controls, clock=lambda: clock, **kwargs)


def test_suppression_is_explained_audited_and_logged_without_preview_writes(tmp_path, caplog):
    catalog = Catalog()
    catalog.upsert("bucket", "key", 101, "hot")
    controls = MovementConstraints(size_hysteresis_bytes=10)
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    runner = runner_for(catalog, drivers, controls=controls)
    preview = runner.evaluate_once("bucket", "key")
    assert preview.action == "stay"
    assert preview.constraints["suppression_reason"] == "hysteresis"
    assert preview.constraints["hysteresis"]["checks"][0]["effective_threshold"] == 110
    assert catalog.list_audit_events() == []
    with caplog.at_level(logging.INFO, logger="cognistore.core.policy_runner"):
        assert runner.plan_once("bucket") == []
    event = catalog.list_audit_events()[0]
    assert event.details["constraints"]["suppression_reason"] == "hysteresis"
    assert event.details["dataset"]["replay"]["supported"] is False
    assert any(getattr(record, "suppression_reason", None) == "hysteresis"
               for record in caplog.records)


def test_completed_move_cooldown_survives_catalog_and_worker_restart(tmp_path):
    database = tmp_path / "catalog.sqlite"
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    drivers["hot"].put_object("bucket", "key", b"payload")
    controls = MovementConstraints(cooldown_seconds=60)
    with SQLiteCatalog(database) as catalog:
        catalog.upsert("bucket", "key", 7, "hot")
        assert len(runner_for(catalog, drivers, controls=controls, threshold=1).run_once("bucket")) == 1
        assert catalog.get("bucket", "key").last_tier_move_at == "2026-09-10T00:00:00.000000Z"
    with SQLiteCatalog(database) as restarted:
        # A routine metadata/size refresh must not change the trusted move clock.
        restarted.upsert("bucket", "key", 7, "warm", {"last_tier_move_at": "1970-01-01T00:00:00Z"})
        during = runner_for(restarted, drivers, controls=controls, clock=START + timedelta(seconds=59))
        assert during.run_once("bucket") == []
        assert restarted.get("bucket", "key").tier == "warm"
        decision = restarted.list_audit_events(AuditQuery(event_types={AuditEventType.POLICY_DECISION}))[-1]
        assert decision.details["constraints"]["suppression_reason"] == "cooldown"
        after = runner_for(restarted, drivers, controls=controls, clock=START + timedelta(seconds=60))
        assert len(after.run_once("bucket")) == 1
        assert restarted.get("bucket", "key").tier == "hot"
        assert drivers["hot"].get_object("bucket", "key") == b"payload"


def test_stale_plan_rechecks_cooldown_after_round_trip(tmp_path):
    catalog = Catalog()
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    drivers["hot"].put_object("bucket", "key", b"payload")
    catalog.upsert("bucket", "key", 7, "hot")
    controls = MovementConstraints(cooldown_seconds=60)
    runner = runner_for(catalog, drivers, controls=controls, threshold=1)
    action = runner.plan_once("bucket", dry_run=True)[0]
    # ABA tier changes preserve the same bytes but must invalidate eligibility.
    catalog.update_placement("bucket", "key", "warm")
    catalog.update_placement("bucket", "key", "hot")
    with pytest.raises(MovementConstraintError, match="cooldown"):
        runner.execute(action)
    assert drivers["hot"].get_object("bucket", "key") == b"payload"


@pytest.mark.parametrize("checkpoint", [MoveJobState.PREPARED, MoveJobState.COMMITTED])
def test_override_is_audited_frozen_and_recovery_finishes_under_cooldown(tmp_path, checkpoint):
    database = tmp_path / "recovery.sqlite"
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    drivers["hot"].put_object("bucket", "key", b"payload")
    controls = MovementConstraints(cooldown_seconds=3600,
                                  stability_override=StabilityOverride("emergency", "Incident 45"))
    context = AuditContext("override-45", "user", "operator-45")

    def crash(job):
        if job.state is checkpoint:
            raise RuntimeError("simulated crash")

    with SQLiteCatalog(database) as catalog:
        catalog.upsert("bucket", "key", 7, "warm")
        catalog.update_placement("bucket", "key", "hot")
        mover = Mover(drivers, catalog, clock=lambda: START, transition_hook=crash)
        with pytest.raises(RuntimeError, match="simulated crash"):
            mover.move("hot", "warm", "bucket", "key", idempotency_key="override-move",  # gitleaks:allow (synthetic retry identity)
                       movement_constraints=controls, audit_context=context)
    with SQLiteCatalog(database) as catalog:
        mover = Mover(drivers, catalog, clock=lambda: START + timedelta(seconds=31))
        with pytest.raises(MoveJobConflictError, match="Movement constraints differ"):
            mover.move("hot", "warm", "bucket", "key", idempotency_key="override-move",  # gitleaks:allow (synthetic retry identity)
                       movement_constraints=MovementConstraints(cooldown_seconds=3600))
        mover.move("hot", "warm", "bucket", "key", idempotency_key="override-move")  # gitleaks:allow (synthetic retry identity)
        assert catalog.get("bucket", "key").tier == "warm"
        event = next(e for e in catalog.list_audit_events()
                     if e.event_type == AuditEventType.MOVE_PREPARED)
        assert event.actor_id == "operator-45"
        assert event.details["stability_override"] == {"kind": "emergency", "reason": "Incident 45"}


def test_seeded_noisy_evaluations_bound_moves_and_preserve_band(tmp_path):
    catalog = Catalog()
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    controls = MovementConstraints(cooldown_seconds=60, size_hysteresis_bytes=10)
    runner = runner_for(catalog, drivers, controls=controls)
    rng = random.Random(45)
    record = moved_record(last_tier_move_at=None)
    previous_move = None
    moves = 0
    for second in range(2000):
        # Mostly boundary noise with occasional decisive signal changes.
        size = rng.randint(95, 105) if second % 20 else rng.choice([75, 125])
        record = replace(record, size=size)
        now = START + timedelta(seconds=second)
        evaluation = runner.evaluate_record(record, as_of=now)
        if evaluation.action == "move":
            assert previous_move is None or second - previous_move >= 60
            assert size <= 90 or size > 110
            previous_move = second
            moves += 1
            record = replace(record, tier=evaluation.destination_tier, last_tier_move_at=now.isoformat())
    assert 5 < moves <= 34


def test_arbitrary_policy_cannot_request_override_in_its_output(tmp_path):
    class UnsafePolicy:
        def evaluate(self, current_tier, size):
            pytest.fail("active cooldown must settle decision before calling provider")
            return PolicyDecision("move", "emergency bypass", "warm")

    catalog = Catalog()
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    runner = PolicyRunner(catalog, drivers, Mover(drivers, catalog), UnsafePolicy(),
                          movement_constraints=MovementConstraints(cooldown_seconds=60))
    evaluation = runner.evaluate_record(moved_record(), as_of=START)
    assert evaluation.action == "stay"
    assert evaluation.constraints["stability_override"] is None


def test_unconfigured_llm_stays_without_hysteresis_claims(tmp_path):
    catalog = Catalog()
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    runner = PolicyRunner(
        catalog, drivers, Mover(drivers, catalog),
        build_policy("llm", threshold=100, allowed_tiers=["hot", "warm"]),
        allowed_tiers=["hot"], movement_constraints=MovementConstraints(size_hysteresis_bytes=10),
    )
    evaluation = runner.evaluate_record(moved_record(), as_of=START)
    assert evaluation.action == "stay"
    assert evaluation.constraints["suppression_reason"] is None
    assert "hysteresis" not in evaluation.constraints
    assert evaluation.reason == "llm_provider_unavailable"


def test_future_preview_uses_one_clock_but_execution_checks_present(tmp_path):
    catalog = Catalog()
    catalog.upsert("bucket", "key", 7, "warm")
    catalog.update_placement("bucket", "key", "hot")
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    drivers["hot"].put_object("bucket", "key", b"payload")
    now = datetime.now(timezone.utc)
    future = now + timedelta(seconds=3601)
    runner = runner_for(catalog, drivers, controls=MovementConstraints(cooldown_seconds=3600),
                        threshold=1, clock=now)
    assert runner.preview_once("bucket", as_of=future)[0].action == "move"
    planned = runner.plan_once("bucket", as_of=future, dry_run=True)
    assert len(planned) == 1
    assert catalog.list_audit_events() == []
    with pytest.raises(MovementConstraintError, match="cooldown"):
        runner.execute(planned[0])


def test_policy_override_reason_is_audited_and_changes_retry_identity(tmp_path):
    catalog = Catalog()
    catalog.upsert("bucket", "key", 101, "hot")
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    drivers["hot"].put_object("bucket", "key", b"x" * 101)
    context = AuditContext("explicit-exception", "user", "operator-45")
    for kind in ("emergency", "compliance"):
        controls = MovementConstraints(size_hysteresis_bytes=10,
                                      stability_override=StabilityOverride(kind, f"Request {kind}"))
        runner = runner_for(catalog, drivers, controls=controls, audit_context=context)
        assert runner.plan_once("bucket")[0].to_tier == "warm"
    events = catalog.list_audit_events()
    assert len(events) == 2
    assert {event.details["stability_override"]["kind"] for event in events} == {"emergency", "compliance"}
    assert all(event.actor_id == "operator-45" for event in events)
    assert all(not event.details["dataset"]["replay"]["supported"] for event in events)
