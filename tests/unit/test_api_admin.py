from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.auth.authorization import RBACAuthorizer, RBACPolicy
from cognistore.auth.tenancy import TenantResolver, tenant_context
from cognistore.core.audit import AuditContext, AuditEvent, AuditEventType, AuditOutcome
from cognistore.core.catalog import Catalog
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver
from tests.unit.test_api_authentication import ISSUER
from tests.unit.test_api_authentication import identity_provider as identity_provider


def _authorizer(role="admin"):
    return RBACAuthorizer(RBACPolicy({(ISSUER, "alice"): [role], (ISSUER, "bob"): [role]}))


@pytest.mark.parametrize("role,allowed,denied", [
    ("reader", "list_jobs", "get_admin_storage"),
    ("auditor", "list_audit_events", "list_jobs"),
    ("operator", "get_admin_storage", "submit_policy_run"),
    ("policy_manager", "preview_policy", "submit_policy_run"),
])
def test_session_discovers_only_current_grants(identity_provider, role, allowed, denied):
    auth, token, _ = identity_provider
    tenancy = TenantResolver({(ISSUER, "alice"): "tenant-a"})
    gateway = CogniStoreGateway(Catalog(), {})
    with TestClient(create_app(gateway, authentication=auth, authorization=_authorizer(role),
                               tenancy=tenancy)) as client:
        response = client.get("/v1/admin/session", headers={
            "Authorization": "Bearer " + token(roles=["admin"]), "X-Tenant-ID": "other",
        })
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    session = response.json()
    assert session["tenant_id"] == "tenant-a"
    assert session["actor_id"]
    assert allowed in session["operations"]
    assert denied not in session["operations"]


def test_session_reload_revokes_grants_and_denies_malformed_policy(identity_provider, tmp_path):
    auth, token, _ = identity_provider
    policy = tmp_path / "rbac.json"
    policy.write_text(json.dumps({"bindings": [{"issuer": ISSUER, "subject": "alice", "roles": ["admin"]}]}))
    app = create_app(CogniStoreGateway(Catalog(), {}), authentication=auth,
                     authorization=RBACAuthorizer(policy_path=policy))
    with TestClient(app, headers={"Authorization": "Bearer " + token()}) as client:
        assert "submit_repair" in client.get("/v1/admin/session").json()["operations"]
        policy.write_text('{"bindings": []}')
        assert client.get("/v1/admin/session").status_code == 403
        policy.write_text("malformed")
        assert client.get("/v1/admin/session").status_code == 403


@pytest.mark.parametrize("service_role,http_role", [("admin", "reader"), ("reader", "admin")])
def test_session_intersects_transport_and_service_authorizers(identity_provider, service_role, http_role):
    auth, token, _ = identity_provider
    app = create_app(
        CogniStoreGateway(Catalog(), {}, authorization=_authorizer(service_role)),
        authentication=auth, authorization=_authorizer(http_role),
    )
    with TestClient(app, headers={"Authorization": "Bearer " + token()}) as client:
        session = client.get("/v1/admin/session")
        assert session.status_code == 200
        assert "list_jobs" in session.json()["operations"]
        assert "get_admin_storage" not in session.json()["operations"]
        assert client.get("/v1/admin/storage").status_code == 403


@pytest.mark.parametrize("path", ["/v1/admin/session", "/v1/admin/storage", "/v1/jobs", "/v1/admin/repairs"])
def test_operational_views_require_authentication_even_in_local_mode(path):
    with TestClient(create_app(CogniStoreGateway(Catalog(), {}))) as client:
        response = client.get(path)
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"


