import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from cognistore.auth.authorization import RBACAuthorizer, RBACPolicy, authorization_context
from cognistore.auth.principal import Principal, principal_context
from cognistore.core.catalog import ObjectRecord
from cognistore.core.locality import (
    LocalityConstraintError,
    assert_locality_allowed,
    evaluate_locality,
)
from cognistore.core.topology import Pool, Tier

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
TIERS = [Tier("hot"), Tier("warm"), Tier("cold")]
PRINCIPAL = Principal("https://identity.example.test", "operator")


def pool(pool_id="hot-eu", tier="hot", region="eu-west-1", **changes):
    values = {
        "pool_id": pool_id, "tier": tier, "region": region, "members": ("storage-1",),
        "localities": ("EU", "regulated"),
        "metadata": {"locality_evidence": {
            "observed_at": NOW.isoformat(), "max_age_seconds": 60, "source": "operator-inventory",
        }},
    }
    return Pool(**(values | changes))


def record(**changes):
    return ObjectRecord(**({"bucket": "records", "key": "patient/one", "size": 42,
                           "tier": "hot"} | changes))


def tenant(**changes):
    return {
        "allowed_regions": ["eu-west-1"], "required_localities": ["EU"],
        "tier_pools": {"hot": "hot-eu", "warm": "warm-eu", "cold": "cold-us"},
    } | changes


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    path = tmp_path / "locality.json"
    monkeypatch.setenv("COGNISTORE_LOCALITY_CONFIG", str(path))

    def write(value=None, *, tenants=None):
        path.write_text(json.dumps({"version": 1, "tenants": tenants if tenants is not None else {
            "default": tenant() if value is None else value,
        }}))
        return path

    write()
    return write


def evaluate(source=None, *, pools=None, **changes):
    arguments = {
        "tenant_id": "default", "tiers": TIERS, "as_of": NOW,
        "pools": [pool(), pool("warm-eu", "warm"), pool("cold-us", "cold", "us-east-1")]
        if pools is None else pools,
    }
    return evaluate_locality(record() if source is None else source, **(arguments | changes))


def approval(**changes):
    return {
        "id": "case-64", "bucket": "records", "key": "patient/one",
        "destination_pool_id": "cold-us", "issuer": PRINCIPAL.issuer,
        "subject": PRINCIPAL.subject, "reason": "Approved emergency recovery",
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    } | changes


def test_unconfigured_policy_preserves_legacy_without_topology(monkeypatch):
    monkeypatch.delenv("COGNISTORE_LOCALITY_CONFIG", raising=False)
    evidence = assert_locality_allowed(
        record(), "unregistered", tenant_id="default", tiers=[], pools=[], as_of=NOW,
    )
    assert evidence["configured"] is False
    denied = evaluate_locality(
        record(), tenant_id="default", tiers=[], pools=[], as_of=NOW, exception_id="case-64",
    )
    assert denied["configured"] is True
    assert denied["allowed_destination_tiers"] == []
    with pytest.raises(LocalityConstraintError, match="requires configured"):
        assert_locality_allowed(
            record(), "unregistered", tenant_id="default", tiers=[], pools=[], as_of=NOW,
            exception_id="case-64",
        )


def test_conjunctive_tenant_and_all_matching_object_rules(policy_file):
    policy_file(tenant(objects=[
        {"bucket": "records", "key_prefix": "", "required_localities": ["regulated"]},
        {"bucket": "records", "key_prefix": "patient/", "allowed_regions": ["eu-west-1"]},
        {"bucket": "other", "key_prefix": "", "allowed_regions": []},
        {"bucket": "records", "key_prefix": "financial/", "allowed_regions": []},
    ]))
    evidence = evaluate(pools=[pool(), pool("warm-eu", "warm", localities=("EU",)),
                               pool("cold-us", "cold", "us-east-1")])
    assert evidence["allowed_destination_tiers"] == ["hot"]
    assert evidence["allowed_pool_ids"] == ["hot-eu"]
    assert len(evidence["rules"]) == 3
    assert "required locality" in evidence["rejected"]["warm"]
    assert "region is forbidden" in evidence["rejected"]["cold"]


