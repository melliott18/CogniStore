"""Adversarial tenant requests against real catalogs, storage, and JWT verification."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.auth.authorization import RBACAuthorizer, RBACPolicy, authorization_context
from cognistore.auth.principal import Principal, principal_context
from cognistore.auth.tenancy import (
    TenantIsolationError,
    TenantResolver,
    current_tenant_id,
    tenant_context,
)
from cognistore.core.audit import AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver
from tests.unit.test_api_authentication import ISSUER
from tests.unit.test_api_authentication import identity_provider as identity_provider
from tests.unit.test_rest_api import _RecordingSubmissionQueue


@pytest.fixture(params=["memory", "sqlite"])
def tenant_app(request, identity_provider, tmp_path: Path):
    authenticator, token, _ = identity_provider
    catalog = Catalog() if request.param == "memory" else SQLCatalog(tmp_path / "catalog.db")
    queue = _RecordingSubmissionQueue()
    gateway = CogniStoreGateway(
        catalog, {
            "hot": PosixDriver(str(tmp_path / "hot")),
            "warm": PosixDriver(str(tmp_path / "warm")),
        }, queue=queue,
    )
    app = create_app(
        gateway, authentication=authenticator,
        authorization=RBACAuthorizer(RBACPolicy({
            (ISSUER, subject): ["admin"] for subject in ("alice", "bob", "unassigned")
        })),
        tenancy=TenantResolver({(ISSUER, "alice"): "alpha", (ISSUER, "bob"): "beta"}),
    )
    headers = {
        subject: {"Authorization": "Bearer " + token(subject)}
        for subject in ("alice", "bob", "unassigned")
    }
    with TestClient(app) as client:
        yield client, gateway, queue, headers
    close = getattr(catalog, "close", None)
    if close is not None:
        close()


def test_objects_enumeration_search_policy_and_jobs_are_isolated(tenant_app):
    client, gateway, queue, headers = tenant_app
    alice, bob = headers["alice"], headers["bob"]
    object_url = "/v1/objects/hot/docs/alpha-secret.txt"
    response = client.put(object_url, content=b"secret alpha", headers=alice)
    assert response.status_code == 201, response.text
    for method in ("GET", "HEAD", "DELETE"):
        assert client.request(method, object_url, headers=bob).status_code == 404
    assert client.get("/v1/catalog/objects/docs/alpha-secret.txt", headers=bob).status_code == 404
    for filters in ("", "&prefix=alpha", "&tier=hot", "&prefix=alpha-secret.txt"):
        response = client.get("/v1/catalog/objects?bucket=docs" + filters, headers=bob)
        assert response.status_code == 200, response.text
        assert response.json()["items"] == []
    response = client.post("/v1/ask", headers=bob, json={
        "text": "alpha-secret", "filters": {"bucket": "docs", "key_prefix": "alpha"},
    })
    assert response.status_code == 200, response.text
    assert response.json()["results"] == []
    for path in ("/v1/policies/evaluate", "/v1/policies/preview"):
        assert client.post(path, headers=bob, json={
            "bucket": "docs", "key": "alpha-secret.txt",
        }).status_code == 404
    assert client.post("/v1/catalog/importance", headers=bob, json={
        "bucket": "docs", "key": "alpha-secret.txt", "level": "normal",
        "actor_id": "alice", "provenance": "crafted",
    }).status_code == 404

    # Equal logical coordinates are independent resources, including overwrites.
    assert client.put(object_url, content=b"beta value", headers=bob).status_code == 201
    assert client.get(object_url, headers=alice).content == b"secret alpha"
    assert client.get(object_url, headers=bob).content == b"beta value"
    assert client.delete(object_url, headers=bob).status_code == 204
    assert client.get(object_url, headers=alice).content == b"secret alpha"

    response = client.post("/v1/actions/catalog-scans", headers=alice, json={
        "tier": "hot", "bucket": "docs",
    })
    assert response.status_code == 202, response.text
    status_url = response.json()["status_url"]
    assert client.get(status_url, headers=alice).status_code == 200
    assert client.get(status_url, headers=bob).status_code == 404
    job, _ = queue.enqueued[0]
    assert job.tenant_id == "alpha"
    assert gateway.catalog.for_tenant("beta").list_audit_events(AuditQuery(job_id=job.job_id)) == []


def test_policy_decision_direct_ids_and_filters_cannot_cross_tenants(tenant_app):
    client, _, _, headers = tenant_app
    assert client.put(
        "/v1/objects/hot/docs/a.txt", content=b"alpha", headers=headers["alice"],
    ).status_code == 201
    response = client.post("/v1/catalog/importance", headers=headers["alice"], json={
        "bucket": "docs", "key": "a.txt", "level": "normal",
        "actor_id": "alice", "provenance": "owner requested",
    })
    assert response.status_code == 200, response.text
    decisions = client.get("/v1/policy-decisions", headers=headers["alice"]).json()["items"]
    assert decisions
    decision_id = decisions[0]["decision_id"]
    assert client.get("/v1/policy-decisions/" + decision_id, headers=headers["bob"]).status_code == 404
    for suffix in ("", "?bucket=docs", "?bucket=docs&key=a.txt"):
        response = client.get("/v1/policy-decisions" + suffix, headers=headers["bob"])
        assert response.status_code == 200, response.text
        assert response.json()["items"] == []


def test_tenant_hints_and_pagination_cannot_change_membership(tenant_app):
    client, _, _, headers = tenant_app
    for key in ("a.txt", "b.txt"):
        assert client.put("/v1/objects/hot/docs/" + key, headers=headers["alice"], content=b"a").status_code == 201
    cursor = client.get(
        "/v1/catalog/objects?bucket=docs&limit=1", headers=headers["alice"],
    ).json()["page"]["next_cursor"]
    assert cursor
    response = client.get("/v1/catalog/objects", params={
        "bucket": "docs", "limit": 1, "cursor": cursor,
    }, headers=headers["bob"])
    assert response.status_code == 422, response.text
    response = client.get("/v1/catalog/objects?bucket=docs&tenant_id=alpha", headers={
        **headers["bob"], "X-Tenant-ID": "alpha", "X-Tenant": "alpha",
    })
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert client.get("/v1/catalog/objects?bucket=docs", headers=headers["unassigned"]).status_code == 403
    assert client.get("/metrics", headers=headers["alice"]).status_code == 404
    assert client.get("/metrics").status_code == 404
    assert current_tenant_id() is None


def test_concurrent_requests_bind_tenant_context_through_thread_workers(tenant_app):
    client, _, _, headers = tenant_app

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as http:
            async def put_get(subject, index):
                body = f"{subject}-{index}".encode()
                url = f"/v1/objects/hot/docs/{index}.txt"
                response = await http.put(url, content=body, headers=headers[subject])
                assert response.status_code == 201, response.text
                response = await http.get(url, headers=headers[subject])
                assert response.content == body
            await asyncio.gather(*(put_get(subject, index) for subject in ("alice", "bob") for index in range(4)))
    asyncio.run(exercise())
    assert current_tenant_id() is None


def test_download_cannot_be_transferred_to_another_tenant(tenant_app):
    client, gateway, _, headers = tenant_app
    assert client.put("/v1/objects/hot/docs/a.txt", content=b"private", headers=headers["alice"]).status_code == 201
    with tenant_context("alpha"), principal_context(Principal(ISSUER, "alice")), authorization_context(client.app.state.authorizer):
        download = gateway.open_object("hot", "docs", "a.txt", byte_range=None)
    try:
        with tenant_context("beta"), pytest.raises(TenantIsolationError):
            next(download.chunks)
    finally:
        download.close()
