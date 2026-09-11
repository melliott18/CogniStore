from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from cognistore.core.audit import AuditContext, AuditEventType, AuditQuery
from cognistore.core.budgets import BudgetConstraintError
from cognistore.core.catalog import Catalog
from cognistore.core.move_jobs import MoveJobLeaseError, MoveJobState
from cognistore.core.mover import Mover
from cognistore.db import MigrationManager, SQLCatalog
from cognistore.db.sqlite_import import import_sqlite_catalog
from cognistore.drivers.posix_driver import PosixDriver
from tests.budget_fixtures import END, NOW, USER, claim, definition


@pytest.fixture(params=["memory", "sqlite"])
def catalog(request, tmp_path):
    value = Catalog() if request.param == "memory" else SQLCatalog(tmp_path / "catalog.db")
    for tier in ("hot", "warm"):
        value.register_tier(tier)
        value.register_pool(f"{tier}-pool", tier, region="us-test", members=(tier,))
    for key in ("a", "b"):
        value.upsert("bucket", key, size=4, tier="hot")
        value.assign_pool("bucket", key, "hot-pool")
    yield value
    if isinstance(value, SQLCatalog):
        value.close()




def test_budget_configuration_is_immutable_and_persistent(catalog):
    budget = definition()
    catalog.configure_budget(budget, audit_context=USER, occurred_at=NOW)
    catalog.configure_budget(budget, audit_context=USER, occurred_at=NOW)
    with pytest.raises(BudgetConstraintError, match="immutable"):
        catalog.configure_budget(replace(budget, cost_limit_usd="2"), audit_context=USER)
    assert [item.to_dict() for item in catalog.list_budgets()] == [budget.to_dict()]
    events = catalog.list_audit_events(AuditQuery(event_types={AuditEventType.BUDGET_CONFIGURED.value}))
    assert len(events) == 1
    if isinstance(catalog, SQLCatalog):
        with SQLCatalog(catalog.db_path, read_only=True) as reopened:
            assert reopened.list_budgets()[0].to_dict() == budget.to_dict()
            with pytest.raises(Exception):
                reopened.configure_budget(definition(budget_id="new"), audit_context=USER)


def test_configuration_cannot_grandfather_in_flight_unbudgeted_moves(catalog):
    claim(catalog)
    before = catalog.list_audit_events()
    with pytest.raises(BudgetConstraintError, match="in flight"):
        catalog.configure_budget(definition(), audit_context=USER, occurred_at=NOW)
    assert not catalog.list_budgets()
    assert catalog.list_audit_events() == before


def test_identical_configuration_remains_idempotent_with_matching_in_flight_move(catalog):
    catalog.configure_budget(definition(), audit_context=USER, occurred_at=NOW)
    claim(catalog)
    catalog.configure_budget(definition(), audit_context=USER, occurred_at=NOW)
    assert len(catalog.list_budgets()) == 1


def test_concurrent_objects_share_atomic_budget(catalog):
    catalog.configure_budget(definition(), audit_context=USER, occurred_at=NOW)
    other = SQLCatalog(catalog.db_path) if isinstance(catalog, SQLCatalog) else catalog
    barrier = threading.Barrier(2)

    def run(store, key):
        barrier.wait(timeout=5)
        try:
            return claim(store, key).idempotency_key
        except BudgetConstraintError:
            return "blocked"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [future.result(timeout=10) for future in (
                executor.submit(run, catalog, "a"), executor.submit(run, other, "b"),
            )]
        assert results.count("blocked") == 1
        reservations = catalog.list_budget_reservations()
        assert len(reservations) == 1
        assert reservations[0]["cost_usd"] == "1"
        assert len(catalog.list_move_jobs()) == 1
    finally:
        if other is not catalog:
            other.close()


def test_recovery_claim_reserves_again_but_lease_conflict_and_terminal_do_not(catalog):
    catalog.configure_budget(definition("2"), audit_context=USER, occurred_at=NOW)
    claim(catalog)
    with pytest.raises(MoveJobLeaseError):
        claim(catalog, owner="another-worker")
    assert len(catalog.list_budget_reservations()) == 1
    claim(catalog)
    assert [item["attempt"] for item in catalog.list_budget_reservations()] == [1, 2]
    with pytest.raises(BudgetConstraintError):
        claim(catalog)
    assert len(catalog.list_budget_reservations()) == 2
    catalog.transition_move_job(
        "move-a", owner_id="worker", expected_state=MoveJobState.PREPARED,
        to_state=MoveJobState.FAILED, reason="test failure", now=NOW, lease_expires_at=END,
    )
    assert claim(catalog).state == MoveJobState.FAILED
    assert len(catalog.list_budget_reservations()) == 2


def test_all_scopes_and_audits_commit_atomically(catalog):
    catalog.configure_budget(definition("2", budget_id="first"), audit_context=USER, occurred_at=NOW)
    catalog.configure_budget(definition("0", budget_id="second"), audit_context=USER, occurred_at=NOW)
    before = catalog.list_audit_events()
    with pytest.raises(BudgetConstraintError):
        claim(catalog)
    assert not catalog.list_budget_reservations()
    assert not catalog.list_move_jobs()
    assert catalog.list_audit_events() == before


