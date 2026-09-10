"""Policy stability configuration across HTTP, SDK, and scheduled jobs."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cognistore.api import models as api_models
from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.core.catalog import Catalog
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.jobs.handlers import policy_job_schema_version
from cognistore.jobs.models import JOB_SCHEMA_VERSION_V3, JobEnvelope
from cognistore.jobs.scheduler import load_schedule_config
from cognistore.sdk import CogniStoreClient, MovementConstraintsConfig, StabilityOverrideConfig
from cognistore.sdk import models as sdk_models

STABILITY_CONTROLS = {
    "cooldown_seconds": 3600,
    "size_hysteresis_bytes": 100,
    "similarity_hysteresis": 0.1,
    "stability_override": {
        "kind": "compliance",
        "reason": "Relocate records for approved retention request 45",
    },
}


@pytest.mark.parametrize("models", [api_models, sdk_models])
@pytest.mark.parametrize("request_name", ["PolicyEvaluationRequest", "PolicyRunRequest"])
def test_stability_controls_roundtrip_in_nested_policy_requests(models, request_name: str) -> None:
    body = {"bucket": "bucket", "config": {"movement_constraints": STABILITY_CONTROLS}}
    if request_name == "PolicyEvaluationRequest":
        body["key"] = "key"
    request_type = getattr(models, request_name)
    request = request_type.model_validate(body)
    decoded = request_type.model_validate_json(request.model_dump_json())
    controls = decoded.config.movement_constraints.model_dump()
    for name, value in STABILITY_CONTROLS.items():
        assert controls[name] == value


def test_sdk_exports_stability_override_and_preserves_disabled_defaults() -> None:
    controls = MovementConstraintsConfig()
    assert controls.cooldown_seconds == 0
    assert controls.size_hysteresis_bytes == 0
    assert controls.similarity_hysteresis == 0.0
    assert controls.stability_override is None
    override = StabilityOverrideConfig(kind="emergency", reason="Incident 45")
    assert MovementConstraintsConfig(stability_override=override).stability_override == override


def test_sdk_omits_disabled_stability_fields_for_older_strict_v1_servers() -> None:
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, request=request, json={
            "schema_version": 1, "bucket": "bucket", "key": "key",
            "current_tier": "hot", "size": 101, "action": "stay", "reason": "preview",
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with CogniStoreClient("https://sdk.test", http_client=transport) as client:
            for controls in (
                {"minimum_residency_seconds": {"hot": 300}},
                STABILITY_CONTROLS,
            ):
                client.evaluate_policy(sdk_models.PolicyEvaluationRequest(
                    bucket="bucket", key="key",
                    config=sdk_models.PolicyConfig(movement_constraints=controls),
                ))
    legacy = bodies[0]["config"]["movement_constraints"]
    assert set(legacy) == {"minimum_residency_seconds", "importance_tiers"}
    assert legacy["minimum_residency_seconds"] == {"hot": 300}
    active = bodies[1]["config"]["movement_constraints"]
    for name, value in STABILITY_CONTROLS.items():
        assert active[name] == value


@pytest.mark.parametrize("models", [api_models, sdk_models])
@pytest.mark.parametrize("invalid", [
    {"cooldown_seconds": True},
    {"cooldown_seconds": "1"},
    {"cooldown_seconds": 1.0},
    {"cooldown_seconds": -1},
    {"cooldown_seconds": 315360001},
    {"size_hysteresis_bytes": True},
    {"size_hysteresis_bytes": "1"},
    {"size_hysteresis_bytes": 1.0},
    {"size_hysteresis_bytes": -1},
    {"size_hysteresis_bytes": 2**63},
    {"similarity_hysteresis": True},
    {"similarity_hysteresis": "0.1"},
    {"similarity_hysteresis": -0.1},
    {"similarity_hysteresis": 2.01},
    {"similarity_hysteresis": float("inf")},
    {"similarity_hysteresis": float("nan")},
    {"stability_override": True},
    {"stability_override": {}},
    {"stability_override": {"kind": "routine", "reason": "request"}},
    {"stability_override": {"kind": "emergency"}},
    {"stability_override": {"kind": "emergency", "reason": ""}},
    {"stability_override": {"kind": "emergency", "reason": " "}},
    {"stability_override": {"kind": "emergency", "reason": " request"}},
    {"stability_override": {"kind": "emergency", "reason": "request\n45"}},
    {"stability_override": {"kind": "emergency", "reason": "request\x7f45"}},
    {"stability_override": {"kind": "emergency", "reason": "\ud800"}},
    {"stability_override": {"kind": "emergency", "reason": "é" * 1025}},
    {"stability_override": {"kind": "emergency", "reason": "request", "actor": "spoof"}},
])
def test_stability_public_models_reject_invalid_controls(models, invalid: dict) -> None:
    with pytest.raises(ValidationError):
        models.MovementConstraintsConfig.model_validate(invalid)


@pytest.mark.parametrize("models", [api_models, sdk_models])
def test_stability_public_models_accept_exact_upper_limits(models) -> None:
    controls = models.MovementConstraintsConfig(
        cooldown_seconds=315360000,
        size_hysteresis_bytes=2**63 - 1,
        similarity_hysteresis=2.0,
        stability_override={"kind": "compliance", "reason": "é" * 1024},
    )
    assert controls.stability_override.reason == "é" * 1024


def test_api_hysteresis_preview_and_override_preserve_hard_guards(tmp_path: Path) -> None:
    catalog = Catalog()
    catalog.upsert("bucket", "key", 101, "hot")
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    body = {
        "bucket": "bucket",
        "key": "key",
        "config": {"threshold": 100, "movement_constraints": {"size_hysteresis_bytes": 10}},
    }
    with TestClient(create_app(CogniStoreGateway(catalog, drivers))) as client:
        blocked = client.post("/v1/policies/evaluate", json=body)
        assert blocked.status_code == 200, blocked.text
        assert blocked.json()["action"] == "stay"
        assert "hysteresis" in blocked.json()["reason"]

        body["config"]["movement_constraints"]["stability_override"] = {
            "kind": "emergency", "reason": "Approved incident response 45",
        }
        overridden = client.post("/v1/policies/evaluate", json=body)
        assert overridden.status_code == 200, overridden.text
        assert overridden.json()["action"] == "move"
        assert overridden.json()["destination_tier"] == "warm"

        body["config"]["movement_constraints"]["minimum_residency_seconds"] = {"hot": 3600}
        residency = client.post("/v1/policies/evaluate", json=body)
        assert residency.status_code == 200, residency.text
        assert residency.json()["action"] == "stay"
        assert "residency" in residency.json()["reason"]
    assert catalog.get("bucket", "key").tier == "hot"
    assert catalog.list_audit_events() == []


@pytest.mark.parametrize("invalid", [
    {"cooldown_seconds": True},
    {"size_hysteresis_bytes": -1},
    {"similarity_hysteresis": 3},
    {"stability_override": {"kind": "emergency", "reason": ""}},
])
def test_api_rejects_invalid_stability_before_importance_mutation(
    tmp_path: Path, invalid: dict,
) -> None:
    catalog = Catalog()
    catalog.upsert("bucket", "key", 101, "hot")
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    with TestClient(create_app(CogniStoreGateway(catalog, drivers))) as client:
        response = client.post("/v1/catalog/importance", json={
            "bucket": "bucket", "key": "key", "level": "critical",
            "actor_id": "operator", "provenance": "request-45",
            "config": {"movement_constraints": invalid},
        })
    assert response.status_code == 422, response.text
    assert catalog.get("bucket", "key").importance_revision == 0
    assert catalog.list_audit_events() == []


def test_scheduled_stability_controls_survive_job_serialization(tmp_path: Path) -> None:
    path = tmp_path / "schedules.yaml"
    path.write_text(json.dumps({"jobs": {"stable": {
        "type": "policy.run", "enabled": True, "interval_seconds": 300,
        "payload": {
            "bucket": "bucket", "policy": "simple", "threshold": 100,
            "allowed_tiers": ["hot", "warm"],
            "movement_constraints": STABILITY_CONTROLS,
        },
    }}}), encoding="utf-8")
    schedule = load_schedule_config(path, known_tiers={"hot", "warm"})[0]
    envelope = JobEnvelope.create(
        schedule.job_type, schedule.payload,
        schema_version=policy_job_schema_version(schedule.payload),
    )
    restored = JobEnvelope.from_bytes(envelope.to_bytes())
    assert restored.schema_version == JOB_SCHEMA_VERSION_V3
    for name, value in STABILITY_CONTROLS.items():
        assert restored.payload["movement_constraints"][name] == value
