from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
import yaml

from cognistore.slo import SLO_DEFINITIONS, evaluate_qualification, evaluate_ratio, main

ARCHIVED_REPORT = (Path(__file__).resolve().parents[2]
                   / "docs/evidence/m1/full-20260827-205845.json")


@pytest.fixture
def report() -> dict:
    return json.loads(ARCHIVED_REPORT.read_text(encoding="utf-8"))


def _result(evaluation: dict, slo_id: str) -> dict:
    return next(item for item in evaluation["results"] if item["slo_id"] == slo_id)


def test_definitions_have_explicit_versioned_operational_contract() -> None:
    assert {item.id for item in SLO_DEFINITIONS} == {
        "move_success", "move_latency", "move_throughput", "api_availability", "indexing_lag",
    }
    for item in SLO_DEFINITIONS:
        assert item.owner and item.formula and item.data_sources
        assert "30 days" in item.review_window
        assert 0 < item.target < 1


def test_deployed_recordings_match_model_targets_and_thresholds() -> None:
    path = ARCHIVED_REPORT.parents[3] / "docker/observability/rules/slo.yml"
    groups = yaml.safe_load(path.read_text(encoding="utf-8"))["groups"]
    recordings = [rule for group in groups for rule in group["rules"] if "record" in rule]
    targets = {}
    for rule in recordings:
        if rule["record"] == "cognistore:slo_target":
            match = re.fullmatch(r"vector\((\d+\.\d+)\)", rule["expr"])
            assert match is not None
            assert rule["labels"]["slo"] not in targets
            targets[rule["labels"]["slo"]] = float(match[1])
    assert targets == {definition.id: definition.target for definition in SLO_DEFINITIONS}
    definitions = {definition.id: definition for definition in SLO_DEFINITIONS}
    for rule in recordings:
        slo = rule.get("labels", {}).get("slo")
        if rule["record"] == "cognistore:slo_bad_ratio" and slo in ("move_latency", "indexing_lag"):
            assert f'le="{definitions[slo].threshold}"' in rule["expr"]
        if rule["record"] == "cognistore:move_throughput_good":
            threshold = definitions["move_throughput"].threshold
            assert f">= bool {threshold:g})" in rule["expr"]


def test_archived_full_qualification_is_a_baseline_not_production_attainment(report: dict) -> None:
    result = evaluate_qualification(report)
    assert result["status"] == "baseline_passed"
    assert result["model_version"] == 1
    assert result["evidence_profile"] == "full"
    assert result["reported_full_scale_qualification"] is True
    assert result["qualification_verification"] == "input_consistency_only"
    assert result["provenance"]["run_id"] == "full-20260827-205845"
    assert result["provenance"]["git_revision"] == "7961c82b560cc661587d20a925a63266d870b4e1"
    assert result["provenance"]["git_dirty"] is False
    assert result["scope"] == "qualification_baseline"
    assert result["rolling_30_day_attainment"] == "unavailable"
    success = _result(result, "move_success")
    assert success["ratio"] == pytest.approx(1_000_000 / 1_000_006)
    assert success["exact_event_ratio"] is False
    latency = _result(result, "move_latency")
    assert latency["good_lower_bound"] == latency["good_upper_bound"] == 1_000_000
    assert latency["ratio_lower_bound"] < 1
    assert latency["exact_event_ratio"] is False
    for slo_id in ("api_availability", "indexing_lag", "move_throughput"):
        assert _result(result, slo_id)["status"] == "unavailable"
    throughput = _result(result, "move_throughput")
    assert throughput["baseline_status"] == "met"
    assert throughput["observed_per_second"] == pytest.approx(35.98437407703288)


def test_reduced_qualification_never_claims_full_scale(report: dict) -> None:
    report["profile"] = report["configuration"]["profile"] = "reduced"
    report["acceptance_status"] = "reduced_scale_only"
    result = evaluate_qualification(report)
    assert result["status"] == "baseline_passed"
    assert result["evidence_profile"] == "reduced"
    assert result["reported_full_scale_qualification"] is False


def test_ratios_and_budget_at_and_beyond_threshold() -> None:
    result = evaluate_ratio("move_success", 999, 1000)
    assert result["status"] == "met"
    assert result["budget_remaining_ratio"] == pytest.approx(0)
    assert result["burn_rate"] == pytest.approx(1)
    breached = evaluate_ratio("move_success", 998, 1000)
    assert breached["status"] == "not_met"
    assert breached["budget_remaining_ratio"] == pytest.approx(-1)
    assert breached["burn_rate"] == pytest.approx(2)
    assert evaluate_ratio("move_throughput", 99, 100)["status"] == "met"


@pytest.mark.parametrize(("good", "total"), [
    (0, 0), (1, 0), (-1, 10), (11, 10), (True, 1), (1, False),
    (float("nan"), 1), (1, float("inf")), (1.0, 2),
    (10**400, 10**400),
])
def test_unavailable_ratio_counts_are_not_success(good, total) -> None:
    assert evaluate_ratio("move_success", good, total)["status"] == "unavailable"


