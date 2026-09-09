from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from cognistore.core.audit import (
    AuditContext,
    AuditEvent,
    AuditEventType,
    AuditOutcome,
    AuditQuery,
    AuditRetentionPolicy,
)
from cognistore.core.catalog import Catalog
from cognistore.core.mover import Mover
from cognistore.core.policy import (
    ContentAwarePolicy,
    EmbeddingPolicyRule,
    PolicyDecision,
    SimplePolicy,
)
from cognistore.core.policy_dataset import export_policy_dataset, validate_policy_dataset
from cognistore.core.policy_runner import PolicyRunner
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver

_NO_RETENTION = AuditRetentionPolicy(None)
_WINDOW = 3600


def _timestamp(base: datetime | str, seconds: int = 0) -> str:
    parsed = (
        datetime.fromisoformat(base.replace("Z", "+00:00"))
        if isinstance(base, str)
        else base
    )
    return (
        (parsed + timedelta(seconds=seconds))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _decision_time(decision: AuditEvent) -> str:
    return decision.details["dataset"]["decision_at"]


def _runner(tmp_path: Path, catalog, *, policy=None, count: int = 1):
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    drivers = {"hot": hot, "warm": warm}
    for index in range(count):
        key = f"private-customer-{index}.bin"
        hot.put_object("private-bucket", key, b"123456")
        catalog.upsert("private-bucket", key, 6, tier="hot")
    return PolicyRunner(
        catalog,
        drivers,
        Mover(drivers, catalog),
        SimplePolicy(size_threshold=5) if policy is None else policy,
        idempotency_namespace="dataset-test-job",
        audit_context=AuditContext(
            correlation_id="dataset-test-request",
            actor_type="worker",
            actor_id="dataset-test-worker",
            job_id="dataset-test-job",
        ),
    )


def _planned(tmp_path: Path, *, policy=None):
    catalog = Catalog(audit_retention=_NO_RETENTION)
    runner = _runner(tmp_path, catalog, policy=policy)
    runner.plan_once("private-bucket")
    decisions = catalog.list_audit_events(
        AuditQuery(event_types=frozenset({AuditEventType.POLICY_DECISION}))
    )
    assert len(decisions) == 1
    return catalog, decisions[0]


def _outcome(
    catalog,
    decision: AuditEvent,
    event_type: AuditEventType = AuditEventType.MOVE_COMPLETED,
    *,
    seconds: int = 10,
    recorded_seconds: int | None = None,
    event_id: str | None = None,
) -> AuditEvent:
    outcome = {
        AuditEventType.MOVE_COMPLETED: AuditOutcome.SUCCEEDED,
        AuditEventType.MOVE_FAILED: AuditOutcome.FAILED,
        AuditEventType.MOVE_RETRY: AuditOutcome.RETRYING,
        AuditEventType.MOVE_PREPARED: AuditOutcome.STARTED,
    }[event_type]
    return catalog.append_audit_event(
        AuditEvent.create(
            event_type,
            outcome,
            AuditContext(
                correlation_id=decision.correlation_id,
                actor_type="worker",
                actor_id="dataset-test-worker",
                job_id=decision.job_id,
                causation_id=decision.event_id,
            ),
            bucket=decision.bucket,
            object_key=decision.object_key,
            move_id=decision.move_id,
            occurred_at=_timestamp(_decision_time(decision), seconds),
            recorded_at=_timestamp(
                _decision_time(decision),
                seconds if recorded_seconds is None else recorded_seconds,
            ),
            retention=_NO_RETENTION,
            event_id=event_id,
        )
    )


def _export(catalog, decision: AuditEvent, *, seconds: int = _WINDOW, **kwargs):
    return export_policy_dataset(
        catalog,
        as_of=_timestamp(_decision_time(decision), seconds),
        observation_seconds=_WINDOW,
        **kwargs,
    )


def test_sqlite_export_survives_restart_and_never_recomputes_features(tmp_path: Path) -> None:
    database = tmp_path / "dataset.sqlite3"
    with SQLCatalog(database, audit_retention=_NO_RETENTION) as catalog:
        runner = _runner(tmp_path, catalog)
        runner.run_once("private-bucket")
        decision = catalog.list_audit_events(
            AuditQuery(event_types=frozenset({AuditEventType.POLICY_DECISION}))
        )[0]
        before = _export(catalog, decision)
        persisted = copy.deepcopy(decision.details["dataset"])
        catalog.upsert(
            "private-bucket",
            "private-customer-0.bin",
            999,
            tier="hot",
            metadata={"mime": "text/plain", "replacement": "new corpus"},
        )
        assert _export(catalog, decision) == before

    with SQLCatalog(database, audit_retention=_NO_RETENTION) as reopened:
        assert reopened.get_audit_event(decision.event_id).details["dataset"] == persisted
        assert _export(reopened, decision) == before

    assert before["rows"][0]["label"]["status"] == "observed"
    assert before["rows"][0]["label"]["value"] == 1
    assert before["rows"][0]["snapshot"]["object"]["size"] == 6
    assert validate_policy_dataset(before) == []


def test_completed_move_remains_pending_until_full_observation_window(tmp_path: Path) -> None:
    catalog, decision = _planned(tmp_path)
    completed = _outcome(catalog, decision)

    pending = _export(catalog, decision, seconds=_WINDOW - 1)
    assert pending["rows"][0]["label"]["status"] == "pending"
    assert pending["rows"][0]["label"]["value"] is None
    assert "missing_label" in {error["code"] for error in validate_policy_dataset(pending)}
    assert validate_policy_dataset(pending, require_labels=False) == []

    mature = _export(catalog, decision)
    label = mature["rows"][0]["label"]
    assert label["name"] == "move_succeeded"
    assert label["version"] == 1
    assert label["status"] == "observed"
    assert label["value"] == 1
    assert label["evidence_event_id"] == completed.event_id
    assert label["window_start"] == _decision_time(decision)
    assert label["window_end"] == _timestamp(_decision_time(decision), _WINDOW)
    assert validate_policy_dataset(mature) == []


def test_failure_is_a_negative_label_and_retry_invalidates_it(tmp_path: Path) -> None:
    catalog, decision = _planned(tmp_path)
    failed = _outcome(catalog, decision, AuditEventType.MOVE_FAILED)
    failure = _export(catalog, decision)["rows"][0]["label"]
    assert failure["status"] == "observed"
    assert failure["value"] == 0
    assert failure["evidence_event_id"] == failed.event_id

    _outcome(catalog, decision, AuditEventType.MOVE_RETRY, seconds=20)
    retried = _export(catalog, decision)
    assert retried["rows"][0]["label"]["status"] == "missing"
    assert retried["rows"][0]["label"]["value"] is None
    assert "missing_label" in {error["code"] for error in validate_policy_dataset(retried)}

    completed = _outcome(catalog, decision, seconds=30)
    recovered = _export(catalog, decision)
    assert recovered["rows"][0]["label"]["value"] == 1
    assert recovered["rows"][0]["label"]["evidence_event_id"] == completed.event_id
    assert validate_policy_dataset(recovered) == []


def test_prepared_attempt_after_failure_has_no_terminal_label(tmp_path: Path) -> None:
    catalog, decision = _planned(tmp_path)
    _outcome(catalog, decision, AuditEventType.MOVE_FAILED)
    _outcome(catalog, decision, AuditEventType.MOVE_PREPARED, seconds=20)
    assert _export(catalog, decision)["rows"][0]["label"]["status"] == "missing"


def test_same_timestamp_retry_uses_causal_order_instead_of_uuid_order(tmp_path: Path) -> None:
    catalog, decision = _planned(tmp_path)
    failed = _outcome(
        catalog, decision, AuditEventType.MOVE_FAILED,
        seconds=10, event_id=str(UUID(int=200)),
    )
    retried = _outcome(
        catalog, decision, AuditEventType.MOVE_RETRY,
        seconds=10, event_id=str(UUID(int=100)),
    )
    assert retried.causation_id == failed.event_id
    exported = _export(catalog, decision)
    row = exported["rows"][0]
    assert [outcome["event_id"] for outcome in row["outcomes"]] == [
        failed.event_id, retried.event_id,
    ]
    assert row["label"]["status"] == "missing"
    assert row["label"]["value"] is None
    assert validate_policy_dataset(exported, require_labels=False) == []


def test_terminal_requires_causal_bridge_to_be_known_by_export_cutoff(tmp_path: Path) -> None:
    catalog, decision = _planned(tmp_path)
    bridge = _outcome(
        catalog, decision, AuditEventType.MOVE_PREPARED,
        seconds=5, recorded_seconds=_WINDOW + 1,
    )
    completed = _outcome(catalog, decision, seconds=10)
    assert completed.causation_id == bridge.event_id
    before_bridge = _export(catalog, decision)
    assert before_bridge["rows"][0]["outcomes"] == []
    assert before_bridge["rows"][0]["label"]["status"] == "missing"
    after_bridge = _export(catalog, decision, seconds=_WINDOW + 1)
    assert after_bridge["rows"][0]["label"]["evidence_event_id"] == completed.event_id


@pytest.mark.parametrize(
    ("occurred_seconds", "recorded_seconds"),
    [(_WINDOW + 1, 10), (10, _WINDOW + 1)],
)
def test_export_ignores_future_occurrence_and_future_recording(
    tmp_path: Path,
    occurred_seconds: int,
    recorded_seconds: int,
) -> None:
    catalog, decision = _planned(tmp_path)
    _outcome(
        catalog,
        decision,
        seconds=occurred_seconds,
        recorded_seconds=recorded_seconds,
    )
    exported = _export(catalog, decision)
    assert exported["rows"][0]["outcomes"] == []
    assert exported["rows"][0]["label"]["status"] == "missing"


def test_event_outside_label_window_does_not_supply_positive_label(tmp_path: Path) -> None:
    catalog, decision = _planned(tmp_path)
    _outcome(catalog, decision, seconds=_WINDOW + 1)
    exported = _export(catalog, decision, seconds=_WINDOW * 2)
    assert exported["rows"][0]["label"]["status"] == "missing"
    assert exported["rows"][0]["label"]["evidence_event_id"] is None


def test_late_recording_does_not_rewrite_an_earlier_as_of_export(tmp_path: Path) -> None:
    catalog, decision = _planned(tmp_path)
    original = _export(catalog, decision)
    completed = _outcome(catalog, decision, recorded_seconds=_WINDOW + 10)
    assert _export(catalog, decision) == original
    later = _export(catalog, decision, seconds=_WINDOW + 10)
    assert later["rows"][0]["label"]["status"] == "observed"
    assert later["rows"][0]["label"]["evidence_event_id"] == completed.event_id


def test_later_policy_decision_with_same_move_cannot_label_earlier_decision(
    tmp_path: Path,
) -> None:
    catalog, first = _planned(tmp_path)
    later_details = copy.deepcopy(first.details)
    later_details["dataset"]["decision_at"] = _timestamp(_decision_time(first), 5)
    later = catalog.append_audit_event(
        replace(
            first,
            event_id=str(uuid4()),
            occurred_at=_timestamp(_decision_time(first), 5),
            recorded_at=_timestamp(_decision_time(first), 5),
            details=later_details,
        )
    )
    completed = _outcome(catalog, later)
    exported = _export(catalog, first, seconds=_WINDOW + 5)
    rows = {row["decision_id"]: row for row in exported["rows"]}
    assert rows[first.event_id]["label"]["status"] == "missing"
    assert rows[first.event_id]["outcomes"] == []
    assert rows[later.event_id]["label"]["evidence_event_id"] == completed.event_id


class _RejectedPolicy:
    def evaluate(self, current_tier: str, size: int) -> PolicyDecision:
        return PolicyDecision(action="move", dst_tier="unavailable", reason="rejected move")


@pytest.mark.parametrize("policy", [SimplePolicy(size_threshold=99), _RejectedPolicy()])
def test_stayed_and_rejected_decisions_are_explicitly_not_applicable(
    tmp_path: Path, policy
) -> None:
    catalog, decision = _planned(tmp_path, policy=policy)
    exported = _export(catalog, decision)
    assert exported["rows"][0]["label"]["status"] == "not_applicable"
    assert exported["rows"][0]["label"]["value"] is None
    assert validate_policy_dataset(exported) == []


def test_seeded_sampling_is_repeatable_and_documents_population(tmp_path: Path) -> None:
    catalog = Catalog(audit_retention=_NO_RETENTION)
    runner = _runner(tmp_path, catalog, count=40)
    runner.plan_once("private-bucket")
    decisions = catalog.list_audit_events(
        AuditQuery(event_types=frozenset({AuditEventType.POLICY_DECISION}))
    )
    anchor = max(decisions, key=_decision_time)
    first = _export(catalog, anchor, sample_rate=0.5, seed="train-v1")
    assert first == _export(catalog, anchor, sample_rate=0.5, seed="train-v1")
    alternate = _export(catalog, anchor, sample_rate=0.5, seed="train-v2")
    assert {row["decision_id"] for row in first["rows"]} != {
        row["decision_id"] for row in alternate["rows"]
    }
    assert 0 < len(first["rows"]) < 40
    assert first["manifest"]["candidate_count"] == 40
    assert first["manifest"]["exported_count"] == len(first["rows"])
    sampling = first["manifest"]["sampling"]
    assert sampling["algorithm"] == "sha256"
    assert sampling["seed"] == "train-v1"
    assert sampling["rate"] == 0.5
    assert sampling["unit"] == "decision_id"
    assert sampling["population"] == "retained_versioned_decisions"


def test_legacy_audit_decisions_are_counted_without_inventing_features(tmp_path: Path) -> None:
    catalog, decision = _planned(tmp_path)
    catalog.append_audit_event(
        replace(
            decision,
            event_id=str(uuid4()),
            move_id=None,
            details={"action": "move", "size": 6},
        )
    )
    exported = _export(catalog, decision)
    assert exported["manifest"]["legacy_count"] == 1
    assert [row["decision_id"] for row in exported["rows"]] == [decision.event_id]


def test_occurrence_bounds_select_decisions_without_dropping_their_later_outcomes(
    tmp_path: Path,
) -> None:
    catalog, decision = _planned(tmp_path)
    completed = _outcome(catalog, decision, seconds=10)
    exported = _export(
        catalog,
        decision,
        occurred_after=_timestamp(decision.occurred_at, -1),
        occurred_before=_timestamp(decision.occurred_at, 1),
    )
    assert [row["decision_id"] for row in exported["rows"]] == [decision.event_id]
    assert exported["rows"][0]["label"]["evidence_event_id"] == completed.event_id
    assert _export(
        catalog, decision, occurred_after=_timestamp(decision.occurred_at, 1),
    )["rows"] == []


@pytest.mark.parametrize("seconds", [_WINDOW - 1, _WINDOW + 1])
@pytest.mark.parametrize("source_upper_seconds", [None, _WINDOW - 2, _WINDOW + 100])
def test_export_queries_only_the_effective_cutoff_and_observation_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seconds: int,
    source_upper_seconds: int | None,
) -> None:
    catalog, decision = _planned(tmp_path)
    _outcome(catalog, decision)
    queries = []
    original_query = catalog.list_audit_events

    def record_query(query: AuditQuery | None = None):
        assert query is not None
        queries.append(query)
        return original_query(query)

    monkeypatch.setattr(catalog, "list_audit_events", record_query)
    start = _decision_time(decision)
    source_upper = (
        None if source_upper_seconds is None else _timestamp(start, source_upper_seconds)
    )
    _export(catalog, decision, seconds=seconds, occurred_before=source_upper)
    assert len(queries) == 2
    decisions, history = queries
    assert decisions.event_types == frozenset({AuditEventType.POLICY_DECISION.value})
    cutoff = datetime.fromisoformat(_timestamp(start, seconds).replace("Z", "+00:00"))
    exclusive_cutoff = _timestamp(cutoff + timedelta(microseconds=1))
    expected_upper = exclusive_cutoff if source_upper is None else min(
        source_upper, exclusive_cutoff,
    )
    assert decisions.occurred_before == expected_upper
    assert history.occurred_after == start
    horizon = datetime.fromisoformat(
        _timestamp(start, min(seconds, _WINDOW)).replace("Z", "+00:00")
    )
    assert history.occurred_before == _timestamp(horizon + timedelta(microseconds=1))