def test_object_rule_cannot_relax_tenant_policy_or_use_caller_metadata(policy_file):
    policy_file(tenant(objects=[{
        "bucket": "records", "key_prefix": "patient/", "allowed_regions": ["us-east-1"],
    }]))
    evidence = evaluate(record(metadata={"allowed_regions": ["us-east-1"],
                                         "locality_exception": approval()}))
    assert evidence["allowed_destination_tiers"] == []
    assert "tenant rule" in evidence["rejected"]["cold"]
    assert "object rule" in evidence["rejected"]["hot"]


def test_empty_regions_deny_and_null_regions_are_unrestricted(policy_file):
    policy_file(tenant(allowed_regions=[]))
    assert evaluate()["allowed_destination_tiers"] == []
    policy_file(tenant(allowed_regions=None, required_localities=[]))
    assert evaluate()["allowed_destination_tiers"] == ["cold", "hot", "warm"]


def test_tenant_policy_is_scoped_and_reloaded(policy_file):
    path = policy_file(tenants={"default": tenant(), "other": tenant(allowed_regions=["us-east-1"])})
    assert evaluate()["allowed_destination_tiers"] == ["hot", "warm"]
    assert evaluate(tenant_id="other")["allowed_destination_tiers"] == ["cold"]
    assert evaluate(tenant_id="missing")["allowed_destination_tiers"] == []
    policy_file(tenant(allowed_regions=[]))
    assert evaluate()["allowed_destination_tiers"] == []
    path.unlink()
    assert evaluate()["configuration_error"] is True


@pytest.mark.parametrize("raw", [
    "{", "null", "[]", '{"version":1,"version":1,"tenants":{}}',
    '{"version":true,"tenants":{}}', '{"version":2,"tenants":{}}',
    '{"version":1,"tenants":[],"extra":1}',
    '{"version":1,"tenants":{"default":{"tier_pools":{"hot":"one","hot":"two"}}}}',
    " " * (1024 * 1024 + 1),
], ids=["syntax", "null", "list", "duplicate-key", "boolean-version", "unknown-version",
        "unknown-field", "duplicate-binding", "oversized"])
def test_unreadable_or_malformed_config_fails_closed(policy_file, raw):
    path = policy_file()
    path.write_text(raw)
    evidence = evaluate()
    assert evidence["configured"] is True
    assert evidence["allowed_destination_tiers"] == []
    assert evidence["configuration_error"] is True


@pytest.mark.parametrize("change", [
    {"unknown": True}, {"tier_pools": []}, {"allowed_regions": "eu-west-1"},
    {"allowed_regions": ["eu-west-1", "eu-west-1"]}, {"required_localities": [1]},
    {"objects": [{"bucket": "records", "key_prefix": "", "unknown": 1}]},
    {"objects": [{"bucket": "records", "key": "patient/one"}]},
    {"exceptions": [approval(), approval()]},
    {"exceptions": [approval(expires_at="2026-09-20T00:00:00")]},
    {"exceptions": [approval(reason="")]},
    {"exceptions": [approval(subject="line\nbreak")]},
])
def test_strict_tenant_schema_fails_closed(policy_file, change):
    policy_file(tenant(**change))
    assert evaluate()["configuration_error"] is True


