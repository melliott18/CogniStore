from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from cognistore.core.access import AccessConfig, AccessEvent, compute_access_features
from cognistore.core.audit import AuditEventType, AuditOutcome, AuditQuery
from cognistore.core.catalog import Catalog, ObjectRecord
from cognistore.core.mover import Mover
from cognistore.core.policy import (
    ContentAwarePolicy,
    EmbeddingPolicyRule,
    LLMPolicy,
    PolicyDecision,
    SimplePolicy,
)
from cognistore.core.policy_factory import ThresholdProvider
from cognistore.core.policy_features import (
    EmbeddingPolicyFeature,
    FeatureState,
    MimePolicyFeature,
    PolicyFeatureProvenance,
    PolicyFeatures,
)
from cognistore.core.policy_runner import PolicyRunner
from cognistore.core.policy_snapshot import (
    capture_policy_snapshot,
    replay_policy_snapshot,
    snapshot_from_audit_details,
    validate_policy_snapshot,
)
from cognistore.drivers.posix_driver import PosixDriver

AS_OF = "2026-09-01T12:00:00.000000Z"
DECIDED = "2026-09-01T12:00:01.000000Z"


def feature_projection(*, state=FeatureState.FRESH, query="active documents"):
    catalog = Catalog()
    catalog.append_access_event(
        AccessEvent.create(
            kind="read",
            bucket="bk",
            key="report.txt",
            occurred_at="2026-09-01T11:59:00Z",
            sample_rate=0.5,
        )
    )
    return PolicyFeatures(
        mime=MimePolicyFeature(
            state,
            PolicyFeatureProvenance("catalog.mime", 1, "a" * 64, {"detector": "test"}),
            "text/plain" if state is FeatureState.FRESH else None,
        ),
        embeddings=(
            EmbeddingPolicyFeature(
                "active",
                query,
                state,
                PolicyFeatureProvenance("embedding", 1, "a" * 64, {"model": "test-v1"}),
                0.95 if state is FeatureState.FRESH else None,
            ),
        ),
        access=compute_access_features(
            catalog,
            "bk",
            "report.txt",
            AccessConfig(sample_rate=0.5),
            as_of=AS_OF,
        ),
    )


def capture(policy=None, *, features=None, record=None, **kwargs):
    policy = policy or SimplePolicy(size_threshold=20)
    record = record or ObjectRecord("bk", "report.txt", 10, "warm")
    features = features or feature_projection()
    decision = (
        policy.evaluate_features(record, features)
        if hasattr(policy, "evaluate_features")
        else policy.evaluate(record.tier, record.size)
    )
    return capture_policy_snapshot(
        record=record,
        features=features,
        policy=policy,
        allowed_tiers=["hot", "warm", "cold"],
        decision=decision,
        outcome=AuditOutcome.SELECTED if decision.action == "move" else AuditOutcome.STAYED,
        policy_name="test",
        policy_version="1",
        decision_at=DECIDED,
        **kwargs,
    )


@pytest.mark.parametrize(
    "policy",
    [
        SimplePolicy(size_threshold=20),
        ContentAwarePolicy(hot_name_patterns=["*.txt"]),
        ContentAwarePolicy(hot_mime_prefixes=["text/"]),
        ContentAwarePolicy(
            embedding_rules=[EmbeddingPolicyRule("active", "active documents", 0.8, "hot")]
        ),
        LLMPolicy(ThresholdProvider(20, ["hot", "warm"])),
    ],
)
def test_snapshot_replays_builtin_decision_with_all_feature_evidence(policy):
    snapshot = capture(policy)
    expected = snapshot["decision"]
    replayed = replay_policy_snapshot(json.loads(json.dumps(snapshot)))
    assert replayed == PolicyDecision(
        expected["action"], expected["reason"], expected["destination_tier"]
    )
    assert snapshot["features"]["access"]["windows"]["3600"]["read"] == 1
    assert snapshot["features"]["access"]["estimated_windows"]["3600"]["read"] == 2.0
    assert snapshot["features"]["access"]["sampling"]["configured_rate"] == 0.5
    assert snapshot["features"]["mime"]["provenance"]["content_sha256"] == "a" * 64
    assert snapshot["replay"] == {"supported": True, "reason": None}


@pytest.mark.parametrize(
    "state", [FeatureState.MISSING, FeatureState.STALE, FeatureState.UNAVAILABLE]
)
def test_snapshot_replays_closed_semantic_decision(state):
    snapshot = capture(
        ContentAwarePolicy(hot_mime_prefixes=["text/"]), features=feature_projection(state=state)
    )
    assert (
        replay_policy_snapshot(snapshot).reason
        == f"required policy features unavailable: mime={state.value}"
    )
    assert snapshot["features"]["mime"]["state"] == state.value


