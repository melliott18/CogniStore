"""Locality is a hard input to every policy and its public explanations."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from cognistore.core.audit import AuditEventType, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.core.estimation import (
    RATE_UNITS,
    EstimationProfile,
    EstimationWorkload,
    RateAssumption,
    StorageImpactEstimator,
)
from cognistore.core.impact_policy import EstimatePolicy
from cognistore.core.mover import Mover
from cognistore.core.policy import ContentAwarePolicy, LLMPolicy, PolicyDecision, SimplePolicy
from cognistore.core.policy_runner import PolicyRunner
from cognistore.core.policy_simulation import simulate_policy
from cognistore.core.topology import AttributeValue, PlacementConstraints
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.sdk.models import PolicyReason as SDKPolicyReason

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)
AS_OF = NOW.isoformat()
END = "2027-01-01T00:00:00Z"


def configure(tmp_path, monkeypatch, *, allowed_regions=None, objects=()):
    path = tmp_path / "locality.json"
    policy = {
        "version": 1,
        "tenants": {"default": {
            "allowed_regions": ["eu"] if allowed_regions is None else allowed_regions,
            "tier_pools": {tier: tier + "-pool" for tier in ("hot", "warm", "cold")},
            "objects": list(objects),
        }},
    }
    path.write_text(json.dumps(policy))
    monkeypatch.setenv("COGNISTORE_LOCALITY_CONFIG", str(path))
    return path


def setup(tmp_path, monkeypatch, policy=None, **config):
    configure(tmp_path, monkeypatch, **config)
    catalog = Catalog()
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm", "cold")}
    for tier, region, latency in (("hot", "eu", 100), ("warm", "us", 1), ("cold", "eu", 10)):
        catalog.register_tier(tier)
        catalog.register_pool(
            tier + "-pool", tier, region=region, members=(tier,), localities=(region,),
            metadata={"locality_evidence": {
                "observed_at": AS_OF, "max_age_seconds": 86400, "source": "operator",
            }},
            attributes={"latency": AttributeValue(
                latency, "ms", "synthetic", "configured", AS_OF, 86400,
            )},
        )
    drivers["hot"].put_object("bucket", "object", b"contents")
    catalog.upsert("bucket", "object", 8, "hot")
    catalog.assign_pool("bucket", "object", "hot-pool")
    mover = Mover(drivers, catalog, clock=lambda: NOW)
    runner = PolicyRunner(
        catalog, drivers, mover,
        policy or SimplePolicy(1, allowed_tiers=("hot", "warm", "cold")),
        clock=lambda: NOW,
    )
    return runner


@pytest.mark.parametrize("kind", ["simple", "content", "custom"])
def test_hard_locality_rejects_policy_destination_and_preserves_other_candidates(
    tmp_path, monkeypatch, kind,
):
    class UnsafePolicy:
        def evaluate(self, tier, size):
            return PolicyDecision("move", "ignore every constraint", "warm")

    policies = {
        "simple": SimplePolicy(1, allowed_tiers=("hot", "warm", "cold")),
        "content": ContentAwarePolicy(size_threshold=1, allowed_tiers=("hot", "warm", "cold")),
        "custom": UnsafePolicy(),
    }
    runner = setup(tmp_path, monkeypatch, policies[kind])
    result = runner.evaluate_once("bucket", "object", as_of=NOW)
    assert result.action == "stay"
    assert result.constraints["suppression_reason"] == "locality"
    assert result.constraints["rejected_destination_tier"] == "warm"
    assert result.constraints["allowed_destination_tiers"] == ["cold", "hot"]
    assert result.constraints["locality"]["rejected"]["warm"]
    assert runner.run_once("bucket") == []
    assert runner.catalog.list_move_jobs() == []
    assert list(runner.drivers["warm"].list_objects("bucket")) == []


def test_preview_and_retained_reason_explain_locality_without_claiming_replay(tmp_path, monkeypatch):
    runner = setup(tmp_path, monkeypatch)
    before = runner.catalog.list_audit_events()
    snapshot, reason = runner.preview_decision("bucket", "object", as_of=NOW)
    assert not snapshot["replay"]["supported"]
    assert reason["code"] == "locality_constraint"
    assert reason["disposition"] == "suppressed"
    assert reason["constraints"]["locality"]["rejected"]["warm"]
    assert runner.catalog.list_audit_events() == before
    assert SDKPolicyReason.model_validate(reason).model_dump(mode="json") == reason
    runner.reevaluate_object("bucket", "object", as_of=NOW)
    events = runner.catalog.list_audit_events(AuditQuery(
        event_types=frozenset({AuditEventType.POLICY_DECISION}),
    ))
    assert events[-1].details["structured_reason"]["code"] == "locality_constraint"
    assert not events[-1].details["dataset"]["replay"]["supported"]


def test_complete_block_skips_external_provider_and_optional_feature_loader(tmp_path, monkeypatch):
    class ForbiddenProvider:
        def decide(self, request):
            pytest.fail("blocked object reached the external provider")

    class ForbiddenFeatures:
        def load(self, records, requests):
            pytest.fail("blocked object reached optional feature providers")

    runner = setup(tmp_path, monkeypatch, LLMPolicy(ForbiddenProvider(), ("hot", "warm")))
    runner.allowed_tiers = frozenset({"hot", "warm"})
    runner.feature_loader = ForbiddenFeatures()
    result = runner.evaluate_once("bucket", "object", as_of=NOW)
    assert result.action == "stay"
    assert result.constraints["suppression_reason"] == "locality"
    assert "no destination tier" in result.reason


def test_unrestrictive_locality_does_not_mislabel_an_ordinary_tier_allowlist(tmp_path, monkeypatch):
    runner = setup(tmp_path, monkeypatch, allowed_regions=["eu", "us"])
    runner.allowed_tiers = frozenset({"hot"})
    _, reason = runner.preview_decision("bucket", "object", as_of=NOW)
    assert reason["code"] == "destination_not_allowed"
    assert reason["constraints"]["locality"]["rejected"] == {}


def test_object_locality_combines_with_tenant_rule(tmp_path, monkeypatch):
    runner = setup(tmp_path, monkeypatch, allowed_regions=["eu", "us"], objects=[{
        "bucket": "bucket", "key_prefix": "object", "allowed_regions": ["eu"],
    }])
    assert runner.evaluate_once("bucket", "object", as_of=NOW).action == "stay"
    runner.catalog.upsert("bucket", "other", 8, "hot")
    runner.catalog.assign_pool("bucket", "other", "hot-pool")
    assert runner.evaluate_once("bucket", "other", as_of=NOW).destination_tier == "warm"


@pytest.mark.parametrize("state", ["missing", "stale", "future-dated"])
def test_preview_explains_unknown_or_stale_destination_evidence(tmp_path, monkeypatch, state):
    runner = setup(tmp_path, monkeypatch, allowed_regions=["eu", "us"])
    evidence = {} if state == "missing" else {"locality_evidence": {
        "observed_at": "2026-01-01T00:00:00Z" if state == "stale" else END,
        "max_age_seconds": 86400, "source": "operator",
    }}
    runner.catalog.register_pool(
        "warm-pool", "warm", region="us", members=("warm",), metadata=evidence,
    )
    _, reason = runner.preview_decision("bucket", "object", as_of=NOW)
    assert reason["code"] == "locality_constraint"
    assert state in reason["constraints"]["locality"]["rejected"]["warm"]


def test_unused_exception_is_not_reported_as_an_executed_override(tmp_path, monkeypatch):
    from cognistore.auth.authorization import RBACAuthorizer, RBACPolicy, authorization_context
    from cognistore.auth.principal import Principal, principal_context

    runner = setup(tmp_path, monkeypatch, SimplePolicy(100))
    path = tmp_path / "locality.json"
    document = json.loads(path.read_text())
    document["tenants"]["default"]["exceptions"] = [{
        "id": "approved-transfer", "bucket": "bucket", "key": "object",
        "destination_pool_id": "warm-pool", "issuer": "issuer", "subject": "operator",
        "reason": "approved exception", "expires_at": END,
    }]
    path.write_text(json.dumps(document))
    runner.locality_exception_id = "approved-transfer"
    authorizer = RBACAuthorizer(RBACPolicy({("issuer", "operator"): ["admin"]}))
    with principal_context(Principal(issuer="issuer", subject="operator")), authorization_context(authorizer):
        _, reason = runner.preview_decision("bucket", "object", as_of=NOW)
    assert reason["disposition"] == "stay"
    assert not reason["constraints"]["locality"]["exception"]["used"]
    assert reason["constraints"]["locality"]["exception"]["bypassed_rules"] == []


def test_retry_preserves_first_policy_decision_when_only_evaluation_time_changes(tmp_path, monkeypatch):
    runner = setup(tmp_path, monkeypatch)
    runner.reevaluate_object("bucket", "object", as_of=NOW)
    runner.reevaluate_object("bucket", "object", as_of="2026-09-18T00:01:00Z")
    events = runner.catalog.list_audit_events(AuditQuery(
        event_types=frozenset({AuditEventType.POLICY_DECISION}),
    ))
    assert len(events) == 1


def profile(value):
    values = {name: "0" for name in RATE_UNITS}
    values.update(storage_price=value, storage_energy=value, carbon_intensity="1")
    return EstimationProfile("v1", "synthetic", rates={
        name: RateAssumption(
            amount, RATE_UNITS[name], "synthetic", "v1", AS_OF, END, lower=amount, upper=amount,
        )
        for name, amount in values.items()
    })


@pytest.mark.parametrize("objective", ["cost", "carbon", "latency", "locality"])
def test_locality_filters_before_conflicting_objective_optimization(tmp_path, monkeypatch, objective):
    from cognistore.core.budgets import ObjectiveWeights

    runner = setup(tmp_path, monkeypatch)
    estimator = StorageImpactEstimator({
        tier + "-pool": profile(value)
        for tier, value in (("hot", "100"), ("warm", "1"), ("cold", "10"))
    })
    weights = {name: "0" for name in ("cost", "carbon", "latency", "locality")}
    weights[objective] = "1"
    runner.policy = EstimatePolicy(
        runner.catalog, estimator,
        EstimationWorkload(
            duration_hours=730, read_requests=0, write_requests=0,
            transfer_bytes=0, retrieval_bytes=0,
        ),
        {tier: tier + "-pool" for tier in ("hot", "warm", "cold")},
        ObjectiveWeights(**weights), preferred_region="us",
    )
    record = runner.catalog.get("bucket", "object")
    assert runner.policy.evaluate_record(record, as_of=NOW).dst_tier == "warm"
    result = runner.evaluate_once("bucket", "object", as_of=NOW)
    objectives = result.constraints["objectives"]
    assert {item["tier"] for item in objectives["candidates"]} == {"hot", "cold"}
    assert objectives["selected"]["tier"] == ("hot" if objective == "locality" else "cold")
    assert runner.policy.placement_constraints is None
    simulation = simulate_policy(runner, "bucket", as_of=NOW)
    projected = simulation["objects"][0]["baseline"]
    assert any(item["kind"] == "locality" for item in projected["binding_constraints"])
    assert projected["pool_id"] == objectives["selected"]["pool_id"]


def test_explicit_policy_pool_restriction_cannot_be_relaxed_by_locality(tmp_path, monkeypatch):
    runner = setup(tmp_path, monkeypatch)
    runner.policy = EstimatePolicy(
        runner.catalog, StorageImpactEstimator({"hot-pool": profile("100"), "cold-pool": profile("1")}),
        EstimationWorkload(), {"hot": "hot-pool", "cold": "cold-pool"},
        placement_constraints=PlacementConstraints(allowed_pools=("hot-pool",)),
    )
    result = runner.evaluate_once("bucket", "object", as_of=NOW)
    assert result.action == "stay"
    assert [item["pool_id"] for item in result.constraints["objectives"]["candidates"]] == ["hot-pool"]


def test_locality_pool_binding_is_carried_into_the_move_plan(tmp_path, monkeypatch):
    runner = setup(tmp_path, monkeypatch, allowed_regions=["eu", "us"])
    actions = runner.plan_once("bucket", dry_run=True, as_of=NOW)
    assert len(actions) == 1
    assert actions[0].destination_pool_id == "warm-pool"
    runner.execute(actions[0])
    assert runner.catalog.get("bucket", "object").pool_id == "warm-pool"