@pytest.mark.parametrize("metadata,reason", [
    ({}, "missing"),
    ({"locality_evidence": {}}, "malformed"),
    ({"locality_evidence": {"observed_at": NOW.isoformat(), "max_age_seconds": True,
                             "source": "inventory"}}, "malformed"),
    ({"locality_evidence": {"observed_at": NOW.isoformat(), "max_age_seconds": float("inf"),
                             "source": "inventory"}}, "malformed"),
    ({"locality_evidence": {"observed_at": NOW.isoformat(), "max_age_seconds": 60,
                             "source": ""}}, "malformed"),
    ({"locality_evidence": {"observed_at": "2026-09-18T12:00:00", "max_age_seconds": 60,
                             "source": "inventory"}}, "malformed"),
    ({"locality_evidence": {"observed_at": (NOW - timedelta(seconds=61)).isoformat(),
                             "max_age_seconds": 60, "source": "inventory"}}, "stale"),
    ({"locality_evidence": {"observed_at": (NOW + timedelta(microseconds=1)).isoformat(),
                             "max_age_seconds": 60, "source": "inventory"}}, "future-dated"),
])
def test_region_evidence_must_be_current_and_valid(policy_file, metadata, reason):
    evidence = evaluate(pools=[pool(metadata=metadata)])
    assert evidence["allowed_destination_tiers"] == []
    assert reason in evidence["rejected"]["hot"]


def test_evidence_freshness_boundary_and_optimization_attributes_independent(policy_file):
    assert evaluate(as_of=NOW + timedelta(seconds=60))["allowed_destination_tiers"] == ["hot", "warm"]
    assert evaluate(as_of=NOW + timedelta(seconds=60, microseconds=1))["allowed_destination_tiers"] == []


def test_missing_inactive_and_unbound_topology_rejected(policy_file):
    policy_file(tenant(tier_pools={"hot": "hot-eu", "warm": "warm-eu"}))
    evidence = evaluate(pools=[pool(active=False)])
    assert "inactive" in evidence["rejected"]["hot"]
    assert "missing" in evidence["rejected"]["warm"]
    assert "no configured physical" in evidence["rejected"]["cold"]
    evidence = evaluate(tiers=[Tier("hot", active=False)], pools=[pool()])
    assert "tier is missing or inactive" in evidence["rejected"]["hot"]
    assert "tier is missing or inactive" in evidence["rejected"]["warm"]
    assert evaluate(pools=[pool(), pool()])["reason"] == "destination topology is ambiguous"
    evidence = evaluate(pools=[pool(tier="cold")])
    assert "another tier" in evidence["rejected"]["hot"]


def test_physical_selection_must_agree_with_server_binding(policy_file):
    with pytest.raises(LocalityConstraintError, match="binding changed"):
        assert_locality_allowed(
            record(), "hot", tenant_id="default", tiers=TIERS, pools=[pool()], as_of=NOW,
            destination_pool_id="different-pool",
        )
    evidence = assert_locality_allowed(
        record(), "hot", tenant_id="default", tiers=TIERS, pools=[pool()], as_of=NOW,
        destination_pool_id="hot-eu",
    )
    assert evidence["tier_pools"]["hot"] == "hot-eu"


def test_exception_is_explicit_exact_and_only_relaxes_geography(policy_file):
    policy_file(tenant(exceptions=[approval()]))
    authorizer = RBACAuthorizer(RBACPolicy({(PRINCIPAL.issuer, PRINCIPAL.subject): ["operator"]}))
    with principal_context(PRINCIPAL), authorization_context(authorizer):
        assert evaluate()["allowed_destination_tiers"] == ["hot", "warm"]
        evidence = evaluate(exception_id="case-64")
        assert evidence["allowed_destination_tiers"] == ["cold", "hot", "warm"]
        assert evidence["exception"]["used"] is True
        assert evidence["exception"]["bypassed_rules"]
        assert evidence["exception"]["reason"] == approval()["reason"]
        assert evidence["exception"]["actor_id"] == PRINCIPAL.actor_id
        assert "issuer" not in evidence["exception"]
        assert "subject" not in evidence["exception"]
        assert PRINCIPAL.issuer not in json.dumps(evidence)
        assert PRINCIPAL.subject not in evidence["exception"].values()
        evidence = evaluate(exception_id="case-64", pools=[pool("cold-us", "cold", "us-east-1",
                                                             metadata={})])
        assert evidence["allowed_destination_tiers"] == []
        assert evidence["exception"]["used"] is False
        assert "missing" in evidence["rejected"]["cold"]
        assert evaluate(record(key="patient/two"), exception_id="case-64")["allowed_destination_tiers"] == []
        assert evaluate(exception_id="case-64", as_of=NOW + timedelta(minutes=5))["allowed_destination_tiers"] == []
        assert evaluate(exception_id="unknown")["allowed_destination_tiers"] == []


