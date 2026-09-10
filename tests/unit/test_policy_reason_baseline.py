"""Structured explanations remain evidence, never supervised baseline features."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from cognistore.core import policy_baseline as baseline
from cognistore.core.audit import AuditQuery, AuditRetentionPolicy
from cognistore.core.catalog import Catalog, ObjectRecord
from cognistore.core.mover import Mover
from cognistore.core.policy import ContentAwarePolicy, SimplePolicy
from cognistore.core.policy_dataset import validate_policy_dataset
from cognistore.core.policy_runner import PolicyRunner
from cognistore.drivers.posix_driver import PosixDriver

ROOT = Path(__file__).resolve().parents[2]


def test_recorded_reasons_do_not_change_baseline_features_labels_or_metrics(tmp_path):
    dataset = json.loads((ROOT / "tests/fixtures/policy_baseline/dataset-v1.json").read_text())
    config = json.loads((ROOT / "configs/policy-baseline-v1.json").read_text())
    enriched = copy.deepcopy(dataset)
    catalog = Catalog(audit_retention=AuditRetentionPolicy(None))
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}

    # Re-evaluate each fixture's real rule proposal to obtain retained reasons
    # with matching policy/model identities, actions, signals, and constraints.
    for index, row in enumerate(enriched["rows"]):
        snapshot = row["snapshot"]
        metadata = snapshot["policy"]
        policy_type = SimplePolicy if metadata["implementation"] == "simple" else ContentAwarePolicy
        runner = PolicyRunner(
            catalog, drivers, Mover(drivers, catalog), policy_type(size_threshold=1024),
            policy_name=metadata["name"], policy_version=metadata["version"],
        )
        record = ObjectRecord(
            "synthetic", f"object-{index}.bin", snapshot["object"]["size"],
            snapshot["object"]["tier"],
        )
        evaluated = runner.reevaluate_record(record, as_of=snapshot["decision_at"])
        assert evaluated.action == snapshot["decision"]["action"]
        assert evaluated.destination_tier == snapshot["decision"]["destination_tier"]
        event, = catalog.list_audit_events(AuditQuery(
            bucket=record.bucket, object_key=record.key,
            event_types=frozenset({"policy.decision"}),
        ))
        row["structured_reason"] = event.details["structured_reason"]

    assert validate_policy_dataset(enriched) == []
    assert [baseline._vector(row) for row in enriched["rows"]] == [
        baseline._vector(row) for row in dataset["rows"]
    ]
    assert [row["label"] for row in enriched["rows"]] == [row["label"] for row in dataset["rows"]]
    original_model = baseline.train_baseline(dataset, config, code_version="test-revision")
    enriched_model = baseline.train_baseline(enriched, config, code_version="test-revision")
    assert enriched_model["stump"] == original_model["stump"]
    assert enriched_model["checks"] == original_model["checks"]
    assert enriched_model["dataset_sha256"] != original_model["dataset_sha256"]
    assert enriched_model["model_version"] != original_model["model_version"]
    original_report = baseline.evaluate_baseline(
        dataset, original_model, code_version="test-revision",
    )
    enriched_report = baseline.evaluate_baseline(
        enriched, enriched_model, code_version="test-revision",
    )
    assert enriched_report["metrics"] == original_report["metrics"]
    assert enriched_report["by_recorded_policy"] == original_report["by_recorded_policy"]

    enriched["rows"][0]["structured_reason"]["schema_version"] = 999
    with pytest.raises(ValueError, match="invalid_structured_reason"):
        baseline.train_baseline(enriched, config, code_version="test-revision")
