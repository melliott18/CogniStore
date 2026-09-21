"""Audit evidence must not turn missing, stale or skipped checks into a pass."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def audit():
    path = Path(__file__).resolve().parents[2] / "scripts/system_audit.py"
    spec = importlib.util.spec_from_file_location("system_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def junit(path, cases):
    path.write_text('<testsuites><testsuite>' + cases + '</testsuite></testsuites>')


@pytest.mark.parametrize(("cases", "exit_code", "status"), [
    ('<testcase classname="tests.unit.test_one" name="positive"/>', 0, "passed"),
    ('<testcase classname="tests.unit.test_one" name="x"><skipped/></testcase>', 0, "incomplete"),
    ('<testcase classname="tests.unit.test_one" name="x"><failure/></testcase>', 0, "failed"),
    ('<testcase classname="tests.unit.test_one" name="x"><error/></testcase>', 0, "failed"),
    ('<testcase classname="tests.unit.test_one" name="x"/>', 1, "failed"),
    ('<testcase classname="tests.unit.test_other" name="x"/>', 0, "failed"),
    ("", 0, "incomplete"),
])
def test_results_fail_closed(audit, tmp_path, cases, exit_code, status):
    report = tmp_path / "tests.xml"
    junit(report, cases)
    result = audit.summarize(report, ["tests/unit/test_one.py"], exit_code)
    assert result["status"] == status
    assert result["tests"] == sum(result[k] for k in ("passed", "failures", "errors", "skipped"))


def test_missing_file_and_nested_suites_are_not_false_pass(audit, tmp_path):
    path = tmp_path / "tests.xml"
    assert audit.summarize(path, ["tests/unit/test_one.py"], 0)["status"] == "failed"
    path.write_text('<testsuites tests="100"><testsuite tests="100"><testsuite>'
                    '<testcase classname="tests.unit.test_one.TestClass" name="x"/>'
                    '</testsuite></testsuite></testsuites>')
    result = audit.summarize(path, ["tests/unit/test_one.py", "tests/unit/test_two.py"], 0)
    assert result["tests"] == 1
    assert result["status"] == "incomplete"
    assert result["missing_test_files"] == ["tests/unit/test_two.py"]


def test_repository_matrix_has_every_review_and_staging_boundary(audit):
    matrix = audit.load_matrix(audit.ROOT)
    assert len(matrix["boundaries"]) == 14


@pytest.fixture
def repo(audit, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    (root / "source.py").write_text("initial\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-qm", "fixture"], cwd=root, check=True)
    matrix = {"scope": "m5-pilot-v1", "revision": 1, "boundaries": [
        {"id": "test", "tests": {"local": ["tests/unit/test_one.py"]}},
    ]}
    monkeypatch.setattr(audit, "load_matrix", lambda root: matrix)
    return root


def fake_pytest(monkeypatch, audit, action=None):
    original = subprocess.run

    def run(command, **kwargs):
        if "pytest" not in command:
            return original(command, **kwargs)
        report = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")))
        junit(report, '<testcase classname="tests.unit.test_one" name="positive"/>')
        if action:
            action()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(audit.subprocess, "run", run)


def test_run_binds_inputs_and_never_grants_signoff(audit, repo, tmp_path, monkeypatch):
    fake_pytest(monkeypatch, audit)
    output = tmp_path / "evidence"
    assert audit.run(repo, output, "local") == 0
    report = json.loads((output / "report.json").read_text())
    assert report["production_signoff"] is False
    assert report["result"]["passed"] == 1
    assert report["source"]["files"]["source.py"] == audit.digest(repo / "source.py")
    audit.verify(repo, output)
    (repo / "source.py").write_text("changed after run\n")
    with pytest.raises(ValueError, match="re-audit"):
        audit.verify(repo, output)


def test_changes_during_run_invalidate_evidence(audit, repo, tmp_path, monkeypatch):
    fake_pytest(monkeypatch, audit, lambda: (repo / "source.py").write_text("concurrent edit\n"))
    output = tmp_path / "evidence"
    assert audit.run(repo, output, "local") == 1
    report = json.loads((output / "report.json").read_text())
    assert report["result"]["status"] == "invalidated"


@pytest.mark.parametrize("change", ["log", "remove", "extra", "signoff", "scope", "totals",
                                   "head", "schema", "runtime", "command", "interval"])
def test_verify_rejects_changed_artifacts_and_claims(audit, repo, tmp_path, monkeypatch, change):
    fake_pytest(monkeypatch, audit)
    output = tmp_path / "evidence"
    audit.run(repo, output, "local")
    path = output / "report.json"
    report = json.loads(path.read_text())
    if change == "log":
        (output / "pytest.log").write_text("changed")
    elif change == "remove":
        (output / "tests.xml").unlink()
    elif change == "extra":
        (output / "extra").write_text("unrecorded")
    elif change == "signoff":
        report["production_signoff"] = True
    elif change == "scope":
        report["boundaries"] = {}
    elif change == "head":
        report["source"]["head"] = "not-a-commit"
    elif change == "schema":
        report["schema_version"] = 999
    elif change == "runtime":
        report["runtime"]["python"] = ""
    elif change == "command":
        report["command"] = []
    elif change == "interval":
        report["started_at"] = "2999-01-01T00:00:00+00:00"
    else:
        report["result"]["passed"] = 999
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        audit.verify(repo, output)


def test_raw_output_cannot_enter_checkout_or_overwrite(audit, repo, tmp_path, monkeypatch):
    fake_pytest(monkeypatch, audit)
    with pytest.raises(ValueError, match="outside"):
        audit.run(repo, repo / "raw", "local")
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(FileExistsError):
        audit.run(repo, output, "local")


def test_ambient_pytest_selection_cannot_hide_failing_case(audit, repo, tmp_path, monkeypatch):
    test_dir = repo / "tests/unit"
    test_dir.mkdir(parents=True)
    (test_dir / "test_one.py").write_text(
        "def test_selected():\n    assert True\n\n"
        "def test_required():\n    assert False, 'required negative control'\n"
    )
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k selected")
    monkeypatch.setenv("PYTEST_PLUGINS", "nonexistent_ambient_plugin")
    output = tmp_path / "selection"
    assert audit.run(repo, output, "local") == 1
    report = json.loads((output / "report.json").read_text())
    assert report["result"]["status"] == "failed"
    assert report["result"]["tests"] == 2
    assert report["result"]["passed"] == report["result"]["failures"] == 1


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '[]'])
def test_ambiguous_json_is_rejected(audit, tmp_path, raw):
    path = tmp_path / "report.json"
    path.write_text(raw)
    with pytest.raises(ValueError):
        audit.read_json(path)