def test_storage_projection_omits_secrets_and_handles_partial_failure(identity_provider, tmp_path):
    auth, token, _ = identity_provider
    catalog = Catalog()
    catalog.register_tier("hot", metadata={"secret": "hidden-tier-secret"})
    catalog.register_pool("pool-a", "hot", region="region-a", members=("hidden-member-endpoint",),
                          metadata={"secret": "hidden-pool-secret"})
    driver = PosixDriver(str(tmp_path / "private-root"))
    driver.encryption_status = lambda: {"source": "provider", "mode": "provider-default",
                                        "key_configured": True, "key_id": "hidden-key"}
    failed = PosixDriver(str(tmp_path / "failed"))

    def fail():
        raise RuntimeError("hidden-backend-diagnostic")

    failed.encryption_status = fail
    with TestClient(create_app(CogniStoreGateway(catalog, {"hot": driver, "cold": failed}),
                               authentication=auth, authorization=_authorizer())) as client:
        response = client.get("/v1/admin/storage", headers={"Authorization": "Bearer " + token()})
    assert response.status_code == 200, response.text
    for secret in ("hidden-", "private-root", str(tmp_path)):
        assert secret not in response.text
    body = response.json()
    assert body["catalog"]["status"] == "ready"
    assert body["queue"]["status"] == "not_configured"
    by_name = {tier["name"]: tier for tier in body["tiers"]}
    assert by_name["hot"]["health"]["status"] == "unverified"
    assert by_name["cold"]["health"]["status"] == "unavailable"
    assert by_name["hot"]["pools"][0]["member_count"] == 1
    assert by_name["hot"]["encryption"]["key_configured"] is True


def _queued(catalog, tenant, day, job_type="catalog.scan", job_id=None):
    job_id = job_id or str(uuid4())
    with tenant_context(tenant):
        catalog.for_tenant(tenant).append_audit_event(AuditEvent.create(
            AuditEventType.JOB_QUEUED, AuditOutcome.REQUESTED,
            AuditContext(str(uuid4()), "api", "worker", job_id=job_id),
            occurred_at=f"2026-09-{day:02d}T00:00:00Z", details={"job_type": job_type},
        ))
    return job_id


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_job_history_is_paginated_and_tenant_bound(identity_provider, backend, tmp_path):
    auth, token, _ = identity_provider
    catalog = Catalog() if backend == "memory" else SQLCatalog(tmp_path / "catalog.db")
    a1 = _queued(catalog, "tenant-a", 1)
    a2 = _queued(catalog, "tenant-a", 2)
    b1 = _queued(catalog, "tenant-b", 3)
    tenancy = TenantResolver({(ISSUER, "alice"): "tenant-a", (ISSUER, "bob"): "tenant-b"})
    app = create_app(CogniStoreGateway(catalog, {}), authentication=auth,
                     authorization=_authorizer("reader"), tenancy=tenancy)
    alice = {"Authorization": "Bearer " + token()}
    bob = {"Authorization": "Bearer " + token(subject="bob")}
    with TestClient(app) as client:
        first = client.get("/v1/jobs?limit=1", headers=alice)
        assert first.status_code == 200, first.text
        assert [item["job_id"] for item in first.json()["items"]] == [a2]
        cursor = first.json()["page"]["next_cursor"]
        second = client.get("/v1/jobs", params={"cursor": cursor}, headers=alice)
        assert [item["job_id"] for item in second.json()["items"]] == [a1]
        assert second.json()["page"]["next_cursor"] is None
        assert client.get("/v1/jobs", params={"cursor": cursor}, headers=bob).status_code == 422
        assert [item["job_id"] for item in client.get("/v1/jobs", headers=bob).json()["items"]] == [b1]
        assert client.get(f"/v1/jobs/{b1}", headers=alice).status_code == 404
        for params in ({"limit": 0}, {"limit": 201}, {"cursor": "bad"}):
            assert client.get("/v1/jobs", params=params, headers=alice).status_code == 422
    if isinstance(catalog, SQLCatalog):
        catalog.close()


def test_job_history_keeps_healthy_rows_when_one_status_is_unavailable(identity_provider):
    auth, token, _ = identity_provider
    catalog = Catalog()
    healthy = _queued(catalog, "default", 1)
    _queued(catalog, "default", 2, job_id=healthy)
    broken = _queued(catalog, "default", 3, job_type="unsupported.secret-diagnostic")
    app = create_app(CogniStoreGateway(catalog, {}), authentication=auth,
                     authorization=_authorizer("reader"))
    with TestClient(app, headers={"Authorization": "Bearer " + token()}) as client:
        response = client.get("/v1/jobs")
        assert response.status_code == 200
        assert [item["job_id"] for item in response.json()["items"]] == [healthy]
        assert response.json()["errors"] == [{"job_id": broken, "message": "Job status unavailable"}]
        assert "secret-diagnostic" not in response.text
        first = client.get("/v1/jobs?limit=1").json()
        assert first["items"] == []
        assert first["page"]["next_cursor"]
        second = client.get("/v1/jobs", params={"cursor": first["page"]["next_cursor"]}).json()
        assert [item["job_id"] for item in second["items"]] == [healthy]