def test_future_decisions_cannot_exhaust_the_current_export_query_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, decision = _planned(tmp_path)
    _outcome(catalog, decision)
    future = replace(
        decision,
        event_id=str(uuid4()),
        occurred_at=_timestamp(_decision_time(decision), _WINDOW * 2),
        recorded_at=_timestamp(_decision_time(decision), _WINDOW * 2),
    )
    original_query = catalog.list_audit_events
    observed_bounds = []

    def populated_future_query(query: AuditQuery | None = None):
        assert query is not None
        if query.event_types == frozenset({AuditEventType.POLICY_DECISION.value}):
            observed_bounds.append(query.occurred_before)
            if query.occurred_before is None:
                # Model a large retained future population without creating
                # ten thousand filesystem objects or unrelated audit records.
                return [future] * 10_000
        return original_query(query)

    monkeypatch.setattr(catalog, "list_audit_events", populated_future_query)
    exported = _export(catalog, decision)
    assert len(observed_bounds) == 1
    assert observed_bounds[0] is not None
    assert [row["decision_id"] for row in exported["rows"]] == [decision.event_id]
    assert exported["rows"][0]["label"]["value"] == 1


def test_export_removes_private_fields_and_custom_nested_paths_without_mutation(
    tmp_path: Path,
) -> None:
    catalog, decision = _planned(tmp_path)
    original = copy.deepcopy(decision.details)
    exported = _export(
        catalog,
        decision,
        exclude_fields=("snapshot.object.size", "snapshot.features.mime.provenance.source"),
    )
    row = exported["rows"][0]
    assert "size" not in row["snapshot"]["object"]
    assert "source" not in row["snapshot"]["features"]["mime"]["provenance"]
    assert row["snapshot"]["replay"]["supported"] is False
    assert "export_redaction" in json.dumps(row["snapshot"]["replay"])
    rendered = json.dumps(exported, sort_keys=True)
    assert "private-bucket" not in rendered
    assert "private-customer-0.bin" not in rendered
    assert original["dataset"]["decision"]["reason"] not in rendered
    assert catalog.get_audit_event(decision.event_id).details == original


