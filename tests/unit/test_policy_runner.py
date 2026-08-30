from datetime import datetime, timezone
from pathlib import Path

import pytest

from cognistore.core.audit import AuditContext, AuditEventType, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.core.mover import Mover
from cognistore.core.policy import PolicyDecision, SimplePolicy
from cognistore.core.policy_runner import PolicyRunner
from cognistore.drivers.posix_driver import PosixDriver


class UnsafePolicy:
    """Policy stub that deliberately ignores all placement constraints."""

    def __init__(self, destination: str):
        self.destination = destination

    def evaluate(self, current_tier: str, size: int) -> PolicyDecision:
        return PolicyDecision(
            action="move",
            dst_tier=self.destination,
            reason="unsafe test decision",
        )


def make_runner(tmp_path: Path, policy, *, allowed_tiers=None):
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    drivers = {"hot": hot, "warm": warm}
    catalog = Catalog()
    mover = Mover(drivers, catalog)
    runner = PolicyRunner(
        catalog,
        drivers,
        mover,
        policy,
        allowed_tiers=allowed_tiers,
    )
    return runner, catalog, hot, warm


def test_policy_runner_moves_objects(tmp_path: Path):
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    drivers = {"hot": hot, "warm": warm}

    catalog = Catalog()
    mover = Mover(drivers, catalog)
    # Threshold 5 bytes: <=5 goes to hot, >5 goes to warm
    policy = SimplePolicy(size_threshold=5)
    runner = PolicyRunner(catalog, drivers, mover, policy)

    bucket = "bk"
    # Prepare objects in storage to match the catalog's current tier
    warm.put_object(bucket, "small.txt", b"12345")  # size 5, currently in warm
    hot.put_object(bucket, "big.txt", b"123456")   # size 6, currently in hot
    catalog.upsert(bucket, "small.txt", 5, tier="warm")  # policy should move to hot
    catalog.upsert(bucket, "big.txt", 6, tier="hot")     # policy should move to warm

    results = runner.run_once(bucket)
    # Expect 2 moves: small -> hot, big -> warm
    keys = sorted([(r.key, r.from_tier, r.to_tier) for r in results])
    assert keys == [("big.txt", "hot", "warm"), ("small.txt", "warm", "hot")]
    assert {result.status for result in results} == {"completed"}

    # Validate storage side effects
    assert "small.txt" in list(hot.list_objects(bucket))
    assert "big.txt" in list(warm.list_objects(bucket))


def test_simple_policy_respects_allowed_destination_tiers():
    hot_only = SimplePolicy(size_threshold=5, allowed_tiers=("hot",))
    warm_only = SimplePolicy(size_threshold=5, allowed_tiers=("warm",))

    disallowed_warm = hot_only.evaluate(current_tier="hot", size=6)
    disallowed_hot = warm_only.evaluate(current_tier="warm", size=5)
    allowed_hot = hot_only.evaluate(current_tier="warm", size=5)

    assert disallowed_warm.action == "stay"
    assert disallowed_warm.dst_tier is None
    assert disallowed_hot.action == "stay"
    assert disallowed_hot.dst_tier is None
    assert allowed_hot.action == "move"
    assert allowed_hot.dst_tier == "hot"


def test_policy_runner_rejects_disallowed_destination_from_unsafe_policy(tmp_path: Path):
    runner, catalog, hot, warm = make_runner(
        tmp_path,
        UnsafePolicy("warm"),
        allowed_tiers=("hot",),
    )
    bucket = "bk"
    key = "object.bin"
    data = b"source data"
    hot.put_object(bucket, key, data)
    catalog.upsert(bucket, key, size=len(data), tier="hot")

    results = runner.run_once(bucket)

    assert results == []
    assert hot.get_object(bucket, key) == data
    assert list(warm.list_objects(bucket)) == []
    record = catalog.get(bucket, key)
    assert record is not None
    assert record.tier == "hot"


def test_policy_runner_rejects_unknown_allowed_tiers(tmp_path: Path):
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    drivers = {"hot": hot, "warm": warm}
    catalog = Catalog()

    with pytest.raises(ValueError, match=r"Unknown allowed tier\(s\): archive, ghost"):
        PolicyRunner(
            catalog,
            drivers,
            Mover(drivers, catalog),
            SimplePolicy(),
            allowed_tiers=("ghost", "hot", "archive"),
        )


