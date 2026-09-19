"""Audit disclosure is bounded, tenant-owned, and recorded before returning evidence."""

from __future__ import annotations

from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.auth.authorization import RBACAuthorizer, RBACPolicy
from cognistore.auth.principal import Principal
from cognistore.auth.tenancy import TenantResolver
from cognistore.core.audit import AuditContext, AuditEvent, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.db import SQLCatalog
from tests.unit.test_api_authentication import ISSUER
from tests.unit.test_api_authentication import identity_provider as identity_provider


@pytest.fixture(params=["memory", "sqlite"])
def audit_catalog(request, tmp_path):
    catalog = Catalog() if request.param == "memory" else SQLCatalog(tmp_path / "audit.db")
    yield catalog
    if request.param == "sqlite":
        catalog.close()


def client_for(catalog, identity_provider, *, role="auditor"):
    authenticator, issue_token, _ = identity_provider
    authorization = RBACAuthorizer(RBACPolicy.from_dict({"bindings": [
        {"issuer": ISSUER, "subject": subject, "roles": [role]} for subject in ("alice", "bob")
    ]}))
    tenancy = TenantResolver({(ISSUER, "alice"): "alpha", (ISSUER, "bob"): "beta"})
    app = create_app(CogniStoreGateway(catalog, {}), authentication=authenticator,
                     authorization=authorization, tenancy=tenancy)
    return TestClient(app, headers={"Authorization": "Bearer " + issue_token()})


def seed(catalog, owner, *, count=1):
    events = []
    for index in range(count):
        events.append(catalog.for_tenant(owner).append_audit_event(AuditEvent.create(
            "policy.decision", "succeeded", AuditContext("seed", "system", owner),
            occurred_at="2026-01-01T00:00:00Z",
            details={"owner": owner, "index": index},
        )))
    return events


def test_audit_list_get_are_tenant_scoped_and_self_audited(audit_catalog, identity_provider):
    first = seed(audit_catalog, "alpha", count=3)
    second = seed(audit_catalog, "beta")
    with client_for(audit_catalog, identity_provider) as client:
        response = client.get("/v1/audit/events", params={"event_type": "policy.decision", "limit": 2},
                              headers={"X-Request-ID": "audit-list"})
        assert response.status_code == 200, response.text
        page = response.json()
        assert len(page["items"]) == 2
        assert all(event["details"]["owner"] == "alpha" for event in page["items"])
        remaining = client.get("/v1/audit/events", params={
            "event_type": "policy.decision", "limit": 2, "cursor": page["page"]["next_cursor"],
        })
        assert remaining.status_code == 200, remaining.text
        assert len(remaining.json()["items"]) == 1
        assert remaining.json()["page"]["next_cursor"] is None
        assert {event["event_id"] for event in page["items"] + remaining.json()["items"]} == {
            event.event_id for event in first
        }
        assert client.get("/v1/audit/events/" + first[0].event_id).json() == asdict(first[0])
        assert client.get("/v1/audit/events/" + second[0].event_id).status_code == 404
        _, issue_token, _ = identity_provider
        foreign_cursor = client.get("/v1/audit/events", params={
            "event_type": "policy.decision", "cursor": page["page"]["next_cursor"],
        }, headers={"Authorization": "Bearer " + issue_token("bob")})
        assert foreign_cursor.status_code == 422
    access = audit_catalog.for_tenant("alpha").list_audit_events(AuditQuery(event_types={"audit.access"}))
    assert len(access) == 4
    assert {event.actor_id for event in access} == {Principal(ISSUER, "alice").actor_id}
    assert any(event.correlation_id == "audit-list" and event.details["returned"] == 2 for event in access)
    assert all("beta" not in repr(event.details) for event in access)
    correlated = audit_catalog.for_tenant("alpha").list_audit_events(AuditQuery(correlation_id="audit-list"))
    assert {event.event_type for event in correlated} == {"authorization.decision", "audit.access"}
    assert len(correlated) == 3