def test_custom_wildcards_remove_fields_inside_feature_and_config_arrays(tmp_path: Path) -> None:
    private_query = "customer's confidential earnings forecast"
    private_pattern = "secret-project-*"
    policy = ContentAwarePolicy(
        size_threshold=5,
        hot_name_patterns=(private_pattern,),
        embedding_rules=(
            EmbeddingPolicyRule(
                name="financial", query=private_query,
                minimum_similarity=0.8, destination_tier="warm",
            ),
        ),
    )
    catalog, decision = _planned(tmp_path, policy=policy)
    exported = _export(
        catalog, decision,
        exclude_fields=(
            "snapshot.features.embeddings.*.name",
            "snapshot.policy.config.embedding_rules.*.name",
        ),
    )
    snapshot = exported["rows"][0]["snapshot"]
    assert snapshot["features"]["embeddings"]
    assert "name" not in snapshot["features"]["embeddings"][0]
    assert "name" not in snapshot["policy"]["config"]["embedding_rules"][0]
    rendered = json.dumps(exported)
    assert private_query not in rendered
    assert private_pattern not in rendered
    assert "financial" not in rendered


def test_default_privacy_exclusions_remove_digest_copies_from_embedding_provenance(
    tmp_path: Path,
) -> None:
    _, decision = _planned(tmp_path)
    digest = "a1" * 32
    aliases = (
        "hit_content_sha256", "evidence_content_sha256",
        "document_text_sha256", "passage_text_sha256",
    )
    details = copy.deepcopy(decision.details)
    details["dataset"]["features"]["embeddings"] = [{
        "name": "example",
        "query": "example query",
        "state": "fresh",
        "similarity": 0.9,
        "provenance": {
            "source": "dataset-test-index",
            "source_version": 1,
            "content_sha256": digest,
            "details": {alias: digest for alias in aliases},
        },
    }]
    catalog = Catalog(audit_retention=_NO_RETENTION)
    stored = catalog.append_audit_event(replace(decision, details=details))
    assert digest in json.dumps(stored.details)
    exported = _export(catalog, stored)
    assert digest not in json.dumps(exported)
    provenance = exported["rows"][0]["snapshot"]["features"]["embeddings"][0]["provenance"]
    assert "content_sha256" not in provenance
    assert all(alias not in provenance["details"] for alias in aliases)
    assert validate_policy_dataset(exported, require_labels=False) == []


