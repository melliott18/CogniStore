from __future__ import annotations

import math
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from cognistore import budget_telemetry
from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.budget_telemetry import budget_metrics_response, budget_snapshot
from cognistore.core.budgets import evaluate_budget
from cognistore.core.catalog import Catalog
from cognistore.core.estimation import (
    ESTIMATION_FORMULA_VERSION,
    ESTIMATION_SCHEMA_VERSION,
    StorageImpactEstimator,
)
from cognistore.db import SQLCatalog
from cognistore.db.schema import budget_reservations
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


def _budget(**changes):
    original = definition("1.5")
    profiles = dict(original.profiles)
    rates = dict(profiles["warm-pool"].rates)
    rates["write_request_energy"] = replace(rates["write_request_energy"], value="2")
    rates["carbon_intensity"] = replace(rates["carbon_intensity"], value="1")
    profiles["warm-pool"] = replace(profiles["warm-pool"], rates=rates)
    return replace(original, **{
        "profiles": profiles, "carbon_limit_gco2e": "4",
        "opening_cost_usd": ".25", "opening_carbon_gco2e": ".5", **changes,
    })


def _install(catalog, budget):
    catalog.configure_budget(budget, audit_context=USER, occurred_at=NOW)


def _samples(body):
    return {sample.name + ":" + sample.labels["dimension"]: sample.value
            for family in text_string_to_metric_families(body.decode())
            for sample in family.samples if "dimension" in sample.labels}


def test_balances_cross_limit_and_recover_only_in_new_exact_scope_period(catalog):
    budget = _budget()
    renewal = replace(
        budget, budget_id="next-period", period_start=END, period_end="2026-11-11T00:00:00Z",
        opening_cost_usd=".15", opening_carbon_gco2e=".4",
    )
    _install(catalog, budget)
    _install(catalog, renewal)
    initial = budget_snapshot(catalog, as_of=NOW)
    assert initial["cost"].consumption_ratio == pytest.approx(1 / 6)
    assert initial["carbon"].consumption_ratio == .125

    claim(catalog)
    below = budget_snapshot(catalog, as_of=NOW)
    assert below["cost"].consumption_ratio == pytest.approx(5 / 6)
    assert below["carbon"].consumption_ratio == .625
    claim(catalog, "b", context=USER, metadata={
        "cognistore_budget_override": {"actor_id": "operator", "reason": "approved recovery"},
    })
    breach = budget_snapshot(catalog, as_of=NOW)
    assert breach["cost"].consumption_ratio == 1.5
    assert breach["carbon"].consumption_ratio == 1.125
    assert all(state.unknown == 0 for state in breach.values())

    renewed = budget_snapshot(catalog, as_of=END)
    assert renewed["cost"].consumption_ratio == .1
    assert renewed["carbon"].consumption_ratio == .1
    assert all(state.unknown == 0 for state in renewed.values())
    # Renewal does not delete or rebate conservative historical commitments.
    assert len(catalog.list_budget_reservations()) == 2


def test_uses_frozen_versioned_estimator_charge_and_admission_balance(catalog, monkeypatch):
    budget = _budget()
    _install(catalog, budget)
    claim(catalog)
    reservation = catalog.list_budget_reservations()[0]
    charge = reservation["evidence"]["charge"]
    holding = charge["assumptions"]["estimates"]["destination_holding"]
    assert holding["schema_version"] == ESTIMATION_SCHEMA_VERSION
    assert holding["formulas"]["version"] == ESTIMATION_FORMULA_VERSION
    assert holding["profile_version"] == budget.profiles["warm-pool"].version
    assert charge["assumptions"]["formula"] == "remaining-destination-plus-migration-v1"
    assert reservation["cost_usd"] == charge["cost_usd"] == "1"
    assert reservation["carbon_gco2e"] == charge["carbon_gco2e"] == "2"
    replay = evaluate_budget(
        budget, {"cost_usd": "0", "carbon_gco2e": "0"}, [reservation], as_of=NOW,
    )
    assert replay["before"] == reservation["evidence"]["after"]

    def forbidden_estimation(*args, **kwargs):
        pytest.fail("scrapes must not re-estimate frozen admission quotes")

    monkeypatch.setattr(StorageImpactEstimator, "estimate", forbidden_estimation)
    state = budget_snapshot(catalog, as_of="2026-10-10T00:00:00Z")
    for dimension, amount in (("cost", "cost_usd"), ("carbon", "carbon_gco2e")):
        expected = Decimal(replay["before"][amount]) / Decimal(replay["limits"][amount])
        assert state[dimension].consumption_ratio == float(expected)


def test_overlapping_scopes_use_maximum_and_never_sum(catalog):
    _install(catalog, _budget(budget_id="wide", bucket=None,
                              cost_limit_usd=10, opening_cost_usd=3))
    _install(catalog, _budget(budget_id="narrow", prefix="a",
                              cost_limit_usd=2, opening_cost_usd=1))
    claim(catalog)
    state = budget_snapshot(catalog, as_of=NOW)
    assert state["cost"].consumption_ratio == 1
    assert state["carbon"].consumption_ratio == .625
    assert len(catalog.list_budget_reservations()) == 2


@pytest.mark.parametrize("as_of", ["2026-09-10T23:59:59Z", END])
def test_inactive_scope_is_unknown_not_healthy(catalog, as_of):
    _install(catalog, _budget())
    state = budget_snapshot(catalog, as_of=as_of)
    assert all(math.isnan(item.consumption_ratio) and item.unknown == 1
               for item in state.values())


