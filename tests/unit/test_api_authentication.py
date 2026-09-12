from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from cognistore.api import server
from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.auth.jwt import JWTAuthConfig, JWTAuthenticator
from cognistore.auth.principal import Principal, current_principal, principal_context
from cognistore.core.audit import AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver

ISSUER = "https://identity.example.test/realm"
AUDIENCE = "cognistore-api"


@pytest.fixture
def identity_provider():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="key-1", alg="RS256", use="sig")
    requests = []

    def fetch(request):
        requests.append(request)
        return httpx.Response(200, json={"keys": [public]})

    client = httpx.Client(transport=httpx.MockTransport(fetch))
    authenticator = JWTAuthenticator(
        JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, jwks_uri=ISSUER + "/keys"),
        http_client=client,
    )

    def token(subject="alice", **claims):
        now = int(time.time())
        return jwt.encode(
            {"iss": ISSUER, "aud": AUDIENCE, "sub": subject, "iat": now, "exp": now + 300, **claims},
            key, algorithm="RS256", headers={"kid": "key-1"},
        )

    yield authenticator, token, requests
    authenticator.close()
    client.close()


def test_all_versioned_operations_require_auth_before_body_or_hook(identity_provider):
    authenticator, _, requests = identity_provider
    hook_calls = []
    app = create_app(authentication=authenticator, authorization_hook=lambda: hook_calls.append(1))
    with TestClient(app) as client:
        for path, operations in app.openapi()["paths"].items():
            if not path.startswith("/v1/"):
                continue
            for method in operations:
                response = client.request(method, path, content=b"{invalid json")
                assert response.status_code == 401, (method, path)
                assert response.headers["WWW-Authenticate"] == "Bearer"
                if method != "head":
                    assert response.json()["error"]["code"] == "authentication_required"
        for path in ("/healthz", "/ui/", "/docs", "/openapi.json"):
            assert client.get(path).status_code == 200
    assert hook_calls == []
    assert requests == []


@pytest.mark.parametrize("authorization", [
    "", "Basic secret", "Bearer", "Bearer ", "Bearer a.b.c", "Bearer  a.b.c",
    "Bearer a.b.c ", "Bearer " + "x" * 17_000,
], ids=["empty", "basic", "no-token", "blank-token", "malformed", "leading-space", "trailing-space", "oversize"])
def test_malformed_bearer_errors_are_safe_and_consistent(identity_provider, authorization):
    authenticator, _, _ = identity_provider
    with TestClient(create_app(authentication=authenticator)) as client:
        response = client.get("/v1/catalog/objects", headers={
            "Authorization": authorization, "X-Request-ID": "auth-test",
        })
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'
    assert response.json() == {
        "schema_version": 1, "request_id": "auth-test",
        "error": {"code": "invalid_token", "message": "The bearer access token is invalid",
                  "retryable": False, "details": []},
    }