@pytest.fixture
def valid_dataset(tmp_path: Path):
    catalog, decision = _planned(tmp_path)
    _outcome(catalog, decision)
    return _export(catalog, decision)


@pytest.mark.parametrize(
    "path",
    [
        (), ("rows", 0), ("rows", 0, "snapshot"), ("rows", 0, "label"),
        ("rows", 0, "snapshot", "features"),
        ("rows", 0, "snapshot", "features", "access"),
        ("rows", 0, "outcomes", 0),
    ],
)
def test_validator_rejects_unknown_schema_versions(valid_dataset, path) -> None:
    target = valid_dataset
    for part in path:
        target = target[part]
    target["schema_version"] = 999
    errors = validate_policy_dataset(valid_dataset)
    assert errors
    assert all({"code", "path", "message"} <= set(error) for error in errors)


def test_validator_rejects_missing_labels(valid_dataset) -> None:
    del valid_dataset["rows"][0]["label"]
    assert validate_policy_dataset(valid_dataset)


def test_validator_rejects_private_fields_without_echoing_values(valid_dataset) -> None:
    marker = "private-customer-with-personal-data"
    valid_dataset["rows"][0]["snapshot"]["object"]["bucket"] = marker
    errors = validate_policy_dataset(valid_dataset)
    assert errors
    assert marker not in json.dumps(errors)