def test_active_wider_scope_cannot_hide_expired_narrow_scope(catalog):
    _install(catalog, _budget(budget_id="wide", bucket=None))
    _install(catalog, _budget(budget_id="expired-narrow", prefix="private/",
                              period_start="2026-08-01T00:00:00Z", period_end=NOW,
                              carbon_limit_gco2e=None))
    state = budget_snapshot(catalog, as_of=NOW)
    assert state["cost"].unknown == 1
    assert math.isnan(state["cost"].consumption_ratio)
    assert state["carbon"].unknown == 0
    assert state["carbon"].consumption_ratio == .125


def test_missing_held_amount_is_unknown_only_in_its_dimension(catalog):
    _install(catalog, _budget())
    claim(catalog)
    reservation = catalog.list_budget_reservations()[0]
    reservation["carbon_gco2e"] = None
    if isinstance(catalog, SQLCatalog):
        with catalog.engine.begin() as connection:
            connection.execute(sa.update(budget_reservations).values(reservation=reservation))
    else:
        identity = (reservation["budget_id"], reservation["move_id"], reservation["attempt"])
        catalog._budget_reservations[identity] = reservation
    state = budget_snapshot(catalog, as_of=NOW)
    assert state["cost"].consumption_ratio == pytest.approx(5 / 6)
    assert state["cost"].unknown == 0
    assert math.isnan(state["carbon"].consumption_ratio)
    assert state["carbon"].unknown == 1


@pytest.mark.parametrize("opening,expected", [("0", 0), (".01", math.inf)])
def test_zero_limits_preserve_empty_and_nonempty_balances(catalog, opening, expected):
    _install(catalog, _budget(cost_limit_usd=0, carbon_limit_gco2e=0,
                              opening_cost_usd=opening, opening_carbon_gco2e=opening))
    assert all(item.consumption_ratio == expected and item.unknown == 0
               for item in budget_snapshot(catalog, as_of=NOW).values())


def test_no_budgets_and_unconfigured_dimension_have_zero_known_balance(catalog):
    assert all(item.consumption_ratio == 0 and item.unknown == 0
               for item in budget_snapshot(catalog, as_of=NOW).values())
    _install(catalog, _budget(carbon_limit_gco2e=None))
    assert budget_snapshot(catalog, as_of=NOW)["carbon"].consumption_ratio == 0


def test_sql_snapshot_is_one_read_statement_and_works_read_only(tmp_path):
    path = tmp_path / "readonly.db"
    with SQLCatalog(path) as writable:
        _install(writable, _budget())
    with SQLCatalog(path, read_only=True) as readonly:
        statements = []
        sa.event.listen(readonly.engine, "before_cursor_execute",
                        lambda conn, cursor, statement, parameters, context, many:
                        statements.append(statement))
        state = budget_snapshot(readonly, as_of=NOW)
        assert state["cost"].consumption_ratio == pytest.approx(1 / 6)
        assert len(statements) == 1
        assert statements[0].startswith("SELECT") and "UNION ALL" in statements[0]


def test_snapshot_returns_detached_values(catalog):
    _install(catalog, _budget())
    claim(catalog)
    definitions, reservations = catalog.read_budget_snapshot()
    definitions.clear()
    reservations[0]["cost_usd"] = "999"
    assert budget_snapshot(catalog, as_of=NOW)["cost"].consumption_ratio == pytest.approx(5 / 6)


@pytest.mark.parametrize("catalog", [None, object(), SimpleNamespace(
    read_budget_snapshot=lambda: ([], [{"budget_id": "missing-definition"}]),
)])
def test_missing_or_invalid_ledger_never_becomes_healthy_zero(catalog):
    state = budget_snapshot(catalog, as_of=NOW)
    assert all(math.isnan(item.consumption_ratio) and item.unknown == 1
               for item in state.values())


def test_metrics_are_bounded_private_and_refreshed_per_scrape(catalog, monkeypatch):
    actual_snapshot = budget_snapshot
    monkeypatch.setattr(budget_telemetry, "budget_snapshot",
                        lambda store: actual_snapshot(store, as_of=NOW))
    _install(catalog, _budget(budget_id="private-budget", prefix="private-prefix/"))
    first = budget_metrics_response(catalog)
    samples = _samples(first)
    assert len(samples) == 4
    assert samples["cognistore_policy_budget_consumption_ratio:cost"] == pytest.approx(1 / 6)
    for secret in (b"private-budget", b"private-prefix", b"bucket", b"warm-pool", b"operator"):
        assert secret not in first

    def broken_snapshot():
        raise RuntimeError("private database password")

    monkeypatch.setattr(catalog, "read_budget_snapshot", broken_snapshot)
    failed = _samples(budget_metrics_response(catalog))
    assert math.isnan(failed["cognistore_policy_budget_consumption_ratio:cost"])
    assert failed["cognistore_policy_budget_unknown:cost"] == 1


def test_metrics_endpoint_stays_available_when_catalog_snapshot_fails(catalog, monkeypatch):
    def broken_snapshot():
        raise RuntimeError("private catalog credentials")

    monkeypatch.setattr(catalog, "read_budget_snapshot", broken_snapshot)
    with TestClient(create_app(CogniStoreGateway(catalog, {}))) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "private catalog credentials" not in response.text
    samples = _samples(response.content)
    assert samples["cognistore_policy_budget_unknown:cost"] == 1
    assert samples["cognistore_policy_budget_unknown:carbon"] == 1
