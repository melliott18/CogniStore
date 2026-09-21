"""Synthetic record checks never establish live qualification or pilot authority."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("pilot_review", ROOT / "scripts/pilot_review.py")
assert spec and spec.loader
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)

START = datetime(2026, 8, 1, tzinfo=timezone.utc)
END = START + timedelta(days=14)
NOW = END + timedelta(days=1)


def utc(value):
    return value.isoformat().replace("+00:00", "Z")


@pytest.fixture
def campaign(tmp_path):
    """Fabricate records, not passing observations, for the offline checker only."""
    data = pilot.template()
    identity = data["identity"]
    identity.update(source_commit="a" * 40, image_digest="sha256:" + "b" * 64,
                    environment_id="synthetic-fixture-environment")
    for key in data["identity_artifacts"]:
        artifact = tmp_path / f"{key}.txt"
        artifact.write_text(f"synthetic fixture: {key}\n")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        data["artifacts"][key] = {"path": artifact.name, "sha256": digest}
        data["identity_artifacts"][key] = key
        identity[key] = digest
    evidence = tmp_path / "observations.txt"
    evidence.write_text("Synthetic fixture only; no deployment or live observations.\n")
    data["artifacts"]["observations"] = {
        "path": evidence.name, "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
    }
    refs = ["observations"]

    def review(at):
        return {"result": "passed", "reviewer": "fixture-reviewer", "at": utc(at),
                "identity": dict(identity), "evidence": list(refs)}

    data["owner"] = {"name": "fixture-owner", "accepted_at": utc(START - timedelta(days=2)),
                     "evidence": list(refs)}
    data["entry"] = {"decision": "go", "by": "fixture-owner",
                     "at": utc(START - timedelta(days=1)),
                     "rationale": "Synthetic review scenario", "evidence": list(refs)}
    for group in ("qualifications", "readiness"):
        data[group] = {key: review(START - timedelta(days=1)) for key in data[group]}
    data["blockers"] = []
    data["retention"] = {"owner": "fixture-owner", "until": utc(END + timedelta(days=90)),
                         "evidence": list(refs)}
    data["cohort"].update(testers=["tester-one", "tester-two", "tester-three"],
                          evidence=list(refs))
    data["plan"] = {
        "declared_at": utc(START - timedelta(days=1)), "start_at": utc(START),
        "end_at": utc(END), "evidence": list(refs),
        "windows": [
            {"start_at": utc(START + timedelta(days=day, hours=8)),
             "end_at": utc(START + timedelta(days=day, hours=16))}
            for day in range(10)
        ],
    }
    data["daily"] = [
        {"date": (START + timedelta(days=day)).date().isoformat(),
         "at": utc(START + timedelta(days=day, hours=20)), "reviewer": "fixture-reviewer",
         "identity": dict(identity), "evidence": list(refs),
         "checks": {key: True for key in pilot.DAILY}, "operator_minutes": 60,
         "objects": 50000, "logical_bytes": 14213120000, "backup_age_hours": 24}
        for day in range(14)
    ]
    data["tasks"] = [
        {"id": f"task-{index}", "tester": data["cohort"]["testers"][index % 3],
         "tenant": "pilot-a" if index % 2 else "pilot-b",
         "workflow": pilot.WORKFLOWS[index % 4], "at": utc(START + timedelta(hours=9)),
         "completed": index < 27, "maintainer_intervention": False,
         "completion_seconds": 30, "identity": dict(identity), "evidence": list(refs)}
        for index in range(30)
    ]
    data["feedback"] = [
        {"tester": tester, "at": utc(END), "usefulness": "Fixture assessment",
         "failure_clarity": "Fixture errors were understandable", "evidence": list(refs)}
        for tester in data["cohort"]["testers"]
    ]
    # Exactly 80 hours at 10 requests/s and 99.5% success, with the declared mix.
    data["measurements"] = {
        "offered_attempts": 2880000, "expected_successes": 2865600, "scan_attempts": 1000,
        "operation_attempts": {"get": 576000, "head": 576000, "catalog": 576000,
                               "ask": 432000, "put": 432000, "delete": 144000,
                               "policy_preview": 144000},
        "size_bin_attempts": {
            "get": {"4096": 345600, "65536": 172800, "1048576": 51840, "16777216": 5760},
            "put": {"4096": 259200, "65536": 129600, "1048576": 38880, "16777216": 4320},
        }, "evidence": list(refs),
    }
    data["slo_reviews"] = {f"day-{day}": review(START + timedelta(days=day)) for day in (7, 14)}
    data["exit_gates"] = {key: review(END) for key in data["exit_gates"]}
    data["exit"] = {
        "decision": "expand", "by": "fixture-owner", "at": utc(END),
        "rationale": "Synthetic review scenario", "evidence": list(refs), "followups": [],
        "followup_review": list(refs), "roadmap_evidence": list(refs),
        "operator_docs_evidence": list(refs), "ticket_mirror_evidence": list(refs),
    }
    return tmp_path / "campaign.json", data


def inspect(campaign, stage="exit", *, now=NOW):
    path, data = campaign
    path.write_text(json.dumps(data))
    return pilot.inspect(path, stage=stage, now=now)


def assert_incomplete(report, check):
    assert report["status"] == "incomplete"
    assert report["checks"][check] is False


@pytest.mark.parametrize("stage", ["entry", "exit"])
def test_consistent_records_never_authorize_entry_or_expansion(campaign, stage):
    report = inspect(campaign, stage)
    assert report["status"] == "complete-for-review", report["checks"]
    assert report["entry_records_complete"] is True
    if stage == "exit":
        assert report["success_records_complete"] is True
    assert report["pilot_entry_authorized"] is False
    assert report["expansion_authorized"] is False
    assert report["resume_authorized"] is False
    assert report["production_qualified"] is False
    assert report["hosted_ci"] == (
        "skipped: user instruction; known GitHub billing/spending restriction"
    )
    assert report["campaign_sha256"] == hashlib.sha256(campaign[0].read_bytes()).hexdigest()
    output = json.dumps(report)
    assert "fixture-owner" not in output
    assert "synthetic-fixture-environment" not in output
    assert "tester-one" not in output


def test_template_is_incomplete_and_independent_calls_do_not_share_state(tmp_path):
    first, second = pilot.template(), pilot.template()
    first["qualifications"]["156"]["result"] = "passed"
    assert second["qualifications"]["156"]["result"] == "pending"
    for data in (second, json.loads((ROOT / "release/pilot/campaign.example.json").read_text())):
        report = inspect((tmp_path / "campaign.json", data))
        assert report["status"] == "incomplete"
        assert report["entry_records_complete"] is False


@pytest.mark.parametrize("ticket", ["156", "157", "158", "160", "161", "162", "163", "164", "165"])
def test_every_exact_candidate_qualification_is_required(campaign, ticket):
    del campaign[1]["qualifications"][ticket]
    assert_incomplete(inspect(campaign, "entry"), f"qualifications.{ticket}")


@pytest.mark.parametrize("result", ["skipped", "pending", "failed", "local-passed"])
def test_hosted_ci_suspension_cannot_be_inferred_as_qualification(campaign, result):
    campaign[1]["qualifications"]["158"]["result"] = result
    assert_incomplete(inspect(campaign, "entry"), "qualifications.158")


@pytest.mark.parametrize("group,key", [
    ("qualifications", "160"), ("readiness", "current_encryption_attestations"),
    ("exit_gates", "integrity_and_isolation"), ("slo_reviews", "day-14"),
])
def test_review_from_another_candidate_cannot_be_reused(campaign, group, key):
    campaign[1][group][key]["identity"]["source_commit"] = "c" * 40
    report = inspect(campaign)
    assert report["status"] == "incomplete"


def test_modified_evidence_invalidates_references_and_cannot_leak_values(campaign):
    path, data = campaign
    (path.parent / data["artifacts"]["observations"]["path"]).write_text("private-fixture-canary")
    report = inspect(campaign)
    assert_incomplete(report, "owner")
    assert "private-fixture-canary" not in json.dumps(report)


def test_identity_hash_must_bind_to_the_named_artifact(campaign):
    campaign[1]["identity_artifacts"]["configuration_sha256"] = "workload_sha256"
    assert_incomplete(inspect(campaign, "entry"), "identity_artifact.configuration_sha256")


@pytest.mark.parametrize("references", [[], ["missing"], ["observations", "observations"], "observations"])
def test_reviews_need_unique_resolving_evidence(campaign, references):
    campaign[1]["qualifications"]["162"]["evidence"] = references
    assert_incomplete(inspect(campaign, "entry"), "qualifications.162")


@pytest.mark.parametrize("kind", ["absolute", "parent", "symlink"])
def test_artifacts_must_remain_inside_the_campaign_directory(campaign, kind):
    path, data = campaign
    external = path.parent.parent / f"outside-{path.parent.name}.txt"
    external.write_text("Synthetic outside artifact\n")
    artifact = data["artifacts"]["observations"]
    artifact["sha256"] = hashlib.sha256(external.read_bytes()).hexdigest()
    if kind == "absolute":
        artifact["path"] = str(external)
    elif kind == "parent":
        artifact["path"] = "../" + external.name
    else:
        (path.parent / "linked.txt").symlink_to(external)
        artifact["path"] = "linked.txt"
    assert_incomplete(inspect(campaign, "entry"), "owner")


@pytest.mark.parametrize("mutation,check", [
    (lambda d: d["owner"].update(accepted_at=utc(START)), "owner"),
    (lambda d: d["entry"].update(by="other-owner"), "entry.go"),
    (lambda d: d.update(blockers=["Qualification unresolved"]), "blockers.none"),
    (lambda d: d["plan"].update(declared_at=utc(START)), "plan"),
    (lambda d: d["plan"].update(end_at=utc(END - timedelta(seconds=1))), "plan"),
    (lambda d: d["retention"].update(until=utc(END + timedelta(days=90, seconds=-1))), "retention"),
    (lambda d: d["qualifications"]["165"].update(at=utc(START)), "qualifications.165"),
    (lambda d: d["exit"].update(at=utc(NOW + timedelta(seconds=1))), "exit.owner_decision"),
    (lambda d: d["slo_reviews"]["day-7"].update(at=utc(START + timedelta(days=5))), "slo_review.day-7"),
])
def test_reviews_and_decisions_must_follow_the_recorded_chronology(campaign, mutation, check):
    mutation(campaign[1])
    assert_incomplete(inspect(campaign), check)


def test_future_campaign_cannot_supply_completed_exit_records(campaign):
    assert_incomplete(inspect(campaign, now=START - timedelta(hours=1)), "exit.owner_decision")


def test_retention_runs_for_ninety_days_after_the_actual_exit_decision(campaign):
    delayed_exit = END + timedelta(days=30)
    campaign[1]["exit"]["at"] = utc(delayed_exit)
    assert_incomplete(inspect(campaign, now=delayed_exit), "exit.retention")
    campaign[1]["retention"]["until"] = utc(delayed_exit + timedelta(days=90))
    assert inspect(campaign, now=delayed_exit)["status"] == "complete-for-review"


@pytest.mark.parametrize("mutation", [
    lambda d: d["plan"]["windows"].pop(),
    lambda d: d["plan"]["windows"].append(copy.deepcopy(d["plan"]["windows"][0])),
    lambda d: d["plan"]["windows"][0].update(end_at=utc(START + timedelta(hours=15))),
    lambda d: d["plan"]["windows"][1].update(
        start_at=utc(START + timedelta(hours=12)), end_at=utc(START + timedelta(hours=20))),
    lambda d: d["plan"]["windows"][0].update(
        start_at=utc(START - timedelta(hours=8)), end_at=utc(START)),
])
def test_staffed_windows_require_ten_distinct_nonoverlapping_eight_hour_shifts(campaign, mutation):
    mutation(campaign[1])
    assert_incomplete(inspect(campaign), "plan.staffed_windows")


@pytest.mark.parametrize("mutation", [
    lambda d: d["daily"].pop(),
    lambda d: d["daily"].append(copy.deepcopy(d["daily"][0])),
    lambda d: d["daily"][0].update(date="2026-07-31"),
])
def test_daily_coverage_includes_unstaffed_days_without_duplicates(campaign, mutation):
    mutation(campaign[1])
    assert_incomplete(inspect(campaign), "daily.coverage")


@pytest.mark.parametrize("field,value", [
    ("objects", 100001), ("objects", True), ("logical_bytes", 30 * 1024**3 + 1),
    ("backup_age_hours", 24.0001), ("backup_age_hours", -1),
    ("operator_minutes", 1441), ("operator_minutes", "60"),
    ("at", utc(START + timedelta(days=1))),
])
def test_daily_limits_and_observation_types_are_enforced(campaign, field, value):
    campaign[1]["daily"][0][field] = value
    assert_incomplete(inspect(campaign), "daily.0")


def test_daily_caps_at_the_exact_boundary_are_reportable(campaign):
    campaign[1]["daily"][0].update(objects=100000, logical_bytes=30 * 1024**3,
                                    operator_minutes=1440, backup_age_hours=24)
    assert inspect(campaign)["status"] == "complete-for-review"


def test_daily_review_cannot_be_recorded_after_the_exit_decision(campaign):
    data = campaign[1]
    midday_exit = END + timedelta(hours=12)
    data["plan"]["end_at"] = data["exit"]["at"] = utc(midday_exit)
    data["retention"]["until"] = utc(midday_exit + timedelta(days=90))
    for gate in data["exit_gates"].values():
        gate["at"] = utc(midday_exit)
    final_day = copy.deepcopy(data["daily"][-1])
    final_day.update(date=END.date().isoformat(), at=utc(END + timedelta(hours=20)))
    data["daily"].append(final_day)
    assert_incomplete(inspect(campaign), "daily.14")
    final_day["at"] = utc(midday_exit)
    assert inspect(campaign)["status"] == "complete-for-review"


@pytest.mark.parametrize("check_value", [False, "true", 1])
def test_daily_check_flags_are_explicit_booleans(campaign, check_value):
    campaign[1]["daily"][0]["checks"]["integrity"] = check_value
    assert_incomplete(inspect(campaign), "daily.0")


@pytest.mark.parametrize("mutation,check", [
    (lambda d: d["measurements"].update(offered_attempts=2879999), "measurements.offered"),
    (lambda d: d["measurements"].update(expected_successes=2865599), "measurements.successes"),
    (lambda d: d["measurements"].update(expected_successes=2880001), "measurements.successes"),
    (lambda d: d["measurements"].update(scan_attempts=999), "measurements.scans"),
    (lambda d: d["measurements"]["operation_attempts"].update(head=9999), "measurements.operations"),
    (lambda d: d["measurements"]["operation_attempts"].update(head=576001), "measurements.operations"),
    (lambda d: d["measurements"]["size_bin_attempts"]["get"].update({"16777216": 999}),
     "measurements.size_bins"),
    (lambda d: d["measurements"]["size_bin_attempts"]["get"].update({"4096": 345601}),
     "measurements.size_bins"),
])
def test_sample_minima_counts_and_exact_success_ratio(campaign, mutation, check):
    mutation(campaign[1])
    assert_incomplete(inspect(campaign), check)


def test_extending_staffed_time_also_extends_required_offered_load(campaign):
    campaign[1]["plan"]["windows"].append({
        "start_at": utc(START + timedelta(days=10, hours=8)),
        "end_at": utc(START + timedelta(days=10, hours=16)),
    })
    report = inspect(campaign)
    assert report["checks"]["plan.staffed_windows"] is True
    assert_incomplete(report, "measurements.offered")
    assert report["checks"]["measurements.successes"] is False


@pytest.mark.parametrize("mutation,check", [
    (lambda d: d["tasks"].pop(), "tasks.minimum"),
    (lambda d: d["tasks"][0].update(completed=False), "tasks.usefulness"),
    (lambda d: d["tasks"][0].update(maintainer_intervention=True), "tasks.usefulness"),
    (lambda d: d["tasks"][0].update(id=d["tasks"][1]["id"]), "tasks.unique"),
    (lambda d: d["tasks"][0].update(at=utc(START + timedelta(hours=16))), "task.0"),
    (lambda d: d["tasks"][0].update(tester="unenrolled"), "task.0"),
    (lambda d: d["tasks"][0].update(completed="true"), "task.0"),
    (lambda d: d["tasks"][0].update(completion_seconds=-1), "task.0"),
])
def test_user_task_gate_preserves_failures_and_maintainer_intervention(campaign, mutation, check):
    mutation(campaign[1])
    assert_incomplete(inspect(campaign), check)


def test_workflow_coverage_cannot_be_substituted_by_repeated_uploads(campaign):
    for task in campaign[1]["tasks"]:
        task["workflow"] = "upload-retrieve"
    assert_incomplete(inspect(campaign), "tasks.workflows")


def test_three_feedback_records_must_be_from_three_enrolled_testers(campaign):
    campaign[1]["feedback"][2]["tester"] = "tester-two"
    assert_incomplete(inspect(campaign), "feedback.distinct_testers")


@pytest.mark.parametrize("testers", ["abc", {"tester-one": 1, "tester-two": 2, "tester-three": 3}])
def test_cohort_roster_requires_a_list_not_an_iterable(campaign, testers):
    campaign[1]["cohort"]["testers"] = testers
    assert_incomplete(inspect(campaign, "entry"), "cohort")


@pytest.mark.parametrize("group,check", [("tasks", "task.0"), ("feedback", "feedback.0")])
def test_unhashable_tester_identifiers_fail_without_crashing(campaign, group, check):
    campaign[1][group][0]["tester"] = ["tester-one"]
    campaign[1]["cohort"]["testers"].append(["tester-one"])
    assert_incomplete(inspect(campaign), check)


@pytest.mark.parametrize("field", ["followup_review", "roadmap_evidence", "operator_docs_evidence",
                                   "ticket_mirror_evidence"])
def test_exit_documentation_and_followup_review_are_required(campaign, field):
    campaign[1]["exit"][field] = []
    assert_incomplete(inspect(campaign), f"exit.{field}")


def test_failed_or_stopped_pilot_cannot_become_success_by_recording_a_decision(campaign):
    campaign[1]["exit"]["decision"] = "stop"
    campaign[1]["exit_gates"]["integrity_and_isolation"]["result"] = "failed"
    report = inspect(campaign)
    assert report["checks"]["exit.owner_decision"] is True
    assert_incomplete(report, "exit.gate.integrity_and_isolation")


@pytest.mark.parametrize("content", [
    "", "null", "[]", "true", '{"schema_version":1,"schema_version":1}',
    '{"data":{"duplicate":1,"duplicate":2}}', '{"value":NaN}', '{"value":Infinity}',
    '{"value":-Infinity}', '{"broken":',
])
def test_malformed_or_ambiguous_json_fails_closed(tmp_path, content):
    path = tmp_path / "campaign.json"
    path.write_text(content)
    assert_incomplete(pilot.inspect(path, "exit", now=NOW), "input")


@pytest.mark.parametrize("field,value", [
    ("identity", None), ("artifacts", "malformed"), ("owner", False),
    ("qualifications", None), ("readiness", "malformed"), ("cohort", False),
    ("plan", None), ("daily", False), ("measurements", "malformed"),
    ("tasks", None), ("feedback", "malformed"), ("slo_reviews", False),
    ("exit_gates", None), ("exit", "malformed"),
])
def test_wrong_record_shapes_are_incomplete_without_crashing(campaign, field, value):
    campaign[1][field] = value
    assert inspect(campaign)["status"] == "incomplete"


@pytest.mark.parametrize("value", [True, "2880000", -1, 10**400, float("nan"), float("inf")])
def test_counts_reject_coercion_nonfinite_and_unbounded_values(campaign, value):
    campaign[1]["measurements"]["offered_attempts"] = value
    assert inspect(campaign)["status"] == "incomplete"


def test_cli_initializes_without_overwriting_existing_observations(tmp_path, monkeypatch):
    output = tmp_path / "new" / "campaign.json"
    monkeypatch.setattr(sys, "argv", ["pilot_review.py", "init", "--output", str(output)])
    assert pilot.main() == 0
    original = output.read_bytes()
    with pytest.raises(SystemExit) as error:
        pilot.main()
    assert error.value.code == 2
    assert output.read_bytes() == original


def test_cli_report_does_not_replace_campaign_or_previous_report(campaign, monkeypatch):
    path, _ = campaign
    inspect(campaign)
    original = path.read_bytes()
    monkeypatch.setattr(sys, "argv", ["pilot_review.py", "check", "--campaign", str(path),
                                     "--output", str(path)])
    with pytest.raises(SystemExit) as error:
        pilot.main()
    assert error.value.code == 2
    assert path.read_bytes() == original
    output = path.parent / "report.json"
    output.write_text("Existing observations\n")
    monkeypatch.setattr(sys, "argv", ["pilot_review.py", "check", "--campaign", str(path),
                                     "--output", str(output)])
    with pytest.raises(SystemExit) as error:
        pilot.main()
    assert error.value.code == 2
    assert output.read_text() == "Existing observations\n"


def test_cli_writes_an_incomplete_report_and_nonzero_exit_status(tmp_path, monkeypatch):
    path, output = tmp_path / "campaign.json", tmp_path / "report.json"
    path.write_text(json.dumps(pilot.template()))
    monkeypatch.setattr(sys, "argv", ["pilot_review.py", "check", "--campaign", str(path),
                                     "--stage", "exit", "--output", str(output)])
    assert pilot.main() == 1
    report = json.loads(output.read_text())
    assert report["status"] == "incomplete"
    assert report["pilot_entry_authorized"] is False