def test_cold_runtime_rules_and_detached_inputs_are_frozen():
    policy = ContentAwarePolicy(allowed_tiers=["hot", "warm", "cold"])
    policy.cold_name_patterns = ["*.txt"]
    record = ObjectRecord("bk", "report.txt", 10, "warm", metadata={"password": "never-store-this"})
    snapshot = capture(policy, record=record)
    policy.cold_name_patterns.clear()
    record.key = "changed.bin"
    record.metadata["new"] = "later"
    assert replay_policy_snapshot(snapshot).dst_tier == "cold"
    assert "metadata" not in snapshot["object"]
    assert "never-store-this" not in json.dumps(snapshot)
    copied = validate_policy_snapshot(snapshot)
    copied["object"]["key"] = "another"
    assert snapshot["object"]["key"] == "report.txt"


@pytest.mark.parametrize(
    "policy",
    [
        SimplePolicy(size_threshold=-1),
        ContentAwarePolicy(size_threshold=-1),
        LLMPolicy(ThresholdProvider(-1, ["hot", "warm"])),
    ],
)
def test_negative_builtin_thresholds_remain_replayable(policy):
    snapshot = capture(policy)
    assert replay_policy_snapshot(snapshot).action == "stay"


def test_redaction_keeps_distinct_rule_identities_without_secrets():
    names = ["api_key=secret-one", "api_key=secret-two"]
    policy = ContentAwarePolicy(
        embedding_rules=[
            EmbeddingPolicyRule(name, "active documents", 0.8, "hot") for name in names
        ]
    )
    original = feature_projection()
    features = replace(
        original, embeddings=tuple(replace(original.embeddings[0], name=name) for name in names)
    )
    snapshot = capture(policy, features=features)
    rule_names = [rule["name"] for rule in snapshot["policy"]["config"]["embedding_rules"]]
    feature_names = [feature["name"] for feature in snapshot["features"]["embeddings"]]
    assert len(set(rule_names)) == 2
    assert set(feature_names) == set(rule_names)
    assert "secret-one" not in json.dumps(snapshot)
    assert "secret-two" not in json.dumps(snapshot)
    assert snapshot["replay"]["reason"] == "required_inputs_redacted"


def test_secrets_redacted_before_capture_returns_and_disable_exact_replay():
    secret = "https://example.com/data?api_key=do-not-store"
    policy = ContentAwarePolicy(embedding_rules=[EmbeddingPolicyRule("active", secret, 0.8, "hot")])
    snapshot = capture(policy, features=feature_projection(query=secret))
    assert "do-not-store" not in json.dumps(snapshot)
    assert snapshot["replay"] == {"supported": False, "reason": "required_inputs_redacted"}
    with pytest.raises(ValueError, match="not replayable"):
        replay_policy_snapshot(snapshot)


def test_external_provider_and_custom_policy_are_never_serialized_or_replayed():
    class ExternalProvider:
        password = "never-store-provider-secret"
        calls = 0

        def decide(self, inputs):
            self.calls += 1
            return {"action": "move", "dst_tier": "hot", "reason": "token=private-output"}

    provider = ExternalProvider()
    snapshot = capture(
        LLMPolicy(provider), model_identity="external-model", model_version="2026-09"
    )
    assert snapshot["policy"]["config"] == {}
    assert snapshot["policy"]["model"] == {"identity": "external-model", "version": "2026-09"}
    assert "never-store-provider-secret" not in json.dumps(snapshot)
    assert "private-output" not in json.dumps(snapshot)
    assert snapshot["decision"]["action"] == "move"
    with pytest.raises(ValueError, match="unsupported_policy_or_provider"):
        replay_policy_snapshot(snapshot)
    assert provider.calls == 1

    class CustomPolicy(SimplePolicy):
        pass

    assert capture(CustomPolicy())["replay"]["supported"] is False


def test_model_version_requires_an_explicit_identity():
    with pytest.raises(ValueError, match="model_version requires model_identity"):
        capture(model_version="2026-09")


@pytest.mark.parametrize("redacted", [False, True])
def test_raw_snapshot_rejects_selected_outcome_for_stay_action(redacted):
    snapshot = capture()
    snapshot["decision"]["action"] = "stay"
    if redacted:
        snapshot["replay"] = {"supported": False, "reason": "required_inputs_redacted"}
    with pytest.raises(ValueError, match="outcome contradicts action"):
        validate_policy_snapshot(snapshot)