def test_move_audit_failure_rolls_back_budget_reservation(catalog, monkeypatch):
    catalog.configure_budget(definition(), audit_context=USER, occurred_at=NOW)
    before = catalog.list_audit_events()

    def reject(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    method = "_insert_move_audit_event" if isinstance(catalog, SQLCatalog) else "_append_move_audit_event"
    monkeypatch.setattr(catalog, method, reject)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        claim(catalog)
    assert not catalog.list_budget_reservations()
    assert not catalog.list_move_jobs()
    assert catalog.list_audit_events() == before


def test_override_requires_actor_and_remains_audited_on_recovery(catalog):
    catalog.configure_budget(definition("0"), audit_context=USER, occurred_at=NOW)
    metadata = {"cognistore_budget_override": {"actor_id": "operator", "reason": "approved exception"}}
    with pytest.raises(BudgetConstraintError, match="authenticated user"):
        claim(catalog, metadata=metadata)
    claim(catalog, metadata=metadata, context=USER)
    claim(catalog, context=AuditContext("recovery", "worker", "worker"))
    reservations = catalog.list_budget_reservations()
    assert len(reservations) == 2
    assert all(item["evidence"]["override_applied"] for item in reservations)
    assert all(item["evidence"]["override"]["actor_id"] == "operator" for item in reservations)
    audits = catalog.list_audit_events(AuditQuery(event_types={AuditEventType.BUDGET_RESERVED.value}))
    assert len(audits) == 2


def test_unaudited_override_is_rejected_without_registered_budgets(catalog):
    with pytest.raises(BudgetConstraintError, match="authenticated user"):
        claim(catalog, metadata={"cognistore_budget_override": {"actor_id": "operator", "reason": "forged"}})
    assert not catalog.list_move_jobs()
    assert not catalog.list_budget_reservations()


def test_budgeted_move_commits_bound_pool_and_terminal_replay_is_free(catalog, tmp_path):
    catalog.configure_budget(definition(), audit_context=USER, occurred_at=NOW)
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    drivers["hot"].put_object("bucket", "a", b"data")
    mover = Mover(drivers, catalog, clock=lambda: datetime(2026, 9, 11, tzinfo=timezone.utc))
    mover.move("hot", "warm", "bucket", "a", idempotency_key="move-a")
    assert catalog.get("bucket", "a").pool_id == "warm-pool"
    assert len(catalog.list_budget_reservations()) == 1
    mover.move("hot", "warm", "bucket", "a", idempotency_key="move-a")
    assert len(catalog.list_budget_reservations()) == 1


def test_native_import_preserves_budget_enforcement(tmp_path):
    source_path = tmp_path / "source.db"
    with SQLCatalog(source_path) as source:
        for tier in ("hot", "warm"):
            source.register_tier(tier)
            source.register_pool(f"{tier}-pool", tier, region="us-test", members=(tier,))
        source.upsert("bucket", "a", size=4, tier="hot")
        source.configure_budget(definition(), audit_context=USER, occurred_at=NOW)
        claim(source)
        reservations = source.list_budget_reservations()
    with SQLCatalog(tmp_path / "target.db") as destination:
        report = import_sqlite_catalog(source_path, destination)
        assert report.budget_definitions == 1
        assert report.budget_reservations == 1
        assert destination.list_budget_reservations() == reservations
        assert destination.list_budgets()[0].to_dict() == definition().to_dict()
        with pytest.raises(BudgetConstraintError):
            claim(destination, "b")


def test_explicit_destination_pool_is_bound_even_without_budget(catalog):
    job = claim(catalog, metadata={"cognistore_expected_destination_pool_id": "warm-pool"})
    assert job.source_metadata["cognistore_destination_pool_id"] == "warm-pool"
    with pytest.raises(BudgetConstraintError, match="unavailable"):
        claim(catalog, "b", metadata={"cognistore_expected_destination_pool_id": "missing"})


def test_explicit_destination_must_match_budget(catalog):
    catalog.register_pool("other-warm", "warm", region="us-test", members=("other",))
    catalog.configure_budget(definition(), audit_context=USER, occurred_at=NOW)
    with pytest.raises(BudgetConstraintError, match="differs from the budget"):
        claim(catalog, metadata={"cognistore_expected_destination_pool_id": "other-warm"})
    assert not catalog.list_budget_reservations()


def test_budget_migration_refuses_to_discard_registered_guards(tmp_path):
    with SQLCatalog(tmp_path / "catalog.db") as catalog:
        catalog.configure_budget(definition(), audit_context=USER, occurred_at=NOW)
        with pytest.raises(RuntimeError, match="cannot downgrade.*budget"):
            MigrationManager().downgrade(catalog.engine, "0010_tier_stability")
        assert MigrationManager().is_at_head(catalog.engine)
        assert catalog.list_budgets()[0].to_dict() == definition().to_dict()
