"""Legal holds fence actual object bytes, including durable move recovery."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from cognistore.core.audit import AuditContext, AuditEventType, AuditOutcome, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.core.legal_holds import LegalHoldError
from cognistore.core.move_jobs import MoveJobFailedError, MoveJobState
from cognistore.core.mover import Mover
from cognistore.core.placement_controls import MovementConstraints, StabilityOverride
from cognistore.core.policy import SimplePolicy
from cognistore.core.policy_dataset import export_policy_dataset, validate_policy_dataset
from cognistore.core.policy_reasons import reason_from_audit_details
from cognistore.core.policy_runner import PolicyRunner
from cognistore.core.policy_snapshot import replay_policy_snapshot, validate_policy_snapshot
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.jobs.handlers import POLICY_RUN_JOB, build_handlers, policy_job_payload
from cognistore.jobs.models import JobContext, JobEnvelope
from cognistore.jobs.retry import FailureCategory, classify_job_error

BUCKET = "records"
KEY = "case-62/evidence.txt"
DATA = b"retained evidence must survive every movement checkpoint"
ACTOR = AuditContext("hold-62", "user", "records-officer")
CHECKPOINTS = [
    MoveJobState.PREPARED, MoveJobState.TRANSFERRED, MoveJobState.VERIFIED,
    MoveJobState.COMMITTED, MoveJobState.CLEANUP,
]


class InjectedCrash(BaseException):
    pass


@pytest.fixture(params=["memory", "sqlite"])
def storage(tmp_path, request):
    catalog = Catalog() if request.param == "memory" else SQLiteCatalog(tmp_path / "catalog.db")
    drivers = {tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm")}
    drivers["hot"].put_object(BUCKET, KEY, DATA)
    catalog.upsert(BUCKET, KEY, len(DATA), "hot")
    yield catalog, drivers
    if isinstance(catalog, SQLiteCatalog):
        catalog.close()


def hold(catalog, **scope):
    return catalog.place_legal_hold(
        BUCKET, reason="Preserve case evidence", context=ACTOR, **scope,
    )


def assert_copies_preserved(drivers, *, copied):
    assert drivers["hot"].get_object(BUCKET, KEY) == DATA
    if copied:
        assert drivers["warm"].get_object(BUCKET, KEY) == DATA
    else:
        with pytest.raises(FileNotFoundError):
            drivers["warm"].stat_object(BUCKET, KEY)


@pytest.mark.parametrize("scope", [{"key": KEY}, {"prefix": "case-62/"}, {}])
def test_direct_move_and_plan_fail_closed_for_every_hold_scope(storage, scope):
    catalog, drivers = storage
    hold(catalog, **scope)
    mover = Mover(drivers, catalog, audit_context=ACTOR)
    for operation in (mover.plan, mover.move):
        with pytest.raises(LegalHoldError):
            operation("hot", "warm", BUCKET, KEY)
        assert_copies_preserved(drivers, copied=False)
    assert catalog.get(BUCKET, KEY).tier == "hot"
    assert catalog.list_move_jobs() == []
    denied = catalog.list_audit_events(AuditQuery(
        event_types=frozenset({AuditEventType.LEGAL_HOLD_DENIED}),
    ))
    assert len(denied) == 2
    assert all(event.actor_id == ACTOR.actor_id for event in denied)


@pytest.mark.parametrize("checkpoint", CHECKPOINTS)
@pytest.mark.parametrize("recover", [False, True])
def test_hold_stops_every_live_or_recovered_movement_phase(storage, checkpoint, recover):
    catalog, drivers = storage
    now = datetime.now(timezone.utc)

    def after_transition(job):
        if job.state == checkpoint:
            if recover:
                raise InjectedCrash()
            hold(catalog, key=KEY)

    mover = Mover(
        drivers, catalog, owner_id="first", lease_seconds=1,
        clock=lambda: now, transition_hook=after_transition,
    )
    if recover:
        with pytest.raises(InjectedCrash):
            mover.move("hot", "warm", BUCKET, KEY, idempotency_key="held-move")
        hold(catalog, key=KEY)
        mover = Mover(
            drivers, catalog, owner_id="recovery", lease_seconds=1,
            clock=lambda: now + timedelta(seconds=2),
        )
    with pytest.raises(LegalHoldError):
        mover.move("hot", "warm", BUCKET, KEY, idempotency_key="held-move")

    assert_copies_preserved(drivers, copied=checkpoint != MoveJobState.PREPARED)
    failed = catalog.get_move_job("held-move")
    assert failed.state == MoveJobState.FAILED
    assert "legal hold" in failed.terminal_reason
    with pytest.raises(MoveJobFailedError):
        mover.move("hot", "warm", BUCKET, KEY, idempotency_key="held-move")
    assert_copies_preserved(drivers, copied=checkpoint != MoveJobState.PREPARED)


def test_cleanup_rechecks_hold_after_source_reader_is_prepared(storage, monkeypatch):
    catalog, drivers = storage
    original_reader = drivers["hot"].open_object_reader_if_generation
    source_reads = 0

    def reader_with_racing_hold(*args, **kwargs):
        nonlocal source_reads
        source_reads += 1
        if source_reads == 2:
            # First read transfers; second opens the source for deletion.
            # This runs after the mover's ordinary phase preflight check.
            hold(catalog, key=KEY)
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(drivers["hot"], "open_object_reader_if_generation", reader_with_racing_hold)
    with pytest.raises(LegalHoldError):
        Mover(drivers, catalog).move("hot", "warm", BUCKET, KEY, idempotency_key="cleanup-race")
    assert_copies_preserved(drivers, copied=True)
    assert catalog.get_move_job("cleanup-race").state == MoveJobState.FAILED


def test_policy_rejects_live_hold_before_provider_and_cannot_override_it(storage):
    catalog, drivers = storage
    placed = hold(catalog, prefix="case-62/")

    class NeverCalled:
        def evaluate(self, current_tier, size):
            pytest.fail("legal hold must precede provider evaluation")

    controls = MovementConstraints(stability_override=StabilityOverride(
        "compliance", "Prioritize another compliance operation",
    ))
    runner = PolicyRunner(
        catalog, drivers, Mover(drivers, catalog), NeverCalled(),
        movement_constraints=controls, idempotency_namespace="held-policy",
    )
    preview = runner.preview_once(BUCKET, as_of="2000-01-01T00:00:00Z")
    assert preview[0].action == "stay"
    assert preview[0].reason_code == "legal_hold"
    assert preview[0].constraints["legal_hold"] == {"active": True, "hold_ids": [placed.hold_id]}
    assert runner.plan_once(BUCKET) == []
    events = catalog.list_audit_events(AuditQuery(
        event_types=frozenset({AuditEventType.POLICY_DECISION}),
    ))
    assert len(events) == 1
    assert events[0].outcome == AuditOutcome.REJECTED
    assert "legal hold" in events[0].details["reason"]
    reason = reason_from_audit_details(events[0].details)
    assert reason["code"] == "legal_hold"
    assert reason["disposition"] == "rejected"
    assert reason["constraints"]["legal_hold_ids"] == [placed.hold_id]
    assert events[0].details["dataset"]["replay"] == {"supported": False, "reason": "legal_hold"}
    with pytest.raises(ValueError, match="not replayable"):
        replay_policy_snapshot(events[0].details["dataset"])
    corrupted = deepcopy(events[0].details["dataset"])
    corrupted["decision"].update(action="move", destination_tier="warm", outcome="selected")
    with pytest.raises(ValueError, match="rejected stay"):
        validate_policy_snapshot(corrupted)
    dataset = export_policy_dataset(
        catalog, as_of=datetime.now(timezone.utc) + timedelta(hours=1), observation_seconds=60,
    )
    assert validate_policy_dataset(dataset, require_labels=False) == []
    assert_copies_preserved(drivers, copied=False)


def test_held_policy_retry_cannot_resurrect_selected_snapshot_or_stale_action(storage):
    catalog, drivers = storage
    runner = PolicyRunner(
        catalog, drivers, Mover(drivers, catalog), SimplePolicy(1),
        idempotency_namespace="policy-before-hold",
    )
    planned = runner.plan_once(BUCKET)
    assert len(planned) == 1
    hold(catalog, key=KEY)
    assert runner.plan_once(BUCKET) == []
    with pytest.raises(LegalHoldError):
        runner.execute(planned[0])
    events = catalog.list_audit_events(AuditQuery(
        event_types=frozenset({AuditEventType.POLICY_DECISION}),
    ))
    assert {event.outcome for event in events} == {AuditOutcome.SELECTED, AuditOutcome.REJECTED}
    assert_copies_preserved(drivers, copied=False)


def test_policy_worker_terminalizes_held_committed_recovery(storage):
    catalog, drivers = storage
    payload = policy_job_payload(
        bucket=BUCKET, prefix="", policy="simple", threshold=1, llm_threshold=None,
        allowed_tiers=("hot", "warm"), hot_name_patterns=(), warm_name_patterns=(),
        cold_name_patterns=(), hot_mime_prefixes=(), warm_mime_prefixes=(), cold_mime_prefixes=(),
    )
    envelope = JobEnvelope.create(POLICY_RUN_JOB, payload)
    move_id = f"{envelope.job_id}:retained-copy"

    def crash(job):
        if job.state == MoveJobState.COMMITTED:
            raise InjectedCrash()

    mover = Mover(
        drivers, catalog, lease_seconds=1,
        clock=lambda: datetime.now(timezone.utc) - timedelta(seconds=10),
        transition_hook=crash,
    )
    with pytest.raises(InjectedCrash):
        mover.move("hot", "warm", BUCKET, KEY, idempotency_key=move_id)
    hold(catalog, key=KEY)

    async def execute():
        context = JobContext(
            attempt=2, redelivered=True, stream_sequence=1, consumer_sequence=2,
            shutdown_requested=asyncio.Event(),
        )
        await build_handlers(drivers, catalog)[POLICY_RUN_JOB](envelope, context)

    with pytest.raises(LegalHoldError) as blocked:
        asyncio.run(execute())
    failure = classify_job_error(blocked.value)
    assert failure.retryable is False
    assert failure.category == FailureCategory.AUTHORIZATION
    assert catalog.get_move_job(move_id).state == MoveJobState.FAILED
    assert_copies_preserved(drivers, copied=True)
