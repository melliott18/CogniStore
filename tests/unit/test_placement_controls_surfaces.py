"""Public mutation, preview, and durable movement contracts for ticket #44."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.cli import cognistore_cli
from cognistore.core.audit import AuditContext, AuditEventType, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.core.move_jobs import MoveJobState
from cognistore.core.mover import Mover
from cognistore.core.placement_controls import (
    ImportanceTag,
    MovementConstraintError,
    MovementConstraints,
)
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver


def test_api_tag_change_audits_and_reevaluates_without_moving(tmp_path: Path) -> None:
    catalog = Catalog()
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    drivers["hot"].put_object("bucket", "nested/key", b"large")
    catalog.upsert("bucket", "nested/key", 5, "hot")
    body = {
        "bucket": "bucket", "key": "nested/key", "level": "critical",
        "actor_id": "operator-44", "provenance": "customer-request-44",
        "config": {"threshold": 1},
    }
    with TestClient(create_app(CogniStoreGateway(catalog, drivers))) as client:
        response = client.post("/v1/catalog/importance", json=body,
                               headers={"X-Request-ID": "tag-change-44"})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["action"] == "stay"
        assert result["constraints"]["importance"]["level"] == "critical"
        assert result["constraints"]["importance_revision"] == 1
        assert result["constraints"]["allowed_destination_tiers"] == ["hot"]
        events = catalog.list_audit_events(AuditQuery(correlation_id="tag-change-44"))
        assert {event.event_type for event in events} == {
            AuditEventType.IMPORTANCE_CHANGED.value, AuditEventType.POLICY_DECISION.value,
        }
        assert all(event.actor_id == "operator-44" for event in events)
        assert catalog.get("bucket", "nested/key").tier == "hot"
        assert drivers["hot"].get_object("bucket", "nested/key") == b"large"
        cleared = client.post("/v1/catalog/importance", json={**body, "level": None},
                              headers={"X-Request-ID": "tag-clear-44"})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["action"] == "move"
        assert cleared.json()["constraints"]["importance"] is None
        events = catalog.list_audit_events(AuditQuery(correlation_id="tag-clear-44"))
        tag_event = next(e for e in events if e.event_type == AuditEventType.IMPORTANCE_CHANGED)
        assert "customer-request-44" in json.dumps(tag_event.details)
        assert catalog.get("bucket", "nested/key").tier == "hot"


@pytest.mark.parametrize("change", [
    {"level": "urgent"}, {"actor_id": " "}, {"provenance": ""},
    {"config": {"movement_constraints": {"minimum_residency_seconds": {"hot": True}}}},
    {"config": {"movement_constraints": {"minimum_residency_seconds": {"hot": -1}}}},
    {"config": {"movement_constraints": {"unknown": 1}}},
    {"config": {"movement_constraints": {"minimum_residency_seconds": {"wram": 3600}}}},
])
def test_api_invalid_controls_never_change_tags(tmp_path: Path, change: dict) -> None:
    catalog = Catalog()
    catalog.upsert("bucket", "key", 5, "hot")
    body = {"bucket": "bucket", "key": "key", "level": "normal",
            "actor_id": "operator", "provenance": "request", **change}
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    with TestClient(create_app(CogniStoreGateway(catalog, drivers))) as client:
        assert client.post("/v1/catalog/importance", json=body).status_code == 422
    assert catalog.get("bucket", "key").importance_revision == 0
    assert catalog.list_audit_events() == []


def test_api_evaluation_reports_residency_without_writes(tmp_path: Path) -> None:
    catalog = Catalog()
    catalog.upsert("bucket", "key", 5, "hot")
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    with TestClient(create_app(CogniStoreGateway(catalog, drivers))) as client:
        result = client.post("/v1/policies/evaluate", json={
            "bucket": "bucket", "key": "key", "config": {
                "threshold": 1,
                "movement_constraints": {"minimum_residency_seconds": {"hot": 3600}},
            },
        })
    assert result.status_code == 200, result.text
    assert result.json()["action"] == "stay"
    assert result.json()["constraints"]["residency_active"] is True
    assert catalog.list_audit_events() == []


def test_cli_importance_preview_and_change_then_policy_preview(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "catalog.sqlite"
    with SQLiteCatalog(database) as catalog:
        catalog.upsert("bucket", "key", 5, "hot")
    drivers_path = tmp_path / "drivers.yaml"
    drivers_path.write_text(
        f"tiers:\n  hot:\n    driver: posix\n    path: {tmp_path / 'hot'}\n"
        f"  warm:\n    driver: posix\n    path: {tmp_path / 'warm'}\n"
    )
    base = ["--no-config", "--drivers", str(drivers_path), "--catalog-db", str(database)]
    command = [*base, "importance-set", "bucket", "key", "critical", "--actor", "operator",
               "--provenance", "request-44", "--threshold", "1", "--json"]
    assert cognistore_cli.main([*command, "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["evaluation"]["action"] == "stay"
    with SQLiteCatalog(database) as catalog:
        assert catalog.get("bucket", "key").importance is None
        assert catalog.list_audit_events() == []
    assert cognistore_cli.main(command) == 0
    changed = json.loads(capsys.readouterr().out)
    assert changed["evaluation"]["constraints"]["importance"]["level"] == "critical"
    assert cognistore_cli.main([*base, "policy-run", "bucket", "--dry-run", "--json",
                              "--minimum-residency", "hot", "3600"]) == 0
    evaluated = json.loads(capsys.readouterr().out)["evaluations"][0]
    assert evaluated["constraints"]["residency_active"] is True


def test_mover_rechecks_tag_at_commit_and_retains_source(tmp_path: Path) -> None:
    catalog = Catalog()
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    drivers["hot"].put_object("bucket", "key", b"payload")
    catalog.upsert("bucket", "key", 7, "hot")
    context = AuditContext(correlation_id="tag-during-move", actor_type="user", actor_id="operator")

    def hook(job):
        if job.state == MoveJobState.VERIFIED:
            catalog.set_importance("bucket", "key", ImportanceTag(
                "critical", "user", "operator", "protect-during-transfer",
                datetime.now(timezone.utc).isoformat(),
            ), audit_context=context)

    mover = Mover(drivers, catalog, transition_hook=hook)
    with pytest.raises(MovementConstraintError):
        mover.move("hot", "warm", "bucket", "key",
                idempotency_key="move-44")
    assert catalog.get("bucket", "key").tier == "hot"
    assert drivers["hot"].get_object("bucket", "key") == b"payload"
    with pytest.raises(MovementConstraintError):
        Mover(drivers, catalog).move("hot", "warm", "bucket", "key",
                idempotency_key="move-44")


def test_mover_recovery_keeps_original_policy_constraints(tmp_path: Path) -> None:
    catalog = Catalog()
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    drivers["hot"].put_object("bucket", "key", b"payload")
    catalog.upsert("bucket", "key", 7, "hot")
    catalog.set_importance("bucket", "key", ImportanceTag(
        "critical", "user", "operator", "custom-placement",
        datetime.now(timezone.utc).isoformat(),
    ), audit_context=AuditContext(correlation_id="custom", actor_type="user", actor_id="operator"))

    def crash(job):
        if job.state == MoveJobState.PREPARED:
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        Mover(drivers, catalog, transition_hook=crash).move(
            "hot", "warm", "bucket", "key",
                idempotency_key="custom-controls-44",
            movement_constraints=MovementConstraints(importance_tiers={"critical": ("warm",)}),
        )
    Mover(drivers, catalog, clock=lambda: datetime.now(timezone.utc) + timedelta(seconds=31)).move(
        "hot", "warm", "bucket", "key",
                idempotency_key="custom-controls-44"
    )
    assert catalog.get("bucket", "key").tier == "warm"
    assert drivers["warm"].get_object("bucket", "key") == b"payload"


def test_retry_rejects_changed_movement_contract_before_side_effects(tmp_path: Path) -> None:
    from cognistore.core.move_jobs import MoveJobConflictError

    catalog = Catalog()
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    drivers["hot"].put_object("bucket", "key", b"payload")
    catalog.upsert("bucket", "key", 7, "hot")

    def crash(job):
        if job.state == MoveJobState.PREPARED:
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        Mover(drivers, catalog, transition_hook=crash).move(
            "hot", "warm", "bucket", "key",
                idempotency_key="retry-contract-44"
        )
    with pytest.raises(MoveJobConflictError, match="Movement constraints differ"):
        Mover(drivers, catalog).move(
            "hot", "warm", "bucket", "key",
                idempotency_key="retry-contract-44",
            movement_constraints=MovementConstraints(minimum_residency_seconds={"hot": 3600}),
        )
    assert drivers["hot"].get_object("bucket", "key") == b"payload"
    with pytest.raises(FileNotFoundError):
        drivers["warm"].stat_object("bucket", "key")


def test_internal_movement_contract_is_not_published_as_storage_metadata(tmp_path: Path) -> None:
    class InspectingDriver(PosixDriver):
        def put_object_stream(self, bucket, key, stream, *, size, metadata=None, overwrite=True):
            assert "cognistore_movement_constraints" not in (metadata or {})
            return super().put_object_stream(
                bucket, key, stream, size=size, metadata=metadata, overwrite=overwrite
            )

    catalog = Catalog()
    hot = PosixDriver(tmp_path / "hot")
    warm = InspectingDriver(tmp_path / "warm")
    hot.put_object("bucket", "key", b"payload")
    catalog.upsert("bucket", "key", 7, "hot")
    Mover({"hot": hot, "warm": warm}, catalog).move("hot", "warm", "bucket", "key")
    assert warm.get_object("bucket", "key") == b"payload"


def test_tag_response_and_reevaluation_audit_use_own_committed_revision(tmp_path: Path) -> None:
    class RacingCatalog(Catalog):
        def set_importance(self, bucket, key, tag, **kwargs):
            committed = super().set_importance(bucket, key, tag, **kwargs)
            super().set_importance(bucket, key, ImportanceTag(
                "normal", "user", "second-operator", "later-change",
                datetime.now(timezone.utc).isoformat(),
            ), audit_context=AuditContext(
                correlation_id="later-change", actor_type="user", actor_id="second-operator"
            ))
            return committed

    catalog = RacingCatalog()
    catalog.upsert("bucket", "key", 5, "hot")
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    with TestClient(create_app(CogniStoreGateway(catalog, drivers))) as client:
        response = client.post("/v1/catalog/importance", json={
            "bucket": "bucket", "key": "key", "level": "critical",
            "actor_id": "first-operator", "provenance": "first-change",
            "config": {"threshold": 1},
        }, headers={"X-Request-ID": "first-change"})
    assert response.status_code == 200, response.text
    assert response.json()["action"] == "stay"
    assert response.json()["constraints"]["importance_revision"] == 1
    assert catalog.get("bucket", "key").importance_revision == 2
    decisions = catalog.list_audit_events(AuditQuery(
        correlation_id="first-change", event_types=(AuditEventType.POLICY_DECISION.value,)
    ))
    assert len(decisions) == 1
    assert decisions[0].details["importance_revision"] == 1
