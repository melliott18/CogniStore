"""Legal hold priority survives composition with locality and PII governance."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest

from cognistore.core.audit import AuditContext, AuditEventType, AuditOutcome, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.core.content_identity import ContentIdentityBuilder
from cognistore.core.legal_holds import LegalHoldError
from cognistore.core.mover import Mover
from cognistore.core.policy import ContentAwarePolicy, PIIPolicyRule
from cognistore.core.policy_dataset import export_policy_dataset, validate_policy_dataset
from cognistore.core.policy_reasons import reason_from_audit_details
from cognistore.core.policy_runner import PolicyRunner
from cognistore.core.policy_snapshot import replay_policy_snapshot, validate_policy_snapshot
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.sdk.models import PolicyReason as SDKPolicyReason
from tests.unit.test_pii_policy import PAYLOAD, classification, planned_runner

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


@pytest.mark.parametrize("pii_status", ["succeeded", "unknown", "disabled"])
def test_hold_precedes_pii_locality_and_keeps_combined_evidence(tmp_path, monkeypatch, pii_status):
    config = tmp_path / "locality.json"
    config.write_text(json.dumps({"version": 1, "tenants": {"default": {
        "allowed_regions": ["eu"],
        "tier_pools": {"hot": "hot-pool", "warm": "warm-pool"},
    }}}))
    monkeypatch.setenv("COGNISTORE_LOCALITY_CONFIG", str(config))
    catalog = Catalog()
    drivers = {tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm")}
    for tier, region in (("hot", "eu"), ("warm", "us")):
        catalog.register_tier(tier)
        catalog.register_pool(tier + "-pool", tier, region=region, members=(tier,),
                              localities=(region,), metadata={"locality_evidence": {
                                  "observed_at": NOW.isoformat(), "max_age_seconds": 86400,
                                  "source": "operator",
                              }})
    content = ContentIdentityBuilder().build(BytesIO(PAYLOAD), expected_size=len(PAYLOAD))
    catalog.upsert_scan_observation(
        "bucket", "record.txt", size=len(PAYLOAD), tier="hot", generation="1",
        metadata={"pii_detection": classification(status=pii_status)}, content=content,
        fence=catalog.capture_scan_fence("bucket", "record.txt"),
    )
    catalog.assign_pool("bucket", "record.txt", "hot-pool")
    drivers["hot"].put_object("bucket", "record.txt", PAYLOAD)
    held = catalog.place_legal_hold("bucket", key="record.txt", reason="Preserve evidence",
                                   context=AuditContext("case-governance", "user", "reviewer"))
    policy = ContentAwarePolicy(allowed_tiers=("hot", "warm"),
                                pii_rules=(PIIPolicyRule("EMAIL_ADDRESS", "warm"),))
    runner = PolicyRunner(catalog, drivers, Mover(drivers, catalog), policy, clock=lambda: NOW)
    snapshot, reason = runner.preview_decision("bucket", "record.txt", as_of=NOW)
    assert reason["code"] == "legal_hold"
    assert reason["disposition"] == "rejected"
    assert reason["decisive_signals"] == []
    assert reason["constraints"]["legal_hold_ids"] == [held.hold_id]
    assert reason["constraints"]["locality"]["rejected"]["warm"]
    assert SDKPolicyReason.model_validate(reason).model_dump(mode="json") == reason
    assert snapshot["features"]["pii"]["state"] == (
        "fresh" if pii_status == "succeeded" else "unavailable"
    )
    assert snapshot["replay"] == {"supported": False, "reason": "legal_hold"}
    assert snapshot["decision"]["outcome"] == AuditOutcome.REJECTED.value
    assert validate_policy_snapshot(snapshot) == snapshot
    with pytest.raises(ValueError, match="not replayable: legal_hold"):
        replay_policy_snapshot(snapshot)
    assert runner.plan_once("bucket") == []
    events = catalog.list_audit_events(AuditQuery(
        event_types=frozenset({AuditEventType.POLICY_DECISION}),
    ))
    assert len(events) == 1
    assert reason_from_audit_details(events[0].details)["code"] == "legal_hold"
    assert events[0].details["dataset"]["replay"]["reason"] == "legal_hold"
    dataset = export_policy_dataset(
        catalog, as_of=datetime.now(timezone.utc) + timedelta(hours=1), observation_seconds=60,
    )
    assert validate_policy_dataset(dataset, require_labels=False) == []
    assert drivers["hot"].get_object("bucket", "record.txt") == PAYLOAD
    assert list(drivers["warm"].list_objects("bucket")) == []


def test_execution_reports_hold_before_new_pii_rejection(tmp_path):
    catalog, content, drivers, runner, planned = planned_runner(tmp_path)
    catalog.upsert_scan_observation(
        "bucket", "record.txt", size=len(PAYLOAD), tier="warm", generation="2",
        metadata={"pii_detection": classification(status="unknown")}, content=content,
        fence=catalog.capture_scan_fence("bucket", "record.txt"),
    )
    catalog.place_legal_hold("bucket", key="record.txt", reason="Preserve evidence",
                            context=AuditContext("case-governance", "user", "reviewer"))
    with pytest.raises(LegalHoldError):
        runner.execute(planned)
    denials = catalog.list_audit_events(AuditQuery(
        event_types=frozenset({AuditEventType.LEGAL_HOLD_DENIED}),
    ))
    assert len(denials) == 1
    assert denials[0].details["operation"] == "policy.execute"
    assert denials[0].causation_id == planned.decision_event_id
    assert drivers["warm"].get_object("bucket", "record.txt") == PAYLOAD
    assert list(drivers["cold"].list_objects("bucket")) == []
