from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

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
