from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest

from cognistore.auth.tenancy import tenant_context
from cognistore.core.audit import AuditOutcome, AuditRetentionPolicy
from cognistore.core.catalog import Catalog, ObjectRecord
from cognistore.core.content_identity import ContentIdentityBuilder
from cognistore.core.mover import Mover
from cognistore.core.pii import ClassifiedPIIFinding, DetectorIdentity, PIIClassification
from cognistore.core.policy import ContentAwarePolicy, PIIPolicyRule
from cognistore.core.policy_dataset import export_policy_dataset, validate_policy_dataset
from cognistore.core.policy_factory import build_policy
from cognistore.core.policy_features import CatalogPolicyFeatureLoader, FeatureState
from cognistore.core.policy_runner import PolicyRunner
from cognistore.core.policy_snapshot import capture_policy_snapshot, replay_policy_snapshot
from cognistore.drivers.posix_driver import PosixDriver

PAYLOAD = b"document bytes"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
IDENTITY = DetectorIdentity("test-detector", "2.0")


def classification(*, status="succeeded", confidence=0.9, findings=True):
    return PIIClassification(
        status=status,
        content_sha256=DIGEST,
        findings=(ClassifiedPIIFinding("EMAIL_ADDRESS", confidence, "rule", IDENTITY.name,
                                      IDENTITY.version),)
        if findings and status == "succeeded" else (),
        detectors=() if status == "disabled" else (IDENTITY,),
        failure_code="detector_error" if status == "unknown" else None,
    ).to_metadata()


def record(metadata=None):
    return ObjectRecord("bucket", "record.txt", len(PAYLOAD), "warm", metadata={
        "sha256": DIGEST,
        "content_identity": {
            "schema_version": 1, "representation": "source-bytes",
            "digest_algorithm": "sha256", "sha256": DIGEST, "size": len(PAYLOAD),
        },
        **({"pii_detection": classification()} if metadata is None else metadata),
    })


def policy(**kwargs):
    return ContentAwarePolicy(
        allowed_tiers=("hot", "warm", "cold"),
        pii_rules=(PIIPolicyRule("EMAIL_ADDRESS", "cold", 0.8),),
        **kwargs,
    )


def project(rec):
    return CatalogPolicyFeatureLoader().load((rec,), as_of="2026-09-01T00:00:00Z")[(rec.bucket, rec.key)]


def test_pii_governance_precedes_filename_rules_and_threshold():
    result = policy(hot_name_patterns=["*.txt"], size_threshold=100).evaluate_record(record())
    assert (result.action, result.dst_tier, result.reason_code) == ("move", "cold", "pii_rule")
    assert [signal["name"] for signal in result.decisive_signals] == [
        "pii_state", "pii_match", "pii_confidence",
    ]
    assert "EMAIL_ADDRESS" not in result.reason


@pytest.mark.parametrize("status", ["unknown", "disabled"])
def test_failed_or_disabled_detection_holds_before_name_and_size(status):
    rec = record({"pii_detection": classification(status=status)})
    features = project(rec)
    assert features.pii.state is FeatureState.UNAVAILABLE
    assert features.pii.findings == ()
    decision = policy(hot_name_patterns=["*.txt"]).evaluate_features(rec, features)
    assert decision.action == "stay"
    assert decision.reason == "required policy features unavailable: pii=unavailable"


def test_missing_evidence_and_scalar_compatibility_evaluation_fail_closed():
    assert policy().evaluate_record(record({})).action == "stay"
    assert policy().evaluate("warm", 0).action == "stay"
    assert "pii" not in project(record({})).to_dict()


