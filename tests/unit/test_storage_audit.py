"""Storage mutations keep intent/outcome evidence across transport boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.auth.authorization import RBACAuthorizer, RBACPolicy
from cognistore.auth.principal import Principal, principal_context
from cognistore.cli import cognistore_cli
from cognistore.core.audit import AuditEventType, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.drivers.posix_driver import PosixDriver


def _storage_events(catalog):
    return catalog.list_audit_events(AuditQuery(
        event_types=frozenset({AuditEventType.STORAGE_OPERATION}),
    ))


def test_api_write_and_delete_have_correlated_intent_and_outcome(tmp_path: Path) -> None:
    catalog = Catalog()
    gateway = CogniStoreGateway(catalog, {"hot": PosixDriver(str(tmp_path / "hot"))})
    with TestClient(create_app(gateway)) as client:
        assert client.put(
            "/v1/objects/hot/documents/report.txt", content=b"report",
            headers={"X-Request-ID": "write-audit-63"},
        ).status_code == 201
        assert client.delete(
            "/v1/objects/hot/documents/report.txt",
            headers={"X-Request-ID": "delete-audit-63"},
        ).status_code == 204
    events = _storage_events(catalog)
    assert len(events) == 4
    for correlation, operation in (("write-audit-63", "put_object"),
                                   ("delete-audit-63", "delete_object")):
        started, finished = [event for event in events if event.correlation_id == correlation]
        assert (started.outcome, finished.outcome) == ("started", "succeeded")
        assert finished.causation_id == started.event_id
        assert finished.details == {"operation": operation, "tier": "hot"}
        assert (finished.bucket, finished.object_key) == ("documents", "report.txt")


def test_storage_failure_records_safe_error_and_verified_actor(tmp_path: Path, monkeypatch) -> None:
    catalog = Catalog()
    driver = PosixDriver(str(tmp_path / "hot"))
    gateway = CogniStoreGateway(catalog, {"hot": driver})
    principal = Principal("https://issuer.example", "operator-63")
    gateway.authorization = RBACAuthorizer(RBACPolicy({
        (principal.issuer, principal.subject): ["writer"],
    }))

    def broken(*args, **kwargs):
        raise OSError("password=do-not-retain-this")

    monkeypatch.setattr(driver, "put_object", broken)
    with principal_context(principal), pytest.raises(OSError):
        gateway.put_object("hot", "documents", "report.txt", b"report",
                           overwrite=True, content_type=None)
    started, failed = _storage_events(catalog)
    assert failed.outcome == "failed"
    assert failed.actor_id == principal.actor_id
    assert failed.actor_type == "authenticated"
    assert failed.correlation_id == started.correlation_id
    assert failed.causation_id == started.event_id
    assert failed.details["error_type"] == "OSError"
    assert "do-not-retain-this" not in json.dumps(dict(failed.details))


def test_unavailable_intent_audit_prevents_storage_mutation(tmp_path: Path, monkeypatch) -> None:
    catalog = Catalog()
    driver = PosixDriver(str(tmp_path / "hot"))
    gateway = CogniStoreGateway(catalog, {"hot": driver})

    def unavailable(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(catalog, "append_audit_event", unavailable)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        gateway.put_object("hot", "documents", "report.txt", b"report",
                           overwrite=True, content_type=None)
    assert list(driver.list_objects("documents")) == []
    assert catalog.get("documents", "report.txt") is None


def test_cli_put_and_get_are_audited(tmp_path: Path, monkeypatch) -> None:
    catalog = Catalog()
    monkeypatch.setattr(cognistore_cli, "Catalog", lambda **kwargs: catalog)
    source = tmp_path / "source.txt"
    source.write_bytes(b"report")
    options = ["--no-config", "--base", str(tmp_path / "storage")]
    assert cognistore_cli.main([*options, "put", "documents", "report.txt", str(source)]) == 0
    assert cognistore_cli.main([
        *options, "get", "documents", "report.txt", str(tmp_path / "download.txt"),
    ]) == 0
    events = _storage_events(catalog)
    assert [event.outcome for event in events] == ["started", "succeeded"] * 2
    assert [event.details["operation"] for event in events] == ["put_object"] * 2 + ["get_object"] * 2
    assert all(event.actor_type == "operator" for event in events)
    assert (tmp_path / "download.txt").read_bytes() == b"report"
