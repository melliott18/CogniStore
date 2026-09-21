"""Evidence-calculation regression tests; synthetic fixtures never qualify staging."""

from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3

import pytest

from scripts import load_qualification as load


def campaign():
    return {
        "bindings": {
            "source_revision": "a" * 40, "image_digest": "sha256:" + "b" * 64,
            "configuration_sha256": "c" * 64, "specification_sha256": "d" * 64,
            "dependency_manifest_sha256": "e" * 64, "corpus_manifest_sha256": "f" * 64,
            "environment_id": "synthetic-fixture",
        },
        "profile_sha256": hashlib.sha256(load.PROFILE.read_bytes()).hexdigest(),
        "execution_scope": "synthetic",
        "phases": {name: {"duration_seconds": 10, "warmup_seconds": 1800,
                          "object_count": 200000 if name == "capacity" else 100000,
                          "started_at": f"2026-09-{20 + index}T00:00:00Z"}
                   for index, name in enumerate(load.PHASES)},
    }


def request(seq, *, cohort="nominal", success=True, operation="head", elapsed=0.1):
    rate = 30 if cohort == "burst" else 10
    return {"sequence": seq, "cohort": cohort, "scheduled_seconds": seq / rate,
            "operation": operation, "elapsed_seconds": elapsed, "actual_seconds": elapsed,
            "success": success, "outcome": "ok" if success else "http_error",
            "tenant": "pilot-a" if seq % 2 == 0 else "pilot-b", "size_bytes": 4096}


def resource(cohort="nominal", seconds=0):
    return {"cohort": cohort, "seconds": seconds,
            **dict.fromkeys(load.RATIOS, 0.3), **dict.fromkeys(load.DISKS, 0.5),
            **dict.fromkeys(load.OBSERVATIONS, 0)}


def write_run(path, requests=None, resources=None, metadata=None):
    path.mkdir(exist_ok=True)
    (path / "campaign.json").write_text(json.dumps(metadata or campaign()))
    for name, records in (("foreground.jsonl", requests if requests is not None else [request(i) for i in range(100)]),
                          ("telemetry.jsonl", resources if resources is not None else [resource(c) for c in load.PHASES])):
        (path / name).write_text("".join(json.dumps(row) + "\n" for row in records))
    return load.evaluate(path)


def test_nearest_rank_uses_all_samples_and_matches_independent_sort():
    rng = random.Random(165)
    for count in (1, 19, 20, 99, 100, 1001):
        values = [rng.random() * 30 for _ in range(count)]
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE TABLE samples(cohort TEXT, metric TEXT, value REAL)")
            db.executemany("INSERT INTO samples VALUES ('nominal','head',?)", [(v,) for v in values])
            for percentile in (50, 95, 99):
                assert load.quantile(db, "nominal", "head", count, percentile) == sorted(values)[math.ceil(count * percentile / 100) - 1]


def test_fast_errors_are_not_fast_latency_and_missing_arrivals_are_bad(tmp_path):
    records = [request(i, success=i < 94, elapsed=0.01) for i in range(100)]
    report = write_run(tmp_path, records)
    assert report["checks"]["nominal.head.p95"]["seconds"] == 30
    assert report["checks"]["nominal.head.p95"]["status"] == "failed"
    assert report["foreground"]["nominal"]["availability"] == 0.94
    assert report["foreground"]["nominal"]["actual_max_seconds"] == 0.01
    report = write_run(tmp_path, records[:50])
    assert report["checks"]["nominal.availability"]["denominator"] == 100
    assert report["foreground"]["nominal"]["availability"] == 0.5
    assert report["checks"]["nominal.head.p95"]["status"] == "incomplete"


def test_out_of_order_completions_are_valid_but_duplicate_slots_are_rejected(tmp_path):
    records = [request(i) for i in reversed(range(100))]
    assert write_run(tmp_path, records)["errors"] == []
    assert write_run(tmp_path, records + [request(0)])["errors"] == ["duplicate_slot"]


def test_burst_is_separate_and_does_not_improve_nominal(tmp_path):
    records = [request(i, success=False) for i in range(100)]
    records += [request(i, cohort="burst") for i in range(300)]
    report = write_run(tmp_path, records)
    assert report["foreground"]["nominal"]["availability"] == 0
    assert report["foreground"]["burst"]["availability"] == 1
    assert "burst.availability" not in report["checks"]


def test_per_size_latency_is_not_hidden_by_small_objects(tmp_path):
    records = [request(i, operation="get") for i in range(100)]
    records[-1].update(size_bytes=16777216, elapsed_seconds=11, actual_seconds=11)
    report = write_run(tmp_path, records)
    assert report["checks"]["nominal.get.4096.p99"]["status"] == "passed"
    assert report["checks"]["nominal.get.16777216.p99"]["status"] == "failed"
    assert report["checks"]["nominal.put.4096.p99"]["status"] == "incomplete"


