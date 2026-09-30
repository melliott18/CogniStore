#!/usr/bin/env python3
"""Fail-closed, offline assessment of a bound M5 campaign evidence bundle.

The manifest hashes fixed artifact names; records bind to the immutable run and
candidate identity. Hashes establish consistency, not independent attestation.
Even complete passing measurements require named owner review and never grant
production qualification or close an issue. Synthetic inputs stay synthetic.

All normalized records carry ``run_id`` and ``binding_sha256``. The latter is
SHA256 of canonical JSON {"run_id": ..., "bindings": campaign["bindings"]}.
Canonical JSON uses sorted keys, separators=(',', ':'), and no NaN values.
See the unit fixtures for the complete normalized evidence contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts import load_qualification as base
except ModuleNotFoundError:  # Direct execution from the repository root.
    import load_qualification as base

FILES = (
    "campaign.json", "foreground.jsonl", "telemetry.jsonl", "movement.jsonl",
    "jobs.jsonl", "scans.jsonl", "reconciliations.jsonl", "faults.jsonl",
    "resource-checkpoints.jsonl", "alerts.jsonl", "observed-artifacts.jsonl",
    "capacity-cost.json", "movement-backlog.jsonl",
)
RECONCILIATION_CHECKS = (
    "checksum_mismatches", "missing_objects", "extra_objects", "catalog_mismatches",
    "hold_bypasses", "audit_gaps", "cross_tenant_leaks", "duplicate_destructive_effects",
    "unresolved_jobs", "unknown_outcomes", "lost_acknowledged_objects",
)
RESOURCE_VALUES = ("api_rss_bytes", "worker_rss_bytes", "api_tmp_bytes", "worker_tmp_bytes")
INCIDENTS = ("oom", "disk_exhaustion", "temp_exhaustion", "monotonic_leak")
DIRECTIONS = ("hot-to-warm", "warm-to-hot")
FAULT_KINDS = ("saturation", "backend_throttle", "bounded_retry", "burst")
HEX = re.compile(r"[0-9a-f]{64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def binding_digest(campaign):
    return base.digest(canonical({"run_id": campaign["run_id"], "bindings": campaign["bindings"]}))


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise base.InvalidEvidence("invalid_evidence_identifier")
    return value


def boolean(value):
    if type(value) is not bool:
        raise base.InvalidEvidence("invalid_boolean")
    return value


def ratio_check(report, name, numerator, denominator, per_thousand):
    base.check(report, name, numerator * 1000 >= denominator * per_thousand if denominator else None,
               numerator=numerator, denominator=denominator)


def fixed_file(directory, name):
    path = directory / name
    if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
        raise base.InvalidEvidence("missing_or_unsafe_artifact")
    return path


def read_json(path, limit=4 * 1024 * 1024):
    with path.open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise base.InvalidEvidence("artifact_too_large")
    value = base.decode(raw)
    if not isinstance(value, dict):
        raise base.InvalidEvidence("invalid_document")
    return value


def snapshot(directory, destination, report):
    """Copy and hash once, then evaluate those exact bytes (avoids TOCTOU reads)."""
    manifest_path = fixed_file(directory, "acceptance-manifest.json")
    manifest = read_json(manifest_path)
    if manifest.get("schema_version") != 1 or set(manifest.get("artifacts", {})) != set(FILES):
        raise base.InvalidEvidence("invalid_artifact_manifest")
    report["manifest_sha256"] = base.digest(manifest_path.read_bytes())
    for name in FILES:
        expected = manifest["artifacts"][name]
        if not isinstance(expected, str) or not HEX.fullmatch(expected):
            raise base.InvalidEvidence("invalid_artifact_digest")
        source = fixed_file(directory, name)
        with source.open("rb") as incoming, (destination / name).open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        hasher = hashlib.sha256()
        with (destination / name).open("rb") as copied:
            while chunk := copied.read(1024 * 1024):
                hasher.update(chunk)
        if hasher.hexdigest() != expected:
            raise base.InvalidEvidence("artifact_digest_mismatch")
        report["input_sha256"][name] = expected
    campaign = read_json(destination / "campaign.json")
    if (manifest.get("run_id") != identifier(campaign.get("run_id"))
            or manifest.get("campaign_sha256") != report["input_sha256"]["campaign.json"]):
        raise base.InvalidEvidence("manifest_identity_mismatch")
    return campaign


class Evidence:
    def __init__(self, directory, campaign, report):
        self.directory, self.campaign, self.report = directory, campaign, report
        self.binding = binding_digest(campaign)
        self.run_id = campaign["run_id"]
        self.start, self.end = base.timestamp(campaign["started_at"]), base.timestamp(campaign["ended_at"])
        if not self.start < self.end or self.end - self.start > 90 * 86400:
            raise base.InvalidEvidence("invalid_campaign_interval")
        self.artifacts = {}
        self.used_proofs = set()

    def bound(self, row):
        if row.get("run_id") != self.run_id or row.get("binding_sha256") != self.binding:
            raise base.InvalidEvidence("record_identity_mismatch")
        return row

    def rows(self, name):
        for row in base.rows(self.directory / name, {"input_sha256": {}}):
            yield self.bound(row)

    def instant(self, value):
        instant = base.timestamp(value)
        if not self.start <= instant <= self.end:
            raise base.InvalidEvidence("observation_outside_campaign")
        return instant

    def interval(self, row, first="started_at", last="completed_at"):
        start, end = self.instant(row[first]), self.instant(row[last])
        if end < start:
            raise base.InvalidEvidence("reversed_observation_interval")
        return start, end

    def proof(self, row):
        proof_id = row.get("source_evidence_id")
        artifact = self.artifacts.get(proof_id)
        if artifact is None:
            raise base.InvalidEvidence("missing_observation_artifact")
        if proof_id in self.used_proofs:
            raise base.InvalidEvidence("reused_observation_artifact")
        expected = {key: value for key, value in row.items()
                    if key not in ("run_id", "binding_sha256", "source_evidence_id")}
        if artifact["content"].get("observation") != expected:
            raise base.InvalidEvidence("observation_artifact_content_mismatch")
        self.used_proofs.add(proof_id)
        return artifact

    def load_artifacts(self):
        digests = set()
        for row in self.rows("observed-artifacts.jsonl"):
            evidence_id = identifier(row["evidence_id"])
            identifier(row["kind"])
            self.instant(row["observed_at"])
            if not isinstance(row["content"], dict) or not row["content"]:
                raise base.InvalidEvidence("empty_observation_artifact")
            if evidence_id in self.artifacts:
                raise base.InvalidEvidence("duplicate_observation_artifact")
            # The raw producer's identity is included in content too: merely
            # wrapping yesterday's unbound artifact in today's row is rejected.
            self.bound(row["content"])
            digest = base.digest(canonical(row["content"]))
            if digest in digests:
                raise base.InvalidEvidence("replayed_observation_artifact")
            digests.add(digest)
            self.artifacts[evidence_id] = {**row, "sha256": digest}


def phases(evidence):
    intervals = {}
    for name in base.PHASES:
        row = evidence.campaign["phases"][name]
        start = evidence.instant(row["started_at"])
        end = start + base.number(row["duration_seconds"], integer=True, minimum=1)
        if end > evidence.end:
            raise base.InvalidEvidence("phase_outside_campaign")
        # Independently observed stop time prevents a declared 72h duration
        # from replacing a short run when normalizing its campaign metadata.
        if evidence.instant(row["ended_at"]) != end:
            raise base.InvalidEvidence("phase_duration_mismatch")
        if row.get("completed") is not True or evidence.instant(row["actual_ended_at"]) < end:
            raise base.InvalidEvidence("phase_not_completed")
        intervals[name] = (start, end)
    return intervals


def campaign_state(evidence):
    campaign, report = evidence.campaign, evidence.report
    base.check(report, "campaign.staging_scope", True if campaign.get("execution_scope") == "isolated-staging" else None)
    base.check(report, "campaign.completed", campaign.get("status") == "observations-collected"
               if campaign.get("status") is not None else None)
    base.check(report, "campaign.no_stop_latch", not boolean(campaign["stop_latched"]))
    base.check(report, "campaign.no_uncertain_mutations",
               base.number(campaign["uncertain_mutations"], integer=True) == 0)


def jobs_and_scans(evidence):
    report, jobs, scans = evidence.report, {}, Counter()
    scan_attempts = defaultdict(list)
    for row in evidence.rows("jobs.jsonl"):
        job_id = identifier(row["job_id"])
        if job_id in jobs:
            raise base.InvalidEvidence("duplicate_job")
        if row["tenant"] not in ("pilot-a", "pilot-b") or row["kind"] not in ("scan", "policy", "move"):
            raise base.InvalidEvidence("invalid_job")
        published, completed = evidence.interval(row, "published_at")
        accepted = evidence.instant(row["accepted_at"])
        if not published <= accepted <= completed:
            raise base.InvalidEvidence("invalid_job_chronology")
        if row["outcome"] not in ("succeeded", "failed", "cancelled", "expected_conflict"):
            raise base.InvalidEvidence("unresolved_job")
        count = base.number(row["object_count"], integer=True, minimum=1)
        if (row["kind"] == "scan" and count > 100) or (row["kind"] == "policy" and count > 1000):
            raise base.InvalidEvidence("background_prefix_exceeds_limit")
        if row["kind"] == "scan":
            base.number(row["attempt_count"], integer=True, minimum=1)
        jobs[job_id] = row
    attempt_ids, ontime, total = set(), 0, 0
    for row in evidence.rows("scans.jsonl"):
        attempt_id = identifier(row["attempt_id"])
        if attempt_id in attempt_ids:
            raise base.InvalidEvidence("duplicate_scan_attempt")
        attempt_ids.add(attempt_id)
        job = jobs.get(row["job_id"])
        if job is None or job["kind"] != "scan" or row["published_at"] != job["published_at"]:
            raise base.InvalidEvidence("scan_publication_mismatch")
        published, completed = evidence.interval(row, "published_at")
        if completed > evidence.instant(job["completed_at"]):
            raise base.InvalidEvidence("scan_after_job_terminal")
        success = boolean(row["success"])
        scan_attempts[row["job_id"]].append((base.number(row["attempt_number"], integer=True, minimum=1),
                                             completed, success))
        total += 1
        ontime += int(success and completed - published <= 300)
        scans[row["job_id"]] += 1
    for job_id, attempts in scan_attempts.items():
        attempts.sort()
        job = jobs[job_id]
        if [attempt[0] for attempt in attempts] != list(range(1, job["attempt_count"] + 1)):
            raise base.InvalidEvidence("missing_or_duplicate_scan_attempt")
        if (attempts[-1][1] != evidence.instant(job["completed_at"])
                or any(attempt[2] for attempt in attempts[:-1])
                or attempts[-1][2] != (job["outcome"] == "succeeded")
                or any(a[1] > b[1] for a, b in zip(attempts, attempts[1:]))):
            raise base.InvalidEvidence("scan_terminal_mismatch")
    base.check(report, "scans.samples", total >= 1000, samples=total, minimum=1000)
    ratio_check(report, "scans.publication_to_completion", ontime, total, 990)
    base.check(report, "jobs.scan_reconciliation", bool(jobs) and
               all(job_id in scans for job_id, job in jobs.items() if job["kind"] == "scan"))
    base.check(report, "jobs.background_coverage", all(
        any(job["kind"] == kind and job["tenant"] == tenant for job in jobs.values())
        for kind in ("scan", "policy") for tenant in ("pilot-a", "pilot-b")))
    nominal = evidence.campaign["phases"]["nominal"]
    nominal_start = evidence.instant(nominal["started_at"])
    duration = base.number(nominal["duration_seconds"], integer=True, minimum=1)
    for kind, period in (("scan", 300), ("policy", 3600)):
        slots = {(tenant, slot) for tenant in ("pilot-a", "pilot-b")
                 for slot in range((duration + period - 1) // period)}
        observed = set()
        for job in jobs.values():
            offset = evidence.instant(job["published_at"]) - nominal_start
            if job["kind"] == kind and 0 <= offset < duration:
                observed.add((job["tenant"], int(offset // period)))
        base.check(report, f"jobs.{kind}.nominal_schedule", observed == slots,
                   expected_slots=len(slots), observed_slots=len(observed))
    peak = 0
    # Deliberately overloaded drills run outside the ordinary measured phases.
    # Do not count their induced queue saturation as a nominal concurrency fault.
    for phase in evidence.campaign["phases"].values():
        start, end = evidence.instant(phase["started_at"]), evidence.instant(phase["ended_at"])
        events = []
        for job in jobs.values():
            accepted, completed = evidence.instant(job["accepted_at"]), evidence.instant(job["completed_at"])
            if accepted < end and completed > start:
                events.extend(((max(accepted, start), 1), (min(completed, end), -1)))
        outstanding = 0
        for _, change in sorted(events):
            outstanding += change
            peak = max(peak, outstanding)
    base.check(report, "jobs.maximum_in_flight", peak <= 2, observed_maximum=peak)
    return jobs


def movements(evidence, jobs, phase_intervals):
    report, windows, calls, logical = evidence.report, {}, set(), defaultdict(list)
    sizes = {int(size): percent for size, percent in base.decode(base.PROFILE.read_bytes())["sizes"].items()}
    for row in evidence.campaign["movement_windows"]:
        window_id = identifier(row["window_id"])
        start, end = evidence.interval(row, last="ended_at")
        if window_id in windows or row["direction"] not in DIRECTIONS or row["dedicated"] is not True:
            raise base.InvalidEvidence("invalid_movement_window")
        duration = end - start
        if duration < 3600 or duration % 300:
            raise base.InvalidEvidence("invalid_movement_window_duration")
        if any(start < b and end > a for a, b in phase_intervals.values()):
            raise base.InvalidEvidence("movement_window_overlaps_foreground")
        if any(start < window["end"] and end > window["start"] for window in windows.values()):
            raise base.InvalidEvidence("overlapping_movement_windows")
        windows[window_id] = {"start": start, "end": end, "direction": row["direction"],
                              "counts": [0] * int(duration / 300), "sizes": Counter(), "logical": set()}
    eligible, successes, ontime = 0, 0, 0
    cohort_counts = {name: [0, 0, 0] for name in ("nominal", "capacity", "movement")}
    nominal_sizes = {direction: Counter() for direction in DIRECTIONS}
    capacity_directions = set()
    for row in evidence.rows("movement.jsonl"):
        call_id, move_id = identifier(row["call_id"]), identifier(row["logical_move_id"])
        if call_id in calls:
            raise base.InvalidEvidence("duplicate_movement_call")
        calls.add(call_id)
        job = jobs.get(row["job_id"])
        if job is None or job["kind"] not in ("policy", "move"):
            raise base.InvalidEvidence("movement_job_mismatch")
        start, end = evidence.interval(row)
        if start < evidence.instant(job["published_at"]) or end > evidence.instant(job["completed_at"]):
            raise base.InvalidEvidence("movement_outside_job")
        direction, cohort = row["direction"], row["cohort"]
        size = base.number(row["size_bytes"], integer=True, minimum=1)
        if direction not in DIRECTIONS or cohort not in ("nominal", "capacity", "movement", "fault", "correctness"):
            raise base.InvalidEvidence("invalid_movement_cohort")
        if size not in sizes:
            raise base.InvalidEvidence("invalid_movement_size")
        if cohort in phase_intervals and not phase_intervals[cohort][0] <= start < phase_intervals[cohort][1]:
            raise base.InvalidEvidence("movement_outside_phase")
        success, verified = boolean(row["success"]), boolean(row["verified"])
        if success and not verified:
            raise base.InvalidEvidence("unverified_movement_success")
        terminal = row["terminal_state"]
        if terminal not in (None, "succeeded", "expected_conflict"):
            raise base.InvalidEvidence("invalid_movement_terminal")
        if terminal == "succeeded" and not (success and verified):
            raise base.InvalidEvidence("movement_terminal_mismatch")
        if terminal == "expected_conflict" and cohort != "correctness":
            raise base.InvalidEvidence("unexpected_movement_conflict")
        logical[move_id].append((base.number(row["attempt_number"], integer=True, minimum=1), row))
        if cohort not in ("fault", "correctness"):
            eligible += 1
            successes += int(success)
            ontime += int(success and end - start <= 30)
            cohort_counts[cohort][0] += 1
            cohort_counts[cohort][1] += int(success)
            cohort_counts[cohort][2] += int(success and end - start <= 30)
        window_id = row["movement_window_id"]
        if cohort == "movement":
            window = windows.get(window_id)
            if window is None or window["direction"] != direction or not window["start"] <= start < window["end"]:
                raise base.InvalidEvidence("movement_window_mismatch")
            if success and end < window["end"] and move_id not in window["logical"]:
                window["logical"].add(move_id)
                window["counts"][int((end - window["start"]) // 300)] += 1
                window["sizes"][size] += 1
        elif window_id is not None:
            raise base.InvalidEvidence("unexpected_movement_window")
        if terminal == "succeeded" and cohort == "nominal":
            nominal_sizes[direction][size] += 1
        if terminal == "succeeded" and cohort == "capacity":
            capacity_directions.add(direction)
    for attempts in logical.values():
        attempts.sort(key=lambda pair: pair[0])
        if [pair[0] for pair in attempts] != list(range(1, len(attempts) + 1)):
            raise base.InvalidEvidence("missing_or_duplicate_movement_attempt")
        descriptors = {(row["direction"], row["size_bytes"], row["job_id"], row["cohort"],
                        row["movement_window_id"]) for _, row in attempts}
        if len(descriptors) != 1 or any(row["terminal_state"] is not None for _, row in attempts[:-1]):
            raise base.InvalidEvidence("replayed_or_inconsistent_logical_move")
        if any(evidence.instant(current[1]["started_at"]) < evidence.instant(previous[1]["completed_at"])
               for previous, current in zip(attempts, attempts[1:])):
            raise base.InvalidEvidence("overlapping_movement_attempts")
        if attempts[-1][1]["terminal_state"] is None:
            raise base.InvalidEvidence("unresolved_logical_move")
    ratio_check(report, "movement.success", successes, eligible, 999)
    ratio_check(report, "movement.within_30_seconds", ontime, eligible, 990)
    for cohort, (total, successful, timely) in cohort_counts.items():
        ratio_check(report, f"movement.{cohort}.success", successful, total, 999)
        ratio_check(report, f"movement.{cohort}.within_30_seconds", timely, total, 990)
    for direction in DIRECTIONS:
        selected = [window for window in windows.values() if window["direction"] == direction]
        base.check(report, f"movement.{direction}.window_coverage", bool(selected))
        for index, window in enumerate(selected):
            total = sum(window["sizes"].values())
            prefix = f"movement.{direction}.{index}"
            base.check(report, prefix + ".samples", total >= 10000, successes=total)
            base.check(report, prefix + ".size_mix", bool(total) and all(
                window["sizes"][size] * 100 == total * percent for size, percent in sizes.items()))
            ratio_check(report, prefix + ".throughput", sum(n >= 300 for n in window["counts"]),
                        len(window["counts"]), 990)
        total = sum(nominal_sizes[direction].values())
        base.check(report, f"movement.{direction}.nominal_samples", total >= 10000, successes=total)
        base.check(report, f"movement.{direction}.nominal_size_mix", bool(total) and all(
            nominal_sizes[direction][size] * 100 == total * percent for size, percent in sizes.items()))
    base.check(report, "movement.capacity_round_trip", capacity_directions == set(DIRECTIONS))


def movement_backlog(evidence):
    """Require actual directional backlog observations at the profile cadence.

    Planned work, client pacing, and ``dedicated: true`` do not establish that
    queued movement work was available. Source sample times and exact bound
    artifacts are required; unknown and zero backlog cannot become positive.
    """
    profile = base.decode(base.PROFILE.read_bytes())
    interval = profile["telemetry"]["interval_seconds"]
    windows = {}
    for window in evidence.campaign["movement_windows"]:
        start, end = evidence.interval(window, last="ended_at")
        windows[window["window_id"]] = {
            "start": start, "end": end, "last": start, "last_slot": -1, "direction": window["direction"],
            "samples": 0, "positive": 0, "max_gap": 0, "minimum": None,
        }
    for row in evidence.rows("movement-backlog.jsonl"):
        state = windows.get(row["window_id"])
        if state is None or row["direction"] != state["direction"]:
            raise base.InvalidEvidence("unknown_backlog_window")
        instant = evidence.instant(row["observed_at"])
        if not state["start"] <= instant < state["end"]:
            raise base.InvalidEvidence("backlog_outside_movement_window")
        slot = int((instant - state["start"]) // interval)
        if slot <= state["last_slot"]:
            raise base.InvalidEvidence("duplicate_unordered_backlog_sample")
        artifact = evidence.proof(row)
        if artifact["observed_at"] != row["observed_at"]:
            raise base.InvalidEvidence("backlog_artifact_time_mismatch")
        pending = base.number(row["pending_moves"], integer=True)
        base.number(row["in_flight_moves"], integer=True)
        state["max_gap"] = max(state["max_gap"], instant - state["last"])
        state["last"], state["last_slot"] = instant, slot
        state["samples"] += 1
        state["positive"] += int(pending > 0)
        state["minimum"] = pending if state["minimum"] is None else min(state["minimum"], pending)
    base.check(evidence.report, "movement.backlog.window_coverage", bool(windows))
    for index, state in enumerate(windows.values()):
        duration = state["end"] - state["start"]
        expected = int((duration + interval - 1) // interval)
        samples = state["samples"]
        prefix = f"movement.backlog.{index}"
        ratio_check(evidence.report, prefix + ".coverage", samples, expected, 999)
        gap = max(state["max_gap"], state["end"] - state["last"])
        base.check(evidence.report, prefix + ".gap", gap <= profile["telemetry"]["max_gap_seconds"],
                   maximum_gap_seconds=gap)
        base.check(evidence.report, prefix + ".nonempty", state["positive"] == samples if samples else None,
                   positive_samples=state["positive"], samples=samples, minimum_pending_moves=state["minimum"])


def fault_observations(evidence, phase_intervals):
    report, observed = evidence.report, {}
    induced = set()
    required = evidence.campaign["required_faults"]
    expected = {identifier(row["fault_id"]): row["kind"] for row in required}
    if len(expected) != len(required):
        raise base.InvalidEvidence("duplicate_required_fault")
    base.check(report, "faults.required_kinds", set(FAULT_KINDS) <= set(expected.values()))
    for row in evidence.rows("faults.jsonl"):
        fault_id = identifier(row["fault_id"])
        if fault_id in observed or expected.get(fault_id) != row["kind"]:
            raise base.InvalidEvidence("unexpected_or_duplicate_fault")
        evidence.proof(row)
        alert_ids = row["alert_ids"]
        if (not isinstance(alert_ids, list) or not alert_ids or len(set(alert_ids)) != len(alert_ids)
                or not set(alert_ids) <= set(evidence.campaign["induced_alert_ids"])):
            raise base.InvalidEvidence("fault_alert_coverage_missing")
        induced.update(alert_ids)
        start, recovered = evidence.interval(row, last="recovered_at")
        cleared = evidence.instant(row["cleared_at"])
        if not start <= cleared <= recovered:
            raise base.InvalidEvidence("invalid_fault_chronology")
        observed[fault_id] = (start, recovered)
        measurements = row["observations"]
        kind = row["kind"]
        if kind == "saturation":
            valid = (base.number(measurements["queue_cap"], integer=True) == 10000
                     and base.number(measurements["rejected_submissions"], integer=True) > 0
                     and boolean(measurements["stop_submissions_observed"]))
        elif kind == "backend_throttle":
            valid = base.number(measurements["throttled_calls"], integer=True) > 0
        elif kind == "bounded_retry":
            attempts = base.number(measurements["attempts"], integer=True)
            maximum = base.number(measurements["max_attempts"], integer=True)
            valid = (1 < attempts <= maximum <= base.number(evidence.campaign["retry_limit"], integer=True, minimum=2)
                     and base.number(measurements["unresolved"], integer=True) == 0)
        elif kind == "burst":
            valid = (start == phase_intervals["burst"][0] and cleared >= phase_intervals["burst"][1]
                     and boolean(measurements["nominal_latency_restored"])
                     and boolean(measurements["queue_age_restored"]) and recovered - cleared <= 600)
        else:
            # Additional owner-specified drills retain real observations and
            # full before/after reconciliation; they have no invented SLO.
            valid = bool(measurements)
        base.check(report, f"fault.{len(observed)}.observations", valid)
    base.check(report, "faults.coverage", set(observed) == set(expected) and bool(observed))
    base.check(report, "faults.alert_coverage", induced == set(evidence.campaign["induced_alert_ids"]))
    return observed


def reconciliations(evidence, phase_intervals, faults):
    scopes = {**{f"phase:{name}": interval for name, interval in phase_intervals.items()},
              **{f"fault:{name}": interval for name, interval in faults.items()}}
    expected = {(scope, boundary, tenant) for scope in scopes for boundary in ("before", "after")
                for tenant in ("pilot-a", "pilot-b")}
    observed, ids, clean = set(), set(), True
    for row in evidence.rows("reconciliations.jsonl"):
        checkpoint = identifier(row["checkpoint_id"])
        key = (row["scope"], row["boundary"], row["tenant"])
        if checkpoint in ids or key in observed or key not in expected:
            raise base.InvalidEvidence("unexpected_or_duplicate_reconciliation")
        ids.add(checkpoint)
        observed.add(key)
        artifact = evidence.proof(row)
        instant = evidence.instant(row["observed_at"])
        if evidence.instant(artifact["observed_at"]) != instant:
            raise base.InvalidEvidence("reconciliation_artifact_time_mismatch")
        start, end = scopes[row["scope"]]
        if (row["boundary"] == "before" and instant > start) or (row["boundary"] == "after" and instant < end):
            raise base.InvalidEvidence("invalid_reconciliation_boundary")
        clean &= base.number(row["expected_objects"], integer=True) == base.number(row["observed_objects"], integer=True)
        for name in RECONCILIATION_CHECKS:
            clean &= base.number(row["checks"][name], integer=True) == 0
    base.check(evidence.report, "integrity.coverage", observed == expected and bool(observed))
    base.check(evidence.report, "integrity.zero_discrepancies", clean if observed else None)


def resources_and_cost(evidence, phase_intervals):
    checkpoints = {}
    for row in evidence.rows("resource-checkpoints.jsonl"):
        key = (row["cohort"], row["stage"])
        if key in checkpoints or key[0] not in ("nominal", "capacity") or key[1] not in ("baseline", "drain"):
            raise base.InvalidEvidence("invalid_resource_checkpoint")
        evidence.proof(row)
        instant = evidence.instant(row["observed_at"])
        start, end = phase_intervals[key[0]]
        if (key[1] == "baseline" and instant > start) or (key[1] == "drain" and instant < end):
            raise base.InvalidEvidence("invalid_resource_checkpoint_time")
        for metric in RESOURCE_VALUES:
            base.number(row["values"][metric], integer=True)
        base.check(evidence.report, f"resources.{key[0]}.{key[1]}.incidents", all(
            base.number(row["incidents"][name], integer=True) == 0 for name in INCIDENTS))
        checkpoints[key] = row
    for cohort in ("nominal", "capacity"):
        before, after = checkpoints.get((cohort, "baseline")), checkpoints.get((cohort, "drain"))
        base.check(evidence.report, f"resources.{cohort}.drain_coverage", bool(before and after))
        for metric in RESOURCE_VALUES:
            base.check(evidence.report, f"resources.{cohort}.{metric}.drained", (
                after["values"][metric] * 100 <= before["values"][metric] * 110) if before and after else None)
    row = evidence.bound(read_json(evidence.directory / "capacity-cost.json"))
    evidence.proof(row)
    evidence.instant(row["observed_at"])
    for name in ("hot_bytes", "catalog_bytes", "broker_bytes", "ingress_bytes", "egress_bytes"):
        base.number(row["physical_usage"][name], integer=True)
    inputs = row["cost_inputs"]
    for name in ("compute_hours", "storage_gib_hours", "egress_gib", "observed_cost"):
        base.number(inputs[name])
    if not isinstance(inputs["currency"], str) or not re.fullmatch(r"[A-Z]{3}", inputs["currency"]):
        raise base.InvalidEvidence("invalid_cost_currency")
    recommendation = row["scaling_recommendation"]
    base.check(evidence.report, "capacity.observed_cost_and_recommendation", isinstance(recommendation, str)
               and bool(recommendation.strip()) and len(recommendation) <= 4096)


def alerts(evidence):
    try:
        from scripts.load_observations import validate_receipts
    except ModuleNotFoundError:
        from load_observations import validate_receipts
    records = list(evidence.rows("alerts.jsonl"))
    # A receiver artifact and recovery artifact must be distinct, bound raw
    # observations. Receipt timestamps are actual event times, never local
    # fixture-delivery calculations or a successful promtool exit status.
    used = set()
    for row in records:
        for field in ("condition_started_at", "fired_at", "received_at", "acknowledged_at",
                      "runbook_executed_at", "cleared_at", "recovery_received_at"):
            evidence.instant(row[field])
        for reference, instant, extras in (
            ("receipt_id", "received_at", ()), ("recovery_receipt_id", "recovery_received_at", ()),
            ("acknowledgement_id", "acknowledged_at", ()),
            ("runbook_evidence_id", "runbook_executed_at", ("runbook_url",)),
        ):
            artifact_id = row[reference]
            if artifact_id in used or artifact_id in evidence.used_proofs:
                raise base.InvalidEvidence("replayed_alert_receipt")
            used.add(artifact_id)
            artifact = evidence.artifacts.get(artifact_id)
            if artifact is None:
                raise base.InvalidEvidence("alert_artifact_mismatch")
            expected = {field: row[field] for field in ("alert_id", "receiver_id", reference, instant, *extras)}
            if artifact["content"].get("observation") != expected:
                raise base.InvalidEvidence("alert_artifact_content_mismatch")
            if reference == "receipt_id" and artifact["sha256"] != row["source_evidence_sha256"]:
                raise base.InvalidEvidence("alert_artifact_mismatch")
            if artifact["observed_at"] != row[instant]:
                raise base.InvalidEvidence("alert_artifact_time_mismatch")
    result = validate_receipts(records, artifact_ids=set(evidence.artifacts))
    base.check(evidence.report, "alerts.receipts", True if result["status"] == "passed"
               else False if result["status"] == "failed" else None, receipt_count=len(records))
    # The declared list is frozen with the campaign, and every drill must link
    # an induced alert; omitting a slow/missing delivery cannot improve ratios.
    expected = evidence.campaign["induced_alert_ids"]
    if len(set(expected)) != len(expected):
        raise base.InvalidEvidence("duplicate_induced_alert")
    base.check(evidence.report, "alerts.induced_coverage", bool(expected) and
               {row["alert_id"] for row in records} == set(expected))


def evaluate(directory: Path) -> dict:
    report = {
        "schema": "cognistore.m5-load-acceptance", "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(), "status": "incomplete",
        "qualification_status": "measurements-incomplete", "production_qualified": False,
        "input_sha256": {}, "checks": {}, "errors": [],
        "hosted_ci": "skipped: user instruction; known GitHub billing/spending restriction",
        "limitations": ["Hashes bind supplied evidence; they do not attest to a real deployment or owner approval.",
                        "Passing measurements require independent owner review before qualification or ticket closure."],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="cognistore-load-acceptance-") as temporary:
            snapshot_dir = Path(temporary)
            campaign = snapshot(Path(directory), snapshot_dir, report)
            evidence = Evidence(snapshot_dir, campaign, report)
            scope = campaign.get("execution_scope")
            report["execution_scope"] = scope if scope in ("isolated-staging", "synthetic", "local-rehearsal") else "unknown"
            report["binding_sha256"] = evidence.binding
            campaign_state(evidence)
            phase_intervals = phases(evidence)
            # Validate raw row identities before the established calculator
            # consumes its backwards-compatible foreground/telemetry format.
            for name in ("foreground.jsonl", "telemetry.jsonl"):
                for _ in evidence.rows(name):
                    pass
            result = base.evaluate(snapshot_dir)
            report["checks"].update({f"client_resource.{key}": value for key, value in result["checks"].items()})
            report["errors"].extend(result["errors"])
            evidence.load_artifacts()
            jobs = jobs_and_scans(evidence)
            movements(evidence, jobs, phase_intervals)
            movement_backlog(evidence)
            faults = fault_observations(evidence, phase_intervals)
            reconciliations(evidence, phase_intervals, faults)
            resources_and_cost(evidence, phase_intervals)
            alerts(evidence)
    except base.InvalidEvidence as error:
        report["errors"].append(str(error))
    except (OSError, ValueError, KeyError, TypeError, OverflowError, ImportError):
        report["errors"].append("missing_malformed_or_unreadable_evidence")
    statuses = [item["status"] for item in report["checks"].values()]
    report["status"] = ("incomplete" if report["errors"] or "incomplete" in statuses or not statuses
                        else "failed" if "failed" in statuses else "passed")
    report["qualification_status"] = ("measurements-passed-review-required" if report["status"] == "passed"
                                       else "measurements-" + report["status"])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = evaluate(args.input)
    try:
        with args.output.open("x") as destination:
            json.dump(report, destination, indent=2, sort_keys=True, allow_nan=False)
            destination.write("\n")
    except OSError:
        parser.error("output must be a fresh writable path")
    print(f"load measurements: {report['status']}; independent owner review required")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