@pytest.mark.parametrize("mutation", ["digest", "extra", "raw_type", "raw_provenance", "raw_version", "confidence", "schema", "identity"])
def test_malformed_and_stale_evidence_is_never_exposed(mutation):
    rec = record()
    metadata = rec.metadata["pii_detection"]
    secret = "person@example.com"
    if mutation == "digest":
        metadata["content_sha256"] = "b" * 64
    elif mutation == "extra":
        metadata["findings"][0]["match"] = secret
    elif mutation == "raw_type":
        metadata["findings"][0]["type"] = secret
    elif mutation == "raw_provenance":
        metadata["findings"][0]["provenance"] = secret
    elif mutation == "raw_version":
        metadata["findings"][0]["detector_version"] = secret
    elif mutation == "confidence":
        metadata["findings"][0]["confidence"] = float("nan")
    elif mutation == "schema":
        metadata["schema_version"] = True
    else:
        del rec.metadata["content_identity"]
    features = project(rec)
    assert features.pii.state is FeatureState.STALE
    assert secret not in json.dumps(features.to_dict())
    assert policy().evaluate_features(rec, features).action == "stay"


@pytest.mark.parametrize("findings, confidence", [(False, 0.9), (True, 0.7)])
def test_complete_nonmatching_detection_allows_existing_rules(findings, confidence):
    rec = record({"pii_detection": classification(findings=findings, confidence=confidence)})
    result = policy(hot_name_patterns=["*.txt"]).evaluate_record(rec)
    assert (result.action, result.dst_tier, result.reason_code) == ("move", "hot", "name_rule")


def test_rule_order_confidence_boundary_and_runtime_allowed_tiers():
    configured = ContentAwarePolicy(
        pii_rules=[PIIPolicyRule("EMAIL_ADDRESS", "hot", 0.9),
                   PIIPolicyRule("EMAIL_ADDRESS", "warm", 0.5)],
    )
    assert configured.evaluate_record(record()).dst_tier == "hot"
    configured.allowed_tiers = ["warm"]
    result = configured.evaluate_record(record())
    assert result.action == "stay"
    assert result.reason_code == "destination_not_allowed"
    assert result.proposed_dst_tier == "hot"


@pytest.mark.parametrize("confidence", [True, -0.1, 1.1, float("inf"), float("nan"), "0.5"])
def test_rule_rejects_invalid_confidence(confidence):
    with pytest.raises(ValueError):
        PIIPolicyRule("EMAIL_ADDRESS", "hot", confidence)


def test_factory_rejects_pii_rules_for_non_content_policy():
    with pytest.raises(ValueError, match="require the content policy"):
        build_policy("simple", threshold=1, allowed_tiers=("hot", "warm"),
                     pii_rules=[PIIPolicyRule("EMAIL_ADDRESS", "hot")])


@pytest.mark.parametrize("status", ["succeeded", "unknown", "disabled"])
def test_pii_snapshot_replays_normalized_evidence(status):
    rec = record({"pii_detection": classification(status=status)})
    configured = policy(hot_name_patterns=["*.txt"])
    features = project(rec)
    decision = configured.evaluate_features(rec, features)
    snapshot = capture_policy_snapshot(
        record=rec, features=features, policy=configured, allowed_tiers=("hot", "warm", "cold"),
        decision=decision,
        outcome=AuditOutcome.SELECTED if decision.action == "move" else AuditOutcome.STAYED,
        policy_name="content", policy_version="1", decision_at="2026-09-01T00:00:00Z",
    )
    assert replay_policy_snapshot(json.loads(json.dumps(snapshot))) == decision
    assert snapshot["policy"]["config"]["pii_rules"] == [configured.pii_rules[0].to_mapping()]
    assert "record.txt" not in json.dumps(snapshot["features"]["pii"])