@pytest.mark.parametrize("field,value", [
    ("success", 1), ("elapsed_seconds", -1), ("elapsed_seconds", float("nan")),
    ("elapsed_seconds", float("inf")), ("sequence", True), ("sequence", 100),
    ("scheduled_seconds", 8), ("outcome", "private service exception text"),
    ("tenant", "unknown"), ("actual_seconds", 2), ("operation", "unknown"),
])
def test_invalid_records_fail_closed_without_echoing_values(tmp_path, field, value):
    row = request(0)
    row[field] = value
    report = write_run(tmp_path, [row])
    assert report["errors"]
    assert report["automated_status"] == "incomplete"
    assert report["production_qualified"] is False
    assert "private service exception text" not in json.dumps(report)


def test_telemetry_gaps_unknown_queue_age_and_resource_limits(tmp_path):
    metadata = campaign()
    metadata["phases"]["nominal"]["duration_seconds"] = 120
    records = [resource(seconds=i) for i in (0, 15, 30, 105)]
    records[-1]["worker_rss_ratio"] = 0.7
    records[-1]["hot_free_ratio"] = 0.29
    report = write_run(tmp_path, resources=records, metadata=metadata)
    assert report["checks"]["nominal.telemetry_gap"]["max_gap_seconds"] == 75
    assert report["checks"]["nominal.telemetry_gap"]["status"] == "failed"
    assert report["checks"]["nominal.worker_rss_ratio.p95"]["status"] == "failed"
    assert report["checks"]["nominal.hot_free_ratio"]["status"] == "failed"
    records[0]["queue_oldest_seconds"] = None
    assert write_run(tmp_path, resources=records)["errors"] == ["invalid_number"]


def test_telemetry_duplicate_bucket_cannot_inflate_coverage(tmp_path):
    report = write_run(tmp_path, resources=[resource(seconds=0), resource(seconds=1)])
    assert report["errors"] == ["duplicate_unordered_or_extra_telemetry"]


def test_evidence_is_hashed_and_synthetic_results_never_qualify(tmp_path):
    report = write_run(tmp_path)
    for name, expected in report["input_sha256"].items():
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == expected
    assert report["production_qualified"] is False
    assert report["pending_evidence"]
    assert report["checks"]["identity.staging_scope"]["status"] == "incomplete"
    assert report["status"] != "passed"
    assert report["checks"]["nominal.duration"]["status"] == "failed"


def test_missing_files_and_duplicate_json_keys_are_incomplete(tmp_path):
    assert load.evaluate(tmp_path)["errors"] == ["missing_malformed_or_unreadable_evidence"]
    write_run(tmp_path)
    (tmp_path / "foreground.jsonl").write_text('{"success":true,"success":false}\n')
    assert load.evaluate(tmp_path)["errors"] == ["duplicate_json_key"]


@pytest.mark.parametrize("document", [[], {"bindings": [], "phases": {}},
                                      {"bindings": {}, "phases": []}])
def test_invalid_campaign_shapes_are_sanitized(tmp_path, document):
    (tmp_path / "campaign.json").write_text(json.dumps(document))
    assert load.evaluate(tmp_path)["errors"] == ["invalid_campaign_structure"]


def test_success_requires_observed_duration_and_cannot_exceed_deadline(tmp_path):
    row = request(0)
    row.pop("actual_seconds")
    assert write_run(tmp_path, [row])["errors"] == ["missing_actual_duration"]
    for elapsed in (30, 31):
        row.update(actual_seconds=elapsed, elapsed_seconds=elapsed)
        assert write_run(tmp_path, [row])["errors"] == ["success_after_deadline"]


def test_identity_mismatch_and_overlapping_windows_are_not_accepted(tmp_path):
    metadata = campaign()
    metadata["profile_sha256"] = "0" * 64
    metadata["bindings"]["environment_id"] = "REPLACE_STAGING"
    metadata["phases"]["capacity"]["started_at"] = metadata["phases"]["nominal"]["started_at"]
    report = write_run(tmp_path, metadata=metadata)
    assert report["checks"]["identity.profile"]["status"] == "failed"
    assert report["checks"]["identity.environment_id"]["status"] == "incomplete"
    assert report["checks"]["cohorts.separate"]["status"] == "failed"


def test_cli_retains_incomplete_report_and_never_overwrites_input(tmp_path):
    write_run(tmp_path)
    output = tmp_path / "result.json"
    assert load.main(["--input", str(tmp_path), "--output", str(output)]) == 1
    assert json.loads(output.read_text())["production_qualified"] is False
    before = (tmp_path / "campaign.json").read_bytes()
    with pytest.raises(SystemExit):
        load.main(["--input", str(tmp_path), "--output", str(tmp_path / "campaign.json")])
    assert (tmp_path / "campaign.json").read_bytes() == before
