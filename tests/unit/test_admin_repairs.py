"""Administration repair requests preserve the existing recovery safety boundary."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from cognistore.api.admin_repairs import RepairPreviewRequest, RepairSubmitRequest
from cognistore.api.app import create_app
from cognistore.api.errors import (
    BackendUnavailableError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from cognistore.api.gateway import CogniStoreGateway
from cognistore.auth.authorization import AuthorizationError, RBACAuthorizer, RBACPolicy
from cognistore.auth.principal import Principal, principal_context
from cognistore.auth.tenancy import TenantResolver
from cognistore.core.audit import AuditContext
from cognistore.core.catalog import Catalog
from cognistore.core.consistency import ConsistencyScanner, ScanScope
from cognistore.core.move_jobs import MoveJobState
from cognistore.core.mover import Mover
from cognistore.drivers.posix_driver import PosixDriver
from tests.unit.test_api_authentication import ISSUER
from tests.unit.test_api_authentication import identity_provider as identity_provider

BUCKET, KEY, BODY = "documents", "reports/a.txt", b"verified bytes survive repair"
SCOPE = ScanScope("default", BUCKET, "reports/", ("hot", "warm"))


class Interrupted(BaseException):
    pass


@pytest.fixture
def repairs(tmp_path, monkeypatch):
    monkeypatch.delenv("COGNISTORE_LOCALITY_CONFIG", raising=False)
    monkeypatch.setattr("cognistore.core.consistency._RateLimiter.acquire", lambda *_: None)
    catalog = Catalog()
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in SCOPE.tiers}
    drivers["hot"].put_object(BUCKET, KEY, BODY)
    catalog.upsert(BUCKET, KEY, len(BODY), "hot", {
        "sha256": hashlib.sha256(BODY).hexdigest(), "sample_len": len(BODY),
    })

    def crash(job):
        if job.state == MoveJobState.PREPARED:
            raise Interrupted()

    with pytest.raises(Interrupted):
        Mover(drivers, catalog, clock=lambda: datetime.now(timezone.utc) - timedelta(hours=1),
              lease_seconds=1, transition_hook=crash).move(
            "hot", "warm", BUCKET, KEY, idempotency_key="original-move",
        )
    report = tmp_path / "report.sqlite3"
    ConsistencyScanner(catalog, drivers, SCOPE, binding_id="trusted-source").run(report)
    config = tmp_path / "reports.json"
    config.write_text(json.dumps([
        {"repair_id": "report-one", "tenant_id": "default", "path": str(report), "binding_id": "trusted-source"},
        {"repair_id": "other-tenant", "tenant_id": "other", "path": str(tmp_path / "private"), "binding_id": "secret"},
    ]))
    monkeypatch.setenv("COGNISTORE_ADMIN_REPAIR_REPORTS", str(config))
    authorization = RBACAuthorizer(RBACPolicy({(ISSUER, "alice"): {"admin"}}))
    gateway = CogniStoreGateway(catalog, drivers, authorization=authorization)
    return gateway, report, config


def _client(gateway, identity_provider):
    authenticator, issue_token, _ = identity_provider
    return TestClient(create_app(gateway, authentication=authenticator), headers={
        "Authorization": "Bearer " + issue_token(),
    })


def test_preview_confirmation_execution_history_and_retry(repairs, identity_provider):
    gateway, report, _ = repairs
    before_report = report.read_bytes()
    before_job = gateway.catalog.get_move_job("original-move")
    with _client(gateway, identity_provider) as client:
        listing = client.get("/v1/admin/repairs")
        assert listing.status_code == 200, listing.text
        assert [item["repair_id"] for item in listing.json()["items"]] == ["report-one"]
        assert "other-tenant" not in listing.text
        preview = client.post("/v1/admin/repairs/preview", json={"repair_id": "report-one"})
        assert preview.status_code == 200, preview.text
        plan = preview.json()
        assert plan["counts"] == {"planned": 1}
        assert plan["actions"][0]["source_tier"] == "hot"
        assert plan["actions"][0]["destination_tier"] == "warm"
        assert report.read_bytes() == before_report
        assert gateway.catalog.get_move_job("original-move") == before_job
        payload = {"repair_id": "report-one", "preview_token": plan["preview_token"], "confirmation": plan["scope"]}
        rejected = client.post("/v1/admin/repairs", json={**payload, "confirmation": {**plan["scope"], "prefix": ""}})
        assert rejected.status_code == 422
        assert gateway.catalog.get_move_job("original-move") == before_job
        result = client.post("/v1/admin/repairs", json=payload)
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "completed"
        assert result.json()["counts"] == {"completed": 1}
        assert len(result.json()["history"]) == 2
        assert gateway.catalog.get(BUCKET, KEY).tier == "warm"
        assert gateway.drivers["warm"].get_object(BUCKET, KEY) == BODY
        with pytest.raises(FileNotFoundError):
            gateway.drivers["hot"].stat_object(BUCKET, KEY)
        transitions = gateway.catalog.list_move_job_transitions("original-move")
        replay = client.post("/v1/admin/repairs", json=payload)
        assert replay.status_code == 200
        assert replay.json()["counts"] == result.json()["counts"]
        assert gateway.catalog.list_move_job_transitions("original-move") == transitions
        status = client.get("/v1/admin/repairs/report-one")
        assert status.json()["status"] == "completed"
        assert len(status.json()["history"]) == 2
        assert str(report) not in status.text
        assert "trusted-source" not in status.text
    replacement = CogniStoreGateway(gateway.catalog, gateway._base_drivers, authorization=gateway.authorization)
    with principal_context(Principal(ISSUER, "alice")):
        assert replacement.get_repair("report-one").status == "completed"


@pytest.mark.parametrize("change", ["legal_hold", "generation"])
def test_changed_evidence_requires_new_preview_and_preserves_source(repairs, identity_provider, change):
    gateway, _, _ = repairs
    with _client(gateway, identity_provider) as client:
        plan = client.post("/v1/admin/repairs/preview", json={"repair_id": "report-one"}).json()
        if change == "legal_hold":
            gateway.catalog.place_legal_hold(BUCKET, key=KEY, reason="preserve evidence",
                                            context=AuditContext("new-hold", "user", "operator"))
        else:
            gateway.drivers["hot"].put_object(BUCKET, KEY, BODY)
        result = client.post("/v1/admin/repairs", json={
            "repair_id": "report-one", "preview_token": plan["preview_token"], "confirmation": plan["scope"],
        })
        assert result.status_code == 409, result.text
        assert gateway.drivers["hot"].get_object(BUCKET, KEY) == BODY
        assert gateway.catalog.get_move_job("original-move").state == MoveJobState.PREPARED


def test_quarantined_candidates_have_review_status_without_mutation(repairs, identity_provider):
    gateway, _, _ = repairs
    gateway.catalog.place_legal_hold(BUCKET, key=KEY, reason="preserve evidence",
                                    context=AuditContext("hold", "user", "operator"))
    with _client(gateway, identity_provider) as client:
        plan = client.post("/v1/admin/repairs/preview", json={"repair_id": "report-one"}).json()
        assert plan["counts"] == {"quarantined": 1}
        result = client.post("/v1/admin/repairs", json={
            "repair_id": "report-one", "preview_token": plan["preview_token"], "confirmation": plan["scope"],
        })
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "review_required"
        assert gateway.drivers["hot"].get_object(BUCKET, KEY) == BODY


def test_tenant_hidden_paths_not_accepted_and_registry_partial_failure(repairs, identity_provider):
    gateway, report, config = repairs
    entries = json.loads(config.read_text())
    entries.append({"repair_id": "unavailable", "tenant_id": "default", "path": str(report.parent / "missing"), "binding_id": "x"})
    config.write_text(json.dumps(entries))
    with _client(gateway, identity_provider) as client:
        result = client.get("/v1/admin/repairs").json()
        assert len(result["items"]) == 1
        assert result["errors"] == [{"repair_id": "unavailable", "message": "Repair report unavailable"}]
        assert client.get("/v1/admin/repairs/other-tenant").status_code == 404
        assert client.post("/v1/admin/repairs/preview", json={"repair_id": str(report)}).status_code == 422
        assert client.post("/v1/admin/repairs/preview", json={"repair_id": "report-one", "path": str(report)}).status_code == 422
        entries[0]["binding_id"] = "forged-binding"
        config.write_text(json.dumps(entries))
        assert client.post("/v1/admin/repairs/preview", json={"repair_id": "report-one"}).status_code == 503


@pytest.mark.parametrize("operation", ["preview_repair", "submit_repair", "get_repair", "list_repairs"])
def test_direct_service_authorizes_before_arguments_or_configuration(repairs, monkeypatch, operation):
    gateway, _, _ = repairs
    gateway.authorization = RBACAuthorizer()
    monkeypatch.setattr(gateway, "_repair_reports", lambda: pytest.fail("denial must precede configuration read"))
    with principal_context(Principal(ISSUER, "alice")), pytest.raises(AuthorizationError):
        method = getattr(gateway, operation)
        method() if operation == "list_repairs" else method(None)


def test_missing_registry_empty_and_invalid_registry_fail_closed(repairs, monkeypatch):
    gateway, _, config = repairs
    with principal_context(Principal(ISSUER, "alice")):
        monkeypatch.delenv("COGNISTORE_ADMIN_REPAIR_REPORTS")
        assert gateway.list_repairs().items == []
        with pytest.raises(ResourceNotFoundError):
            gateway.get_repair("report-one")
        monkeypatch.setenv("COGNISTORE_ADMIN_REPAIR_REPORTS", str(config))
        config.write_text("not json")
        with pytest.raises(BackendUnavailableError):
            gateway.list_repairs()


def test_anonymous_service_cannot_preview_or_submit(repairs):
    gateway, _, _ = repairs
    gateway.authorization = None
    with pytest.raises(AuthorizationError):
        gateway.preview_repair(RepairPreviewRequest(repair_id="report-one"))
    with pytest.raises(AuthorizationError):
        gateway.submit_repair(RepairSubmitRequest(
            repair_id="report-one", preview_token="0" * 64, confirmation=SCOPE.to_dict(),
        ))


def test_in_flight_retry_conflicts_then_replays_recorded_outcome(repairs, monkeypatch):
    gateway, _, _ = repairs
    started, release = threading.Event(), threading.Event()
    append = gateway._append_repair_event

    def pause_after_start(report, event_type, details):
        append(report, event_type, details)
        if event_type == "repair.started":
            started.set()
            assert release.wait(timeout=5)

    monkeypatch.setattr(gateway, "_append_repair_event", pause_after_start)
    with principal_context(Principal(ISSUER, "alice")):
        plan = gateway.preview_repair(RepairPreviewRequest(repair_id="report-one"))
        request = RepairSubmitRequest(repair_id="report-one", preview_token=plan.preview_token, confirmation=plan.scope)

        def submit():
            with principal_context(Principal(ISSUER, "alice")):
                return gateway.submit_repair(request)

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(submit)
            try:
                assert started.wait(timeout=5)
                assert gateway.get_repair("report-one").status == "running"
                with pytest.raises(ResourceConflictError):
                    gateway.submit_repair(request)
            finally:
                release.set()
            assert pending.result(timeout=5).status == "completed"
        assert gateway.submit_repair(request).status == "completed"
        assert len(gateway.catalog.list_move_job_transitions("original-move")) == 6


def test_authenticated_tenants_cannot_access_each_others_repair(repairs, identity_provider):
    gateway, _, _ = repairs
    authenticator, issue_token, _ = identity_provider
    gateway.authorization = RBACAuthorizer(RBACPolicy({
        (ISSUER, "alice"): {"admin"}, (ISSUER, "bob"): {"admin"},
    }))
    resolver = TenantResolver({(ISSUER, "alice"): "default", (ISSUER, "bob"): "other"})
    with TestClient(create_app(gateway, authentication=authenticator, tenancy=resolver)) as client:
        alice = {"Authorization": "Bearer " + issue_token("alice")}
        bob = {"Authorization": "Bearer " + issue_token("bob"), "X-Tenant-ID": "default"}
        plan = client.post("/v1/admin/repairs/preview", headers=alice, json={"repair_id": "report-one"}).json()
        assert client.get("/v1/admin/repairs/report-one", headers=bob).status_code == 404
        assert client.post("/v1/admin/repairs/preview", headers=bob, json={"repair_id": "report-one"}).status_code == 404
        assert client.post("/v1/admin/repairs", headers=bob, json={
            "repair_id": "report-one", "preview_token": plan["preview_token"], "confirmation": plan["scope"],
        }).status_code == 404
        listing = client.get("/v1/admin/repairs?tenant_id=default", headers=bob)
        assert listing.status_code == 200
        assert "report-one" not in listing.text
    assert gateway.catalog.get_move_job("original-move").state == MoveJobState.PREPARED


def test_mutable_checkpoint_cannot_expand_the_audited_scope(repairs, identity_provider):
    gateway, report, _ = repairs
    with sqlite3.connect(report) as connection:
        state = json.loads(connection.execute("SELECT state FROM checkpoint").fetchone()[0])
        state["scope"]["prefix"] = ""
        connection.execute("UPDATE checkpoint SET state=?", (json.dumps(state),))
    with _client(gateway, identity_provider) as client:
        response = client.post("/v1/admin/repairs/preview", json={"repair_id": "report-one"})
        assert response.status_code == 503
    assert gateway.catalog.get_move_job("original-move").state == MoveJobState.PREPARED


def test_history_is_bounded_but_old_completed_receipts_remain_replayable(repairs):
    gateway, _, _ = repairs
    with principal_context(Principal(ISSUER, "alice")):
        plan = gateway.preview_repair(RepairPreviewRequest(repair_id="report-one"))
        request = RepairSubmitRequest(repair_id="report-one", preview_token=plan.preview_token, confirmation=plan.scope)
        assert gateway.submit_repair(request).counts == {"completed": 1}
        report = gateway._repair_report("report-one")
        for number in range(55):
            gateway._append_repair_event(report, "repair.completed", {
                "plan_digest": str(number), "result": {"counts": {"resolved": 1}, "actions": []},
            })
        current = gateway.get_repair("report-one")
        assert len(current.history) == 50
        assert current.counts == {"resolved": 1}
        replay = gateway.submit_repair(request)
        assert replay.counts == {"completed": 1}
        assert len(replay.actions) == 1
        assert len(gateway.catalog.list_move_job_transitions("original-move")) == 6