@pytest.mark.parametrize("identity,roles", [
    (None, ["admin"]), (PRINCIPAL, []), (PRINCIPAL, ["reader"]),
    (Principal("https://other.example.test", PRINCIPAL.subject), ["operator"]),
])
def test_exception_requires_current_issuer_scoped_administrative_grants(policy_file, identity, roles):
    policy_file(tenant(exceptions=[approval()]))
    authorizer = RBACAuthorizer(RBACPolicy({(PRINCIPAL.issuer, PRINCIPAL.subject): roles}))
    with principal_context(identity), authorization_context(authorizer):
        assert evaluate(exception_id="case-64")["allowed_destination_tiers"] == []


def test_exception_rechecks_file_grants_and_approval_revocation(policy_file, tmp_path):
    policy_file(tenant(exceptions=[approval()]))
    grants = tmp_path / "rbac.json"
    grants.write_text(json.dumps({"bindings": [{"issuer": PRINCIPAL.issuer,
                                                "subject": PRINCIPAL.subject,
                                                "roles": ["operator"]}]}))
    authorizer = RBACAuthorizer(policy_path=grants)
    with principal_context(PRINCIPAL), authorization_context(authorizer):
        assert "cold" in evaluate(exception_id="case-64")["allowed_destination_tiers"]
        grants.write_text('{"bindings":[]}')
        assert evaluate(exception_id="case-64")["allowed_destination_tiers"] == []
    authorizer = RBACAuthorizer(RBACPolicy({(PRINCIPAL.issuer, PRINCIPAL.subject): ["operator"]}))
    with principal_context(PRINCIPAL), authorization_context(authorizer):
        policy_file(tenant(exceptions=[]))
        assert evaluate(exception_id="case-64")["allowed_destination_tiers"] == []


def test_exception_unavailable_without_authorizer_and_evidence_is_detached(policy_file):
    policy_file(tenant(exceptions=[approval()]))
    with principal_context(PRINCIPAL), authorization_context(None):
        assert evaluate(exception_id="case-64")["allowed_destination_tiers"] == []
    evidence = evaluate()
    evidence["rules"][0]["allowed_regions"].clear()
    assert evaluate()["allowed_destination_tiers"] == ["hot", "warm"]


def test_assert_marks_exception_unused_for_another_eligible_destination(policy_file):
    policy_file(tenant(exceptions=[approval()]))
    authorizer = RBACAuthorizer(RBACPolicy({(PRINCIPAL.issuer, PRINCIPAL.subject): ["operator"]}))
    with principal_context(PRINCIPAL), authorization_context(authorizer):
        evidence = assert_locality_allowed(
            record(), "hot", tenant_id="default", tiers=TIERS,
            pools=[pool(), pool("cold-us", "cold", "us-east-1")], as_of=NOW,
            exception_id="case-64",
        )
    assert evidence["exception"]["used"] is False
    assert evidence["exception"]["bypassed_rules"] == []


def test_exception_cannot_authorize_a_different_forbidden_pool(policy_file):
    policy_file(tenant(exceptions=[approval()]))
    authorizer = RBACAuthorizer(RBACPolicy({(PRINCIPAL.issuer, PRINCIPAL.subject): ["operator"]}))
    with principal_context(PRINCIPAL), authorization_context(authorizer):
        evidence = evaluate(exception_id="case-64", pools=[pool(), pool("cold-us", "cold", "us-east-1"),
                                                           pool("warm-eu", "warm", "us-east-1")])
        assert evidence["allowed_destination_tiers"] == ["cold", "hot"]
        assert "warm" in evidence["rejected"]


def test_missing_source_region_does_not_block_remediation_to_known_destination(policy_file):
    evidence = evaluate(replace(record(), pool_id=None))
    assert evidence["allowed_destination_tiers"] == ["hot", "warm"]
