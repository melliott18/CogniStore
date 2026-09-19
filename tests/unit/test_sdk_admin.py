from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from cognistore.api import admin, admin_repairs
from cognistore.sdk import (
    CogniStoreClient,
    RepairPreviewRequest,
    RepairScope,
    RepairSubmitRequest,
    models,
)
from tests.unit.test_sdk_contract import _normalized_model_schema


@pytest.mark.parametrize("module,name", [
    (admin, name) for name in (
        "AdminSession", "AdminStorage", "DependencyHealth", "DriverCapabilityView",
        "DriverEncryptionView", "PoolView", "TierView", "JobHistoryPage", "JobHistoryError",
    )
] + [
    (admin_repairs, name) for name in (
        "RepairScope", "RepairPreviewRequest", "RepairSubmitRequest", "RepairPreviewResponse",
        "RepairAttempt", "RepairStatusResponse", "RepairListError", "RepairListResponse",
    )
])
def test_admin_sdk_models_match_public_api(module, name):
    assert _normalized_model_schema(getattr(module, name).model_json_schema()) == (
        _normalized_model_schema(getattr(models, name).model_json_schema())
    )


def test_admin_sdk_transports_methods_queries_and_confirmed_scope():
    scope = {"tenant_id": "tenant-a", "bucket": "docs", "prefix": "reports/", "tiers": ["hot"]}
    status = {"repair_id": "repair:one", "scope": scope, "status": "ready", "history": []}
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer test-token"
        path = request.url.path
        if path == "/v1/admin/session":
            return httpx.Response(200, json={
                "tenant_id": "tenant-a", "actor_id": "operator", "operations": ["list_jobs"],
                "future_field": "ignored",
            })
        if path == "/v1/admin/storage":
            return httpx.Response(200, json={
                "tenant_id": "tenant-a", "observed_at": "2026-09-19T01:00:00Z",
                "catalog": {"status": "ready"}, "queue": {"status": "unavailable"},
                "tiers": [{"name": "hot", "active": True, "driver": "PosixDriver",
                           "health": {"status": "unverified"}, "capabilities": {}, "encryption": {}}],
            })
        if path == "/v1/jobs":
            assert dict(request.url.params) == {"limit": "2", "cursor": "opaque+/="}
            return httpx.Response(200, json={
                "items": [], "page": {"limit": 2, "next_cursor": "next"},
                "errors": [{"job_id": "unavailable-job", "message": "Job status unavailable"}],
            })
        if path == "/v1/admin/repairs/preview":
            assert request.method == "POST"
            assert json.loads(request.content) == {"repair_id": "repair:one"}
            return httpx.Response(200, json={
                "repair_id": "repair:one", "scope": scope, "counts": {"eligible": 1},
                "actions": [{"kind": "repair"}], "preview_token": "a" * 64,
            })
        if path == "/v1/admin/repairs" and request.method == "POST":
            submitted = admin_repairs.RepairSubmitRequest.model_validate_json(request.content)
            assert submitted.confirmation.model_dump() == scope
            assert submitted.preview_token == "a" * 64
            return httpx.Response(200, json={**status, "status": "completed"})
        if path == "/v1/admin/repairs":
            return httpx.Response(200, json={"items": [status], "errors": [
                {"repair_id": "missing", "message": "Repair report unavailable"},
            ]})
        assert path == "/v1/admin/repairs/repair:one"
        assert request.url.raw_path == b"/v1/admin/repairs/repair%3Aone"
        return httpx.Response(200, json=status)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with CogniStoreClient("https://sdk.test", http_client=transport,
                              default_headers={"Authorization": "Bearer test-token"}) as sdk:
            session = sdk.get_admin_session()
            assert session.tenant_id == "tenant-a"
            assert not hasattr(session, "future_field")
            storage = sdk.get_admin_storage()
            assert storage.queue.status == "unavailable"
            assert storage.tiers[0].health.status == "unverified"
            jobs = sdk.list_jobs(limit=2, cursor="opaque+/=")
            assert jobs.errors[0].message == "Job status unavailable"
            assert jobs.page.next_cursor == "next"
            repairs = sdk.list_repairs()
            assert repairs.items[0].scope.bucket == "docs"
            assert repairs.errors[0].repair_id == "missing"
            assert sdk.get_repair("repair:one").status == "ready"
            preview = sdk.preview_repair(RepairPreviewRequest(repair_id="repair:one"))
            submitted = sdk.submit_repair(RepairSubmitRequest(
                repair_id=preview.repair_id, preview_token=preview.preview_token,
                confirmation=RepairScope(**scope),
            ))
            assert submitted.status == "completed"
    assert len(calls) == 7


@pytest.mark.parametrize("fields", [
    {}, {"preview_token": "bad"}, {"confirmation": {"tenant_id": "tenant-a"}},
    {"confirmation": {"tenant_id": "tenant-a", "bucket": "docs", "prefix": "", "tiers": []}},
])
def test_sdk_repair_submission_requires_valid_explicit_confirmation(fields):
    with pytest.raises(ValidationError):
        RepairSubmitRequest(repair_id="repair:one", **fields)