@pytest.mark.parametrize("status", ["succeeded", "unknown", "disabled"])
def test_pii_audit_dataset_remains_valid_and_rejects_raw_fields(tmp_path, status):
    catalog = Catalog(audit_retention=AuditRetentionPolicy(None))
    content = ContentIdentityBuilder().build(BytesIO(PAYLOAD), expected_size=len(PAYLOAD))
    catalog.upsert_scan_observation(
        "bucket", "record.txt", size=len(PAYLOAD), tier="cold", generation="1",
        metadata={"pii_detection": classification(status=status)},
        fence=catalog.capture_scan_fence("bucket", "record.txt"), content=content,
    )
    drivers = {tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm", "cold")}
    runner = PolicyRunner(catalog, drivers, Mover(drivers, catalog), policy())
    runner.reevaluate_object("bucket", "record.txt")
    exported = export_policy_dataset(
        catalog, as_of=datetime.now(timezone.utc) + timedelta(seconds=3601),
        observation_seconds=3600,
    )
    assert len(exported["rows"]) == 1
    assert validate_policy_dataset(exported) == []
    if status == "succeeded":
        tampered = copy.deepcopy(exported)
        tampered["rows"][0]["snapshot"]["features"]["pii"]["findings"][0]["match"] = "private"
        assert validate_policy_dataset(tampered)


def planned_runner(tmp_path, *, tenant="default"):
    catalog = Catalog(tenant_id=tenant)
    content = ContentIdentityBuilder().build(BytesIO(PAYLOAD), expected_size=len(PAYLOAD))
    catalog.upsert_scan_observation(
        "bucket", "record.txt", size=len(PAYLOAD), tier="warm", generation="1",
        metadata={"pii_detection": classification()},
        fence=catalog.capture_scan_fence("bucket", "record.txt"), content=content,
    )
    drivers = {tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm", "cold")}
    drivers["warm"].put_object("bucket", "record.txt", PAYLOAD)
    configured = ContentAwarePolicy(
        allowed_tiers=("hot", "warm", "cold"),
        pii_rules=(PIIPolicyRule("EMAIL_ADDRESS", "cold"), PIIPolicyRule("US_SSN", "hot")),
    )
    runner = PolicyRunner(catalog, drivers, Mover(drivers, catalog), configured,
                          idempotency_namespace="pii-execute-test")
    planned = runner.plan_once("bucket")
    assert len(planned) == 1 and planned[0].to_tier == "cold"
    return catalog, content, drivers, runner, planned[0]


@pytest.mark.parametrize("change", ["unknown", "disabled", "missing", "stale", "destination"])
def test_execute_rechecks_current_pii_before_moving_same_bytes(tmp_path, change):
    catalog, content, drivers, runner, planned = planned_runner(tmp_path)
    updated = classification(status=change) if change in {"unknown", "disabled"} else classification()
    if change == "stale":
        updated["content_sha256"] = "b" * 64
    elif change == "destination":
        updated["findings"][0]["type"] = "US_SSN"
    if change == "missing":
        catalog.upsert("bucket", "record.txt", len(PAYLOAD), tier="warm")
    else:
        catalog.upsert_scan_observation(
            "bucket", "record.txt", size=len(PAYLOAD), tier="warm", generation="2",
            metadata={"pii_detection": updated},
            fence=catalog.capture_scan_fence("bucket", "record.txt"), content=content,
        )
    with pytest.raises(ValueError, match="PII governance"):
        runner.execute(planned)
    assert drivers["warm"].get_object("bucket", "record.txt") == PAYLOAD
    with pytest.raises(FileNotFoundError):
        drivers["cold"].get_object("bucket", "record.txt")


@pytest.mark.parametrize("tenant", ["default", "alpha"])
def test_execute_accepts_current_evidence_in_owning_tenant(tmp_path, tenant):
    with tenant_context(tenant):
        catalog, _, drivers, runner, planned = planned_runner(tmp_path, tenant=tenant)
        runner.execute(planned)
        assert drivers["cold"].get_object("bucket", "record.txt") == PAYLOAD
        assert catalog.get("bucket", "record.txt").tier == "cold"
        # A completed action remains idempotent when the current classification
        # selects the object's existing destination.
        runner.execute(planned)
