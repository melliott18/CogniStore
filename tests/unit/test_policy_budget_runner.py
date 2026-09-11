"""Budget evidence follows normal policy, mover, CLI and preview paths."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from cognistore.cli.cognistore_cli import main
from cognistore.core.audit import AuditContext, AuditEventType, AuditQuery
from cognistore.core.budgets import BudgetConstraintError, BudgetDefinition, BudgetOverride
from cognistore.core.catalog import Catalog
from cognistore.core.estimation import (
    RATE_UNITS,
    EstimationProfile,
    EstimationWorkload,
    RateAssumption,
)
from cognistore.core.mover import Mover
from cognistore.core.policy import SimplePolicy
from cognistore.core.policy_runner import PolicyRunner
from cognistore.db.catalog import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver

NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)
ACTOR = AuditContext(correlation_id="budget-operator", actor_type="user", actor_id="operator")


def configure(catalog, *, limit="1"):
    profiles = {}
    for tier in ("hot", "warm"):
        catalog.register_tier(tier)
        catalog.register_pool(tier + "-pool", tier, region="west", members=(tier,))
        rates = {
            name: RateAssumption(
                value="1" if name == "write_request_price" and tier == "warm" else "0",
                unit=unit, source="synthetic-test", source_version="1",
                effective_from="2026-09-01T00:00:00Z", effective_until="2026-11-01T00:00:00Z",
            ) for name, unit in RATE_UNITS.items()
        }
        profiles[tier + "-pool"] = EstimationProfile("1", "posix", rates)
    budget = BudgetDefinition(
        "september", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z",
        cost_limit_usd=limit, bucket="bucket",
        tier_pools={tier: tier + "-pool" for tier in ("hot", "warm")}, profiles=profiles,
        workload=EstimationWorkload(read_requests=0, write_requests=0, transfer_bytes=0, retrieval_bytes=0),
    )
    catalog.configure_budget(budget, audit_context=ACTOR)
    return budget


def runner(tmp_path, *, limit="1", override=None):
    catalog = Catalog()
    configure(catalog, limit=limit)
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    for key in ("a", "b"):
        drivers["hot"].put_object("bucket", key, b"data")
        catalog.upsert("bucket", key, 4, "hot")
        catalog.assign_pool("bucket", key, "hot-pool")
    mover = Mover(drivers, catalog, clock=lambda: NOW)
    return PolicyRunner(
        catalog, drivers, mover, SimplePolicy(1), clock=lambda: NOW,
        budget_override=override, audit_context=ACTOR, idempotency_namespace="budget-test",
    )


def test_batch_preview_projects_allowance_without_reserving(tmp_path):
    subject = runner(tmp_path)
    before = subject.catalog.list_audit_events()
    result = subject.preview_once("bucket", as_of=NOW)
    assert [item.action for item in result] == ["move", "stay"]
    blocked = result[1].constraints["budgets"][0]
    assert blocked["before"]["cost_usd"] == "1"
    assert blocked["after"]["cost_usd"] == "2"
    assert blocked["binding_constraints"] == ["cost_usd_limit"]
    assert subject.catalog.list_budget_reservations() == []
    assert subject.catalog.list_audit_events() == before
    actions = subject.run_once("bucket")
    assert len(actions) == 1
    assert subject.catalog.get("bucket", "a").tier == "warm"
    assert subject.catalog.get("bucket", "a").pool_id == "warm-pool"
    assert subject.catalog.get("bucket", "b").tier == "hot"
    reasons = subject.catalog.list_audit_events(AuditQuery(event_types=frozenset({AuditEventType.POLICY_DECISION})))
    blocked_reason = next(event for event in reasons if event.object_key == "b").details["structured_reason"]
    assert blocked_reason["code"] == "budget_constraint"
    assert blocked_reason["disposition"] == "suppressed"
    assert blocked_reason["constraints"]["budgets"][0]["binding_constraints"] == ["cost_usd_limit"]


def test_stale_plan_cannot_bypass_real_balance(tmp_path):
    subject = runner(tmp_path)
    planned = subject.plan_once("bucket", prefix="b", dry_run=True)[0]
    subject.run_once("bucket", prefix="a")
    with pytest.raises(BudgetConstraintError):
        subject.execute(planned)
    assert subject.catalog.get("bucket", "b").tier == "hot"
    assert list(subject.drivers["warm"].list_objects("bucket")) == ["a"]


def test_explicit_override_survives_action_and_is_audited(tmp_path):
    subject = runner(tmp_path, limit="0", override=BudgetOverride("operator", "incident 53"))
    actions = subject.run_once("bucket", prefix="a")
    assert len(actions) == 1
    reservations = subject.catalog.list_budget_reservations()
    serialized = json.dumps(reservations)
    assert "incident 53" in serialized and "operator" in serialized
    assert subject.catalog.get("bucket", "a").tier == "warm"
    assert "cognistore_budget_override" not in subject.drivers["warm"].stat_object("bucket", "a")


def test_simulation_only_runner_refuses_audits_and_moves(tmp_path):
    subject = runner(tmp_path)
    subject.simulation_only = True
    before = subject.catalog.list_audit_events()
    with pytest.raises(ValueError, match="simulation-only"):
        subject.run_once("bucket")
    with pytest.raises(ValueError, match="simulation-only"):
        subject.reevaluate_object("bucket", "a")
    assert subject.catalog.list_audit_events() == before


def test_cli_what_if_uses_read_only_catalog_and_never_loads_drivers(tmp_path, monkeypatch, capsys):
    path = tmp_path / "catalog.db"
    with SQLCatalog(path) as catalog:
        configure(catalog)
        for key in ("a", "b"):
            catalog.upsert("bucket", key, 4, "hot")
            catalog.assign_pool("bucket", key, "hot-pool")
    before = path.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail("simulation loaded storage drivers")
    monkeypatch.setattr("cognistore.cli.cognistore_cli.load_drivers", forbidden)
    assert main([
        "--no-config", "--catalog-db", str(path), "--json", "policy-what-if", "bucket",
        "--threshold", "1", "--baseline-threshold", "10", "--as-of", NOW.isoformat(),
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True
    assert "cost_usd_limit" in json.dumps(result)
    assert path.read_bytes() == before


def test_cli_budget_configuration_and_dry_run(tmp_path, capsys):
    path = tmp_path / "catalog.db"
    with SQLCatalog(path) as catalog:
        budget = configure(catalog)
    proposed = replace(budget, budget_id="next-period", period_start="2026-10-01T00:00:00Z", period_end="2026-11-01T00:00:00Z")
    config = tmp_path / "budget.json"
    config.write_text(json.dumps(proposed.to_dict()))
    args = ["--no-config", "--catalog-db", str(path), "--json", "budget-configure", "--input", str(config), "--actor", "operator"]
    assert main([*args, "--dry-run"]) == 0
    capsys.readouterr()
    with SQLCatalog(path, read_only=True) as catalog:
        assert len(catalog.list_budgets()) == 1
    assert main(args) == 0
    capsys.readouterr()
    with SQLCatalog(path, read_only=True) as catalog:
        assert len(catalog.list_budgets()) == 2


def test_denied_budget_does_not_hash_policy_content(tmp_path, monkeypatch):
    subject = runner(tmp_path, limit="0")
    def forbidden(*args, **kwargs):
        pytest.fail("denied budget read object bytes before admission")
    monkeypatch.setattr(subject.mover, "_verify_expected_source_content", forbidden)
    with pytest.raises(BudgetConstraintError):
        subject.mover.move("hot", "warm", "bucket", "a", expected_source_sha256="0" * 64)
    assert subject.catalog.list_move_jobs() == []


def test_admitted_content_mismatch_is_charged_without_moving(tmp_path):
    from cognistore.core.mover import MoveSourceContentMismatchError
    subject = runner(tmp_path)
    with pytest.raises(MoveSourceContentMismatchError):
        subject.mover.move("hot", "warm", "bucket", "a", expected_source_sha256="0" * 64)
    assert len(subject.catalog.list_budget_reservations()) == 1
    assert subject.catalog.list_move_jobs()[0].state.value == "failed"
    assert subject.catalog.get("bucket", "a").tier == "hot"
    assert list(subject.drivers["warm"].list_objects("bucket")) == []