def test_validator_rejects_outcome_labels_leaking_into_features(valid_dataset) -> None:
    valid_dataset["rows"][0]["snapshot"]["features"]["label"] = {
        "name": "move_succeeded", "value": 1,
    }
    assert validate_policy_dataset(valid_dataset)


@pytest.mark.parametrize("field", ["occurred_at", "recorded_at"])
def test_validator_rejects_labels_that_use_future_outcomes(valid_dataset, field: str) -> None:
    row = valid_dataset["rows"][0]
    row["outcomes"][-1][field] = _timestamp(row["label"]["as_of"], 1)
    assert validate_policy_dataset(valid_dataset)


def test_validator_rejects_label_value_that_disagrees_with_terminal_evidence(
    valid_dataset,
) -> None:
    valid_dataset["rows"][0]["label"]["value"] = 0
    assert validate_policy_dataset(valid_dataset)


def test_validator_rejects_feature_observations_after_decision_time(valid_dataset) -> None:
    snapshot = valid_dataset["rows"][0]["snapshot"]
    snapshot["features"]["access"]["as_of"] = _timestamp(snapshot["decision_at"], 1)
    assert validate_policy_dataset(valid_dataset)


def test_validator_rejects_embedding_index_provenance_from_after_decision(valid_dataset) -> None:
    snapshot = valid_dataset["rows"][0]["snapshot"]
    snapshot["features"]["embeddings"] = [{
        "name": "financial",
        "state": "fresh",
        "similarity": 0.9,
        "provenance": {
            "source": "dataset-test-index",
            "source_version": 1,
            "details": {"indexed_at": _timestamp(snapshot["decision_at"], 1)},
        },
    }]
    errors = validate_policy_dataset(valid_dataset)
    assert errors
    assert any(error["code"] == "temporal_leakage" for error in errors)


