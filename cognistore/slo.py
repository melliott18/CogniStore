"""Versioned operational objectives and an offline M1 baseline evaluator.

Qualification summaries are deliberately never reported as rolling SLO attainment.
Run ``python -m cognistore.slo evaluate-qualification REPORT.json`` for JSON evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

MODEL_VERSION = 1
REVIEW_WINDOW = "rolling 30 days; reviewed weekly and after incidents"


@dataclass(frozen=True)
class SLODefinition:
    id: str
    owner: str
    formula: str
    data_sources: tuple[str, ...]
    target: float
    review_window: str = REVIEW_WINDOW
    threshold: float | None = None
    threshold_unit: str | None = None


SLO_DEFINITIONS = (
    SLODefinition(
        "move_success",
        "Storage / movement maintainers",
        "successful completed move calls / all completed move calls",
        ("cognistore_operations_total{component=movement,operation=move}",),
        0.999,
    ),
    SLODefinition(
        "move_latency",
        "Storage / movement maintainers",
        "successful completed move calls taking <=30s / all completed move calls",
        ("cognistore_operation_duration_seconds_bucket{component=movement,operation=move}",
         "cognistore_operation_duration_seconds_count{component=movement,operation=move}"),
        0.99,
        threshold=30.0,
        threshold_unit="seconds",
    ),
    SLODefinition(
        "move_throughput",
        "Storage / worker maintainers",
        "eligible 5m window samples with >=1 successful move/s / eligible 5m window samples",
        ("cognistore_operations_total{component=movement,operation=move}",
         "cognistore_job_queue_depth{state=pending}"),
        0.99,
        threshold=1.0,
        threshold_unit="successful moves/second",
    ),
    SLODefinition(
        "api_availability",
        "API / platform maintainers",
        "matched /v1 HTTP completions with non-5xx status / all matched /v1 HTTP completions",
        ("cognistore_http_requests_total",),
        0.999,
    ),
    SLODefinition(
        "indexing_lag",
        "Catalog / indexing maintainers",
        "successful scan attempts completed <=300s after publication / all completed scan "
        "attempts with known publication time",
        ("cognistore_indexing_lag_seconds_bucket", "cognistore_indexing_lag_seconds_count"),
        0.99,
        threshold=300.0,
        threshold_unit="seconds",
    ),
)
_DEFINITIONS = {definition.id: definition for definition in SLO_DEFINITIONS}


def _integer(value: Any, field: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < int(positive):
        raise ValueError(f"{field} must be a {'positive' if positive else 'non-negative'} integer")
    try:
        if not math.isfinite(float(value)):
            raise ValueError(f"{field} is too large to evaluate")
    except OverflowError as exc:
        raise ValueError(f"{field} is too large to evaluate") from exc
    return value


def _number(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError(f"{field} must be finite and {'positive' if positive else 'non-negative'}")
    return result


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _unavailable(slo_id: str, reason: str) -> dict[str, Any]:
    return {"slo_id": slo_id, "status": "unavailable", "reason": reason}


def evaluate_ratio(slo_id: str, good: int, total: int) -> dict[str, Any]:
    """Evaluate exact event/window counts; unknown or empty evidence is unavailable.

    The caller supplies the cohort/window. This does not infer 30-day coverage.
    Negative budget remaining is retained to show overspend rather than clamped.
    """
    definition = _DEFINITIONS[slo_id]
    try:
        good = _integer(good, "good")
        total = _integer(total, "total", positive=True)
        if good > total:
            raise ValueError("good cannot exceed total")
    except ValueError as exc:
        return _unavailable(slo_id, str(exc))
    bad = total - good
    budget = total * (1 - definition.target)
    return {
        "slo_id": slo_id,
        "status": "met" if good / total >= definition.target else "not_met",
        "good": good,
        "total": total,
        "ratio": good / total,
        "target": definition.target,
        "allowed_bad": budget,
        "observed_bad": bad,
        "budget_remaining_ratio": 1 - bad / budget,
        "burn_rate": (bad / total) / (1 - definition.target),
    }


def _validate_report(report: Any) -> tuple[str, list[Mapping[str, Any]]]:
    report = _mapping(report, "report")
    if report.get("schema") != "cognistore.move-qualification":
        raise ValueError("unsupported qualification schema")
    if type(report.get("schema_version")) is not int or report["schema_version"] != 1:
        raise ValueError("unsupported qualification schema_version")
    profile = report.get("profile")
    if profile not in ("full", "reduced"):
        raise ValueError("profile must be full or reduced")
    acceptance = "full_scale_passed" if profile == "full" else "reduced_scale_only"
    if report.get("status") != "passed" or report.get("acceptance_status") != acceptance:
        raise ValueError("qualification is incomplete, failed, or has inconsistent acceptance status")
    configuration = _mapping(report.get("configuration"), "configuration")
    run_id = configuration.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("configuration.run_id must be a nonempty string")
    timestamps = []
    for key in ("started_at", "finished_at"):
        value = report.get(key)
        if not isinstance(value, str):
            raise ValueError(f"{key} must be an ISO-8601 timestamp")
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{key} must be an ISO-8601 timestamp") from exc
        if timestamp.utcoffset() is None:
            raise ValueError(f"{key} must include a timezone")
        timestamps.append(timestamp)
    if timestamps[1] <= timestamps[0]:
        raise ValueError("finished_at must follow started_at")
    if configuration.get("profile") != profile:
        raise ValueError("configuration profile disagrees with report profile")
    objects = _integer(configuration.get("object_count"), "object_count", positive=True)
    if profile == "full" and objects != 1_000_000:
        raise ValueError("full profile requires 1,000,000 objects per path")
    raw_paths = report.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValueError("paths must be a nonempty array")
    paths = [_mapping(path, "path") for path in raw_paths]
    configured_paths = configuration.get("paths")
    if not isinstance(configured_paths, list) or not configured_paths:
        raise ValueError("configuration.paths must be a nonempty array")
    configured_names = [_mapping(path, "configuration path").get("name")
                        for path in configured_paths]
    if any(not isinstance(name, str) or not name.strip() for name in configured_names):
        raise ValueError("configuration path names must be nonempty strings")
    if len(configured_names) != len(set(configured_names)):
        raise ValueError("configuration path names must be unique")
    names: set[str] = set()
    total_objects = total_retries = 0
    for path in paths:
        name = path.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("path names must be nonempty and unique")
        names.add(name)
        if path.get("status") != "passed":
            raise ValueError(f"path {name} is not complete")
        integrity = _mapping(path.get("integrity"), f"{name}.integrity")
        for key in ("silent_loss", "corruption"):
            if _integer(integrity.get(key), f"{name}.integrity.{key}") != 0:
                raise ValueError(f"{name} has reported {key}")
        for direction in ("forward", "reverse"):
            phase = _mapping(path.get(direction), f"{name}.{direction}")
            count = _integer(phase.get("objects"), "objects", positive=True)
            attempts = _integer(phase.get("attempts"), "attempts", positive=True)
            retries = _integer(phase.get("retries"), "retries")
            if count != objects or attempts != count + retries:
                raise ValueError(f"{name}.{direction} has inconsistent object/attempt counts")
            elapsed = _number(phase.get("elapsed_seconds"), "elapsed_seconds", positive=True)
            rate = _number(phase.get("objects_per_second"), "objects_per_second")
            if not math.isclose(rate, count / elapsed, rel_tol=1e-6):
                raise ValueError(f"{name}.{direction} has inconsistent throughput")
            latency = _mapping(phase.get("latency_ms"), "latency_ms")
            values = [_number(latency.get(key), f"latency_ms.{key}")
                      for key in ("min", "p50", "p95", "p99", "max")]
            if values != sorted(values):
                raise ValueError(f"{name}.{direction} has non-monotonic latency quantiles")
            ranks = (1, (count * 50 + 99) // 100, (count * 95 + 99) // 100,
                     (count * 99 + 99) // 100, count)
            for index in range(1, len(ranks)):
                if ranks[index] == ranks[index - 1] and values[index] != values[index - 1]:
                    raise ValueError(f"{name}.{direction} has contradictory nearest-rank quantiles")
            audit = _mapping(integrity.get(direction), f"{name}.integrity.{direction}")
            for key in ("verified_objects", "present_objects", "completed_move_jobs",
                        "catalog_placements"):
                if _integer(audit.get(key), f"integrity.{direction}.{key}") != count:
                    raise ValueError(f"{name}.{direction} has inconsistent integrity counts")
            if _integer(audit.get("absent_objects"), "absent_objects") != 0:
                raise ValueError(f"{name}.{direction} has unexpected remaining objects")
            total_objects += count
            total_retries += retries
    if set(configured_names) != names:
        raise ValueError("completed paths disagree with configuration.paths")
    summary = _mapping(report.get("summary"), "summary")
    for key, expected in (("logical_moves", total_objects), ("failed_attempts", total_retries),
                          ("paths_completed", len(paths)), ("paths_started", len(paths)),
                          ("objects_per_path", objects), ("silent_loss", 0), ("corruption", 0)):
        if _integer(summary.get(key), f"summary.{key}") != expected:
            raise ValueError(f"summary.{key} disagrees with completed phase evidence")
    return profile, paths


def _latency_bounds(phase: Mapping[str, Any]) -> dict[str, Any]:
    """Bound successful-within-threshold samples using nearest-rank quantiles.

    These bounds describe the logical-duration proxy, not individual call
    durations: the successful last retry can be faster than its logical move.
    """
    count, attempts = phase["objects"], phase["attempts"]
    threshold_ms = 1000 * (_DEFINITIONS["move_latency"].threshold or 0)
    lower, upper = 0, count
    for key, rank in (("min", 1), ("p50", (count * 50 + 99) // 100),
                      ("p95", (count * 95 + 99) // 100),
                      ("p99", (count * 99 + 99) // 100),
                      ("max", count)):
        if phase["latency_ms"][key] <= threshold_ms:
            lower = max(lower, rank)
        else:
            upper = min(upper, rank - 1)
    target = _DEFINITIONS["move_latency"].target
    return {
        "slo_id": "move_latency",
        "status": ("met" if lower / attempts >= target else
                   "not_met" if upper / attempts < target else "inconclusive"),
        "evidence_kind": "logical_latency_nearest_rank_bounds_per_attempt",
        "target": target,
        "threshold_seconds": threshold_ms / 1000,
        "good_lower_bound": lower,
        "good_upper_bound": upper,
        "total": attempts,
        "ratio_lower_bound": lower / attempts,
        "ratio_upper_bound": upper / attempts,
        "exact_event_ratio": False,
    }


def evaluate_qualification(report: Any) -> dict[str, Any]:
    """Compare completed M1 campaign summaries to the movement baseline targets."""
    result: dict[str, Any] = {
        "schema": "cognistore.slo-evaluation",
        "schema_version": 1,
        "model_version": MODEL_VERSION,
        "scope": "qualification_baseline",
        "qualification_verification": "input_consistency_only",
        "rolling_30_day_attainment": "unavailable",
        "definitions": [asdict(item) for item in SLO_DEFINITIONS],
        "limitations": [
            "One qualification campaign cannot establish rolling 30-day SLO attainment.",
            "Logical moves/attempts is a conservative qualification proxy, not live call telemetry; "
            "replays and a killed worker do not have identical runtime observation coverage.",
            "Logical latency includes retry/backoff, excludes executor queue wait, and supplies "
            "nearest-rank bounds rather than exact completed-call threshold counts.",
            "Phase-average throughput cannot establish the fraction of eligible 5-minute windows "
            "that meet the throughput SLO.",
            "M1 evidence contains no API availability or indexing-lag observations.",
        ],
    }
    try:
        profile, paths = _validate_report(report)
    except ValueError as exc:
        result.update(status="unavailable", evidence_profile="unavailable", results=[
            _unavailable(item.id, str(exc)) for item in SLO_DEFINITIONS
        ])
        return result
    results = []
    for path in paths:
        for direction in ("forward", "reverse"):
            phase = path[direction]
            success = evaluate_ratio("move_success", phase["objects"], phase["attempts"])
            success["evidence_kind"] = "logical_success_per_attempt_proxy"
            success["exact_event_ratio"] = False
            throughput = {
                "slo_id": "move_throughput",
                "status": "unavailable",
                "reason": "Phase average has no eligible 5-minute window counts.",
                "evidence_kind": "phase_average_baseline_only",
                "baseline_status": ("met" if phase["objects_per_second"] >= 1 else "not_met"),
                "baseline_threshold_per_second": 1.0,
                "observed_per_second": phase["objects_per_second"],
            }
            for item in (success, _latency_bounds(phase), throughput):
                item.update(path=path["name"], direction=direction)
                results.append(item)
    results.extend(_unavailable(slo_id, "Not measured by M1 movement qualification.")
                   for slo_id in ("api_availability", "indexing_lag"))
    comparisons = [item.get("baseline_status", item["status"]) for item in results
                   if item["slo_id"] not in ("api_availability", "indexing_lag")]
    status = ("baseline_failed" if "not_met" in comparisons else
              "inconclusive" if "inconclusive" in comparisons else "baseline_passed")
    environment = report.get("environment", {})
    git = environment.get("git", {}) if isinstance(environment, Mapping) else {}
    if not isinstance(git, Mapping):
        git = {}
    provenance = {
        "source_schema": report["schema"],
        "source_schema_version": report["schema_version"],
        "run_id": report["configuration"]["run_id"],
        "started_at": report["started_at"],
        "finished_at": report["finished_at"],
        "git_revision": git.get("revision") if isinstance(git.get("revision"), str) else None,
        "git_dirty": git.get("dirty") if isinstance(git.get("dirty"), bool) else None,
    }
    result.update(status=status, evidence_profile=profile,
                  reported_full_scale_qualification=profile == "full", provenance=provenance,
                  results=results)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    evaluate = subparsers.add_parser("evaluate-qualification", help="compare an M1 JSON baseline")
    evaluate.add_argument("report", type=Path)
    args = parser.parse_args(argv)
    try:
        report_bytes = args.report.read_bytes()
        report = json.loads(report_bytes.decode("utf-8"), parse_constant=_invalid_constant)
        result = evaluate_qualification(report)
        result["input_sha256"] = hashlib.sha256(report_bytes).hexdigest()
    except (OSError, ValueError) as exc:
        result = evaluate_qualification(None)
        result["input_error"] = str(exc)
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return {"baseline_passed": 0, "baseline_failed": 1}.get(result["status"], 2)


def _invalid_constant(value: str) -> None:
    raise ValueError(f"nonfinite JSON constant: {value}")


if __name__ == "__main__":
    raise SystemExit(main())
