from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from cognistore.api import server
from cognistore.api.app import create_app
from cognistore.api.errors import APIError
from cognistore.api.gateway import CogniStoreGateway
from cognistore.api.permissions import ENDPOINT_OPERATIONS, OPERATION_PERMISSIONS
from cognistore.auth.authorization import (
    AuthorizationError,
    RBACAuthorizer,
    RBACPolicy,
    current_authorizer,
)
from cognistore.auth.jwt import JWTAuthConfig
from cognistore.auth.principal import Principal, current_principal, principal_context
from cognistore.core.audit import AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.drivers.posix_driver import PosixDriver
from tests.unit.test_api_authentication import ISSUER
from tests.unit.test_api_authentication import identity_provider as identity_provider

IDENTIFIER = "11111111-1111-4111-8111-111111111111"
# Expected permissions are independent of the production matrix.
CASES = [
    ("PUT", "/v1/objects/hot/docs/a.txt", {"content": b"hello"}, {"write"}, "put_object"),
    ("HEAD", "/v1/objects/hot/docs/a.txt", {}, {"read"}, "stat_object"),
    ("GET", "/v1/objects/hot/docs/a.txt", {}, {"read"}, "open_object"),
    ("DELETE", "/v1/objects/hot/docs/a.txt", {}, {"write"}, "delete_object"),
    ("GET", "/v1/catalog/objects?bucket=docs", {}, {"read"}, "list_catalog_objects"),
    ("GET", "/v1/catalog/objects/docs/a.txt", {}, {"read"}, "get_catalog_object"),
    ("POST", "/v1/ask", {"json": {"text": "hello"}}, {"read"}, "ask"),
    ("POST", "/v1/catalog/importance", {"json": {
        "bucket": "docs", "key": "a.txt", "level": "normal",
        "actor_id": "alice", "provenance": "requested",
    }}, {"write", "policy"}, "set_importance"),
    ("POST", "/v1/policies/evaluate", {"json": {"bucket": "docs", "key": "a.txt"}},
     {"policy"}, "evaluate_policy"),
    ("POST", "/v1/policies/preview", {"json": {"bucket": "docs", "key": "a.txt"}},
     {"policy"}, "preview_policy"),
    ("GET", "/v1/policy-decisions", {}, {"audit"}, "list_policy_decisions"),
    ("GET", f"/v1/policy-decisions/{IDENTIFIER}", {}, {"audit"}, "get_policy_decision"),
    ("POST", "/v1/actions/catalog-scans", {"json": {"tier": "hot", "bucket": "docs"}},
     {"administration"}, "submit_catalog_scan"),
    ("POST", "/v1/actions/policy-runs", {"json": {"bucket": "docs"}},
     {"policy", "movement"}, "submit_policy_run"),
    ("GET", f"/v1/jobs/{IDENTIFIER}", {}, {"read"}, "get_job"),
    ("GET", "/v1/legal-holds", {}, {"legal_hold_inspect"}, "list_legal_holds"),
    ("POST", "/v1/legal-holds", {"json": {"bucket": "docs", "reason": "Case 62"}},
     {"legal_hold_manage"}, "place_legal_hold"),
    ("POST", f"/v1/legal-holds/{IDENTIFIER}/release", {"json": {"reason": "Case closed"}},
     {"legal_hold_release"}, "release_legal_hold"),
]
ROLE_GRANTS = {
    "reader": {"read", "legal_hold_inspect"},
    "writer": {"read", "write", "legal_hold_inspect"},
    "policy_manager": {"read", "policy", "legal_hold_inspect"},
    "operator": {"read", "movement", "administration", "legal_hold_inspect"},
    "auditor": {"audit", "legal_hold_inspect"},
    "hold_manager": {"legal_hold_inspect", "legal_hold_manage"},
    "hold_releaser": {"legal_hold_inspect", "legal_hold_release"},
    "admin": {"read", "write", "policy", "movement", "administration", "audit",
              "legal_hold_inspect", "legal_hold_manage", "legal_hold_release"},
}


def authorization(*roles: str, issuer: str = ISSUER, subject: str = "alice") -> RBACAuthorizer:
    return RBACAuthorizer(RBACPolicy.from_dict({"bindings": [
        {"issuer": issuer, "subject": subject, "roles": list(roles)},
    ]}))


class BoundarySpy:
    def __init__(self):
        self.calls = []

    async def startup(self):
        pass

    async def shutdown(self):
        pass

    def __getattr__(self, name):
        def reached(*args, **kwargs):
            self.calls.append(name)
            raise APIError(409, "reached_service", "Test service reached")
        return reached


