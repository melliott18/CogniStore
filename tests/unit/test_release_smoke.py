"""Keep installed-artifact evidence strict and its failures safe to publish."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import cognistore
from scripts import release_smoke


@pytest.mark.parametrize("case", ["editable", "source_import", "source_cwd"])
def test_installed_identity_rejects_checkout_evidence(tmp_path, monkeypatch, case):
    installed = tmp_path / "site-packages"
    installed.mkdir()
    source = tmp_path / "checkout"
    source.mkdir()
    package = installed / "cognistore" / "__init__.py"
    package.parent.mkdir()
    package.write_text("# synthetic installation\n")
    distribution = SimpleNamespace(
        files=[Path("cognistore/__init__.py")],
        locate_file=lambda path: installed / path,
        read_text=lambda name: json.dumps({"dir_info": {"editable": case == "editable"}}),
        metadata={"Name": "cognistore"}, version="test",
    )
    monkeypatch.setattr(release_smoke.importlib.metadata, "distribution", lambda name: distribution)
    monkeypatch.setattr(cognistore, "__file__", str(
        source / "cognistore" / "__init__.py" if case == "source_import" else package
    ))
    monkeypatch.chdir(source if case == "source_cwd" else tmp_path)
    checks = release_smoke.Checks()
    with pytest.raises(release_smoke.SmokeFailure):
        release_smoke.installed_identity(checks, source)
    assert checks.results[-1]["passed"] is False
    assert checks.results[-1]["name"] == {
        "editable": "package.noneditable",
        "source_import": "package.import_matches_distribution",
        "source_cwd": "cwd.outside_source_root",
    }[case]


def test_failure_report_never_serializes_exception_or_environment(tmp_path, monkeypatch, capsys):
    secret = "must-never-appear-in-public-evidence"
    monkeypatch.setattr(release_smoke.os, "environ", {"COGNISTORE_TEST_SECRET": secret})
    monkeypatch.setattr(release_smoke.logging, "disable", lambda level: None)

    def failed_identity(checks, source_root):
        checks.current = "package.import"
        raise RuntimeError(f"private endpoint credential={secret}")

    monkeypatch.setattr(release_smoke, "installed_identity", failed_identity)
    output = tmp_path / "report.json"
    assert release_smoke.main(["--output", str(output)]) == 1
    serialized = output.read_text()
    report = json.loads(serialized)
    assert report["status"] == "failed"
    assert report["failure"] == {"check": "package.import", "exception_type": "RuntimeError"}
    assert report["production_qualified"] is False
    assert secret not in serialized + capsys.readouterr().out
    assert report["http_request_count"] == 0


def test_failed_checks_raise_without_python_asserts():
    checks = release_smoke.Checks()
    with pytest.raises(release_smoke.SmokeFailure, match="artifact.rejected"):
        checks.require(False, "artifact.rejected")
    assert checks.results == [{"name": "artifact.rejected", "passed": False}]
