"""Authenticated hold lifecycle and byte-preserving enforcement at HTTP boundaries."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cognistore.api.app import MAX_JSON_BODY_BYTES, create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.api.models import LegalHoldReleaseRequest, LegalHoldRequest
from cognistore.auth.authorization import AuthorizationError, RBACAuthorizer, RBACPolicy
from cognistore.auth.principal import Principal
from cognistore.auth.tenancy import TenantResolver
from cognistore.core.audit import AuditContext, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver
from tests.unit.test_api_authentication import ISSUER
from tests.unit.test_api_authentication import identity_provider as identity_provider


@pytest.fixture(params=["memory", "sqlite"])
def holds_api(request, identity_provider, tmp_path):
    authenticator, issue_token, _ = identity_provider
    catalog = Catalog() if request.param == "memory" else SQLCatalog(tmp_path / "catalog.db")
    drivers = {tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm")}
    gateway = CogniStoreGateway(catalog, drivers)
    roles = {
        "alice": ["admin"], "manager": ["hold_manager"], "releaser": ["hold_releaser"],
        "reader": ["reader"], "auditor": ["auditor"], "writer": ["writer"],
    }
    app = create_app(gateway, authentication=authenticator, authorization=RBACAuthorizer(
        RBACPolicy({(ISSUER, subject): grants for subject, grants in roles.items()}),
    ))
    headers = {
        subject: {"Authorization": "Bearer " + issue_token(subject)} for subject in roles
    }
    with TestClient(app) as client:
        yield client, gateway, drivers, headers
    if isinstance(catalog, SQLCatalog):
        catalog.close()


def test_held_object_keeps_bytes_catalog_and_auditable_release(holds_api):
    client, gateway, drivers, headers = holds_api
    url = "/v1/objects/hot/docs/report.txt"
    assert client.put(url, content=b"evidence", headers=headers["alice"]).status_code == 201
    before = gateway.catalog.get("docs", "report.txt")
    placed = client.post("/v1/legal-holds", headers={
        **headers["manager"], "X-Request-ID": "place-case-62",
    }, json={"bucket": "docs", "key": "report.txt", "reason": "Case 62"})
    assert placed.status_code == 201, placed.text
    hold = placed.json()
    assert hold["active"] is True
    assert hold["actor_id"] == Principal(ISSUER, "manager").actor_id
    assert hold["actor_type"] == "authenticated"
    assert hold["correlation_id"] == "place-case-62"
    for subject in ("reader", "auditor", "writer"):
        listed = client.get("/v1/legal-holds?bucket=docs&key=report.txt&active_only=true",
                            headers=headers[subject])
        assert listed.status_code == 200
        assert listed.json()["items"] == [hold]
    assert client.get(url, headers=headers["reader"]).content == b"evidence"
    assert client.head(url, headers=headers["reader"]).status_code == 200
    for subject in ("writer", "alice"):
        for method in ("DELETE", "PUT"):
            denied = client.request(method, url, content=b"changed", headers=headers[subject])
            assert denied.status_code == 409, denied.text
            assert denied.json()["error"]["code"] == "legal_hold"
            assert denied.json()["error"]["retryable"] is False
    assert drivers["hot"].get_object("docs", "report.txt") == b"evidence"
    assert gateway.catalog.get("docs", "report.txt") == before
    release_url = f"/v1/legal-holds/{hold['hold_id']}/release"
    for subject in ("manager", "writer", "reader", "auditor"):
        denied = client.post(release_url, json={"reason": "Case closed"}, headers=headers[subject])
        assert denied.status_code == 403
    assert gateway.catalog.list_legal_holds(active_only=True)
    released = client.post(release_url, json={"reason": "Case closed"}, headers={
        **headers["releaser"], "X-Request-ID": "release-case-62",
    })
    assert released.status_code == 200, released.text
    assert released.json()["active"] is False
    assert released.json()["released_reason"] == "Case closed"
    assert released.json()["released_actor_id"] == Principal(ISSUER, "releaser").actor_id
    assert released.json()["released_correlation_id"] == "release-case-62"
    assert client.get("/v1/legal-holds?active_only=true", headers=headers["reader"]).json()["items"] == []
    assert client.get("/v1/legal-holds", headers=headers["reader"]).json()["items"] == [released.json()]
    assert client.post(release_url, json={"reason": "Again"}, headers=headers["releaser"]).status_code == 409
    assert client.put(url, content=b"changed", headers=headers["writer"]).status_code == 201
    assert client.delete(url, headers=headers["writer"]).status_code == 204
    events = gateway.catalog.list_audit_events(AuditQuery(
        event_types=frozenset({"legal_hold.placed", "legal_hold.released", "legal_hold.denied"}),
    ))
    assert {event.event_type for event in events} == {
        "legal_hold.placed", "legal_hold.released", "legal_hold.denied",
    }
    assert len([event for event in events if event.event_type == "legal_hold.released"]) == 1


def test_overlapping_scope_holds_protect_new_keys_and_other_tiers(holds_api):
    client, _, drivers, headers = holds_api
    exact = client.post("/v1/legal-holds", json={
        "bucket": "docs", "key": "evidence/new.txt", "reason": "Object case",
    }, headers=headers["manager"]).json()
    prefix = client.post("/v1/legal-holds", json={
        "bucket": "docs", "prefix": "evidence/", "reason": "Dataset case",
    }, headers=headers["manager"]).json()
    matches = client.get("/v1/legal-holds?bucket=docs&key=evidence/new.txt",
                         headers=headers["reader"]).json()["items"]
    assert {hold["hold_id"] for hold in matches} == {exact["hold_id"], prefix["hold_id"]}
    assert client.post(f"/v1/legal-holds/{exact['hold_id']}/release", json={"reason": "Closed"},
                       headers=headers["releaser"]).status_code == 200
    for tier in drivers:
        for overwrite in ("true", "false"):
            response = client.put(f"/v1/objects/{tier}/docs/evidence/new.txt?overwrite={overwrite}",
                                  content=b"new", headers=headers["writer"])
            assert response.status_code == 409
        assert list(drivers[tier].list_objects("docs")) == []
    assert client.put("/v1/objects/hot/docs/evidence-other.txt", content=b"allowed",
                      headers=headers["writer"]).status_code == 201
    assert client.put("/v1/objects/hot/other/evidence/new.txt", content=b"allowed",
                      headers=headers["writer"]).status_code == 201


@pytest.mark.parametrize("payload", [
    {"bucket": "docs", "reason": "   "},
    {"bucket": "docs", "reason": "Case", "key": "a", "prefix": "a"},
    {"bucket": "docs", "reason": "Case", "actor_id": "spoofed"},
    {"bucket": "docs", "reason": "Case", "tenant_id": "other"},
])
def test_hold_requests_validate_scope_and_reject_caller_identity(holds_api, payload):
    client, gateway, _, headers = holds_api
    response = client.post("/v1/legal-holds", json=payload, headers=headers["manager"])
    assert response.status_code == 422
    assert gateway.catalog.list_legal_holds() == []


def test_hold_release_body_bounds_and_missing_hold(holds_api):
    client, _, _, headers = holds_api
    url = "/v1/legal-holds/11111111-1111-4111-8111-111111111111/release"
    assert client.post(url, json={"reason": "Closed"}, headers=headers["releaser"]).status_code == 404
    assert client.post(url, json={"reason": "Closed", "actor_id": "spoofed"},
                       headers=headers["releaser"]).status_code == 422
    assert client.post(url, content=b"x" * (MAX_JSON_BODY_BYTES + 1),
                       headers=headers["releaser"]).status_code == 413
    assert client.get("/v1/legal-holds?key=a.txt", headers=headers["reader"]).status_code == 422


def test_anonymous_gateway_cannot_change_holds_but_hold_still_blocks_objects(tmp_path):
    catalog = Catalog()
    driver = PosixDriver(str(tmp_path / "hot"))
    gateway = CogniStoreGateway(catalog, {"hot": driver})
    gateway.put_object("hot", "docs", "a.txt", b"retained", overwrite=False, content_type=None)
    hold = catalog.place_legal_hold("docs", reason="Case", context=AuditContext(
        correlation_id="local-case", actor_type="user", actor_id="local-operator",
    ))
    with TestClient(create_app(gateway)) as client:
        assert client.get("/v1/legal-holds").status_code == 200
        assert client.delete("/v1/objects/hot/docs/a.txt").status_code == 409
        assert client.post("/v1/legal-holds", json={"bucket": "docs", "reason": "Case"}).status_code == 403
        assert client.post(f"/v1/legal-holds/{hold.hold_id}/release", json={"reason": "Closed"}).status_code == 403
    with pytest.raises(AuthorizationError):
        gateway.place_legal_hold(LegalHoldRequest(bucket="docs", reason="Case"), correlation_id="local")
    with pytest.raises(AuthorizationError):
        gateway.release_legal_hold(hold.hold_id, LegalHoldReleaseRequest(reason="Closed"), correlation_id="local")
    assert catalog.list_legal_holds(active_only=True) == [hold]
    assert driver.get_object("docs", "a.txt") == b"retained"


def test_legal_holds_are_tenant_scoped(identity_provider, tmp_path):
    authenticator, issue_token, _ = identity_provider
    gateway = CogniStoreGateway(Catalog(), {"hot": PosixDriver(str(tmp_path / "hot"))})
    app = create_app(gateway, authentication=authenticator,
                     authorization=RBACAuthorizer(RBACPolicy({
                         (ISSUER, subject): ["admin"] for subject in ("alice", "bob")
                     })), tenancy=TenantResolver({
                         (ISSUER, "alice"): "alpha", (ISSUER, "bob"): "beta",
                     }))
    alice = {"Authorization": "Bearer " + issue_token("alice")}
    bob = {"Authorization": "Bearer " + issue_token("bob")}
    with TestClient(app) as client:
        hold = client.post("/v1/legal-holds", json={"bucket": "docs", "reason": "Alpha"},
                           headers=alice).json()
        assert hold["tenant_id"] == "alpha"
        assert client.get("/v1/legal-holds?bucket=docs", headers=bob).json()["items"] == []
        assert client.post(f"/v1/legal-holds/{hold['hold_id']}/release", json={"reason": "Closed"},
                           headers=bob).status_code == 404
        url = "/v1/objects/hot/docs/a.txt"
        assert client.put(url, content=b"alpha", headers=alice).status_code == 409
        assert client.put(url, content=b"beta", headers=bob).status_code == 201
        assert client.delete(url, headers=bob).status_code == 204
        assert client.get("/v1/legal-holds?active_only=true", headers=alice).json()["items"] == [hold]


@pytest.mark.parametrize("alias", [
    "evidence/évidence.txt", "Evidence/E\u0301vidence.txt", "Evidence//Évidence.txt",
])
def test_path_alias_put_cannot_replace_held_posix_bytes(holds_api, alias):
    client, gateway, drivers, headers = holds_api
    key = "Evidence/Évidence.txt"
    original_url = "/v1/objects/hot/docs/" + key
    assert client.put(original_url, content=b"retained", headers=headers["alice"]).status_code == 201
    placed = client.post("/v1/legal-holds", json={
        "bucket": "docs", "key": key, "reason": "Preserve exact object",
    }, headers=headers["manager"])
    assert placed.status_code == 201
    response = client.put("/v1/objects/hot/DOCS/" + alias, content=b"replacement",
                          headers=headers["writer"])
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "legal_hold"
    assert drivers["hot"].get_object("docs", key) == b"retained"
    assert gateway.catalog.get("docs", key).size == 8
