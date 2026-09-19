from __future__ import annotations

import asyncio
import json
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.api.models import PolicyConfig as APIPolicyConfig
from cognistore.cli import cognistore_cli
from cognistore.core.catalog import Catalog
from cognistore.core.content_identity import ContentIdentityBuilder
from cognistore.core.pii import ClassifiedPIIFinding, DetectorIdentity, PIIClassification
from cognistore.core.policy import PIIPolicyRule
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.jobs import handlers
from cognistore.jobs.handlers import (
    CATALOG_SCAN_JOB,
    POLICY_RUN_JOB,
    build_handlers,
    policy_job_payload,
    policy_job_schema_version,
)
from cognistore.jobs.models import (
    JOB_SCHEMA_VERSION_V4,
    EnqueueReceipt,
    InvalidJobError,
    JobContext,
    JobEnvelope,
)
from cognistore.jobs.scheduler import load_schedule_config
from cognistore.pii_runtime import PIIConfig
from cognistore.sdk import (
    CogniStoreClient,
    PIIPolicyRuleConfig,
    PolicyConfig,
    PolicyDecision,
    PolicyEvaluationResponse,
    PolicyRunRequest,
)

RULE = {"finding_type": "EMAIL_ADDRESS", "destination_tier": "hot", "minimum_confidence": 0.8}


def _context() -> JobContext:
    return JobContext(
        attempt=1, redelivered=False, stream_sequence=1, consumer_sequence=1,
        shutdown_requested=asyncio.Event(),
    )


def _payload(**overrides):
    values = {
        "bucket": "documents", "prefix": "", "policy": "content", "threshold": 1024,
        "llm_threshold": None, "allowed_tiers": ("hot", "warm"),
        "hot_name_patterns": (), "warm_name_patterns": (), "cold_name_patterns": (),
        "hot_mime_prefixes": (), "warm_mime_prefixes": (), "cold_mime_prefixes": (),
        "pii_rules": [RULE],
    }
    return policy_job_payload(**(values | overrides))


@pytest.mark.parametrize("model", [APIPolicyConfig, PolicyConfig])
@pytest.mark.parametrize("updates", [
    {"minimum_confidence": -0.1}, {"minimum_confidence": 1.1},
    {"minimum_confidence": float("nan")}, {"minimum_confidence": True},
    {"finding_type": "secret@example.com"}, {"destination_tier": "cold"},
    {"destination_tier": " hot"}, {"matched_text": "secret@example.com"},
])
def test_api_and_sdk_reject_invalid_pii_rules(model, updates) -> None:
    with pytest.raises(ValidationError):
        model(policy="content", pii_rules=[RULE | updates])


@pytest.mark.parametrize("model", [APIPolicyConfig, PolicyConfig])
def test_api_and_sdk_reject_pii_rules_for_other_policies_and_bound_rule_count(model) -> None:
    with pytest.raises(ValidationError, match="content policy"):
        model(policy="simple", pii_rules=[RULE])
    with pytest.raises(ValidationError):
        model(policy="content", pii_rules=[RULE] * 101)


def test_api_policy_factory_and_sdk_preserve_rules_without_changing_legacy_requests() -> None:
    config = APIPolicyConfig(policy="content", pii_rules=[RULE])
    assert CogniStoreGateway._policy(config).pii_rules == (PIIPolicyRule(**RULE),)
    request = PolicyRunRequest(
        bucket="documents",
        config=PolicyConfig(policy="content", pii_rules=[PIIPolicyRuleConfig(**RULE)]),
    )
    assert CogniStoreClient._policy_request_json(request)["config"]["pii_rules"] == [RULE]
    legacy = CogniStoreClient._policy_request_json(PolicyRunRequest(bucket="documents"))
    assert "pii_rules" not in legacy["config"]


def test_durable_policy_jobs_require_v4_and_preserve_rule_order() -> None:
    second = {"finding_type": "US_SSN", "destination_tier": "warm"}
    payload = _payload(pii_rules=[RULE, second])
    assert policy_job_schema_version(payload) == JOB_SCHEMA_VERSION_V4
    restored = JobEnvelope.from_bytes(JobEnvelope.create(
        POLICY_RUN_JOB, payload, schema_version=policy_job_schema_version(payload),
    ).to_bytes())
    assert restored.payload["pii_rules"] == [RULE, second | {"minimum_confidence": 0.5}]
    assert policy_job_schema_version(_payload(pii_rules=[])) == 1


def test_job_producer_rejects_too_many_pii_rules_before_queueing() -> None:
    with pytest.raises(ValueError, match="at most 100"):
        _payload(pii_rules=[RULE] * 101)


