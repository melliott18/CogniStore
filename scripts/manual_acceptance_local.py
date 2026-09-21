#!/usr/bin/env python3
"""Repeatable loopback-only rehearsal for manual acceptance ticket #163.

This is a synthetic SQLite/POSIX fixture, NOT the production pilot topology.
Its identity provider is an in-process JWKS fixture; its browser session route
selects a fixture identity without login. It cannot qualify OIDC, TLS, a proxy,
S3, PostgreSQL, NATS, worker recovery, or production search/answer quality.
Never put customer data or real credentials in this disposable environment.

Run from the repository root with PYTHONPATH=.:
    python scripts/manual_acceptance_local.py serve --root /tmp/cognistore-163-local
    python scripts/manual_acceptance_local.py exercise --root /tmp/cognistore-163-local
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.auth.authorization import RBACAuthorizer
from cognistore.auth.jwt import JWTAuthConfig, JWTAuthenticator
from cognistore.auth.tenancy import TenantResolver
from cognistore.core.policy_features import CatalogPolicyFeatureLoader
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.sdk import CogniStoreClient
from cognistore.sdk.errors import ConflictError, NotFoundError, RangeNotSatisfiableError
from cognistore.sdk.models import (
    AskRequest,
    LegalHoldReleaseRequest,
    LegalHoldRequest,
    PolicyEvaluationRequest,
)
from cognistore.search import AskService

ISSUER = "https://fixture-identity.example.test/manual-acceptance"
AUDIENCE = "cognistore-local-rehearsal"
TENANTS = ("pilot-a", "pilot-b")
ROLES = ("reader", "writer", "operator", "auditor", "admin")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object, *, private: bool = False) -> None:
    if private:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
        path.chmod(0o600)
    else:
        path.write_text(json.dumps(value, indent=2) + "\n")


def fixture_body(tenant: str, interface: str, repetition: int) -> bytes:
    """A stable non-secret payload, deliberately different for equal tenant keys."""
    marker = f"cognistore-163-seed-v1:{tenant}:{interface}:{repetition}\n".encode()
    return (marker + bytes(range(256))) * 16


def build_app(root: Path):
    if root.is_symlink():
        raise ValueError("fixture root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_uid != os.getuid() or any(root.iterdir()):
        raise ValueError("serve requires a fresh empty directory owned by the current user")
    root.chmod(0o700)
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    public.update(kid="local-fixture", alg="RS256", use="sig")
    identity_client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"keys": [public]}),
    ))
    authenticator = JWTAuthenticator(
        JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, jwks_uri=ISSUER + "/keys"),
        http_client=identity_client,
    )
    issued = int(time.time())

    def token(subject: str, **claims: object) -> str:
        return jwt.encode(
            {"iss": ISSUER, "aud": AUDIENCE, "sub": subject, "iat": issued,
             "exp": issued + 8 * 3600, **claims},
            signing_key, algorithm="RS256", headers={"kid": "local-fixture"},
        )

    subjects = {f"{tenant}-{role}": (tenant, role) for tenant in TENANTS for role in ROLES}
    tokens = {subject: token(subject) for subject in subjects}
    for tenant in TENANTS:
        tokens[f"{tenant}-expired"] = token(f"{tenant}-reader", exp=issued - 600)
        tokens[f"{tenant}-wrong-audience"] = token(f"{tenant}-reader", aud="wrong")
        tokens[f"{tenant}-revoked-role"] = token(f"{tenant}-revoked-role")
        tokens[f"{tenant}-revoked-tenant"] = token(f"{tenant}-revoked-tenant")
    tokens["unmapped"] = token("unmapped")
    write_json(root / "tokens.private.json", tokens, private=True)
    roles = [{"issuer": ISSUER, "subject": subject, "roles": [role]}
             for subject, (_, role) in subjects.items()]
    roles += [{"issuer": ISSUER, "subject": f"{tenant}-revoked-tenant", "roles": ["reader"]}
              for tenant in TENANTS]
    tenants = [{"issuer": ISSUER, "subject": subject, "tenant_id": tenant}
               for subject, (tenant, _) in subjects.items()]
    tenants += [{"issuer": ISSUER, "subject": f"{tenant}-revoked-role", "tenant_id": tenant}
                for tenant in TENANTS]
    write_json(root / "roles.json", {"bindings": roles})
    write_json(root / "tenants.json", {"bindings": tenants})
    catalog = SQLCatalog(root / "catalog.db")
    ask_service = AskService(catalog)
    feature_loader = CatalogPolicyFeatureLoader(access_catalog=catalog)
    provider_inventory = {
        "metadata": type(ask_service.metadata).__name__,
        "keyword_configured": ask_service.keyword is not None,
        "vector_configured": ask_service.vector is not None,
        "answer_configured": ask_service.answer_provider is not None,
        "policy_embedding_configured": feature_loader.embedding_provider is not None,
        "outbound_model_evidence": (
            "Construction inspection: every optional search/answer/embedding provider is None; "
            "there is no model-capable provider to call. This is not packet-capture evidence."
        ),
    }
    if any(provider_inventory[name] for name in (
        "keyword_configured", "vector_configured", "answer_configured", "policy_embedding_configured",
    )):
        raise ValueError("this fixture must not construct model or optional search providers")
    gateway = CogniStoreGateway(catalog, {
        tier: PosixDriver(str(root / tier)) for tier in ("hot", "warm")
    }, ask_service=ask_service, feature_loader=feature_loader)
    app = create_app(
        gateway, authentication=authenticator,
        authorization=RBACAuthorizer(policy_path=root / "roles.json"),
        tenancy=TenantResolver(policy_path=root / "tenants.json"),
    )

    @app.middleware("http")
    async def local_fixture_session(request: Request, call_next):
        # A deliberate local convenience, never represented as the pilot proxy.
        if request.client is None or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            return JSONResponse({"error": "fixture is loopback-only"}, status_code=403)
        subject = request.cookies.get("cognistore_local_fixture")
        if request.url.path.startswith("/v1/") and "authorization" not in request.headers:
            if subject in subjects:
                request.scope["headers"].append(
                    (b"authorization", f"Bearer {tokens[subject]}".encode()),
                )
        response = await call_next(request)
        response.headers["X-CogniStore-Rehearsal"] = "local-fixture-not-production"
        return response

    @app.get("/local-session/{subject}", include_in_schema=False)
    def select_fixture_session(subject: str):
        if subject not in subjects and subject != "sign-out":
            return JSONResponse({"error": "unknown synthetic identity"}, status_code=404)
        response = RedirectResponse("/ui/", status_code=303)
        if subject == "sign-out":
            response.delete_cookie("cognistore_local_fixture")
        else:
            response.set_cookie("cognistore_local_fixture", subject, httponly=True, samesite="strict")
        return response

    write_json(root / "fixture.json", {
        "created_at": utc_now(), "fixture_only": True, "catalog": "SQLite", "storage": "POSIX",
        "tenants": list(TENANTS), "roles": list(ROLES), "ask": "metadata-only",
        "provider_inventory": provider_inventory,
        "queue": "not configured", "identity": "in-process synthetic JWKS; RS256 verification",
        "browser_session": "unauthenticated local subject selector; not an OIDC proxy",
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "python": platform.python_version(), "platform": platform.platform(),
    })
    return app


def main() -> int:
    if not __debug__:
        raise RuntimeError("Acceptance assertions require Python without -O or PYTHONOPTIMIZE")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve", "exercise"))
    parser.add_argument("--root", type=Path, required=True,
                        help="Fresh private directory for serve; existing fixture directory for exercise")
    parser.add_argument("--port", type=int, default=8763)
    args = parser.parse_args()
    if os.environ.get("COGNISTORE_SECURITY_PROFILE") != "development":
        parser.error("this local fixture requires COGNISTORE_SECURITY_PROFILE=development")
    if args.mode == "serve":
        uvicorn.run(build_app(args.root), host="127.0.0.1", port=args.port)
        return 0
    return exercise(args.root, args.port)


def exercise(root: Path, port: int) -> int:
    """Exercise actual HTTP and SDK clients; retain only sanitized observations."""
    if not __debug__:
        raise RuntimeError("Acceptance assertions require Python without -O or PYTHONOPTIMIZE")
    tokens = json.loads((root / "tokens.private.json").read_text())
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    base_url = f"http://127.0.0.1:{port}"
    observations: list[dict[str, object]] = []
    evidence: dict[str, object] = {
        "schema_version": 1, "started_at": utc_now(), "fixture": json.loads(
            (root / "fixture.json").read_text()),
        "run_id": run_id,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "tester": "Codex local rehearsal agent", "qualification": False,
        "hosted_ci": "skipped: user instruction; known GitHub billing/spending restriction",
        "scope": "actual loopback HTTP/API/SDK and separate trusted default-tenant CLI",
        "observations": observations,
        "limitations": [
            "Local SQLite/POSIX development profile, not the frozen candidate/staging topology.",
            "Synthetic JWKS and unauthenticated fixture session selector do not qualify OIDC/proxy/TLS.",
            "No NATS configured: successful queued execution/retry/DLQ/redrive not exercised here.",
            "POSIX-to-POSIX movement is not S3 provider qualification.",
            "CLI runs only against its separate default-tenant database and storage roots.",
            "Metadata-only Ask, no semantic or answer-quality claim; PII disabled.",
            "Inspect extraction mime_detection diagnostics: filename fallback does not qualify native libmagic.",
        ],
    }

    def record(scenario: str, tenant: str, interface: str, repetition: int,
               expected: str, actual: object) -> None:
        observations.append({"scenario": scenario, "tenant": tenant, "interface": interface,
                             "repetition": repetition, "at": utc_now(), "status": "passed",
                             "expected": expected, "actual": actual})

    def headers(subject: str) -> dict[str, str]:
        return {"Authorization": "Bearer " + tokens[subject]}

    with httpx.Client(base_url=base_url, trust_env=False, timeout=30) as http:
        def request(subject: str, method: str, path: str, expected: int, **kwargs):
            response = http.request(method, path, headers=headers(subject), **kwargs)
            assert response.status_code == expected, (
                f"{method} {path}: expected HTTP {expected}, got {response.status_code}: "
                + response.text[:500]
            )
            return response

        try:
            # Retain stable browser content across repeated runs, with distinct tenant bytes.
            for tenant in TENANTS:
                for key in ("manual-report.txt", "no-sensitive-data.txt"):
                    request(f"{tenant}-writer", "PUT", f"/v1/objects/hot/documents/{key}",
                            201, content=f"Synthetic {key} for {tenant}.".encode())

            for tenant in TENANTS:
                for interface in ("API", "SDK"):
                    reader_response: dict[str, object] = {}

                    def remember_reader_response(response: httpx.Response) -> None:
                        reader_response.update(status_code=response.status_code, headers=dict(response.headers))

                    with (
                        httpx.Client(trust_env=False, event_hooks={"response": [remember_reader_response]}) as reader_http,
                        CogniStoreClient(base_url, default_headers=headers(f"{tenant}-writer")) as sdk,
                        CogniStoreClient(base_url, default_headers=headers(f"{tenant}-reader"), http_client=reader_http) as reader_sdk,
                        CogniStoreClient(base_url, default_headers=headers(f"{tenant}-admin")) as admin_sdk,
                    ):
                        for repetition in range(1, 4):
                            key = f"rehearsal/{interface.lower()}-{repetition}.bin"
                            path = f"/v1/objects/hot/documents/{key}"
                            body = fixture_body(tenant, interface, repetition)
                            digest = hashlib.sha256(body).hexdigest()
                            if interface == "API":
                                request(f"{tenant}-writer", "PUT", path, 201, content=body)
                                head = request(f"{tenant}-reader", "HEAD", path, 200)
                                assert int(head.headers["Content-Length"]) == len(body)
                                fetched = request(f"{tenant}-reader", "GET", path, 200).content
                            else:
                                sdk.put_object("hot", "documents", key, body)
                                assert reader_sdk.head_object("hot", "documents", key).content_length == len(body)
                                fetched = reader_sdk.get_object("hot", "documents", key).content
                            assert hashlib.sha256(fetched).hexdigest() == digest
                            catalog_before = (
                                request(f"{tenant}-reader", "GET",
                                        f"/v1/catalog/objects/documents/{key}", 200).json()
                                if interface == "API" else
                                reader_sdk.get_catalog_object("documents", key).model_dump(mode="json")
                            )
                            assert catalog_before["tier"] == "hot" and catalog_before["size"] == len(body)
                            record("MA-01", tenant, interface, repetition,
                                   "Writer upload, reader HEAD/download SHA-256 and catalog placement agree", {
                                       "key": key, "bytes": len(body), "sha256": digest, "tier": "hot",
                                       "upload_role": "writer", "read_role": "reader",
                                       "catalog_interface": interface})
                            ranges = {
                                "bytes=1-7": (body[1:8], f"bytes 1-7/{len(body)}"),
                                "bytes=7-": (body[7:], f"bytes 7-{len(body)-1}/{len(body)}"),
                                "bytes=-7": (body[-7:], f"bytes {len(body)-7}-{len(body)-1}/{len(body)}"),
                            }
                            range_evidence = []
                            for byte_range, (expected_body, expected_header) in ranges.items():
                                if interface == "API":
                                    response = http.get(path, headers={
                                        **headers(f"{tenant}-reader"), "Range": byte_range})
                                    assert response.status_code == 206
                                    content = response.content
                                    content_range = response.headers.get("Content-Range")
                                    response_headers = dict(response.headers)
                                    content_length = int(response.headers["Content-Length"])
                                else:
                                    downloaded = reader_sdk.get_object("hot", "documents", key,
                                                                       byte_range=byte_range)
                                    assert downloaded.status_code == 206
                                    content, content_range = downloaded.content, downloaded.content_range
                                    content_length = downloaded.content_length
                                    response_headers = reader_response["headers"]
                                assert content == expected_body and content_range == expected_header
                                assert content_length == len(expected_body)
                                range_evidence.append({
                                    "range": byte_range, "status": 206, "headers": response_headers,
                                    "exact_bytes_match": True, "content_length": content_length,
                                    "sha256": hashlib.sha256(content).hexdigest(),
                                })
                            invalid_ranges = ("bytes=nope", "bytes=0-1,3-4", f"bytes={len(body) + 1}-")
                            invalid_evidence = []
                            for invalid_range in invalid_ranges:
                                if interface == "API":
                                    invalid = http.get(path, headers={
                                        **headers(f"{tenant}-reader"), "Range": invalid_range})
                                    assert invalid.status_code == 416
                                    assert invalid.headers["Content-Range"] == f"bytes */{len(body)}"
                                    invalid_headers = dict(invalid.headers)
                                else:
                                    try:
                                        reader_sdk.get_object("hot", "documents", key, byte_range=invalid_range)
                                    except RangeNotSatisfiableError as error:
                                        assert error.headers["Content-Range"] == f"bytes */{len(body)}"
                                        invalid_headers = dict(error.headers)
                                    else:
                                        raise AssertionError("Invalid SDK range was accepted")
                                invalid_evidence.append({"range": invalid_range, "status": 416,
                                                         "headers": invalid_headers})
                            zero_key = f"rehearsal/{interface.lower()}-empty-{repetition}.bin"
                            zero_path = f"/v1/objects/hot/documents/{zero_key}"
                            if interface == "API":
                                request(f"{tenant}-writer", "PUT", zero_path, 201, content=b"")
                                zero_response = http.get(zero_path, headers={
                                    **headers(f"{tenant}-reader"), "Range": "bytes=0-0"})
                                assert zero_response.status_code == 416
                                zero_headers = dict(zero_response.headers)
                                request(f"{tenant}-writer", "DELETE", zero_path, 204)
                            else:
                                sdk.put_object("hot", "documents", zero_key, b"")
                                try:
                                    reader_sdk.get_object("hot", "documents", zero_key, byte_range="bytes=0-0")
                                except RangeNotSatisfiableError as error:
                                    zero_headers = dict(error.headers)
                                else:
                                    raise AssertionError("Zero-byte SDK range was accepted")
                                sdk.delete_object("hot", "documents", zero_key)
                            assert zero_headers["content-range"] == "bytes */0"
                            record("MA-02", tenant, interface, repetition,
                                   "Reader ranges return exact bytes/Content-Range/Content-Length; invalid/multiple/out-of-bound/zero-byte ranges return416 and total-size header",
                                   {"ranges": range_evidence, "invalid_ranges": invalid_evidence,
                                    "zero_byte": {"status": 416, "headers": zero_headers}})
                            if interface == "API":
                                found = request(f"{tenant}-reader", "POST", "/v1/ask", 200,
                                                json={"text": key, "filters": {"bucket": "documents"},
                                                      "synthesize": True}).json()
                                empty = request(f"{tenant}-reader", "POST", "/v1/ask", 200,
                                                json={"text": "zzqqnonexistent163"}).json()
                                preview = request(f"{tenant}-admin", "POST", "/v1/policies/preview", 200,
                                                  json={"bucket": "documents", "key": key}).json()
                                still = request(f"{tenant}-reader", "GET", path, 200).content
                            else:
                                found = reader_sdk.ask(AskRequest(text=key, filters={"bucket": "documents"},
                                                                 synthesize=True)).model_dump(mode="json")
                                empty = reader_sdk.ask(AskRequest(text="zzqqnonexistent163")).model_dump(mode="json")
                                preview = admin_sdk.preview_policy_decision(PolicyEvaluationRequest(
                                    bucket="documents", key=key)).model_dump(mode="json")
                                still = reader_sdk.get_object("hot", "documents", key).content
                            assert any(result["citation"]["key"] == key for result in found["results"])
                            assert empty["results"] == [] and found["answer"] is None
                            provider_states = {item["component"]: item["state"] for item in found["providers"]}
                            assert provider_states["keyword"] == provider_states["vector"] == "missing"
                            assert provider_states["generation"] == "missing"
                            metadata_request = {"text": key, "filters": {"bucket": "documents"},
                                                "retrieval_mode": "metadata", "synthesize": False}
                            metadata_no_match = {"text": "zzqqnonexistent163", "retrieval_mode": "metadata"}
                            hybrid_request = {"text": key, "filters": {"bucket": "documents"}}
                            synthesis_no_match = {"text": "zzqqnonexistent163", "synthesize": True}
                            if interface == "API":
                                metadata = request(f"{tenant}-reader", "POST", "/v1/ask", 200,
                                                   json=metadata_request).json()
                                metadata_empty = request(f"{tenant}-reader", "POST", "/v1/ask", 200,
                                                         json=metadata_no_match).json()
                                hybrid = request(f"{tenant}-reader", "POST", "/v1/ask", 200,
                                                 json=hybrid_request).json()
                                synthesis_empty = request(f"{tenant}-reader", "POST", "/v1/ask", 200,
                                                          json=synthesis_no_match).json()
                            else:
                                metadata = reader_sdk.ask(AskRequest(**metadata_request)).model_dump(mode="json")
                                metadata_empty = reader_sdk.ask(AskRequest(**metadata_no_match)).model_dump(mode="json")
                                hybrid = reader_sdk.ask(AskRequest(**hybrid_request)).model_dump(mode="json")
                                synthesis_empty = reader_sdk.ask(AskRequest(**synthesis_no_match)).model_dump(mode="json")
                            metadata_states = {item["component"]: item["state"] for item in metadata["providers"]}
                            assert metadata_states == {"metadata": "succeeded", "keyword": "not_requested",
                                                       "vector": "not_requested", "generation": "not_requested"}
                            assert metadata_empty["results"] == []
                            assert metadata["answer"] is None and metadata_empty["answer"] is None
                            hybrid_states = {item["component"]: item["state"] for item in hybrid["providers"]}
                            assert hybrid_states == {"metadata": "succeeded", "keyword": "missing",
                                                     "vector": "missing", "generation": "not_requested"}
                            assert hybrid["answer"] is None and synthesis_empty["answer"] is None
                            assert synthesis_empty["results"] == []
                            for result, expected_states in ((metadata_empty, metadata_states), (empty, hybrid_states),
                                                            (synthesis_empty, provider_states)):
                                assert {item["component"]: item["state"] for item in result["providers"]} == expected_states
                            citation_downloads = []
                            for mode, result in (("metadata", metadata), ("default_hybrid", hybrid),
                                                 ("synthesize", found)):
                                citation = next(item["citation"] for item in result["results"]
                                                if item["citation"]["key"] == key)
                                if interface == "API":
                                    citation_bytes = request(f"{tenant}-reader", "GET",
                                                             "/v1/objects/{tier}/{bucket}/{key}".format(**citation), 200).content
                                else:
                                    citation_bytes = reader_sdk.get_object(
                                        citation["tier"], citation["bucket"], citation["key"],
                                    ).content
                                assert citation_bytes == body and hashlib.sha256(citation_bytes).hexdigest() == digest
                                citation_downloads.append({"mode": mode, "citation": citation,
                                                           "sha256": digest, "exact_bytes_match": True})
                            inventory = evidence["fixture"]["provider_inventory"]
                            assert all(inventory[name] is False for name in (
                                "keyword_configured", "vector_configured", "answer_configured", "policy_embedding_configured",
                            ))
                            record("MA-13", tenant, interface, repetition,
                                   "Explicit metadata and default hybrid queries distinguish unrequested/missing providers; citation download matches seed; no model provider configured",
                                   {"default_hybrid_synthesize_providers": provider_states,
                                    "default_hybrid_providers": hybrid_states,
                                    "explicit_metadata_providers": metadata_states,
                                    "no_match_counts": {"metadata": 0, "default_hybrid": 0, "synthesize": 0},
                                    "citation_downloads": citation_downloads,
                                    "provider_inventory": inventory})
                            assert still == body
                            catalog_after = (
                                request(f"{tenant}-reader", "GET",
                                        f"/v1/catalog/objects/documents/{key}", 200).json()
                                if interface == "API" else
                                reader_sdk.get_catalog_object("documents", key).model_dump(mode="json")
                            )
                            assert catalog_after["tier"] == catalog_before["tier"]
                            assert catalog_after["size"] == catalog_before["size"]
                            record("MA-05", tenant, interface, repetition,
                                   "Policy preview returns reasons without changing object bytes or placement", preview)
                            # Holds are reviewed synthetic mutations; preserve then release each fixture.
                            hold_input = {"bucket": "documents", "key": key, "reason": "Synthetic UAT hold"}
                            if interface == "API":
                                hold = request(f"{tenant}-admin", "POST", "/v1/legal-holds", 201,
                                               json=hold_input).json()
                            else:
                                hold = admin_sdk.place_legal_hold(LegalHoldRequest(**hold_input)).model_dump(mode="json")
                            for method in ("PUT", "DELETE"):
                                if interface == "API":
                                    denied = request(f"{tenant}-writer", method, path, 409,
                                                     content=b"held replacement" if method == "PUT" else None)
                                    assert denied.json()["error"]["code"] == "legal_hold"
                                else:
                                    try:
                                        if method == "PUT":
                                            sdk.put_object("hot", "documents", key, b"held replacement")
                                        else:
                                            sdk.delete_object("hot", "documents", key)
                                    except ConflictError as error:
                                        assert error.code == "legal_hold"
                                    else:
                                        raise AssertionError("SDK mutation bypassed legal hold")
                            assert request(f"{tenant}-reader", "GET", path, 200).content == body
                            if interface == "API":
                                request(f"{tenant}-admin", "POST", f"/v1/legal-holds/{hold['hold_id']}/release",
                                        200, json={"reason": "Synthetic review complete"})
                                request(f"{tenant}-writer", "DELETE", path, 204)
                            else:
                                admin_sdk.release_legal_hold(hold["hold_id"], LegalHoldReleaseRequest(
                                    reason="Synthetic review complete"))
                                sdk.delete_object("hot", "documents", key)
                            if interface == "API":
                                request(f"{tenant}-reader", "GET", path, 404)
                                request(f"{tenant}-reader", "GET", f"/v1/catalog/objects/documents/{key}", 404)
                            else:
                                for deleted_lookup in (
                                    lambda: reader_sdk.get_object("hot", "documents", key),
                                    lambda: reader_sdk.get_catalog_object("documents", key),
                                ):
                                    try:
                                        deleted_lookup()
                                    except NotFoundError as error:
                                        assert error.status_code == 404
                                    else:
                                        raise AssertionError("Deleted object or catalog entry is still readable")
                            record("MA-01", tenant, interface, repetition,
                                   "Dedicated key deletion leaves neither readable object nor catalog entry",
                                   {"key": key, "delete_status": 204, "object_status": 404, "catalog_status": 404})
                            record("MA-10", tenant, interface, repetition,
                                   "Hold denies overwrite/delete, preserves bytes; release permits deletion",
                                   {"hold_id": hold["hold_id"], "blocked_status": 409, "deleted_status": 204,
                                    "enforcement_interface": interface})

                        # Three persistent same-prefix entries force multiple pages.
                        for index in range(3):
                            sdk.put_object("hot", "documents", f"page/{index}.txt", body)
                        for repetition in range(1, 4):
                            if interface == "API":
                                keys, cursor = [], None
                                while True:
                                    page = request(f"{tenant}-reader", "GET", "/v1/catalog/objects", 200,
                                                   params={"bucket": "documents", "prefix": "page/", "limit": 1,
                                                           **({"cursor": cursor} if cursor else {})}).json()
                                    keys.extend(item["key"] for item in page["items"])
                                    cursor = page["page"]["next_cursor"]
                                    if cursor is None:
                                        break
                            else:
                                keys = [item.key for item in reader_sdk.iter_catalog_objects("documents", prefix="page/", limit=1)]
                            assert keys == [f"page/{index}.txt" for index in range(3)]
                            record("MA-01", tenant, interface, repetition,
                                   "Pagination has no duplicate or missing keys", keys)

                # Role, token, namespace, size, and honest service-unavailability negatives.
                for role, method, path, payload in (
                    ("reader", "PUT", "/v1/objects/hot/documents/denied.txt", None),
                    ("writer", "GET", "/v1/admin/storage", None),
                    ("operator", "POST", "/v1/policies/preview", {"bucket": "documents", "key": "manual-report.txt"}),
                    ("auditor", "GET", "/v1/objects/hot/documents/manual-report.txt", None),
                ):
                    request(f"{tenant}-{role}", method, path, 403, **({"json": payload} if payload else {}))
                    record("MA-07", tenant, "API", 1, f"{role} cannot perform {method} {path}", 403)
                for invalid, status in (("expired", 401), ("wrong-audience", 401)):
                    request(f"{tenant}-{invalid}", "GET", "/v1/catalog/objects?bucket=documents", status)
                    record("MA-09", tenant, "API", 1, f"{invalid} fixture is denied", status)
                for revoked in ("role", "tenant"):
                    roles_path, tenants_path = root / "roles.json", root / "tenants.json"
                    original_roles = json.loads(roles_path.read_text())
                    original_tenants = json.loads(tenants_path.read_text())
                    roles = json.loads(roles_path.read_text())
                    tenants = json.loads(tenants_path.read_text())
                    subject = f"{tenant}-revoked-{revoked}"
                    if revoked == "role":
                        roles["bindings"].append({"issuer": ISSUER, "subject": subject, "roles": ["reader"]})
                    else:
                        tenants["bindings"].append({"issuer": ISSUER, "subject": subject, "tenant_id": tenant})

                    def replace_policy(path: Path, document: object) -> None:
                        temporary = path.with_suffix(".pending.json")
                        write_json(temporary, document)
                        temporary.replace(path)

                    try:
                        replace_policy(roles_path, roles)
                        replace_policy(tenants_path, tenants)
                        request(subject, "GET", "/v1/catalog/objects?bucket=documents", 200)
                        replace_policy(roles_path, original_roles)
                        replace_policy(tenants_path, original_tenants)
                        request(subject, "GET", "/v1/catalog/objects?bucket=documents", 403)
                    finally:
                        replace_policy(roles_path, original_roles)
                        replace_policy(tenants_path, original_tenants)
                    record("MA-09", tenant, "API", 1,
                           f"Live {revoked} grant removal denies the same previously accepted JWT", [200, 403])
                request(f"{tenant}-admin", "POST", "/v1/actions/catalog-scans", 503,
                        json={"tier": "hot", "bucket": "documents"})
                record("MA-04", tenant, "API", 1, "Absent queue gives honest rejection; no accepted job", 503)
                try:
                    request(f"{tenant}-writer", "PUT", "/v1/objects/hot/documents/oversized.bin", 413,
                            content=b"x" * (16 * 1024 * 1024 + 1))
                except httpx.HTTPError as error:
                    observations.append({"scenario": "MA-14", "tenant": tenant, "interface": "API",
                                         "repetition": 1, "at": utc_now(), "status": "failed",
                                         "expected": "Complete HTTP 413 response for over-limit upload",
                                         "actual": {"error_type": type(error).__name__, "message": str(error)}})
                else:
                    record("MA-14", tenant, "API", 1, "Over-limit upload is rejected", {
                        "upload_size": 16 * 1024 * 1024 + 1, "upload_status": 413})
                request(f"{tenant}-reader", "POST", "/v1/ask", 413,
                        content=json.dumps({"text": "x" * (256 * 1024)}))
                record("MA-14", tenant, "API", 1, "Over-limit JSON is rejected", {"json_status": 413})

            for tenant, other in ((TENANTS[0], TENANTS[1]), (TENANTS[1], TENANTS[0])):
                own = f"{tenant}-admin"
                foreign = f"{other}-admin"
                key = f"isolation/{tenant}-only.txt"
                path = f"/v1/objects/hot/documents/{key}"
                body = fixture_body(tenant, "isolation", 1)
                request(own, "PUT", path, 201, content=body)
                for method in ("GET", "HEAD", "DELETE"):
                    request(foreign, method, path, 404)
                assert request(own, "GET", path, 200).content == body
                page = request(own, "GET", "/v1/catalog/objects", 200,
                               params={"bucket": "documents", "limit": 1}).json()
                assert page["page"]["next_cursor"]
                request(foreign, "GET", "/v1/catalog/objects", 422, params={
                    "bucket": "documents", "limit": 1, "cursor": page["page"]["next_cursor"]})
                hold = request(own, "POST", "/v1/legal-holds", 201, json={
                    "bucket": "documents", "key": key, "reason": "Foreign ID isolation fixture"}).json()
                request(foreign, "POST", f"/v1/legal-holds/{hold['hold_id']}/release", 404,
                        json={"reason": "Foreign identity must be denied"})
                request(own, "POST", f"/v1/legal-holds/{hold['hold_id']}/release", 200,
                        json={"reason": "Owner cleanup"})
                citation = request(own, "POST", "/v1/ask", 200, json={"text": key}).json()["results"][0]["citation"]
                assert request(foreign, "POST", "/v1/ask", 200, json={
                    "text": key, "filters": {"bucket": "documents", "key_prefix": key},
                }).json()["results"] == []
                record("MA-08", tenant, "API", 1,
                       "Foreign object and hold IDs/cursor/citation cannot disclose or mutate tenant state",
                       {"foreign_object_status": 404, "foreign_hold_status": 404,
                        "foreign_cursor_status": 422, "foreign_metadata_count": 0,
                        "citation_key": citation["key"], "retained_sha256": hashlib.sha256(body).hexdigest()})
            cli_rehearsal(root, run_dir, record)
        except Exception as error:
            evidence["failure"] = {"type": type(error).__name__, "message": str(error)[:2000]}
            raise
        finally:
            evidence["finished_at"] = utc_now()
            evidence["observation_count"] = len(observations)
            write_json(run_dir / "rehearsal.json", evidence)
            # A convenience snapshot; immutable prior run directories remain authoritative.
            write_json(root / "rehearsal.json", evidence)
    print(json.dumps({"evidence": str(run_dir / "rehearsal.json"), "observations": len(observations)}))
    return int(any(item["status"] == "failed" for item in observations))


def cli_rehearsal(root: Path, run_dir: Path, record) -> None:
    """Use only the ordinary CLI's separate disposable default tenant."""
    cli_root = root / "cli-default"
    cli_root.mkdir(exist_ok=True)
    drivers = cli_root / "drivers.json"
    write_json(drivers, {"tiers": {
        tier: {"driver": "posix", "path": str(cli_root / tier)} for tier in ("hot", "warm")
    }})
    common = [sys.executable, "-m", "cognistore.cli", "--no-config", "--json", "--drivers",
              str(drivers), "--catalog-db", str(cli_root / "catalog.db")]
    commands = []

    def run(*arguments: str):
        completed = subprocess.run([*common, *arguments], check=False, capture_output=True, text=True,
                                   env={**os.environ, "COGNISTORE_SECURITY_PROFILE": "development"})
        commands.append({"argv": ["python", "-m", "cognistore.cli", *common[3:], *arguments],
                         "returncode": completed.returncode, "stdout": completed.stdout,
                         "stderr": completed.stderr})
        write_json(run_dir / "cli-commands.json", commands)
        assert completed.returncode == 0, f"CLI {arguments[0]} failed: {completed.stderr[:500]}"
        return json.loads(completed.stdout)

    for repetition in range(1, 4):
        source = cli_root / f"source-{repetition}.bin"
        body = fixture_body("default", "CLI", repetition)
        source.write_bytes(body)
        key = f"cli-{repetition}.bin"
        run("put", "documents", key, str(source))
        destination = cli_root / f"download-{repetition}.bin"
        run("get", "documents", key, str(destination))
        assert destination.read_bytes() == body
        run("catalog-scan", "hot", "documents", "--prefix", key, "--sync")
        move_ids = []
        for source_tier, target_tier in (("hot", "warm"), ("warm", "hot")):
            move_id = f"manual-163-{repetition}-{source_tier}-{target_tier}-{int(time.time())}"
            run("move", source_tier, target_tier, "documents", key, "--idempotency-key", move_id)
            status = run("move-status", move_id)
            assert status["job"]["state"] == "completed"
            assert status["job"]["source_checksum"] == hashlib.sha256(body).hexdigest()
            assert status["job"]["destination_checksum"] == hashlib.sha256(body).hexdigest()
            move_ids.append({"idempotency_key": move_id, "status": status})
        run("get", "documents", key, str(destination))
        assert destination.read_bytes() == body
        record("MA-01/MA-03/MA-05", "default", "CLI", repetition,
               "Isolated trusted CLI upload/download/scan and hot-warm-hot POSIX moves preserve exact bytes",
               {"sha256": hashlib.sha256(body).hexdigest(), "moves": move_ids,
                "boundary": "default-tenant administrator only; no JWT or named-tenant evidence"})

    # Shipped synthetic documents exercise the real isolated extraction subprocess.
    from pypdf import PdfReader, PdfWriter

    assets = Path(__file__).resolve().parents[1] / "cognistore/samples/content_search/assets"
    fixtures = {
        "supported.pdf": assets / "incident-response.pdf",
        "supported.docx": assets / "database-backups.docx",
        "corrupt.pdf": cli_root / "corrupt.pdf",
        "unsupported.bin": cli_root / "unsupported.bin",
        "encrypted.pdf": cli_root / "encrypted.pdf",
    }
    fixtures["corrupt.pdf"].write_bytes(b"%PDF-1.7\ninvalid synthetic document\n")
    fixtures["unsupported.bin"].write_bytes(bytes(range(256)))
    writer = PdfWriter()
    for page in PdfReader(fixtures["supported.pdf"]).pages:
        writer.add_page(page)
    writer.encrypt("synthetic-fixture-only")
    writer.write(fixtures["encrypted.pdf"])
    for key, source in fixtures.items():
        run("put", "documents", f"extraction/{key}", str(source))
    for repetition in range(1, 4):
        run("catalog-scan", "hot", "documents", "--prefix", "extraction/", "--sync")
        actual = {}
        with SQLCatalog(cli_root / "catalog.db") as catalog:
            for key in fixtures:
                item = catalog.get("documents", f"extraction/{key}")
                assert item is not None
                extraction = item.metadata["document_extraction"]
                actual[key] = {"status": extraction["status"], "failure_code": extraction.get("failure_code"),
                               "text_bytes": extraction["text_bytes"],
                               "mime_detection": item.metadata["mime_detection"],
                               "pii_detection_status": item.metadata["pii_detection"]["status"]}
                if key.startswith("supported"):
                    assert extraction["status"] == "succeeded" and extraction["text_bytes"] > 0
                else:
                    expected_code = {"corrupt.pdf": "corrupt", "unsupported.bin": "unsupported_mime",
                                     "encrypted.pdf": "encrypted"}[key]
                    assert extraction["status"] == "failed" and extraction["failure_code"] == expected_code
                assert item.metadata["pii_detection"]["status"] == "disabled"
        record("MA-03/MA-14/MA-15", "default", "CLI", repetition,
               "PDF/DOCX extract; corrupt/encrypted/unsupported fixtures fail explicitly; unrelated docs succeed; PII disabled",
               actual)


if __name__ == "__main__":
    sys.exit(main())