def test_export_pagination_has_fixed_checkpoint_and_verifies_independently(audit_catalog, identity_provider):
    first = seed(audit_catalog, "alpha", count=3)
    audit_catalog.for_tenant("alpha").append_audit_event(AuditEvent.create(
        "move.prepared", "started", AuditContext("move-export", "system", "export-test"),
        move_id="audit-export-move",
    ))
    seed(audit_catalog, "beta")
    with client_for(audit_catalog, identity_provider) as client:
        response = client.get("/v1/audit/export", params={"limit": 2},
                              headers={"X-Request-ID": "audit-export"})
        assert response.status_code == 200, response.text
        page = response.json()
        anchor, records = page["checkpoint"], page["records"]
        assert anchor["tenant_id"] == "alpha"
        assert not page["complete"] and page["page"]["next_cursor"]
        while not page["complete"]:
            response = client.get("/v1/audit/export", params={
                "limit": 2, "cursor": page["page"]["next_cursor"],
            })
            assert response.status_code == 200, response.text
            page = response.json()
            assert page["checkpoint"] == anchor
            records.extend(page["records"])
            assert len(records) <= anchor["sequence"]
        assert page["page"]["next_cursor"] is None
        assert len(records) == anchor["sequence"]
        assert records[-1]["entry_hash"] == anchor["entry_hash"]
        assert {event.event_id for event in first} <= {record["event_id"] for record in records}
        assert all(record["event"]["details"].get("owner") != "beta" for record in records)
        # Export includes actual retained payloads, not only opaque hash links.
        from cognistore.core.audit_integrity import event_digest, make_entry
        previous = "0" * 64
        for record in records:
            assert record["previous_hash"] == previous
            assert event_digest(AuditEvent(**record["event"])) == record["payload_digest"]
            expected = make_entry("alpha", record["sequence"], previous, kind=record["kind"],
                                  event_id=record["event_id"], payload_digest=record["payload_digest"],
                                  recorded_at=record["recorded_at"], move_id=record["move_id"],
                                  move_sequence=record["move_sequence"])
            assert expected["entry_hash"] == record["entry_hash"]
            previous = record["entry_hash"]
        move_record = next(record for record in records if record["move_id"] == "audit-export-move")
        assert move_record["move_sequence"] == 1
        verified = client.post("/v1/audit/verify", json={"checkpoint": anchor},
                               headers={"X-Request-ID": "audit-verify"})
        assert verified.status_code == 200, verified.text
        assert verified.json()["valid"] and verified.json()["anchored"]
        assert verified.json()["checkpoint"]["sequence"] > anchor["sequence"]
        _, issue_token, _ = identity_provider
        assert client.post("/v1/audit/verify", json={"checkpoint": anchor}, headers={
            "Authorization": "Bearer " + issue_token("bob"),
        }).status_code == 422
    events = audit_catalog.for_tenant("alpha").list_audit_events(AuditQuery(
        event_types={"audit.export", "audit.verification"},
    ))
    assert any(event.correlation_id == "audit-export" for event in events)
    assert any(event.correlation_id == "audit-verify" for event in events)
    assert {event.actor_id for event in events} == {Principal(ISSUER, "alice").actor_id}
    correlated = audit_catalog.for_tenant("alpha").list_audit_events(AuditQuery(correlation_id="audit-export"))
    assert {event.event_type for event in correlated} == {"authorization.decision", "audit.export"}
    assert len(correlated) == 3


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/v1/audit/events", {}),
    ("GET", "/v1/audit/events/11111111-1111-4111-8111-111111111111", {}),
    ("GET", "/v1/audit/export", {}),
    ("POST", "/v1/audit/verify", {"json": {}}),
])
def test_reader_denials_are_audited_before_disclosure(identity_provider, method, path, payload):
    catalog = Catalog()
    seed(catalog, "alpha")
    with client_for(catalog, identity_provider, role="reader") as client:
        response = client.request(method, path, **payload, headers={"X-Request-ID": "denied-audit"})
        assert response.status_code == 403
        assert "owner" not in response.text
    denied = catalog.for_tenant("alpha").list_audit_events(AuditQuery(outcomes={"denied"}))
    assert len(denied) == 1
    assert denied[0].correlation_id == "denied-audit"
    assert denied[0].actor_id == Principal(ISSUER, "alice").actor_id
    assert denied[0].details["required_permissions"] == ["audit"]