def test_runner_records_explicit_external_model_metadata_without_provider_secrets(tmp_path):
    class ExternalProvider:
        password = "not-model-metadata"
        calls = 0

        def decide(self, inputs):
            self.calls += 1
            return {"action": "stay", "reason": "external model selected current tier"}

    catalog = Catalog()
    catalog.upsert("bk", "report.txt", 10, "warm")
    drivers = {tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm")}
    provider = ExternalProvider()
    policy = LLMPolicy(provider)
    runner = PolicyRunner(
        catalog,
        drivers,
        Mover(drivers, catalog),
        policy,
        model_identity="external-model",
        model_version="2026-09",
    )
    assert runner.plan_once("bk") == []
    snapshot = catalog.list_audit_events()[0].details["dataset"]
    assert snapshot["policy"]["model"] == {"identity": "external-model", "version": "2026-09"}
    assert "not-model-metadata" not in json.dumps(snapshot)
    with pytest.raises(ValueError, match="not replayable"):
        replay_policy_snapshot(snapshot)
    assert provider.calls == 1
    with pytest.raises(ValueError, match="model_version requires model_identity"):
        PolicyRunner(catalog, drivers, Mover(drivers, catalog), policy, model_version="2026-09")


@pytest.mark.parametrize("path", [(), ("features",), ("features", "access"), ("provenance",)])
@pytest.mark.parametrize("version", [None, True, 2, "1"])
def test_unknown_missing_and_invalid_versions_are_rejected(path, version):
    snapshot = capture()
    node = snapshot
    for key in path:
        node = node[key]
    node["schema_version"] = version
    with pytest.raises(ValueError, match="schema_version"):
        validate_policy_snapshot(snapshot)


def test_legacy_upcast_preserves_absence_and_refuses_present_invalid_snapshot():
    assert snapshot_from_audit_details({"action": "stay", "size": 10}) is None
    with pytest.raises(ValueError):
        snapshot_from_audit_details({"dataset": None})
    with pytest.raises(ValueError, match="schema_version"):
        snapshot_from_audit_details({"dataset": {"schema_version": 2}})
    snapshot = capture()
    assert snapshot_from_audit_details({"dataset": snapshot}) == snapshot


def test_future_and_malformed_access_evidence_are_rejected():
    snapshot = capture()
    snapshot["features"]["access"]["as_of"] = "2026-09-02T12:00:00Z"
    with pytest.raises(ValueError, match="after decision_at"):
        validate_policy_snapshot(snapshot)
    snapshot = capture()
    snapshot["features"]["access"]["windows"]["3600"]["read"] = True
    with pytest.raises(ValueError, match="access count"):
        validate_policy_snapshot(snapshot)
    snapshot = capture()
    snapshot["features"]["embeddings"][0]["similarity"] = float("nan")
    with pytest.raises(ValueError, match="finite JSON"):
        validate_policy_snapshot(snapshot)


def test_runner_preserves_first_snapshot_and_dry_runs_never_persist(tmp_path):
    catalog = Catalog()
    drivers = {tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm")}
    drivers["warm"].put_object("bk", "report.txt", b"some bytes")
    catalog.upsert("bk", "report.txt", 10, "warm", {"password": "never-persist"})

    class Loader:
        features = feature_projection()

        def load(self, records, requests):
            return {(rec.bucket, rec.key): self.features for rec in records}

    loader = Loader()
    runner = PolicyRunner(
        catalog,
        drivers,
        Mover(drivers, catalog),
        ContentAwarePolicy(size_threshold=20),
        feature_loader=loader,
        idempotency_namespace="snapshot-retry",
        audit_occurred_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    runner.plan_once("bk", dry_run=True)
    runner.preview_once("bk")
    assert catalog.list_audit_events() == []
    first = runner.plan_once("bk")
    event = catalog.get_audit_event(first[0].decision_event_id)
    assert event.schema_version == 1
    original = copy.deepcopy(event.details["dataset"])
    assert original["decision_at"] > original["features"]["access"]["as_of"]
    assert original["decision_at"] > event.occurred_at
    assert {
        key: event.details[key] for key in ("action", "current_tier", "destination_tier", "size")
    } == {
        "action": "move",
        "current_tier": "warm",
        "destination_tier": "hot",
        "size": 10,
    }
    loader.features = feature_projection(state=FeatureState.UNAVAILABLE)
    loader.features = replace(
        loader.features,
        mime=replace(
            loader.features.mime,
            provenance=PolicyFeatureProvenance("catalog.mime", 1, "b" * 64),
        ),
    )
    runner.policy.size_threshold = 100
    second = runner.plan_once("bk")
    assert second[0].decision_event_id == first[0].decision_event_id
    assert second[0].features.to_dict() == original["features"]
    assert second[0].expected_source_sha256 == "a" * 64
    assert second[0].reason == first[0].reason == "<= 20 bytes"
    events = catalog.list_audit_events(
        AuditQuery(event_types=frozenset({AuditEventType.POLICY_DECISION}))
    )
    assert len(events) == 1
    assert events[0].details["dataset"] == original
    assert replay_policy_snapshot(events[0].details["dataset"]).dst_tier == "hot"


def test_retry_cannot_turn_first_rejection_into_a_selected_move(tmp_path):
    catalog = Catalog()
    drivers = {tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm")}
    drivers["hot"].put_object("bk", "large.bin", b"some bytes")
    catalog.upsert("bk", "large.bin", 10, "hot")
    runner = PolicyRunner(
        catalog,
        drivers,
        Mover(drivers, catalog),
        SimplePolicy(size_threshold=1),
        allowed_tiers=["hot"],
        idempotency_namespace="rejected-retry",
    )
    assert runner.plan_once("bk") == []
    runner.allowed_tiers = frozenset(drivers)
    assert runner.plan_once("bk") == []
    events = catalog.list_audit_events()
    assert len(events) == 1
    assert events[0].details["dataset"]["decision"]["outcome"] == "rejected"
