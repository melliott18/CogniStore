#!/usr/bin/env python3
"""Check #166 pilot records offline; never admit data or grant human approval.

This checks record consistency and declared results, not the truth of observations,
reviewer identity/signatures, raw SLO calculations or durable artifact storage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

IDENTITY = (
    "source_commit", "image_digest", "candidate_manifest_sha256", "dependencies_sha256",
    "configuration_sha256", "providers_sha256", "environment_id", "specification_sha256",
    "workload_sha256",
)
QUALIFICATIONS = ("156", "157", "158", "160", "161", "162", "163", "164", "165")
READINESS = (
    "accepted_specification_and_roles", "cohort_and_data_permission", "private_support_and_on_call",
    "backups_and_restore", "current_encryption_attestations", "telemetry_and_alert_delivery",
    "admission_caps_and_stop_controls", "tested_rollback", "release_blocker_review",
)
DAILY = ("integrity", "placement", "holds", "audit", "growth_and_caps", "backups",
         "alerts_and_incidents", "staffing_and_stop_history")
EXIT_GATES = (
    "foreground_slos", "movement_and_scan_slos", "resources_and_queue", "evidence_coverage",
    "integrity_and_isolation", "recovery_and_backups", "nominal_workload_and_bounds",
    "stop_incident_and_resume_review", "material_change_requalification",
)
WORKFLOWS = ("upload-retrieve", "discovery-download", "policy-job", "administration")
OPERATIONS = ("get", "head", "catalog", "ask", "put", "delete", "policy_preview")
SIZE_BINS = ("4096", "65536", "1048576", "16777216")
HOSTED_CI = "skipped: user instruction; known GitHub billing/spending restriction"


def review():
    return {"result": "pending", "reviewer": None, "at": None, "identity": dict.fromkeys(IDENTITY),
            "evidence": []}


def template():
    return {
        "schema_version": 1, "mode": "live-pilot", "specification": "m5-pilot-v1",
        "specification_revision": 1, "identity": dict.fromkeys(IDENTITY), "artifacts": {},
        "identity_artifacts": {key: None for key in IDENTITY if key.endswith("sha256")},
        "owner": {"name": None, "accepted_at": None, "evidence": []},
        "retention": {"owner": None, "until": None, "evidence": []},
        "qualifications": {key: review() for key in QUALIFICATIONS},
        "readiness": {key: review() for key in READINESS},
        "blockers": ["Owner acceptance and exact-candidate qualification remain pending."],
        "cohort": {"testers": [], "tenants": ["pilot-a", "pilot-b"],
                   "synthetic_only": True, "evidence": []},
        "plan": {"declared_at": None, "start_at": None, "end_at": None, "windows": [],
                 "evidence": []},
        "entry": {"decision": "pending", "by": None, "at": None, "rationale": None,
                  "evidence": []},
        "daily": [], "tasks": [], "feedback": [],
        "measurements": {
            "offered_attempts": None, "expected_successes": None, "scan_attempts": None,
            "operation_attempts": dict.fromkeys(OPERATIONS),
            "size_bin_attempts": {op: dict.fromkeys(SIZE_BINS) for op in ("get", "put")},
            "evidence": [],
        },
        "slo_reviews": {"day-7": review(), "day-14": review()},
        "exit_gates": {key: review() for key in EXIT_GATES},
        "exit": {"decision": "pending", "by": None, "at": None, "rationale": None,
                 "evidence": [], "followups": [], "followup_review": [],
                 "roadmap_evidence": [], "operator_docs_evidence": [], "ticket_mirror_evidence": []},
    }


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def reject_constant(_):
    raise ValueError("nonfinite_json")


def label(value):
    return (isinstance(value, str) and bool(value.strip())
            and not re.search(r"REPLACE|TODO|CHANGEME|pending", value, re.I))


def sha(value, length=64):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None


def stamp(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value
    ):
        raise ValueError("utc_timestamp_required")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def number(value, minimum=0, maximum=math.inf, integer=False):
    return (type(value) in ((int,) if integer else (int, float))
            and minimum <= value <= maximum and math.isfinite(value))


def checksum(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def inspect(campaign: Path, stage="entry", *, now=None):
    """Return fixed check labels only; never echo potentially private input values."""
    if stage not in {"entry", "exit"}:
        raise ValueError("invalid_stage")
    now = now or datetime.now(timezone.utc)
    checks = {}
    report = {"schema_version": 1, "stage": stage, "status": "incomplete",
              "pilot_entry_authorized": False, "expansion_authorized": False,
              "resume_authorized": False,
              "production_qualified": False, "hosted_ci": HOSTED_CI, "checks": checks,
              "limitations": ["Record consistency only; human review of raw evidence, signatures and durable retention required.",
                              "No deployment, admission control, live observations or pilot approval performed."]}
    try:
        raw = campaign.read_bytes()
        report["campaign_sha256"] = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
        if not isinstance(data, dict):
            raise ValueError("object_required")
    except (OSError, ValueError, UnicodeError, RecursionError):
        checks["input"] = False
        return report

    def check(key, operation):
        try:
            checks[key] = bool(operation())
        except (KeyError, TypeError, ValueError, AttributeError, OSError, RuntimeError, OverflowError):
            checks[key] = False
        return checks[key]

    check("profile", lambda: type(data["schema_version"]) is int and data["schema_version"] == 1
          and data["mode"] == "live-pilot" and data["specification"] == "m5-pilot-v1"
          and type(data["specification_revision"]) is int and data["specification_revision"] == 1)
    identity = data.get("identity")
    check("identity", lambda: set(identity) == set(IDENTITY) and sha(identity["source_commit"], 40)
          and identity["image_digest"].startswith("sha256:") and sha(identity["image_digest"][7:])
          and label(identity["environment_id"])
          and all(sha(identity[k]) for k in IDENTITY if k.endswith("sha256")))
    artifacts = data.get("artifacts")
    valid_artifacts = set()
    check("artifacts.present", lambda: isinstance(artifacts, dict) and bool(artifacts))
    if isinstance(artifacts, dict):
        for index, (name, artifact) in enumerate(artifacts.items()):
            def valid_artifact():
                relative = Path(artifact["path"])
                path = (campaign.parent / relative).resolve(strict=True)
                return (label(name) and not relative.is_absolute() and ".." not in relative.parts
                        and path.is_relative_to(campaign.parent.resolve())
                        and path != campaign.resolve() and path.is_file()
                        and sha(artifact["sha256"]) and checksum(path) == artifact["sha256"])
            if check(f"artifact.{index}", valid_artifact):
                valid_artifacts.add(name)

    def refs(value):
        return (isinstance(value, list) and bool(value) and all(
            isinstance(item, str) and item in valid_artifacts for item in value
        ) and len(value) == len(set(value)))

    for key in IDENTITY:
        if key.endswith("sha256"):
            check("identity_artifact." + key, lambda key=key: checks["identity"]
                  and refs([data["identity_artifacts"][key]])
                  and artifacts[data["identity_artifacts"][key]]["sha256"] == identity[key])

    check("owner", lambda: label(data["owner"]["name"])
          and stamp(data["owner"]["accepted_at"]) <= stamp(data["entry"]["at"]) <= now
          and refs(data["owner"]["evidence"]))
    check("entry.go", lambda: data["entry"]["decision"] == "go"
          and data["entry"]["by"] == data["owner"]["name"] and label(data["entry"]["by"])
          and label(data["entry"]["rationale"]) and refs(data["entry"]["evidence"]))
    check("blockers.none", lambda: data["blockers"] == [])

    def reviewed(record, earliest, latest, passed=True):
        return (record["result"] in (("passed",) if passed else ("passed", "failed"))
                and label(record["reviewer"]) and refs(record["evidence"])
                and checks["identity"] and record["identity"] == identity
                and earliest <= stamp(record["at"]) <= latest <= now)

    for group, keys in (("qualifications", QUALIFICATIONS), ("readiness", READINESS)):
        check(group + ".keys", lambda group=group, keys=keys: set(data[group]) == set(keys))
        for key in keys:
            check(f"{group}.{key}", lambda group=group, key=key: reviewed(
                data[group][key], datetime.min.replace(tzinfo=timezone.utc), stamp(data["entry"]["at"])))
    check("cohort", lambda: data["cohort"]["tenants"] == ["pilot-a", "pilot-b"]
          and data["cohort"]["synthetic_only"] is True
          and isinstance(data["cohort"]["testers"], list)
          and 1 <= len(data["cohort"]["testers"]) <= 5
          and all(label(t) for t in data["cohort"]["testers"])
          and len(set(data["cohort"]["testers"])) == len(data["cohort"]["testers"])
          and refs(data["cohort"]["evidence"]))
    check("plan", lambda: stamp(data["owner"]["accepted_at"]) <= stamp(data["plan"]["declared_at"])
          <= stamp(data["entry"]["at"]) <= stamp(data["plan"]["start_at"])
          and stamp(data["plan"]["end_at"]) - stamp(data["plan"]["start_at"]) >= timedelta(days=14)
          and refs(data["plan"]["evidence"]))
    windows = []

    def valid_windows():
        if not isinstance(data["plan"]["windows"], list) or len(data["plan"]["windows"]) < 10:
            return False
        for window in data["plan"]["windows"]:
            start, end = stamp(window["start_at"]), stamp(window["end_at"])
            if not (stamp(data["plan"]["start_at"]) <= start < end <= stamp(data["plan"]["end_at"])
                    and end - start == timedelta(hours=8)):
                return False
            windows.append((start, end))
        windows.sort()
        return (all(a[1] <= b[0] for a, b in zip(windows, windows[1:]))
                and len({start.date() for start, _ in windows}) == len(windows))

    check("plan.staffed_windows", valid_windows)
    check("retention", lambda: label(data["retention"]["owner"])
          and stamp(data["retention"]["until"]) >= stamp(data["plan"]["end_at"]) + timedelta(days=90)
          and refs(data["retention"]["evidence"]))
    report["entry_records_complete"] = all(checks.values())
    if stage == "entry":
        report["status"] = "complete-for-review" if all(checks.values()) else "incomplete"
        return report

    # Exit is a success/expansion-readiness check. A truthful stopped/failed pilot
    # remains incomplete here; its decision and all observations must still be retained.
    check("exit.owner_decision", lambda: data["exit"]["decision"] in ("expand", "fix-and-repeat", "stop")
          and data["exit"]["by"] == data["owner"]["name"] and label(data["exit"]["by"])
          and stamp(data["plan"]["end_at"]) <= stamp(data["exit"]["at"]) <= now
          and label(data["exit"]["rationale"]) and refs(data["exit"]["evidence"]))
    check("exit.retention", lambda: stamp(data["retention"]["until"])
          >= stamp(data["exit"]["at"]) + timedelta(days=90))
    check("exit.gates.keys", lambda: set(data["exit_gates"]) == set(EXIT_GATES))
    for key in EXIT_GATES:
        check("exit.gate." + key, lambda key=key: reviewed(data["exit_gates"][key],
              stamp(data["plan"]["end_at"]), stamp(data["exit"]["at"])))
    for day in (7, 14):
        check(f"slo_review.day-{day}", lambda day=day: reviewed(data["slo_reviews"][f"day-{day}"],
              stamp(data["plan"]["start_at"]) + timedelta(days=day - 1),
              min(stamp(data["plan"]["start_at"]) + timedelta(days=day), stamp(data["exit"]["at"]))))
    daily = data.get("daily")
    check("daily.present", lambda: isinstance(daily, list) and bool(daily))
    dates = []
    if isinstance(daily, list):
        for index, item in enumerate(daily):
            def valid_day():
                date = datetime.strptime(item["date"], "%Y-%m-%d").date()
                dates.append(date)
                return (item["date"] == date.isoformat() and label(item["reviewer"])
                        and stamp(item["at"]).date() == date
                        and stamp(item["at"]) <= stamp(data["exit"]["at"]) <= now
                        and item["identity"] == identity and refs(item["evidence"])
                        and set(item["checks"]) == set(DAILY)
                        and all(item["checks"][key] is True for key in DAILY)
                        and number(item["operator_minutes"], maximum=1440)
                        and number(item["objects"], maximum=100000, integer=True)
                        and number(item["logical_bytes"], maximum=30 * 1024**3, integer=True)
                        and number(item["backup_age_hours"], maximum=24))
            check(f"daily.{index}", valid_day)

    def daily_coverage():
        start, end = stamp(data["plan"]["start_at"]), stamp(data["plan"]["end_at"])
        expected = set()
        date = start.date()
        while date <= (end - timedelta(microseconds=1)).date():
            expected.add(date)
            date += timedelta(days=1)
        return len(dates) == len(set(dates)) and set(dates) == expected

    check("daily.coverage", daily_coverage)
    measurements = data.get("measurements")
    check("measurements.evidence", lambda: refs(measurements["evidence"]))
    seconds = sum((end - start).total_seconds() for start, end in windows)
    check("measurements.offered", lambda: checks["plan.staffed_windows"]
          and number(measurements["offered_attempts"], minimum=max(2880000, 10 * seconds), integer=True))
    check("measurements.successes", lambda: number(measurements["expected_successes"], integer=True)
          and measurements["expected_successes"] <= measurements["offered_attempts"]
          and measurements["expected_successes"] * 1000 >= 995 * measurements["offered_attempts"]
          and measurements["expected_successes"] >= 9.95 * seconds)
    check("measurements.scans", lambda: number(measurements["scan_attempts"], minimum=1000, integer=True))
    check("measurements.operations", lambda: set(measurements["operation_attempts"]) == set(OPERATIONS)
          and all(number(v, minimum=10000, integer=True) for v in measurements["operation_attempts"].values())
          and sum(measurements["operation_attempts"].values()) == measurements["offered_attempts"])
    check("measurements.size_bins", lambda: set(measurements["size_bin_attempts"]) == {"get", "put"}
          and all(set(measurements["size_bin_attempts"][op]) == set(SIZE_BINS)
                  and all(number(v, minimum=1000, integer=True)
                          for v in measurements["size_bin_attempts"][op].values())
                  and sum(measurements["size_bin_attempts"][op].values()) == measurements["operation_attempts"][op]
                  for op in ("get", "put")))
    tasks = data.get("tasks")
    check("tasks.minimum", lambda: isinstance(tasks, list) and len(tasks) >= 30)
    task_ids, workflows, successful = [], set(), 0
    if isinstance(tasks, list):
        for index, task in enumerate(tasks):
            def valid_task():
                return (checks["cohort"] and label(task["id"])
                        and label(task["tester"]) and task["tester"] in data["cohort"]["testers"]
                        and task["tenant"] in ("pilot-a", "pilot-b") and task["workflow"] in WORKFLOWS
                        and type(task["completed"]) is bool and type(task["maintainer_intervention"]) is bool
                        and task["identity"] == identity and number(task["completion_seconds"])
                        and refs(task["evidence"]) and any(start <= stamp(task["at"]) < end
                                                           for start, end in windows))
            if check(f"task.{index}", valid_task):
                task_ids.append(task["id"])
                workflows.add(task["workflow"])
                successful += int(task["completed"] and not task["maintainer_intervention"])
        check("tasks.unique", lambda: len(task_ids) == len(set(task_ids)) == len(tasks))
        check("tasks.workflows", lambda: workflows == set(WORKFLOWS))
        check("tasks.usefulness", lambda: bool(tasks) and successful * 100 >= 90 * len(tasks))
    feedback = data.get("feedback")
    check("feedback.present", lambda: isinstance(feedback, list) and len(feedback) >= 3)
    testers = set()
    if isinstance(feedback, list):
        for index, item in enumerate(feedback):
            if check(f"feedback.{index}", lambda: checks["cohort"] and label(item["tester"])
                     and item["tester"] in data["cohort"]["testers"]
                     and label(item["usefulness"]) and label(item["failure_clarity"])
                     and refs(item["evidence"]) and stamp(data["plan"]["start_at"])
                     <= stamp(item["at"]) <= stamp(data["exit"]["at"]) <= now):
                testers.add(item["tester"])
        check("feedback.distinct_testers", lambda: len(testers) >= 3)
    for field in ("followup_review", "roadmap_evidence", "operator_docs_evidence", "ticket_mirror_evidence"):
        check("exit." + field, lambda field=field: refs(data["exit"][field]))
    check("exit.followups", lambda: isinstance(data["exit"]["followups"], list)
          and all(re.fullmatch(r"https://github.com/melliott18/CogniStore/issues/[1-9][0-9]*", item["url"])
                  and item["priority"] in ("P0", "P1", "P2", "P3") and label(item["owner"])
                  and label(item["finding"]) for item in data["exit"]["followups"]))
    report["success_records_complete"] = all(checks.values())
    report["status"] = "complete-for-review" if all(checks.values()) else "incomplete"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--output", type=Path, required=True)
    check = sub.add_parser("check")
    check.add_argument("--campaign", type=Path, required=True)
    check.add_argument("--stage", choices=("entry", "exit"), default="entry")
    check.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "check" and args.output.resolve() == args.campaign.resolve():
        parser.error("output_must_not_replace_campaign")
    report = template() if args.command == "init" else inspect(args.campaign, args.stage)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation preserves earlier observations and decision records.
    try:
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
    except FileExistsError:
        parser.error("output_already_exists")
    return 0 if args.command == "init" or report["status"] == "complete-for-review" else 1


if __name__ == "__main__":
    raise SystemExit(main())