@pytest.mark.parametrize("claims", [{"exp": 1}, {"aud": "another-api"}, {"iss": "https://evil.test"}])
def test_invalid_signed_tokens_never_reach_gateway(identity_provider, claims):
    authenticator, issue_token, _ = identity_provider
    token = issue_token(**claims)
    with TestClient(create_app(authentication=authenticator)) as client:
        response = client.get("/v1/catalog/objects", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"
    assert token not in response.text


def test_duplicate_authorization_headers_fail_closed(identity_provider):
    authenticator, issue_token, requests = identity_provider
    with TestClient(create_app(authentication=authenticator)) as client:
        response = client.get("/v1/catalog/objects", headers=[
            ("Authorization", "Bearer " + issue_token()), ("Authorization", "Bearer extra"),
        ])
    assert response.status_code == 401
    assert requests == []


def test_verified_identity_reaches_sync_hook_and_jobs_without_tokens(identity_provider, tmp_path: Path):
    authenticator, issue_token, _ = identity_provider
    seen = []

    class Queue:
        async def enqueue(self, job, **kwargs):
            seen.append((job, current_principal()))

    def authorize(request: Request):
        assert request.state.principal == current_principal()
        assert request.state.principal == Principal(ISSUER, "service:scanner", "scanner-client")

    catalog = SQLCatalog(tmp_path / "catalog.db")
    gateway = CogniStoreGateway(catalog, {"hot": PosixDriver(str(tmp_path / "hot"))}, queue=Queue())
    token = issue_token("service:scanner", client_id="scanner-client", secret_claim="not-propagated")
    app = create_app(gateway, authentication=authenticator, authorization_hook=authorize)
    with TestClient(app) as client:
        response = client.post("/v1/actions/catalog-scans", json={"tier": "hot", "bucket": "docs"},
                               headers={"Authorization": "bEaReR " + token})
        assert response.status_code == 202, response.text
    job, principal = seen[0]
    assert principal == job.principal == Principal(ISSUER, "service:scanner", "scanner-client")
    events = catalog.list_audit_events(AuditQuery(job_id=str(job.job_id)))
    assert events and all(event.actor_id == principal.actor_id for event in events)
    assert all(event.actor_type == "authenticated" for event in events)
    persisted = job.to_bytes().decode() + repr(events)
    assert token not in persisted
    assert "secret_claim" not in persisted
    assert "not-propagated" not in persisted
    assert current_principal() is None
    catalog.close()


def test_hook_can_deny_authenticated_request(identity_provider):
    authenticator, issue_token, _ = identity_provider

    def deny(request: Request):
        assert request.state.principal.subject == "alice"
        raise HTTPException(403, "internal policy details")

    with TestClient(create_app(authentication=authenticator, authorization_hook=deny)) as client:
        response = client.get("/v1/catalog/objects", headers={"Authorization": "Bearer " + issue_token()})
    assert response.status_code == 403
    assert "internal policy details" not in response.text


def test_concurrent_requests_keep_principals_isolated(identity_provider):
    authenticator, issue_token, _ = identity_provider

    async def check(request: Request):
        before = current_principal()
        await asyncio.sleep(0)
        assert before == current_principal() == request.state.principal
        assert before.subject == request.headers["X-Expected-Subject"]

    app = create_app(CogniStoreGateway(Catalog(), {}), authentication=authenticator, authorization_hook=check)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://api.test") as client:
            responses = await asyncio.gather(*[
                client.get("/v1/catalog/objects?bucket=docs", headers={
                    "Authorization": "Bearer " + issue_token(subject), "X-Expected-Subject": subject,
                }) for subject in ("alice", "bob", "service:scanner")
            ])
        assert all(response.status_code == 200 for response in responses)
        assert current_principal() is None

    asyncio.run(run())


def test_openapi_documents_bearer_on_every_v1_operation(identity_provider):
    authenticator, _, _ = identity_provider
    schema = create_app(authentication=authenticator).openapi()
    assert schema["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http", "scheme": "bearer", "bearerFormat": "JWT",
    }
    for path, operations in schema["paths"].items():
        for operation in operations.values():
            if path.startswith("/v1/"):
                assert operation["security"] == [{"BearerAuth": []}]
            else:
                assert "security" not in operation


@pytest.fixture
def clean_auth_environment(monkeypatch):
    for name in ("ISSUER", "AUDIENCE", "JWKS_URI", "ALGORITHMS", "REQUIRED_CLAIMS"):
        monkeypatch.delenv("COGNISTORE_AUTH_" + name, raising=False)


@pytest.mark.usefixtures("clean_auth_environment")
def test_server_auth_configuration(monkeypatch):
    assert server._auth_config(server._parser().parse_args([])) is None
    monkeypatch.setenv("COGNISTORE_AUTH_ISSUER", ISSUER)
    monkeypatch.setenv("COGNISTORE_AUTH_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("COGNISTORE_AUTH_ALGORITHMS", "RS256, ES256")
    monkeypatch.setenv("COGNISTORE_AUTH_REQUIRED_CLAIMS", "sub,exp,client_id")
    config = server._auth_config(server._parser().parse_args([]))
    assert config.issuer == ISSUER and config.audience == AUDIENCE
    assert config.algorithms == ("RS256", "ES256")
    assert config.required_claims == ("sub", "exp", "client_id")
    override = server._auth_config(server._parser().parse_args([
        "--auth-algorithm", "PS256", "--auth-required-claim", "iat", "--auth-jwks-uri", ISSUER + "/keys",
    ]))
    assert override.algorithms == ("PS256",)
    assert override.required_claims == ("iat",)
    assert override.jwks_uri == ISSUER + "/keys"


@pytest.mark.usefixtures("clean_auth_environment")
@pytest.mark.parametrize("name,value", [
    ("ISSUER", ISSUER), ("ISSUER", ""), ("AUDIENCE", AUDIENCE),
    ("JWKS_URI", ISSUER + "/keys"), ("ALGORITHMS", "RS256"), ("REQUIRED_CLAIMS", "exp"),
])
def test_partial_auth_config_never_falls_back_to_anonymous(monkeypatch, name, value):
    monkeypatch.setenv("COGNISTORE_AUTH_" + name, value)
    with pytest.raises(SystemExit, match="requires both"):
        server._auth_config(server._parser().parse_args([]))


@pytest.mark.usefixtures("clean_auth_environment")
@pytest.mark.parametrize("extra", [["--auth-algorithm", "HS256"], ["--auth-jwks-uri", "http://unsafe.test"]])
def test_server_rejects_unsafe_auth_settings(extra):
    with pytest.raises(SystemExit, match="Invalid JWT authentication configuration"):
        server._auth_config(server._parser().parse_args([
            "--auth-issuer", ISSUER, "--auth-audience", AUDIENCE, *extra,
        ]))


def test_principal_identity_roundtrip_and_scope():
    alice = Principal(ISSUER, "alice", "web-client")
    assert Principal.from_json(alice.to_json()) == alice
    assert alice.actor_id != Principal("https://another.test", "alice").actor_id
    assert alice.actor_id == Principal(ISSUER, "alice", "mobile-client").actor_id
    with principal_context(alice):
        assert current_principal() == alice
        with pytest.raises(RuntimeError), principal_context(None):
            assert current_principal() is None
            raise RuntimeError()
        assert current_principal() == alice
    assert current_principal() is None


@pytest.mark.parametrize("value", [
    '{}', '[]', '{"issuer":"i","subject":"s","client_id":null,"token":"secret"}',
    '{"issuer":"i","subject":null,"client_id":null}', "x" * 40_001,
], ids=["missing-fields", "array", "extra-token", "null-subject", "oversize"])
def test_principal_rejects_invalid_or_expanded_wire_contract(value):
    with pytest.raises(ValueError, match="Invalid principal metadata"):
        Principal.from_json(value)


@pytest.mark.parametrize("subject", ["", " ", "x\ny", "x\x7fy", "x" * 2049, "\ud800", "💠" * 513],
                         ids=["empty", "blank", "newline", "control", "oversize", "surrogate", "utf8-limit"])
def test_principal_bounds_identity_text(subject):
    with pytest.raises(ValueError):
        Principal(ISSUER, subject)


@pytest.mark.parametrize("mounted", [False, True])
def test_authentication_cannot_be_bypassed_by_a_path_prefix(identity_provider, mounted):
    authenticator, issue_token, _ = identity_provider
    app = create_app(CogniStoreGateway(Catalog(), {}), authentication=authenticator)
    if mounted:
        parent = FastAPI()
        parent.mount("/cognistore", app)
        client = TestClient(parent)
    else:
        client = TestClient(app, root_path="/cognistore")
    with client:
        path = "/cognistore/v1/catalog/objects?bucket=docs"
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer " + issue_token()}).status_code == 200
        assert client.get("/cognistore/healthz").status_code == 200


@pytest.mark.parametrize("fail_startup", [False, True])
def test_app_closes_owned_authenticator_even_when_gateway_startup_fails(monkeypatch, fail_startup):
    closed = []
    original_close = JWTAuthenticator.close

    def close(authenticator):
        closed.append(authenticator)
        original_close(authenticator)

    class Gateway(CogniStoreGateway):
        async def startup(self):
            if fail_startup:
                raise RuntimeError("gateway startup failed")

    monkeypatch.setattr(JWTAuthenticator, "close", close)
    config = JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE)
    app = create_app(Gateway(Catalog(), {}), authentication=config)
    if fail_startup:
        with pytest.raises(RuntimeError, match="gateway startup failed"), TestClient(app):
            pass
    else:
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
    assert closed == [app.state.authenticator]


def test_app_leaves_injected_authenticator_owned_by_caller(identity_provider):
    authenticator, issue_token, _ = identity_provider
    with TestClient(create_app(authentication=authenticator)) as client:
        assert client.get("/healthz").status_code == 200
    assert authenticator.authenticate(issue_token()) == Principal(ISSUER, "alice")


def test_authentication_preserves_traces_without_exporting_identity_or_tokens(
    identity_provider, tmp_path, monkeypatch,
):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from cognistore import observability as telemetry
    from cognistore.jobs.models import JobEnvelope

    authenticator, issue_token, _ = identity_provider
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_tracer", provider.get_tracer("auth-integration"))
    jobs = []

    class Queue:
        async def enqueue(self, job, **kwargs):
            jobs.append(JobEnvelope.from_bytes(job.to_bytes()))

    token = issue_token("private-service-subject", client_id="private-client-id")
    trace_id = "1234567890abcdef1234567890abcdef"
    headers = {
        "Authorization": "Bearer " + token,
        "traceparent": f"00-{trace_id}-1234567890abcdef-01",
        "X-Request-ID": "auth-observability",
    }
    try:
        with SQLCatalog(tmp_path / "catalog.db") as catalog:
            gateway = CogniStoreGateway(
                catalog, {"hot": PosixDriver(str(tmp_path / "hot"))}, queue=Queue(),
            )
            with TestClient(create_app(gateway, authentication=authenticator)) as client:
                response = client.post(
                    "/v1/actions/catalog-scans", json={"tier": "hot", "bucket": "docs"},
                    headers=headers,
                )
                assert response.status_code == 202, response.text
                assert response.headers["X-Request-ID"] == "auth-observability"
                assert jobs[0].principal == Principal(ISSUER, "private-service-subject", "private-client-id")
                assert jobs[0].metadata["traceparent"].split("-")[1] == trace_id
                assert jobs[0].correlation_id == response.json()["correlation_id"]
                denied = client.get("/v1/catalog/objects?bucket=docs", headers={
                    "traceparent": headers["traceparent"], "X-Request-ID": "denied-observability",
                })
                assert denied.status_code == 401
                assert denied.json()["request_id"] == "denied-observability"
                metrics = client.get("/metrics")
                assert metrics.status_code == 200
        spans = exporter.get_finished_spans()
        requests = [span for span in spans if span.name == "api.request"]
        assert {span.attributes["http.response.status_code"] for span in requests} == {202, 401}
        assert all(span.context.trace_id == int(trace_id, 16) for span in requests)
        rendered = json.dumps([json.loads(span.to_json()) for span in spans]) + metrics.text
        for value in (token, "private-service-subject", "private-client-id"):
            assert value not in rendered
        assert current_principal() is None
        assert telemetry.current_correlation_id() is None
    finally:
        provider.shutdown()