def test_policy_runner_dry_run_is_planned_and_has_no_mutations(tmp_path: Path):
    runner, catalog, hot, warm = make_runner(
        tmp_path,
        SimplePolicy(size_threshold=5, allowed_tiers=("hot", "warm")),
        allowed_tiers=("hot", "warm"),
    )
    bucket = "bk"
    key = "large.bin"
    data = b"123456"
    hot.put_object(bucket, key, data)
    catalog.upsert(bucket, key, size=len(data), tier="hot")

    results = runner.run_once(bucket, dry_run=True)

    assert len(results) == 1
    assert results[0].status == "planned"
    assert results[0].from_tier == "hot"
    assert results[0].to_tier == "warm"
    assert hot.get_object(bucket, key) == data
    assert list(warm.list_objects(bucket)) == []
    record = catalog.get(bucket, key)
    assert record is not None
    assert record.tier == "hot"
    assert record.size == len(data)


def test_policy_runner_execution_is_completed_and_mutates_placement(tmp_path: Path):
    runner, catalog, hot, warm = make_runner(
        tmp_path,
        SimplePolicy(size_threshold=5, allowed_tiers=("hot", "warm")),
        allowed_tiers=("hot", "warm"),
    )
    bucket = "bk"
    key = "large.bin"
    data = b"123456"
    hot.put_object(bucket, key, data)
    catalog.upsert(bucket, key, size=len(data), tier="hot")

    results = runner.run_once(bucket)

    assert len(results) == 1
    assert results[0].status == "completed"
    with pytest.raises(FileNotFoundError):
        hot.get_object(bucket, key)
    assert warm.get_object(bucket, key) == data
    record = catalog.get(bucket, key)
    assert record is not None
    assert record.tier == "warm"
    assert record.size == len(data)


def test_policy_decision_replay_is_idempotent_before_execution(tmp_path: Path) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    drivers = {"hot": hot, "warm": warm}
    catalog = Catalog()
    data = b"123456"
    hot.put_object("bk", "large.bin", data)
    catalog.upsert("bk", "large.bin", len(data), tier="hot")
    context = AuditContext(
        correlation_id="replayed-policy-31",
        actor_type="worker",
        actor_id="00000000-0000-4000-8000-000000000031",
        job_id="00000000-0000-4000-8000-000000000031",
    )
    runner = PolicyRunner(
        catalog,
        drivers,
        Mover(drivers, catalog),
        SimplePolicy(size_threshold=5),
        idempotency_namespace=context.job_id,
        policy_name="simple",
        policy_version="1",
        audit_context=context,
        audit_occurred_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
    )

    first = runner.plan_once("bk")
    second = runner.plan_once("bk")

    assert first[0].decision_event_id == second[0].decision_event_id
    decisions = catalog.list_audit_events(
        AuditQuery(
            correlation_id=context.correlation_id,
            event_types=frozenset({AuditEventType.POLICY_DECISION}),
        )
    )
    assert len(decisions) == 1


def test_policy_runner_preflights_entire_batch_before_moving(tmp_path: Path):
    runner, catalog, hot, warm = make_runner(
        tmp_path,
        SimplePolicy(size_threshold=5, allowed_tiers=("hot", "warm")),
        allowed_tiers=("hot", "warm"),
    )
    bucket = "bk"
    first_key = "first.bin"
    colliding_key = "second.bin"
    first_data = b"first-source"
    second_data = b"second-source"
    collision_data = b"existing-destination"

    hot.put_object(bucket, first_key, first_data)
    hot.put_object(bucket, colliding_key, second_data)
    warm.put_object(bucket, colliding_key, collision_data)
    catalog.upsert(bucket, first_key, size=len(first_data), tier="hot")
    catalog.upsert(bucket, colliding_key, size=len(second_data), tier="hot")

    with pytest.raises(FileExistsError, match="Destination object already exists"):
        runner.run_once(bucket)

    assert hot.get_object(bucket, first_key) == first_data
    assert hot.get_object(bucket, colliding_key) == second_data
    assert list(warm.list_objects(bucket)) == [colliding_key]
    assert warm.get_object(bucket, colliding_key) == collision_data
    assert catalog.get(bucket, first_key).tier == "hot"  # type: ignore[union-attr]
    assert catalog.get(bucket, colliding_key).tier == "hot"  # type: ignore[union-attr]