def test_http_and_sdk_expose_sanitized_pii_features_and_decision_reason(tmp_path: Path) -> None:
    data = b"document bytes"
    content = ContentIdentityBuilder().build(BytesIO(data), expected_size=len(data))
    digest = content.sha256
    finding = ClassifiedPIIFinding("EMAIL_ADDRESS", 0.9, "rule", "test", "1")
    catalog = Catalog()
    catalog.upsert_scan_observation(
        "documents", "report.pdf", size=len(data), tier="warm", generation="1",
        fence=catalog.capture_scan_fence("documents", "report.pdf"), content=content,
        metadata={"pii_detection": PIIClassification(
            "succeeded", digest, (finding,), (DetectorIdentity("test", "1"),),
        ).to_metadata()},
    )
    gateway = CogniStoreGateway(catalog, {
        tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm")
    })
    request = {
        "bucket": "documents", "key": "report.pdf",
        "config": {"policy": "content", "pii_rules": [RULE]},
    }
    with TestClient(create_app(gateway)) as client:
        evaluation = client.post("/v1/policies/evaluate", json=request)
        assert evaluation.status_code == 200
        response = PolicyEvaluationResponse.model_validate(evaluation.json())
        assert response.destination_tier == "hot"
        assert response.features.pii.state == "fresh"
        assert response.features.pii.findings[0].model_dump() == finding.to_metadata()
        preview = client.post("/v1/policies/preview", json=request)
        assert preview.status_code == 200
        decision = PolicyDecision.model_validate(preview.json())
        reason = decision.explanation.structured_reason
        assert reason.code == "pii_rule"
        assert [signal.name for signal in reason.decisive_signals] == [
            "pii_state", "pii_match", "pii_confidence",
        ]


@pytest.mark.parametrize("schema_version", [1, 2, 3])
def test_worker_rejects_pii_rules_in_older_envelopes(tmp_path: Path, schema_version: int) -> None:
    drivers = {tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm")}
    job = JobEnvelope.create(POLICY_RUN_JOB, _payload(), schema_version=schema_version)
    with pytest.raises(InvalidJobError, match="requires schema_version 4"):
        asyncio.run(build_handlers(drivers, Catalog())[POLICY_RUN_JOB](job, _context()))


@pytest.mark.parametrize("updates", [
    {"pii_rules": "bad"}, {"pii_rules": [RULE] * 101},
    {"pii_rules": [RULE | {"minimum_confidence": True}]},
    {"pii_rules": [RULE | {"destination_tier": "cold"}]}, {"policy": "simple"},
])
def test_worker_validates_untrusted_pii_payloads(tmp_path: Path, updates) -> None:
    drivers = {tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm")}
    job = JobEnvelope.create(POLICY_RUN_JOB, _payload() | updates, schema_version=4)
    with pytest.raises(InvalidJobError):
        asyncio.run(build_handlers(drivers, Catalog())[POLICY_RUN_JOB](job, _context()))


def test_worker_selects_scan_detectors_for_job_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = []

    def scan(**kwargs):
        seen.append((kwargs["catalog"].tenant_id, kwargs["pii_pipeline"].detectors))
        return []

    monkeypatch.setattr(handlers, "scan_catalog", scan)
    config = PIIConfig({"tenants": {"sensitive": {"detectors": ["regex"]}}})
    scan_handler = build_handlers(
        {"hot": PosixDriver(str(tmp_path / "hot"))}, Catalog(), pii_config=config,
    )[CATALOG_SCAN_JOB]
    for tenant in ("sensitive", "ordinary"):
        job = JobEnvelope.create(
            CATALOG_SCAN_JOB, {"tier": "hot", "bucket": "documents", "prefix": ""},
            tenant_id=tenant,
        )
        asyncio.run(scan_handler(job, _context()))
    assert [(tenant, [detector.name for detector in detectors]) for tenant, detectors in seen] == [
        ("sensitive", ["regex"]), ("ordinary", []),
    ]


def test_scheduler_preserves_pii_rules_and_selects_protected_job_schema(tmp_path: Path) -> None:
    path = tmp_path / "schedules.json"
    path.write_text(json.dumps({"jobs": {"sensitive": {
        "type": POLICY_RUN_JOB, "enabled": True, "interval_seconds": 60,
        "payload": _payload(),
    }}}))
    schedules = load_schedule_config(path, known_tiers={"hot", "warm"})
    assert schedules[0].payload["pii_rules"] == [RULE]
    assert policy_job_schema_version(schedules[0].payload) == 4


def test_cli_forwards_json_pii_rules_in_protected_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = []
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda path: {
        tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm")
    })

    async def enqueue(config, job):
        jobs.append(job)
        return EnqueueReceipt(
            job_id=job.job_id, correlation_id=job.correlation_id, stream="TEST", sequence=1,
        )

    monkeypatch.setattr(cognistore_cli, "_enqueue_job", enqueue)
    assert cognistore_cli.main([
        "--drivers", "unused.yaml", "policy-run", "documents", "--policy", "content",
        "--pii-rule", json.dumps(RULE), "--json",
    ]) == 0
    assert jobs[0].schema_version == 4
    assert jobs[0].payload["pii_rules"] == [RULE]
