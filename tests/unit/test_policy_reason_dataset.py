from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from cognistore.core.audit import AuditContext, AuditEventType, AuditQuery, AuditRetentionPolicy
from cognistore.core.catalog import Catalog
from cognistore.core.mover import Mover
from cognistore.core.policy import PolicyDecision, SimplePolicy
from cognistore.core.policy_dataset import export_policy_dataset, validate_policy_dataset
from cognistore.core.policy_reasons import validate_policy_reason
from cognistore.core.policy_runner import PolicyRunner
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver

_NO_RETENTION = AuditRetentionPolicy(None)


def _run(tmp_path: Path, catalog, *, policy=None):
    drivers = {
        "hot": PosixDriver(str(tmp_path / "hot")),
        "warm": PosixDriver(str(tmp_path / "warm")),
    }
    for key, content in (("private-small.bin", b"1"), ("private-large.bin", b"123456")):
        drivers["hot"].put_object("private-bucket", key, content)
        catalog.upsert("private-bucket", key, len(content), tier="hot")
    runner = PolicyRunner(
        catalog,
        drivers,
        Mover(drivers, catalog),
        SimplePolicy(size_threshold=5) if policy is None else policy,
        idempotency_namespace="reason-dataset-job",
        policy_version="49-test",
        audit_context=AuditContext(
            correlation_id="reason-dataset-request",
            actor_type="worker",
            actor_id="reason-dataset-worker",
            job_id="reason-dataset-job",
        ),
    )
    runner.run_once("private-bucket")
    return catalog.list_audit_events(
        AuditQuery(event_types=frozenset({AuditEventType.POLICY_DECISION}))
    )


def _export(catalog, decisions, **kwargs):
    latest = max(event.details["dataset"]["decision_at"] for event in decisions)
    cutoff = datetime.fromisoformat(latest.replace("Z", "+00:00")) + timedelta(hours=1)
    return export_policy_dataset(
        catalog, as_of=cutoff.isoformat(), observation_seconds=60, **kwargs
    )


@pytest.fixture
def recorded(tmp_path):
    catalog = Catalog(audit_retention=_NO_RETENTION)
    decisions = _run(tmp_path, catalog)
    return catalog, decisions


def test_reason_dataset_survives_restart_and_traces_move_outcome(tmp_path):
    database = tmp_path / "reasons.sqlite3"
    with SQLCatalog(database, audit_retention=_NO_RETENTION) as catalog:
        decisions = _run(tmp_path, catalog)
        exported = _export(catalog, decisions)
        assert len(decisions) == len(exported["rows"]) == 2
        assert validate_policy_dataset(exported) == []
        by_id = {event.event_id: event for event in decisions}
        for row in exported["rows"]:
            event = by_id[row["decision_id"]]
            assert row["structured_reason"] == event.details["structured_reason"]
            assert validate_policy_reason(row["structured_reason"])["schema_version"] == 1
            assert row["structured_reason"]["policy"]["version"] == "49-test"
            assert row["job_id"] == event.job_id == "reason-dataset-job"
            assert row["correlation_id"] == event.correlation_id
            assert row["snapshot"]["schema_version"] == 1
            if event.outcome == "selected":
                assert row["move_id"] == event.move_id
                assert row["label"]["value"] == 1
                terminal = catalog.get_audit_event(row["label"]["evidence_event_id"])
                assert terminal.event_type == "move.completed"
                assert terminal.move_id == event.move_id
                while terminal.causation_id != event.event_id:
                    terminal = catalog.get_audit_event(terminal.causation_id)
                    assert terminal is not None
                assert terminal.job_id == event.job_id
            else:
                assert row["outcomes"] == []
                assert row["label"]["status"] == "not_applicable"
            encoded = json.dumps(row["structured_reason"])
            assert "private-bucket" not in encoded
            assert "private-small.bin" not in encoded
            assert "private-large.bin" not in encoded

    with SQLCatalog(database, audit_retention=_NO_RETENTION) as reopened:
        assert _export(reopened, decisions) == exported


