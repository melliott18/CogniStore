from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from cognistore.core import policy_baseline as baseline
from cognistore.core.policy_dataset import validate_policy_dataset
from tests.fixtures.policy_baseline.generate import generate

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def dataset() -> dict:
    return json.loads((ROOT / "tests/fixtures/policy_baseline/dataset-v1.json").read_text())


@pytest.fixture
def config() -> dict:
    return json.loads((ROOT / "configs/policy-baseline-v1.json").read_text())


def _train(dataset: dict, config: dict) -> dict:
    return baseline.train_baseline(dataset, config, code_version="test-revision")


def _relabel(row: dict, value: int | None) -> None:
    row["label"]["value"] = value
    if value is None:
        row["outcomes"] = []
        row["label"].update(status="missing", evidence_event_id=None)
    else:
        row["outcomes"][-1].update(
            event_type="move.completed" if value else "move.failed",
            outcome="succeeded" if value else "failed",
        )


def test_fixture_rebuild_and_model_report_reproducibility(dataset: dict, config: dict) -> None:
    assert generate() == dataset
    assert validate_policy_dataset(dataset) == []
    original = copy.deepcopy(dataset)
    first = _train(dataset, config)
    assert first == _train(dataset, config)
    assert dataset == original
    report = baseline.evaluate_baseline(dataset, first, code_version="evaluation-revision")
    assert report == baseline.evaluate_baseline(dataset, first, code_version="evaluation-revision")
    assert report["training_code_version"] == "test-revision"
    assert report["code_version"] == "evaluation-revision"
    assert first["stump"]["feature_index"] == 0
    assert report["checks"]["windows"]["train"]["rows"] == 20
    assert report["promotion"]["status"] == "offline_candidate"
    assert report["promotion"]["production_eligible"] is False
    assert report["promotion"]["automatic_promotion"] is False
    for name in ("validation", "test"):
        learned = report["metrics"][name]["learned_gate"]
        rules = report["metrics"][name]["recorded_rules"]
        assert learned["accuracy"] == learned["balanced_accuracy"] == 1
        assert rules["accuracy"] == rules["balanced_accuracy"] == 0.5
        assert learned["movement"]["planned_bytes"] == 1045
        assert rules["movement"]["planned_bytes"] == 101090
        assert learned["movement"]["proposed_moves"] == 10
        assert learned["movement"]["actual_cost_usd"] is None
        assert learned["constraints"]["allowed_tier_violations"] == 0
        assert learned["constraints"]["residency_cooldown_locality_violations"] is None
        assert learned["stability"]["observed_tier_reversals"] is None
        assert learned["stability"]["size_noise_gate_flip_rate"] == 0
        assert len(report["by_recorded_policy"][name]) == 2


def test_holdout_labels_do_not_change_fitted_stump(dataset: dict, config: dict) -> None:
    first = _train(dataset, config)
    for row in dataset["rows"]:
        if row["snapshot"]["decision_at"] >= "2026-09-03":
            _relabel(row, 1 - row["label"]["value"])
    second = _train(dataset, config)
    assert first["stump"] == second["stump"]
    assert first["model_version"] != second["model_version"]
    report = baseline.evaluate_baseline(dataset, second, code_version="same")
    assert report["metrics"]["test"]["learned_gate"]["accuracy"] == 0
    assert report["promotion"]["status"] == "rejected"


def test_row_order_has_no_effect_on_fit_or_metrics(dataset: dict, config: dict) -> None:
    first = _train(dataset, config)
    dataset["rows"].reverse()
    second = _train(dataset, config)
    assert first["stump"] == second["stump"]
    assert first["checks"] == second["checks"]


@pytest.mark.parametrize("boundary", ["2026-09-03T00:00:00Z", "2026-09-02T17:00:00-07:00"])
def test_purge_late_recorded_evidence_including_exact_boundary(
    dataset: dict, config: dict, boundary: str
) -> None:
    dataset["rows"][0]["outcomes"][0]["recorded_at"] = boundary
    model = _train(dataset, config)
    assert model["checks"]["purged_rows"]["train"] == 1
    assert model["checks"]["windows"]["train"]["rows"] == 19