@pytest.mark.parametrize("feature", ["mime", "embedding"])
def test_validator_rejects_explicit_null_values_for_fresh_features(valid_dataset, feature) -> None:
    features = valid_dataset["rows"][0]["snapshot"]["features"]
    if feature == "mime":
        features["mime"]["state"] = "fresh"
        features["mime"]["value"] = None
    else:
        features["embeddings"] = [{
            "name": "financial", "state": "fresh", "similarity": None,
            "provenance": {
                "source": "dataset-test-index", "source_version": 1, "details": {},
            },
        }]
    assert validate_policy_dataset(valid_dataset)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("size", -1), ("size", "6"), ("size", True),
        ("allowed_tiers", "warm"), ("allowed_tiers", None),
        ("allowed_tiers", ["warm", 1]), ("allowed_tiers", [""]),
    ],
)
def test_validator_rejects_invalid_object_size_and_allowed_tiers(valid_dataset, field, value) -> None:
    snapshot = valid_dataset["rows"][0]["snapshot"]
    target = snapshot["object"] if field == "size" else snapshot
    target[field] = value
    assert validate_policy_dataset(valid_dataset)


@pytest.mark.parametrize(("field", "value"), [("action", "stay"), ("outcome", "stayed")])
def test_validator_rejects_decision_action_and_outcome_disagreement(valid_dataset, field, value):
    row = valid_dataset["rows"][0]
    row["snapshot"]["decision"][field] = value
    if field == "outcome":
        # Keep the label internally consistent with a stay, so only the
        # contradiction with the captured move action makes this invalid.
        row["outcomes"] = []
        row["label"].update(status="not_applicable", value=None, evidence_event_id=None)
    assert validate_policy_dataset(valid_dataset)


