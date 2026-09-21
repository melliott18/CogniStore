#!/usr/bin/env python3
"""Evaluate M5 client/telemetry evidence offline, without claiming full qualification.

Exact nearest-rank samples are spooled to temporary SQLite storage so a 72-hour
stream need not fit in memory. SQLite here is report storage, never a scheduler
or a substitute for the selected PostgreSQL deployment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "release/load/profile.json"
OPERATIONS = ("get", "head", "catalog", "ask", "put", "delete", "policy_preview")
PHASES = ("nominal", "burst", "capacity")
RATIOS = ("api_cpu_ratio", "worker_cpu_ratio", "api_rss_ratio", "worker_rss_ratio")
DISKS = ("hot_free_ratio", "catalog_free_ratio", "broker_free_ratio")
OBSERVATIONS = (
    "api_rss_bytes", "worker_rss_bytes", "api_tmp_bytes", "worker_tmp_bytes",
    "api_shm_bytes", "worker_shm_bytes", "api_connections", "worker_connections",
    "catalog_connections", "broker_connections", "queue_pending", "queue_oldest_seconds",
    "queue_bytes", "hot_used_bytes", "catalog_used_bytes", "broker_used_bytes",
    "ingress_bytes_total", "egress_bytes_total",
)
PENDING = [
    "Accepted specification/owners and exact qualified candidate, configuration and deployed environment.",
    "Reviewed corpus size/MIME/checksum manifests, hot spots, balanced mutations and background workload.",
    "Verified movement calls/windows, scan publication-to-completion lag and all jobs drained/reconciled.",
    "Full object/checksum/catalog/hold/audit and tenant reconciliation before/after load and every fault.",
    "Saturation, bounded retries, backend throttling, fault recovery and burst recovery within 600 seconds.",
    "Actual private alert receipt <=300s, acknowledgement <=900s, runbook execution and recovery receipt <=300s.",
    "Post-drain RSS/tmp <=110% of warm baseline, no leaks/OOM/exhaustion, stop controls and connection headroom.",
    "Measured physical consumption/cost inputs, fixed-node scaling recommendation and affected reruns.",
]


class InvalidEvidence(ValueError):
    """Only fixed error codes may leave the analyzer, never raw evidence values."""


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidEvidence("duplicate_json_key")
        result[key] = value
    return result


def decode(raw):
    def invalid_constant(_):
        raise InvalidEvidence("nonfinite_json")
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


def number(value, *, integer=False, minimum=0):
    if (type(value) not in ((int,) if integer else (int, float))
            or not math.isfinite(value) or value < minimum):
        raise InvalidEvidence("invalid_number")
    return value


def timestamp(value):
    if not isinstance(value, str):
        raise InvalidEvidence("invalid_timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise InvalidEvidence("timestamp_not_utc")
    return parsed.timestamp()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def quantile(db, cohort, metric, count, percentile):
    if not count:
        return None
    offset = math.ceil(count * percentile / 100) - 1
    return db.execute(
        "SELECT value FROM samples WHERE cohort=? AND metric=? ORDER BY value LIMIT 1 OFFSET ?",
        (cohort, metric, offset),
    ).fetchone()[0]


def check(report, name, passed, **evidence):
    report["checks"][name] = {"status": "incomplete" if passed is None
                              else "passed" if passed else "failed", **evidence}


def rows(path, report):
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while raw := source.readline(65537):
            hasher.update(raw)
            if len(raw) > 65536:
                raise InvalidEvidence("record_too_large")
            value = decode(raw)
            if not isinstance(value, dict):
                raise InvalidEvidence("record_not_object")
            yield value
    report["input_sha256"][path.name] = hasher.hexdigest()


def campaign_inputs(directory, profile, report):
    with (directory / "campaign.json").open("rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise InvalidEvidence("campaign_too_large")
    report["input_sha256"]["campaign.json"] = digest(raw)
    campaign = decode(raw)
    if (not isinstance(campaign, dict) or not isinstance(campaign.get("bindings"), dict)
            or not isinstance(campaign.get("phases"), dict)):
        raise InvalidEvidence("invalid_campaign_structure")
    bindings = campaign["bindings"]
    for name, pattern in (
        ("source_revision", r"[0-9a-f]{40}"), ("image_digest", r"sha256:[0-9a-f]{64}"),
        ("configuration_sha256", r"[0-9a-f]{64}"), ("specification_sha256", r"[0-9a-f]{64}"),
        ("dependency_manifest_sha256", r"[0-9a-f]{64}"),
        ("corpus_manifest_sha256", r"[0-9a-f]{64}"),
        ("environment_id", r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"),
    ):
        valid = isinstance(bindings.get(name), str) and bool(re.fullmatch(pattern, bindings[name]))
        if valid:
            valid = not re.search(r"REPLACE|TODO|CHANGEME|pending", bindings[name], re.I)
        check(report, f"identity.{name}", True if valid else None)
    check(report, "identity.profile", campaign.get("profile_sha256") == report["profile_sha256"])
    check(report, "identity.staging_scope", True if campaign.get("execution_scope")
          == "isolated-staging" else None)
    windows = {}
    for cohort in PHASES:
        phase = campaign["phases"][cohort]
        if not isinstance(phase, dict):
            raise InvalidEvidence("invalid_campaign_structure")
        expected = profile["phases"][cohort]
        duration = number(phase["duration_seconds"], integer=True, minimum=1)
        # Reject corrupt durations before they can allocate unbounded report state.
        if duration > 90 * 86400:
            raise InvalidEvidence("duration_out_of_range")
        start = timestamp(phase["started_at"])
        windows[cohort] = (start, start + duration)
        check(report, f"{cohort}.duration", duration >= expected["duration_seconds"],
              actual_seconds=duration, minimum_seconds=expected["duration_seconds"])
        check(report, f"{cohort}.warmup", number(phase["warmup_seconds"]) >= expected["warmup_seconds"])
        check(report, f"{cohort}.object_count", number(phase["object_count"], integer=True)
              == expected["object_count"])
    intervals = sorted(windows.values())
    check(report, "cohorts.separate", all(a[1] <= b[0] for a, b in zip(intervals, intervals[1:])))
    return campaign


def foreground(directory, campaign, profile, report, db):
    counts = {cohort: {"observed": 0, "successful": 0, "groups": {},
                       "operations": {}, "tenants": {"pilot-a": 0, "pilot-b": 0},
                       "outcomes": {}, "actual_max_seconds": 0} for cohort in PHASES}
    for row in rows(directory / "foreground.jsonl", report):
        cohort = row["cohort"]
        if cohort not in counts:
            raise InvalidEvidence("unknown_cohort")
        state = counts[cohort]
        rate = profile["phases"][cohort]["offered_per_second"]
        expected = campaign["phases"][cohort]["duration_seconds"] * rate
        seq = number(row["sequence"], integer=True)
        if seq >= expected:
            raise InvalidEvidence("extra_slot")
        try:
            db.execute("INSERT INTO arrivals VALUES (?, ?)", (cohort, seq))
        except sqlite3.IntegrityError as error:
            raise InvalidEvidence("duplicate_slot") from error
        scheduled = number(row["scheduled_seconds"])
        if not math.isclose(scheduled, seq / rate, abs_tol=0.000001, rel_tol=0):
            raise InvalidEvidence("arrival_schedule_mismatch")
        operation = row["operation"]
        if operation not in OPERATIONS or type(row["success"]) is not bool:
            raise InvalidEvidence("invalid_request_outcome")
        tenant = row["tenant"]
        if tenant not in state["tenants"]:
            raise InvalidEvidence("unknown_tenant")
        outcome = row["outcome"]
        if outcome not in {"ok", "http_error", "timeout", "transport_error", "missed",
                           "concurrency_limit", "adapter_error"} or (outcome == "ok") != row["success"]:
            raise InvalidEvidence("invalid_request_outcome")
        elapsed = number(row["elapsed_seconds"])
        actual = row.get("actual_seconds")
        if outcome in {"ok", "http_error"} and actual is None:
            raise InvalidEvidence("missing_actual_duration")
        if row["success"] and elapsed >= profile["foreground"]["failure_latency_seconds"]:
            raise InvalidEvidence("success_after_deadline")
        if actual is not None:
            actual = number(actual)
            if actual > elapsed + 0.000001:
                raise InvalidEvidence("response_exceeds_arrival_latency")
            state["actual_max_seconds"] = max(state["actual_max_seconds"], actual)
        metric = operation
        if operation in ("put", "get"):
            size = number(row["size_bytes"], integer=True)
            if str(size) not in profile["sizes"]:
                raise InvalidEvidence("unexpected_object_size")
            metric = f"{operation}.{size}"
        score = elapsed if row["success"] else max(elapsed, profile["foreground"]["failure_latency_seconds"])
        db.execute("INSERT INTO samples VALUES (?, ?, ?)", (cohort, metric, score))
        state["groups"][metric] = state["groups"].get(metric, 0) + 1
        state["operations"][operation] = state["operations"].get(operation, 0) + 1
        state["observed"] += 1
        state["successful"] += int(row["success"])
        state["tenants"][tenant] += 1
        state["outcomes"][outcome] = state["outcomes"].get(outcome, 0) + 1
    db.execute("CREATE INDEX sample_values ON samples(cohort, metric, value)")
    for cohort, state in counts.items():
        duration = campaign["phases"][cohort]["duration_seconds"]
        offered = duration * profile["phases"][cohort]["offered_per_second"]
        observed, good = state["observed"], state["successful"]
        complete = offered == observed
        check(report, f"{cohort}.client_coverage", observed / offered >= profile["telemetry"]["coverage"],
              observed=observed, offered=offered, missing=offered-observed)
        # Missing rows cannot improve availability; per-bin percentiles remain
        # inconclusive because their operation/size cannot be reliably inferred.
        previous, longest_missing = -1, 0
        for (sequence,) in db.execute("SELECT sequence FROM arrivals WHERE cohort=? ORDER BY sequence", (cohort,)):
            longest_missing = max(longest_missing, sequence - previous - 1)
            previous = sequence
        longest_missing = max(longest_missing, offered - previous - 1)
        gap = longest_missing / profile["phases"][cohort]["offered_per_second"]
        check(report, f"{cohort}.client_gap", gap <= profile["telemetry"]["max_gap_seconds"],
              longest_missing_seconds=gap)
        report["foreground"][cohort] = {**state, "offered": offered, "duration_seconds": duration,
                                         "availability": good / offered, "successful_per_second": good / duration}
        if cohort == "burst":
            continue  # Deliberate saturation is never pooled into nominal SLOs.
        check(report, f"{cohort}.availability", good / offered >= profile["foreground"]["availability"],
              numerator=good, denominator=offered)
        check(report, f"{cohort}.throughput", good / duration >= profile["foreground"]["successful_per_second"],
              successes=good, duration_seconds=duration)
        if cohort == "nominal":
            check(report, "nominal.attempts", offered >= profile["foreground"]["nominal_min_attempts"])
        for operation in OPERATIONS:
            op_count = state["operations"].get(operation, 0)
            minimum = profile["foreground"]["nominal_min_per_operation"] if cohort == "nominal" else 1
            check(report, f"{cohort}.{operation}.samples", op_count >= minimum, count=op_count, minimum=minimum)
            bins = [f"{operation}.{size}" for size in profile["sizes"]] if operation in ("put", "get") else [operation]
            for metric in bins:
                count = state["groups"].get(metric, 0)
                threshold = operation
                if operation in ("put", "get"):
                    threshold = "small_object" if int(metric.split(".")[1]) <= 1048576 else "large_object"
                    minimum = profile["foreground"]["nominal_min_per_size_bin"] if cohort == "nominal" else 1
                    check(report, f"{cohort}.{metric}.samples", count >= minimum, count=count, minimum=minimum)
                for percentile, limit in zip((95, 99), profile["foreground"]["latency_seconds"][threshold]):
                    value = quantile(db, cohort, metric, count, percentile)
                    check(report, f"{cohort}.{metric}.p{percentile}",
                          value <= limit if value is not None and complete else None,
                          seconds=value, maximum_seconds=limit, samples=count)


def telemetry(directory, campaign, profile, report, db):
    states = {c: {"count": 0, "last_slot": -1, "last_time": 0, "max_gap": 0,
                   "minima": {}, "maxima": {}} for c in PHASES}
    interval = profile["telemetry"]["interval_seconds"]
    for row in rows(directory / "telemetry.jsonl", report):
        cohort = row["cohort"]
        if cohort not in states:
            raise InvalidEvidence("unknown_cohort")
        state = states[cohort]
        instant = number(row["seconds"])
        duration = campaign["phases"][cohort]["duration_seconds"]
        slot = math.floor(instant / interval)
        if instant >= duration or slot <= state["last_slot"]:
            raise InvalidEvidence("duplicate_unordered_or_extra_telemetry")
        state["max_gap"] = max(state["max_gap"], instant - state["last_time"])
        state["last_time"], state["last_slot"] = instant, slot
        state["count"] += 1
        for metric in RATIOS + DISKS + OBSERVATIONS:
            value = number(row[metric])
            if metric in RATIOS + DISKS and value > 1:
                raise InvalidEvidence("invalid_ratio")
            state["minima"][metric] = min(value, state["minima"].get(metric, value))
            state["maxima"][metric] = max(value, state["maxima"].get(metric, value))
            if metric in RATIOS:
                db.execute("INSERT INTO samples VALUES (?, ?, ?)", (cohort, metric, value))
    for cohort, state in states.items():
        duration = campaign["phases"][cohort]["duration_seconds"]
        count = state["count"]
        expected = math.ceil(duration / interval)
        gap = max(state["max_gap"], duration - state["last_time"])
        coverage = count / expected
        check(report, f"{cohort}.telemetry_coverage", coverage >= profile["telemetry"]["coverage"],
              samples=count, expected=expected, coverage=coverage)
        check(report, f"{cohort}.telemetry_gap", gap <= profile["telemetry"]["max_gap_seconds"], max_gap_seconds=gap)
        report["telemetry"][cohort] = {"samples": count, "minima": state["minima"], "maxima": state["maxima"]}
        if cohort == "burst":
            continue
        for metric in RATIOS:
            value = quantile(db, cohort, metric, count, 95)
            check(report, f"{cohort}.{metric}.p95", value < profile["telemetry"]["cpu_rss_p95_limit_ratio"]
                  if value is not None else None, ratio=value)
        for metric in DISKS:
            value = state["minima"].get(metric)
            check(report, f"{cohort}.{metric}", value >= profile["telemetry"]["min_disk_free_ratio"]
                  if value is not None else None, ratio=value)


def evaluate(directory: Path) -> dict:
    raw_profile = PROFILE.read_bytes()
    profile = decode(raw_profile)
    report = {
        "schema": "cognistore.m5-load-evaluation", "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "incomplete", "production_qualified": False,
        "scope": "offline-client-and-resource-calculation",
        "profile_sha256": digest(raw_profile), "input_sha256": {},
        "checks": {}, "foreground": {}, "telemetry": {}, "errors": [],
        "pending_evidence": PENDING,
        "hosted_ci": "skipped: user instruction; known GitHub billing/spending restriction",
        "limitations": [
            "Input identities and observations are supplied evidence, not independently attested deployment facts.",
            "This calculator does not run background/fault campaigns, measure notifications or authorize pilot entry.",
            "No final passing qualification is emitted: independent evidence and named review remain required.",
            "Actual observations remain in hashed raw JSONL; failures score at least 30 seconds for latency gates.",
        ],
    }
    try:
        campaign = campaign_inputs(directory, profile, report)
        with tempfile.TemporaryDirectory(prefix="cognistore-load-evaluation-") as temporary:
            with sqlite3.connect(Path(temporary) / "samples.sqlite3") as db:
                db.execute("CREATE TABLE samples(cohort TEXT, metric TEXT, value REAL)")
                db.execute("CREATE TABLE arrivals(cohort TEXT, sequence INTEGER, PRIMARY KEY(cohort, sequence))")
                foreground(directory, campaign, profile, report, db)
                telemetry(directory, campaign, profile, report, db)
    except InvalidEvidence as error:
        report["errors"].append(str(error))
    except (OSError, ValueError, KeyError, TypeError, OverflowError, sqlite3.Error):
        report["errors"].append("missing_malformed_or_unreadable_evidence")
    statuses = [item["status"] for item in report["checks"].values()]
    report["automated_status"] = ("incomplete" if report["errors"] or "incomplete" in statuses
                                  else "failed" if "failed" in statuses else "passed")
    if "failed" in statuses:
        report["status"] = "failed"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Directory of campaign.json and raw JSONL")
    parser.add_argument("--output", type=Path, required=True, help="Fresh sanitized JSON report path")
    args = parser.parse_args(argv)
    report = evaluate(args.input)
    try:
        # Exclusive creation prevents replacing input evidence or a retained run.
        with args.output.open("x") as destination:
            json.dump(report, destination, indent=2, sort_keys=True, allow_nan=False)
            destination.write("\n")
    except OSError:
        parser.error("output must be a fresh writable path")
    print(f"load evidence: {report['status']}; full qualification remains pending")
    return 1  # Only the owner can accept the complete independent evidence bundle.


if __name__ == "__main__":
    raise SystemExit(main())