@pytest.mark.parametrize("role", list(ROLE_GRANTS))
def test_every_endpoint_enforces_role_matrix(identity_provider, role):
    authenticator, issue_token, _ = identity_provider
    spy = BoundarySpy()
    app = create_app(spy, authentication=authenticator, authorization=authorization(role))
    with TestClient(app, headers={"Authorization": "Bearer " + issue_token()}) as client:
        for method, path, payload, required, operation in CASES:
            spy.calls.clear()
            response = client.request(method, path, **payload)
            allowed = required <= ROLE_GRANTS[role]
            assert response.status_code == (409 if allowed else 403), (role, method, path, response.text)
            assert spy.calls == ([operation] if allowed else [])
    assert current_authorizer() is None
    assert current_principal() is None


def test_permission_matrix_covers_entire_contract_and_is_documented():
    app = create_app()
    actual = {
        (method, route.path)
        for route in app.routes
        if getattr(route, "path", "").startswith("/v1/")
        for method in route.methods
    }
    # FastAPI may retain included routers instead of flattening app.routes.
    schema = app.openapi()
    expected_paths = {
        (method.upper(), path)
        for path, operations in schema["paths"].items() if path.startswith("/v1/")
        for method in operations
    }
    assert {key for key in actual} <= set(ENDPOINT_OPERATIONS)
    assert len(expected_paths) == len(ENDPOINT_OPERATIONS) == len(CASES)
    for (method, path), operation in ENDPOINT_OPERATIONS.items():
        openapi_path = path.replace(":path", "")
        assert (method, openapi_path) in expected_paths
        assert schema["paths"][openapi_path][method.lower()]["x-required-permissions"] == [
            p.value for p in OPERATION_PERMISSIONS[operation]
        ]
    docs = (Path(__file__).parents[2] / "docs/authorization.md").read_text()
    for method, path in ENDPOINT_OPERATIONS:
        assert path.replace(":path", "") in docs
    for _, _, _, required, operation in CASES:
        assert set(OPERATION_PERMISSIONS[operation]) == required


def test_valid_jwt_without_binding_cannot_supply_its_own_roles(identity_provider):
    authenticator, issue_token, _ = identity_provider
    token = issue_token(roles=["admin"], permissions=list(ROLE_GRANTS["admin"]))
    spy = BoundarySpy()
    with TestClient(create_app(spy, authentication=authenticator)) as client:
        for method, path, _, _, _ in CASES:
            response = client.request(method, path, content=b"{bad json", headers={
                "Authorization": "Bearer " + token, "X-Roles": "admin",
            })
            assert response.status_code == 403
            assert token not in response.text
    assert spy.calls == []


@pytest.mark.parametrize("operation", list(OPERATION_PERMISSIONS))
def test_direct_service_calls_fail_before_using_arguments_or_backends(operation):
    gateway = CogniStoreGateway(Catalog(), {}, authorization=RBACAuthorizer())
    method = getattr(gateway, operation)
    kwargs = {
        name: None for name, parameter in inspect.signature(method).parameters.items()
        if parameter.default is inspect.Parameter.empty
    }
    with principal_context(Principal(ISSUER, "alice")), pytest.raises(AuthorizationError):
        result = method(**kwargs)
        if inspect.isawaitable(result):
            asyncio.run(result)
    events = gateway.catalog.list_audit_events()
    assert len(events) == 1
    assert events[0].outcome == "denied"


@pytest.mark.parametrize("mounted", [False, True])
def test_denials_precede_existence_validation_and_body_reads(identity_provider, tmp_path, mounted):
    authenticator, issue_token, _ = identity_provider
    catalog = Catalog()
    driver = PosixDriver(str(tmp_path / "hot"))
    driver.put_object("docs", "exists.txt", b"existing")
    catalog.upsert("docs", "exists.txt", 8, "hot")
    app = create_app(CogniStoreGateway(catalog, {"hot": driver}), authentication=authenticator,
                     authorization=authorization("reader"))
    if mounted:
        parent = FastAPI()
        parent.mount("/storage", app)
        client = TestClient(parent)
    else:
        client = TestClient(app, root_path="/storage")
    headers = {"Authorization": "Bearer " + issue_token(), "X-Request-ID": "denial"}
    with client:
        responses = [client.delete("/storage/v1/objects/hot/docs/" + key, headers=headers)
                     for key in ("exists.txt", "absent.txt")]
        assert responses[0].json() == responses[1].json()
        assert all(response.status_code == 403 for response in responses)
        for path in ("/actions/catalog-scans", "/actions/policy-runs", "/policies/evaluate"):
            response = client.post("/storage/v1" + path, content=b"bad", headers={
                **headers, "Content-Length": str(1024 * 1024),
            })
            assert response.status_code == 403
        assert client.get("/storage/v1/unknown", headers=headers).status_code == 403
        assert client.patch("/storage/v1/catalog/objects", headers=headers).status_code == 403
        assert client.get("/storage/healthz").status_code == 200
    assert driver.get_object("docs", "exists.txt") == b"existing"
    events = catalog.list_audit_events(AuditQuery(event_types=frozenset({"authorization.decision"})))
    assert events and all(event.outcome == "denied" for event in events)
    assert "exists.txt" not in repr(events) and "absent.txt" not in repr(events)