@pytest.mark.parametrize(("keys", "value"), [
    (("schema",), "another-schema"),
    (("schema_version",), True),
    (("schema_version",), 2),
    (("profile",), "unknown"),
    (("status",), "failed"),
    (("acceptance_status",), "reduced_scale_only"),
    (("configuration", "object_count"), 12),
    (("configuration", "profile"), "reduced"),
    (("configuration", "run_id"), None),
    (("started_at",), None),
    (("started_at",), "not a timestamp"),
    (("started_at",), "2026-08-27T20:59:02"),
    (("finished_at",), "2020-01-01T00:00:00Z"),
    (("paths",), []),
    (("paths", 0, "status"), "failed"),
    (("paths", 0, "forward", "attempts"), 0),
    (("paths", 0, "forward", "retries"), -1),
    (("paths", 0, "forward", "elapsed_seconds"), 0),
    (("paths", 0, "forward", "elapsed_seconds"), float("nan")),
    (("paths", 0, "forward", "objects_per_second"), float("inf")),
    (("paths", 0, "forward", "objects_per_second"), 100),
    (("paths", 0, "forward", "latency_ms", "p99"), None),
    (("paths", 0, "forward", "latency_ms", "min"), 999999),
    (("paths", 0, "forward", "latency_ms", "max"), -1),
    (("summary", "failed_attempts"), 0),
    (("summary", "logical_moves"), True),
    (("summary", "silent_loss"), 1),
    (("configuration", "object_count"), 10**400),
    (("paths", 0, "integrity", "corruption"), 1),
    (("paths", 0, "integrity", "forward", "verified_objects"), 999999),
])
def test_invalid_evidence_is_unavailable(report: dict, keys: tuple, value) -> None:
    parent = report
    for key in keys[:-1]:
        parent = parent[key]
    parent[keys[-1]] = value
    result = evaluate_qualification(report)
    assert result["status"] == "unavailable"
    assert all(item["status"] == "unavailable" for item in result["results"])
    json.dumps(result, allow_nan=False)


def test_missing_input_and_duplicate_paths_are_unavailable(report: dict) -> None:
    for value in (None, [], {}, "invalid"):
        assert evaluate_qualification(value)["status"] == "unavailable"
    report["paths"][1]["name"] = report["paths"][0]["name"]
    assert evaluate_qualification(report)["status"] == "unavailable"


def test_missing_configured_path_is_unavailable(report: dict) -> None:
    report["paths"].pop()
    report["summary"].update(paths_started=1, paths_completed=1,
                             logical_moves=2_000_000, failed_attempts=6)
    assert evaluate_qualification(report)["status"] == "unavailable"


def test_repeated_nearest_rank_must_have_identical_values(report: dict) -> None:
    report["profile"] = report["configuration"]["profile"] = "reduced"
    report["acceptance_status"] = "reduced_scale_only"
    report["configuration"]["object_count"] = 1
    phase = report["paths"][0]["forward"]
    phase.update(objects=1, attempts=1, retries=0, elapsed_seconds=1, objects_per_second=1)
    phase["latency_ms"].update(min=0, p50=0, p95=0, p99=0, max=60_000)
    result = evaluate_qualification(report)
    assert result["status"] == "unavailable"
    assert "contradictory nearest-rank" in result["results"][0]["reason"]


def test_quantile_bound_is_inconclusive_when_retry_denominator_matters(report: dict) -> None:
    # p99 <=30s proves 990,000 good logical moves, not 99% of 1,000,006 calls.
    report["paths"][0]["forward"]["latency_ms"]["max"] = 40_000
    result = evaluate_qualification(report)
    latency = _result(result, "move_latency")
    assert latency["status"] == "inconclusive"
    assert latency["good_lower_bound"] == 990_000
    assert latency["good_upper_bound"] == 999_999
    assert result["status"] == "inconclusive"


def test_above_threshold_p99_proves_not_met_for_nearest_rank(report: dict) -> None:
    phase = report["paths"][0]["forward"]
    phase["latency_ms"].update(p99=31_000, max=40_000)
    latency = _result(evaluate_qualification(report), "move_latency")
    assert latency["status"] == "not_met"
    assert latency["good_upper_bound"] == 989_999


def test_latency_threshold_is_inclusive_and_uses_all_outcomes(report: dict) -> None:
    phase = report["paths"][0]["forward"]
    phase["latency_ms"]["max"] = 30_000
    phase["attempts"] = 1_100_000
    phase["retries"] = 100_000
    report["summary"]["failed_attempts"] += 100_000 - 6
    result = evaluate_qualification(report)
    latency = _result(result, "move_latency")
    assert latency["good_lower_bound"] == 1_000_000
    assert latency["status"] == "not_met"
    assert result["status"] == "baseline_failed"


def test_throughput_floor_failure_does_not_invent_window_counts(report: dict) -> None:
    phase = report["paths"][0]["forward"]
    phase["elapsed_seconds"] = 2_000_000
    phase["objects_per_second"] = 0.5
    result = evaluate_qualification(report)
    throughput = _result(result, "move_throughput")
    assert throughput["status"] == "unavailable"
    assert throughput["baseline_status"] == "not_met"
    assert "budget_remaining_ratio" not in throughput
    assert result["status"] == "baseline_failed"


def test_evaluator_does_not_mutate_evidence(report: dict) -> None:
    original = copy.deepcopy(report)
    evaluate_qualification(report)
    assert report == original


def test_cli_evaluates_archived_report(capsys: pytest.CaptureFixture) -> None:
    assert main(["evaluate-qualification", str(ARCHIVED_REPORT)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "baseline_passed"
    assert len(result["input_sha256"]) == 64


@pytest.mark.parametrize("content", ["{invalid", "[]", '{"schema_version": NaN}'])
def test_cli_invalid_json_returns_json_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture, content: str,
) -> None:
    path = tmp_path / "input.json"
    path.write_text(content, encoding="utf-8")
    assert main(["evaluate-qualification", str(path)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"


def test_cli_missing_file_returns_json_unavailable(tmp_path: Path, capsys) -> None:
    assert main(["evaluate-qualification", str(tmp_path / "missing.json")]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "unavailable"
    assert result["input_error"]