@pytest.mark.parametrize("path", ["/v1/audit/events", "/v1/audit/export"])
def test_unconfigured_authentication_cannot_disclose_audits(path):
    catalog = Catalog()
    with TestClient(create_app(CogniStoreGateway(catalog, {}))) as client:
        response = client.get(path)
        assert response.status_code == 401
    denied = catalog.list_audit_events(AuditQuery(outcomes={"denied"}))
    assert len(denied) == 1 and denied[0].actor_type == "anonymous"


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/v1/audit/events", {}),
    ("GET", "/v1/audit/export", {}),
    ("POST", "/v1/audit/verify", {"json": {}}),
])
def test_self_audit_failure_closes_disclosure(identity_provider, monkeypatch, method, path, payload):
    catalog = Catalog()
    tenant = catalog.for_tenant("alpha")
    append = tenant.append_audit_event

    def unavailable(event):
        if event.event_type.startswith("audit."):
            raise RuntimeError("private database password")
        return append(event)

    monkeypatch.setattr(tenant, "append_audit_event", unavailable)
    with client_for(catalog, identity_provider) as client:
        response = client.request(method, path, **payload)
        assert response.status_code == 503, response.text
        assert "password" not in response.text
        assert "records" not in response.text and "items" not in response.text


def test_audit_query_and_body_bounds(identity_provider):
    with client_for(Catalog(), identity_provider) as client:
        for params in ({"limit": 0}, {"limit": 201}, {"cursor": "bad"},
                       {"key": "missing-bucket"}, {"occurred_after": "bad"}):
            assert client.get("/v1/audit/events", params=params).status_code == 422
        assert client.get("/v1/audit/export", params={"limit": 201}).status_code == 422
        assert client.get("/v1/audit/export", params={"cursor": "bad"}).status_code == 422
        assert client.post("/v1/audit/verify", content=b"x" * 300_000).status_code == 413


def test_checkpoint_mismatch_is_returned_and_failure_is_audited(identity_provider):
    catalog = Catalog()
    seed(catalog, "alpha")
    checkpoint = asdict(catalog.for_tenant("alpha").audit_checkpoint())
    checkpoint["entry_hash"] = "f" * 64
    with client_for(catalog, identity_provider) as client:
        response = client.post("/v1/audit/verify", json={"checkpoint": checkpoint},
                               headers={"X-Request-ID": "bad-checkpoint"})
        assert response.status_code == 200, response.text
        assert response.json()["valid"] is False
        assert "checkpoint_mismatch" in response.json()["issues"]
    events = catalog.for_tenant("alpha").list_audit_events(AuditQuery(event_types={"audit.verification"}))
    assert len(events) == 1 and events[0].outcome == "failed"
    assert events[0].correlation_id == "bad-checkpoint"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer private-invalid-token"}])
def test_all_versioned_authentication_denials_are_recorded_without_credentials(identity_provider, headers):
    catalog = Catalog()
    with client_for(catalog, identity_provider) as client:
        client.headers.pop("Authorization")
        response = client.get("/v1/catalog/objects/private-bucket/private-key",
                              headers={**headers, "X-Request-ID": "auth-denial"})
        assert response.status_code == 401
    events = catalog.list_audit_events(AuditQuery(outcomes={"denied"}))
    assert len(events) == 1
    assert events[0].actor_type == "anonymous" and events[0].correlation_id == "auth-denial"
    assert events[0].details["operation"] == "get_catalog_object"
    assert "private" not in repr(events)