def test_policy_file_revocation_applies_to_next_request(identity_provider, tmp_path):
    authenticator, issue_token, _ = identity_provider
    path = tmp_path / "rbac.json"
    path.write_text(json.dumps({"bindings": [{"issuer": ISSUER, "subject": "alice", "roles": ["reader"]}]}))
    app = create_app(CogniStoreGateway(Catalog(), {}), authentication=authenticator,
                     authorization=RBACAuthorizer(policy_path=path))
    with TestClient(app, headers={"Authorization": "Bearer " + issue_token()}) as client:
        assert client.get("/v1/catalog/objects?bucket=docs").status_code == 200
        path.write_text('{"bindings": []}')
        assert client.get("/v1/catalog/objects?bucket=docs").status_code == 403
        path.write_text("invalid")
        assert client.get("/v1/catalog/objects?bucket=docs").status_code == 403
        path.unlink()
        assert client.get("/v1/catalog/objects?bucket=docs").status_code == 403


def test_additional_hook_cannot_grant_missing_permissions(identity_provider):
    authenticator, issue_token, _ = identity_provider
    called = []
    with TestClient(create_app(authentication=authenticator, authorization=RBACAuthorizer(),
                               authorization_hook=lambda: called.append(1))) as client:
        response = client.get("/v1/catalog/objects?bucket=docs", headers={
            "Authorization": "Bearer " + issue_token(),
        })
    assert response.status_code == 403
    assert called == []


@pytest.mark.parametrize("error_type", ["http", "domain", "authorization"])
def test_hook_denial_is_audited_without_hook_details(identity_provider, error_type):
    authenticator, issue_token, _ = identity_provider
    catalog = Catalog()

    def deny():
        if error_type == "http":
            raise HTTPException(403, "private policy explanation")
        if error_type == "authorization":
            raise AuthorizationError()
        raise APIError(403, "forbidden", "Operation not permitted")

    app = create_app(CogniStoreGateway(catalog, {}), authentication=authenticator,
                     authorization=authorization("reader"), authorization_hook=deny)
    with TestClient(app) as client:
        response = client.get("/v1/catalog/objects?bucket=docs", headers={
            "Authorization": "Bearer " + issue_token(), "X-Request-ID": "hook-denial",
        })
    assert response.status_code == 403
    assert "private policy explanation" not in response.text
    events = catalog.list_audit_events(AuditQuery(outcomes=frozenset({"denied"})))
    assert len(events) == 1
    assert events[0].details["boundary"] == "api_result"
    assert events[0].correlation_id == "hook-denial"
    assert "private policy explanation" not in repr(events)


def test_api_authorization_configuration_requires_authentication(tmp_path, monkeypatch):
    monkeypatch.delenv("COGNISTORE_AUTHORIZATION_POLICY", raising=False)
    with pytest.raises(ValueError, match="requires JWT"):
        create_app(authorization=authorization("admin"))
    path = tmp_path / "rbac.json"
    path.write_text('{"bindings": []}')
    args = server._parser().parse_args(["--authorization-policy", str(path)])
    with pytest.raises(SystemExit, match="requires JWT"):
        server._authorization_config(args, None)
    authentication = JWTAuthConfig(issuer=ISSUER, audience="test")
    assert isinstance(server._authorization_config(args, authentication), RBACAuthorizer)
    monkeypatch.setenv("COGNISTORE_AUTHORIZATION_POLICY", str(path))
    assert server._parser().parse_args([]).authorization_policy == str(path)
    path.write_text('{"bindings": [{"roles": ["typo"]}]}')
    with pytest.raises(SystemExit, match="Invalid authorization"):
        server._authorization_config(args, authentication)
