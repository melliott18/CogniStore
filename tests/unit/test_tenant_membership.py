from __future__ import annotations

import json

import pytest

from cognistore.auth.principal import Principal
from cognistore.auth.tenancy import (
    TenantIsolationError,
    TenantResolver,
    current_tenant_id,
    require_tenant,
    tenant_context,
    tenant_namespace,
    validate_tenant_id,
)


def test_membership_uses_issuer_and_subject_and_reloads_revocations(tmp_path):
    path = tmp_path / "tenants.json"
    policy = {"bindings": [{"issuer": "issuer-a", "subject": "alice", "tenant_id": "alpha"}]}
    path.write_text(json.dumps(policy))
    resolver = TenantResolver(policy_path=path)
    assert resolver.resolve(Principal("issuer-a", "alice")) == "alpha"
    for principal in (None, Principal("issuer-b", "alice"), Principal("issuer-a", "bob")):
        with pytest.raises(TenantIsolationError):
            resolver.resolve(principal)
    path.write_text('{"bindings": []}')
    with pytest.raises(TenantIsolationError):
        resolver.resolve(Principal("issuer-a", "alice"))
    path.write_text("{malformed")
    with pytest.raises(TenantIsolationError):
        resolver.resolve(Principal("issuer-a", "alice"))
    path.unlink()
    with pytest.raises(TenantIsolationError):
        resolver.resolve(Principal("issuer-a", "alice"))


@pytest.mark.parametrize("value", [None, "", "../alpha", "/alpha", "alpha/beta", "a\\b", "a\x00b", "a" * 129, " alpha", "é"])
def test_tenant_identifiers_are_bounded_safe_and_exact(value):
    with pytest.raises(ValueError):
        validate_tenant_id(value)


def test_tenant_policy_rejects_duplicates_and_unknown_fields(tmp_path):
    binding = {"issuer": "issuer", "subject": "alice", "tenant_id": "alpha"}
    for policy in ({"bindings": [binding, binding]}, {"bindings": [{**binding, "roles": ["admin"]}]}, {"bindings": [], "extra": True}):
        with pytest.raises(ValueError):
            TenantResolver.from_dict(policy)
    path = tmp_path / "tenants.json"
    path.write_text('{"bindings": [], "bindings": []}')
    with pytest.raises(ValueError):
        TenantResolver(policy_path=path)


def test_context_guards_and_restores_after_failure():
    assert current_tenant_id() is None
    with pytest.raises(TenantIsolationError), tenant_context("alpha"):
        require_tenant("alpha")
        require_tenant("beta")
    assert current_tenant_id() is None
    assert tenant_namespace("Alpha") != tenant_namespace("alpha")