def test_purge_observation_window_crossing_time_cutoff(dataset: dict, config: dict) -> None:
    row = dataset["rows"][0]
    row["snapshot"]["decision_at"] = "2026-09-02T23:00:00Z"
    row["label"].update(window_start="2026-09-02T23:00:00Z", window_end="2026-09-03T00:00:00Z")
    row["outcomes"][0].update(
        occurred_at="2026-09-02T23:01:00Z", recorded_at="2026-09-02T23:01:00Z"
    )
    assert _train(dataset, config)["checks"]["purged_rows"]["train"] == 1


@pytest.mark.parametrize("second_index", [1, 20, 40])
def test_duplicate_outcome_move_ids_fail_even_without_decision_move_id(
    dataset: dict, config: dict, second_index: int
) -> None:
    assert dataset["rows"][0]["move_id"] is None
    dataset["rows"][second_index]["outcomes"][0]["move_id"] = dataset["rows"][0]["outcomes"][0][
        "move_id"
    ]
    with pytest.raises(ValueError, match="duplicate move/evidence"):
        _train(dataset, config)


def test_duplicate_evidence_across_rows_fails(dataset: dict, config: dict) -> None:
    duplicate = dataset["rows"][0]["outcomes"][0]["event_id"]
    dataset["rows"][20]["outcomes"][0]["event_id"] = duplicate
    dataset["rows"][20]["label"]["evidence_event_id"] = duplicate
    with pytest.raises(ValueError, match="duplicate move/evidence"):
        _train(dataset, config)


def test_unlabeled_rows_are_excluded_and_counted(dataset: dict, config: dict) -> None:
    _relabel(dataset["rows"][0], None)
    _relabel(dataset["rows"][1], None)
    dataset["rows"][1]["snapshot"]["decision"].update(
        action="stay", outcome="stayed", destination_tier=None
    )
    dataset["rows"][1]["label"]["status"] = "not_applicable"
    model = _train(dataset, config)
    assert model["checks"]["excluded_rows"] == {"missing": 1, "not_applicable": 1}
    assert model["checks"]["windows"]["train"]["rows"] == 18


def test_one_class_holdout_is_reported_and_blocks_candidate(dataset: dict, config: dict) -> None:
    for row in dataset["rows"]:
        if row["snapshot"]["decision_at"] >= "2026-09-05":
            _relabel(row, 1)
    model = _train(dataset, config)
    report = baseline.evaluate_baseline(dataset, model, code_version="test")
    assert report["checks"]["class_imbalance"]["test"]["counts"] == {"0": 0, "1": 20}
    assert report["metrics"]["test"]["learned_gate"]["balanced_accuracy"] is None
    assert report["promotion"]["status"] == "rejected"


@pytest.mark.parametrize("bad", ["single_class", "ratio", "small_class"])
def test_training_class_checks_fail_automatically(dataset: dict, config: dict, bad: str) -> None:
    if bad == "single_class":
        for row in dataset["rows"][:20]:
            _relabel(row, 1)
    elif bad == "ratio":
        config["max_class_ratio"] = 1
        _relabel(dataset["rows"][10], 1)
    else:
        config["min_class_count"] = 11
    with pytest.raises(ValueError, match="class-imbalance"):
        _train(dataset, config)


@pytest.mark.parametrize(
    "field,value",
    [
        ("unexpected", 1),
        ("schema_version", True),
        ("schema_version", 2),
        ("train_before", "2026-09-03"),
        ("train_before", 5),
        ("train_before", "2026-09-06T00:00:00Z"),
        ("validation_before", "2026-09-08T00:00:00Z"),
        ("min_leaf_samples", False),
        ("min_class_count", 0),
        ("max_class_ratio", 0.5),
        ("success_threshold", float("nan")),
        ("success_threshold", 0),
        ("success_threshold", 1),
        ("promotion_min_balanced_accuracy_gain", 2),
    ],
)
def test_invalid_config_rejected(dataset: dict, config: dict, field: str, value: object) -> None:
    config[field] = value
    with pytest.raises(ValueError):
        _train(dataset, config)


@pytest.mark.parametrize("field", ["label", "future_success", "target"])
def test_feature_leakage_rejected(dataset: dict, config: dict, field: str) -> None:
    dataset["rows"][0]["snapshot"]["features"]["mime"]["provenance"]["details"][field] = 1
    with pytest.raises(ValueError, match="label_leakage"):
        _train(dataset, config)


