"""Offline acceptance evidence must preserve missing gates and real case scope."""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("manual_acceptance", ROOT / "scripts/manual_acceptance.py")
assert SPEC and SPEC.loader
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


@pytest.fixture
def seeded(tmp_path):
    directory = tmp_path / "corpus"
    acceptance.corpus(directory)
    return directory


@pytest.fixture
def record(seeded, tmp_path):
    args = argparse.Namespace(
        corpus=seeded, tester="Synthetic test operator", source_sha="a" * 40,
        candidate_id="local-fixture", configuration_id="fixture-config",
        environment_id="isolated-test", environment_kind="local", image_digest=None,
    )
    path = tmp_path / "record.json"
    acceptance.write_json(path, acceptance.template(args))
    return path


def edit(path, mutation):
    data = acceptance.read_json(path)
    mutation(data)
    path.write_text(json.dumps(data))


def artifact(path, name="sanitized.log", kind="log", durable=True):
    target = path.parent / name
    target.write_text("Synthetic sanitized observation; no credential material.\n")
    if kind == "screenshot":
        target.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a"
            "3ioAAAAASUVORK5CYII="))
    return {"path": name, "sha256": acceptance.digest(target), "kind": kind, "sanitized": True,
            "durable_uri": "https://artifacts.example.invalid/run/retained" if durable else None}


def pass_case(case, evidence):
    case.update(status="passed", inputs=["synthetic corpus manifest + exact object keys"],
                actual="Observed the complete expected result on synthetic fixtures.",
                executed_at="2026-09-21T12:00:00+00:00", evidence=deepcopy(evidence))
    if case["surface"] == "web":
        case["browser"] = {"name": "Fixture Browser", "version": "1.0",
                           "interaction": "actual-browser"}
    case["additional_attempts"] = [
        {key: deepcopy(case[key]) for key in
         ("status", "inputs", "actual", "executed_at", "evidence", "browser")}
        for _ in range(case["minimum_repetitions"] - 1)
    ]
    for index, attempt in enumerate(case["additional_attempts"], 1):
        attempt["executed_at"] = f"2026-09-21T12:0{index}:00+00:00"


@pytest.fixture
def complete(record):
    evidence = [artifact(record), artifact(record, "screenshot.png", "screenshot")]
    manifest = record.parent / "corpus/manifest.json"
    corpus_evidence = {**artifact(record, "manifest-copy.json", "review"),
                       "sha256": acceptance.digest(manifest)}
    (record.parent / "manifest-copy.json").write_bytes(manifest.read_bytes())

    def fill(data):
        data["corpus"]["evidence"] = [corpus_evidence]
        data["bindings"].update(
            environment_kind="staging", source_state="clean", candidate_manifest_sha256="b" * 64,
            configuration_sha256="c" * 64, registry_manifest_digest="sha256:" + "d" * 64,
        )
        data["scope"].update(
            specification_accepted=True, specification_evidence=[evidence[0]],
            staging_accepted=True, staging_evidence=[evidence[0]],
            production_providers_required=False,
        )
        for case in data["cases"]:
            pass_case(case, evidence)

    edit(record, fill)
    return record