def test_legacy_snapshot_exports_without_invented_reason(recorded):
    _catalog, decisions = recorded
    source = next(event for event in decisions if event.outcome == "stayed")
    details = copy.deepcopy(source.details)
    details.pop("structured_reason")
    legacy = Catalog(audit_retention=_NO_RETENTION)
    legacy.append_audit_event(replace(source, details=details))

    exported = _export(legacy, [source])

    assert len(exported["rows"]) == 1
    assert "structured_reason" not in exported["rows"][0]
    assert exported["manifest"]["legacy_count"] == 0
    assert validate_policy_dataset(exported) == []


@pytest.mark.parametrize("excluded", ["structured_reason", "**.structured_reason"])
def test_whole_reason_exclusion_keeps_dataset_contract(recorded, excluded):
    catalog, decisions = recorded

    exported = _export(catalog, decisions, exclude_fields=[excluded])

    assert all("structured_reason" not in row for row in exported["rows"])
    assert validate_policy_dataset(exported) == []


@pytest.mark.parametrize(
    "excluded",
    [
        "structured_reason.code",
        "structured_reason.confidence",
        "structured_reason.policy.version",
        "**.decisive_signals",
    ],
)
def test_partial_reason_exclusion_fails_before_output(recorded, excluded):
    catalog, decisions = recorded

    with pytest.raises(ValueError, match="partial structured_reason exclusions"):
        _export(catalog, decisions, exclude_fields=[excluded])


@pytest.mark.parametrize("excluded", [[], ["structured_reason"]])
def test_export_rejects_unknown_stored_reason_version_even_if_excluded(recorded, excluded):
    _catalog, decisions = recorded
    source = decisions[0]
    details = copy.deepcopy(source.details)
    details["structured_reason"]["schema_version"] = 999
    malformed = Catalog(audit_retention=_NO_RETENTION)
    malformed.append_audit_event(replace(source, details=details))

    with pytest.raises(ValueError):
        _export(malformed, [source], exclude_fields=excluded)


@pytest.mark.parametrize("mutation", ["version", "unknown_field", "missing_field"])
def test_import_validates_complete_reason_contract_without_echoing_values(recorded, mutation):
    catalog, decisions = recorded
    exported = _export(catalog, decisions)
    reason = exported["rows"][0]["structured_reason"]
    if mutation == "version":
        reason["schema_version"] = 999
    elif mutation == "unknown_field":
        reason["raw_content"] = "private unmarked customer document sentence"
    else:
        reason.pop("confidence")

    issues = validate_policy_dataset(exported)

    assert "invalid_structured_reason" in {issue["code"] for issue in issues}
    assert "private unmarked customer document sentence" not in json.dumps(issues)


@pytest.mark.parametrize("mutation", ["policy", "disposition"])
def test_import_rejects_valid_reason_that_contradicts_its_snapshot(recorded, mutation):
    catalog, decisions = recorded
    exported = _export(catalog, decisions)
    row = next(row for row in exported["rows"] if row["snapshot"]["decision"]["outcome"] == "stayed")
    if mutation == "policy":
        row["structured_reason"]["policy"]["version"] = "other-policy-version"
    else:
        row["structured_reason"]["disposition"] = "move"
    assert validate_policy_reason(row["structured_reason"])

    issues = validate_policy_dataset(exported)

    assert "invalid_structured_reason" in {issue["code"] for issue in issues}


def test_rejected_invalid_action_preserves_snapshot_v1_stayed_outcome(tmp_path):
    class InvalidPolicy:
        def evaluate(self, current_tier, size):
            return PolicyDecision("archive", "invalid action from custom policy")

    catalog = Catalog(audit_retention=_NO_RETENTION)
    decisions = _run(tmp_path, catalog, policy=InvalidPolicy())

    exported = _export(catalog, decisions)

    assert validate_policy_dataset(exported) == []
    for row in exported["rows"]:
        assert row["snapshot"]["decision"]["action"] == "archive"
        assert row["snapshot"]["decision"]["outcome"] == "stayed"
        assert row["structured_reason"]["code"] == "invalid_action"
        assert row["structured_reason"]["disposition"] == "rejected"