def test_missing_size_cannot_silently_become_zero(dataset: dict, config: dict) -> None:
    dataset["manifest"]["excluded_fields"].append("snapshot.object.size")
    for row in dataset["rows"]:
        del row["snapshot"]["object"]["size"]
    with pytest.raises(ValueError, match="object size"):
        _train(dataset, config)


def test_excluded_optional_policy_model_remains_evaluable(dataset: dict, config: dict) -> None:
    dataset["manifest"]["excluded_fields"].append("snapshot.policy.model")
    for row in dataset["rows"]:
        del row["snapshot"]["policy"]["model"]
    assert validate_policy_dataset(dataset) == []
    report = baseline.evaluate_baseline(dataset, _train(dataset, config), code_version="test")
    assert len(report["by_recorded_policy"]["test"]) == 2


def test_extreme_configuration_number_is_a_validation_error(dataset: dict, config: dict) -> None:
    config["max_class_ratio"] = 10**1000
    with pytest.raises(ValueError, match="finite"):
        _train(dataset, config)


def test_empty_time_partition_fails(dataset: dict, config: dict) -> None:
    config["train_before"] = "2026-09-01T00:00:00Z"
    with pytest.raises(ValueError, match="train split has no mature"):
        _train(dataset, config)


def test_size_noise_reports_boundary_sensitivity(dataset: dict, config: dict) -> None:
    for row in dataset["rows"][40:]:
        row["snapshot"]["object"]["size"] = 1050
    model = _train(dataset, config)
    report = baseline.evaluate_baseline(dataset, model, code_version="test")
    assert (
        report["metrics"]["test"]["learned_gate"]["stability"]["size_noise_gate_flip_rate"] == 0.5
    )


@pytest.mark.parametrize("change", ["dataset", "config", "stump", "version", "fields"])
def test_incompatible_or_tampered_artifact_rejected(
    dataset: dict, config: dict, change: str
) -> None:
    model = _train(dataset, config)
    if change == "dataset":
        dataset["rows"][0]["snapshot"]["object"]["size"] += 1
    elif change == "config":
        model["training_config"]["success_threshold"] = 0.9
    elif change == "stump":
        model["stump"]["threshold"] += 1
    elif change == "version":
        model["schema_version"] = 2
    else:
        model["feature_names"].append("label")
    with pytest.raises(ValueError):
        baseline.evaluate_baseline(dataset, model, code_version="test")


def test_returned_artifact_does_not_mutate_module_contract(dataset: dict, config: dict) -> None:
    model = _train(dataset, config)
    model["feature_names"].append("label")
    model["target"]["version"] = 999
    assert _train(dataset, config)["target"]["version"] == 1
    assert "label" not in baseline.FEATURE_NAMES


def test_returned_report_does_not_mutate_model_or_module_contract(
    dataset: dict, config: dict
) -> None:
    model = _train(dataset, config)
    original = copy.deepcopy(model)
    report = baseline.evaluate_baseline(dataset, model, code_version="test")
    report["target"]["version"] = 999
    report["checks"]["feature_allowlist"].append("label")
    report["training_config"]["success_threshold"] = 0.9
    assert model == original
    assert _train(dataset, config)["target"]["version"] == 1
    assert "label" not in baseline.FEATURE_NAMES


def test_missing_recency_and_observed_zero_are_distinguishable(dataset: dict) -> None:
    row = dataset["rows"][0]
    original = baseline._vector(row)
    row["snapshot"]["features"]["access"] = {
        "freshness": "fresh",
        "partial": True,
        "observed_events": 0,
        "recency_seconds": 0,
    }
    vector = baseline._vector(row)
    assert vector != original
    assert vector[4] == original[4] == 0
    assert vector[2:4] == [1, 1]
    assert vector[5] == 1
    row["snapshot"]["features"]["access"]["observed_events"] = -1
    with pytest.raises(ValueError, match="access events"):
        baseline._vector(row)


def test_code_provenance_hashes_installed_source_and_handles_missing_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, "git")

    monkeypatch.setattr(baseline.subprocess, "run", fail)
    version = baseline.code_version()
    assert version.startswith("git:unavailable;python-source-sha256:")
    assert len(version.rsplit(":", 1)[-1]) == 64
    assert version == baseline.code_version()