def test_corpus_is_deterministic_hashed_and_synthetic(seeded, tmp_path):
    second = tmp_path / "second"
    acceptance.corpus(second)
    manifest = acceptance.verify_corpus(seeded)
    assert (seeded / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()
    assert manifest["synthetic"] is True
    assert (seeded / "alpha.txt").read_bytes() == (seeded / "alpha-copy.txt").read_bytes()
    assert (seeded / "sample.docx").read_bytes() == (ROOT / "tests/fixtures/documents/sample.docx").read_bytes()
    assert (seeded / "at-limit.pdf").stat().st_size == manifest["limit_bytes"]
    assert (seeded / "over-limit.pdf").stat().st_size == manifest["limit_bytes"] + 1
    assert "example.invalid" in PdfReader(seeded / "pii.pdf").pages[0].extract_text()
    assert PdfReader(seeded / "image-only.pdf").pages[0].extract_text() == ""
    for name in ("sample.pdf", "at-limit.pdf", "over-limit.pdf"):
        assert "CogniStore Document Fixture" in PdfReader(seeded / name).pages[0].extract_text()


@pytest.mark.parametrize("change", ["changed", "extra", "missing", "duplicate", "symlink"])
def test_corpus_rejects_changed_missing_unrecorded_or_linked_bytes(seeded, tmp_path, change):
    if change == "changed":
        (seeded / "alpha.txt").write_text("changed")
    elif change == "extra":
        (seeded / "extra.txt").write_text("unrecorded")
    elif change == "missing":
        (seeded / "sample.pdf").unlink()
    elif change == "duplicate":
        edit(seeded / "manifest.json", lambda d: d["objects"].append(d["objects"][0]))
    else:
        outside = tmp_path / "outside.txt"
        outside.write_bytes((seeded / "alpha.txt").read_bytes())
        (seeded / "alpha.txt").unlink()
        (seeded / "alpha.txt").symlink_to(outside)
    with pytest.raises(ValueError):
        acceptance.verify_corpus(seeded)


def test_corpus_refuses_overwrite_and_unbounded_sizes(seeded, tmp_path):
    with pytest.raises(FileExistsError):
        acceptance.corpus(seeded)
    for size in (0, -1, 64 * 1024 * 1024 + 1):
        with pytest.raises(ValueError):
            acceptance.corpus(tmp_path / "bad", size)
    assert not (tmp_path / "bad").exists()


def test_unexecuted_record_is_valid_but_cannot_claim_acceptance(record):
    summary = acceptance.validate(record)
    assert summary["record_valid"] is True, summary["issues"]
    assert summary["manual_acceptance_complete"] is False
    assert summary["production_qualified"] is summary["release_qualified"] is False
    assert summary["counts"]["not_run"] == len(acceptance.matrix_cases())
    assert "staging_execution_missing" in summary["blockers"]
    assert acceptance.main(["validate", str(record)]) == 0
    assert acceptance.main(["validate", str(record), "--require-complete"]) == 1


def test_partial_browser_observation_is_retained_without_passing_case(record):
    evidence = artifact(record, durable=False)

    def partial(data):
        case = next(c for c in data["cases"] if c["surface"] == "web")
        case.update(status="blocked", actual="Local browser empty state observed; staging unavailable.",
                    evidence=[evidence])

    edit(record, partial)
    summary = acceptance.validate(record)
    assert summary["record_valid"] is True
    assert summary["manual_acceptance_complete"] is False
    assert summary["counts"]["blocked"] == 1


def test_complete_metadata_only_record_does_not_certify_release_or_models(complete):
    summary = acceptance.validate(complete)
    assert summary["issues"] == summary["blockers"] == []
    assert summary["manual_acceptance_complete"] is True
    assert summary["production_qualified"] is summary["release_qualified"] is False


@pytest.mark.parametrize("mutation,issue", [
    (lambda d: d["cases"].pop(), "cases.coverage"),
    (lambda d: d["cases"].append(d["cases"][0]), "cases.coverage"),
    (lambda d: d["cases"][0].update(expected="easier result"), ".definition"),
    (lambda d: d["cases"][0].update(role="admin"), ".definition"),
    (lambda d: d["cases"][0].update(tenant="pilot-b"), ".definition"),
    (lambda d: d["cases"][0].update(actual=""), ".actual"),
    (lambda d: d["cases"][0].update(inputs=[]), ".inputs"),
    (lambda d: d["cases"][0].update(executed_at=None), ".executed_at"),
    (lambda d: d["cases"][0].update(evidence=[]), ".evidence.missing"),
    (lambda d: d["cases"][0].update(additional_attempts=[]), ".minimum_repetitions"),
    (lambda d: d["cases"][0]["additional_attempts"][0].update(status="failed"), ".minimum_repetitions"),
    (lambda d: d["cases"][0]["additional_attempts"][0].update(evidence=[]), ".evidence.missing"),
    (lambda d: d.update(matrix_sha256="e" * 64), "record.matrix_binding"),
    (lambda d: d.update(production_qualified=True), "record.qualification_claim"),
    (lambda d: d["scope"].update(specification_evidence=[]), "scope.specification_evidence.missing"),
])
def test_pass_cannot_hide_missing_cases_identity_changes_or_missing_evidence(complete, mutation, issue):
    edit(complete, mutation)
    summary = acceptance.validate(complete)
    assert summary["record_valid"] is False
    assert any(value.endswith(issue) for value in summary["issues"]), summary["issues"]
    assert summary["manual_acceptance_complete"] is False


@pytest.mark.parametrize("change", ["html-only", "no-browser", "headless-shell", "no-version"])
def test_browser_pass_requires_actual_interaction_and_screenshot_plus_log(complete, change):
    def mutate(data):
        case = next(c for c in data["cases"] if c["surface"] == "web")
        if change == "html-only":
            case["evidence"] = [e for e in case["evidence"] if e["kind"] == "log"]
        elif change == "no-browser":
            case["browser"] = None
        elif change == "headless-shell":
            case["browser"]["interaction"] = "html-response"
        else:
            case["browser"]["version"] = ""
    edit(complete, mutate)
    result = acceptance.validate(complete)
    assert result["record_valid"] is False
    assert any(i.endswith(".actual_browser") for i in result["issues"])


@pytest.mark.parametrize("change", ["edit", "unsanitized", "traversal", "absolute", "symlink"])
def test_evidence_checks_bytes_sanitization_and_local_containment(complete, tmp_path, change):
    if change == "edit":
        (complete.parent / "sanitized.log").write_text("changed evidence")
    elif change == "symlink":
        source = complete.parent / "sanitized.log"
        destination = tmp_path / "outside"
        source.rename(destination)
        source.symlink_to(destination)
    else:
        def mutate(data):
            evidence = data["cases"][0]["evidence"][0]
            if change == "unsanitized":
                evidence["sanitized"] = False
            else:
                evidence["path"] = "../sanitized.log" if change == "traversal" else "/etc/passwd"
        edit(complete, mutate)
    summary = acceptance.validate(complete)
    assert summary["record_valid"] is False
    assert any(i.endswith(".evidence.invalid") for i in summary["issues"])


@pytest.mark.parametrize("mutation,blocker", [
    (lambda d: d["bindings"].update(environment_kind="local"), "staging_execution_missing"),
    (lambda d: d["bindings"].update(source_state="dirty"), "clean_candidate_source_missing"),
    (lambda d: d["bindings"].update(registry_manifest_digest=None), "bindings.registry_manifest_digest.missing"),
    (lambda d: d["scope"].update(staging_accepted=False), "scope.staging_acceptance_missing"),
    (lambda d: d["scope"].update(production_providers_required=None), "scope.production_providers_unresolved"),
    (lambda d: d["cases"][0]["evidence"][0].update(durable_uri=None), ".durable_retention_missing"),
    (lambda d: d["cases"][0].update(status="blocked"), ".not_passed"),
])
def test_valid_partial_records_cannot_pass_complete_gate(complete, mutation, blocker):
    edit(complete, mutation)
    summary = acceptance.validate(complete)
    assert summary["record_valid"] is True, summary["issues"]
    assert summary["manual_acceptance_complete"] is False
    assert any(value.endswith(blocker) for value in summary["blockers"])


def test_enabling_production_providers_requires_new_quality_matrix(complete):
    edit(complete, lambda d: d["scope"].update(
        production_providers_required=True, provider_evidence=d["cases"][0]["evidence"]))
    summary = acceptance.validate(complete)
    assert summary["record_valid"] is True
    assert summary["manual_acceptance_complete"] is False
    assert "scope.production_provider_matrix_and_quality_evidence_required" in summary["blockers"]


@pytest.mark.parametrize("state", ["open", "fixed-no-rerun", "fixed-rerun", "one-later-attempt"])
def test_release_blocker_requires_fix_and_later_affected_rerun(complete, state):
    def finding(data):
        data["findings"] = [{"id": "finding-1", "severity": "release-blocking",
                             "status": "open" if state == "open" else "fixed",
                             "reproduction": "Synthetic reproducible fixture condition.",
                             "issue_reference": "https://example.invalid/issues/1",
                             "fix_reference": "fixture fix commit",
                             "fix_source_commit": "a" * 40,
                             "affected_cases": [data["cases"][0]["id"]],
                             "fixed_at": "2026-09-21T11:59:00+00:00" if state in {
                                 "fixed-rerun", "one-later-attempt"}
                             else "2026-09-21T12:01:00+00:00",
                             "evidence": data["cases"][0]["evidence"]}]
        if state == "one-later-attempt":
            for index, attempt in enumerate(data["cases"][0]["additional_attempts"]):
                attempt["executed_at"] = f"2026-09-21T10:0{index}:00+00:00"
    edit(complete, finding)
    summary = acceptance.validate(complete)
    assert summary["record_valid"] is True
    assert summary["manual_acceptance_complete"] is (state == "fixed-rerun")


def test_fixed_release_finding_needs_source_revision(complete):
    edit(complete, lambda data: data["findings"].append({
        "id": "finding-1", "severity": "release-blocking", "status": "fixed",
        "affected_cases": [data["cases"][0]["id"]], "reproduction": "Synthetic reproduction",
        "issue_reference": "issue-1", "fix_reference": "fix-1", "fix_source_commit": "unknown",
        "fixed_at": "2026-09-21T11:00:00+00:00", "evidence": data["cases"][0]["evidence"],
    }))
    summary = acceptance.validate(complete)
    assert summary["record_valid"] is False
    assert "findings.fix_source_commit" in summary["issues"]


def test_html_renamed_as_screenshot_cannot_pass_browser_evidence(complete):
    screenshot = complete.parent / "screenshot.png"
    screenshot.write_text("<html>Only an HTML shell response</html>")
    replacement = acceptance.digest(screenshot)

    def replace_hash(data):
        for case in data["cases"]:
            for attempt in [case, *case["additional_attempts"]]:
                for item in attempt["evidence"]:
                    if item["kind"] == "screenshot":
                        item["sha256"] = replacement

    edit(complete, replace_hash)
    assert acceptance.validate(complete)["record_valid"] is False


@pytest.mark.parametrize("content", ["null", "[]", "{}", '{"kind":1,"kind":2}', "invalid JSON"])
def test_malformed_records_fail_closed_without_echoing_input(record, content, capsys):
    record.write_text(content)
    assert acceptance.main(["validate", str(record)]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["record_valid"] is False
    assert summary["manual_acceptance_complete"] is False


def test_summary_never_overwrites_evidence(record):
    original = record.read_bytes()
    assert acceptance.main(["validate", str(record), "--output", str(record)]) == 2
    assert record.read_bytes() == original
