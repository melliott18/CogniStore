from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa

from cognistore.budget_telemetry import budget_snapshot
from cognistore.core.budgets import BudgetConstraintError
from cognistore.db import SQLCatalog
from tests.budget_fixtures import NOW, USER, claim, definition

pytestmark = pytest.mark.integration


def prepare(catalog):
    for tier in ("hot", "warm"):
        catalog.register_tier(tier)
        catalog.register_pool(f"{tier}-pool", tier, region="us-test", members=(tier,))
    for key in ("a", "b"):
        catalog.upsert("bucket", key, size=4, tier="hot")
        catalog.assign_pool("bucket", key, "hot-pool")
    catalog.configure_budget(definition(), audit_context=USER, occurred_at=NOW)


def test_postgres_concurrent_objects_reserve_one_shared_budget(postgres_dsn):
    with SQLCatalog(postgres_dsn) as first, SQLCatalog(postgres_dsn) as second:
        prepare(first)
        barrier = threading.Barrier(2)

        def run(catalog, key):
            barrier.wait(timeout=10)
            try:
                claim(catalog, key)
                return "admitted"
            except BudgetConstraintError:
                return "blocked"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run, catalog, key) for catalog, key in ((first, "a"), (second, "b"))]
            assert sorted(future.result(timeout=20) for future in futures) == ["admitted", "blocked"]
        assert len(first.list_budget_reservations()) == 1
        assert len(second.list_move_jobs()) == 1
    with SQLCatalog(postgres_dsn, read_only=True) as reopened:
        assert reopened.list_budgets()[0].budget_id == "monthly"
        assert reopened.list_budget_reservations()[0]["cost_usd"] == "1"


def test_postgres_move_audit_failure_rolls_back_reservation(postgres_dsn, monkeypatch):
    with SQLCatalog(postgres_dsn) as catalog:
        prepare(catalog)
        before = catalog.list_audit_events()

        def reject(*args, **kwargs):
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr(catalog, "_insert_move_audit_event", reject)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            claim(catalog)
        assert catalog.list_audit_events() == before
        assert not catalog.list_budget_reservations()
        assert not catalog.list_move_jobs()


def test_postgres_budget_snapshot_reads_only_committed_admissions(postgres_dsn, monkeypatch):
    with SQLCatalog(postgres_dsn) as writer:
        prepare(writer)
        inserted = threading.Event()
        release = threading.Event()
        original = writer._insert_move_audit_event

        def hold_before_commit(*args, **kwargs):
            original(*args, **kwargs)
            inserted.set()
            assert release.wait(timeout=10), "admission transaction was not released"

        monkeypatch.setattr(writer, "_insert_move_audit_event", hold_before_commit)
        with SQLCatalog(postgres_dsn, read_only=True) as reader:
            statements = []
            sa.event.listen(reader.engine, "before_cursor_execute",
                            lambda conn, cursor, statement, parameters, context, many:
                            statements.append(statement))
            with ThreadPoolExecutor(max_workers=1) as executor:
                admitted = executor.submit(claim, writer)
                try:
                    assert inserted.wait(timeout=10), "admission did not reach its commit boundary"
                    definitions, reservations = reader.read_budget_snapshot()
                    assert [item.budget_id for item in definitions] == ["monthly"]
                    assert reservations == []
                    pending = budget_snapshot(reader, as_of=NOW)
                    assert pending["cost"].consumption_ratio == 0
                    assert pending["cost"].unknown == 0
                finally:
                    release.set()
                admitted.result(timeout=10)

            definitions, reservations = reader.read_budget_snapshot()
            assert definitions == [definition()]
            assert len(reservations) == 1
            # PostgreSQL's UNION ALL must preserve JSON decoding on both arms,
            # including nested frozen estimator evidence in reservations.
            reservation = reservations[0]
            assert reservation["cost_usd"] == "1"
            assert reservation["evidence"]["charge"]["cost_usd"] == "1"
            assert reservation["evidence"]["after"]["cost_usd"] == "1"
            committed = budget_snapshot(reader, as_of=NOW)
            assert committed["cost"].consumption_ratio == 1
            assert committed["cost"].unknown == 0
            assert len(statements) == 4
            assert all(statement.startswith("SELECT") and "UNION ALL" in statement
                       for statement in statements)