@pytest.mark.parametrize(
    "path",
    [
        ("rows", 0, "snapshot", "policy"),
        ("rows", 0, "snapshot", "features"),
        ("rows", 0, "snapshot", "features", "mime"),
        ("rows", 0, "snapshot", "features", "embeddings"),
    ],
)
def test_validator_detects_missing_policy_and_required_feature_fields(valid_dataset, path) -> None:
    target = valid_dataset
    for part in path[:-1]:
        target = target[part]
    del target[path[-1]]
    assert validate_policy_dataset(valid_dataset)


@pytest.mark.parametrize("value", [None, [], "not a dataset", b"not JSON", object(), float("nan")])
def test_validator_reports_arbitrary_top_level_types_without_raising(value) -> None:
    errors = validate_policy_dataset(value)
    assert errors
    assert all({"code", "path", "message"} <= set(error) for error in errors)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("manifest", "sampling", "rate"), []),
        (("manifest", "sampling", "seed"), {}),
        (("manifest", "excluded_fields"), [{}]),
        (("manifest", "selection"), []),
        (("manifest", "observation_seconds"), []),
        (("rows", 0, "decision_id"), []),
        (("rows", 0, "snapshot", "policy"), []),
        (("rows", 0, "snapshot", "features", "mime", "state"), {}),
        (("rows", 0, "snapshot", "features", "embeddings"), {}),
        (("rows", 0, "snapshot", "features", "access", "as_of"), []),
        (("rows", 0, "snapshot", "decision", "outcome"), []),
        (("rows", 0, "outcomes", 0, "event_type"), []),
        (("rows", 0, "outcomes", 0, "sequence"), {}),
        (("rows", 0, "label", "status"), []),
        (("rows", 0, "label", "evidence_event_id"), {}),
        (("rows", 0, "label", "value"), {}),
    ],
)
def test_validator_reports_malformed_nested_types_without_raising(valid_dataset, path, value) -> None:
    target = valid_dataset
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    errors = validate_policy_dataset(valid_dataset)
    assert errors
    assert all({"code", "path", "message"} <= set(error) for error in errors)


@pytest.mark.parametrize("rate", [-0.1, 0, 1.1, float("nan"), float("inf"), True])
def test_export_rejects_invalid_sampling_rates(tmp_path: Path, rate) -> None:
    catalog, decision = _planned(tmp_path)
    with pytest.raises(ValueError):
        _export(catalog, decision, sample_rate=rate)
