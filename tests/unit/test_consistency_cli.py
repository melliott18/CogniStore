from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

from cognistore.cli import cognistore_cli, consistency_commands
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in tuple(os.environ):
        if variable.startswith("COGNISTORE_"):
            monkeypatch.delenv(variable)
    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "development")


@pytest.fixture
def source(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "catalog": tmp_path / "catalog.sqlite3",
        "drivers": tmp_path / "drivers.yaml",
        "scope": tmp_path / "tenants.yaml",
        "report": tmp_path / "report.sqlite3",
        "hot": tmp_path / "hot",
    }
    paths["drivers"].write_text(
        f"tiers:\n  hot:\n    driver: posix\n    path: {paths['hot']}\n", encoding="utf-8",
    )
    paths["scope"].write_text(
        "tenants:\n  acme:\n    bucket: demo\n    prefix: acme/\n    tiers: [hot]\n"
        "  other:\n    bucket: demo\n    prefix: other/\n    tiers: [hot]\n",
        encoding="utf-8",
    )
    driver = PosixDriver(paths["hot"])
    catalog = SQLiteCatalog(paths["catalog"])
    try:
        for key in ("acme/one", "other/private"):
            data = key.encode()
            driver.put_object("demo", key, data)
            catalog.upsert("demo", key, len(data), tier="hot", metadata={
                "checksum": hashlib.sha256(data).hexdigest(),
            })
    finally:
        catalog.close()
    return paths


def _args(source: dict[str, Path], command: str = "consistency-scan") -> list[str]:
    arguments = [
        "--no-config", "--catalog-db", str(source["catalog"]),
        "--drivers", str(source["drivers"]), command,
        "--scope-config", str(source["scope"]), "--tenant", "acme",
        "--report", str(source["report"]), "--json",
    ]
    if command == "consistency-scan":
        arguments += ["--requests-per-second", "1000000", "--bytes-per-second", "1000000000"]
    else:
        arguments += ["--output", str(source["report"].with_suffix(".jsonl"))]
    return arguments


def _output(capsys: pytest.CaptureFixture[str]) -> dict:
    output = capsys.readouterr()
    assert output.err == ""
    return json.loads(output.out)


def _snapshot(path: Path) -> dict[str, tuple[bytes, int]]:
    files = sorted(path.rglob("*")) if path.is_dir() else [path]
    return {str(file): (file.read_bytes(), file.stat().st_mtime_ns) for file in files if file.is_file()}


def test_scan_and_export_are_scoped_and_source_read_only(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = {name: _snapshot(source[name]) for name in ("catalog", "hot", "scope", "drivers")}
    # The ordinary CLI initialization would add tiers/initialize mutable state.
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda *_: pytest.fail("mutable CLI path"))
    assert cognistore_cli.main(_args(source)) == 0
    scan = _output(capsys)["summary"]
    assert scan["complete"] is True
    assert scan["scope"]["tenant_id"] == "acme"
    assert scan["scope"]["prefix"] == "acme/"
    assert scan["inventory_keys"] == 1
    assert cognistore_cli.main(_args(source, "consistency-export")) == 0
    _output(capsys)
    export = source["report"].with_suffix(".jsonl").read_text()
    assert "other/private" not in export
    rows = [json.loads(line) for line in export.splitlines()]
    assert rows[0]["type"] == "report"
    assert any(row["type"] == "audit" for row in rows)
    assert {name: _snapshot(source[name]) for name in before} == before


def test_preview_scan_and_resume_leave_no_persistent_writes(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    before = _snapshot(source["catalog"].parent)
    assert cognistore_cli.main(_args(source) + ["--dry-run"]) == 0
    assert _output(capsys)["status"] == "planned"
    assert _snapshot(source["catalog"].parent) == before
    assert cognistore_cli.main(_args(source) + ["--max-items", "1"]) == 0
    assert _output(capsys)["summary"]["complete"] is False
    before = _snapshot(source["catalog"].parent)
    assert cognistore_cli.main(_args(source) + ["--resume", "--dry-run"]) == 0
    assert _output(capsys)["summary"]["complete"] is True
    assert _snapshot(source["catalog"].parent) == before
    assert cognistore_cli.main(_args(source) + ["--resume"]) == 0
    assert _output(capsys)["summary"]["complete"] is True


def test_export_preview_does_not_write_output_or_audit(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    assert cognistore_cli.main(_args(source)) == 0
    _output(capsys)
    before = _snapshot(source["catalog"].parent)
    assert cognistore_cli.main(_args(source, "consistency-export") + ["--dry-run"]) == 0
    assert _output(capsys)["status"] == "planned"
    assert _snapshot(source["catalog"].parent) == before


@pytest.mark.parametrize("extra", [
    ["--tenant", "unknown"], ["--prefix", "other/"], ["--tier", "cold"],
])
def test_scope_widening_rejected_before_source_access(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch, extra: list[str],
) -> None:
    monkeypatch.setattr(consistency_commands, "open_catalog", lambda *a, **kw: pytest.fail("source access"))
    assert cognistore_cli.main(_args(source) + extra) == 1
    assert _output(capsys)["status"] == "error"
    assert not source["report"].exists()


@pytest.mark.parametrize("target", ["catalog", "catalog-wal", "scope", "drivers", "hot-new", "symlink", "hardlink"])
def test_report_rejects_protected_paths(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str], target: str,
) -> None:
    if target == "catalog-wal":
        report = Path(str(source["catalog"]) + "-wal")
    elif target == "hot-new":
        report = source["hot"] / "new-report"
    elif target in {"symlink", "hardlink"}:
        report = source["catalog"].with_name(target)
        if target == "symlink":
            report.symlink_to(source["catalog"])
        else:
            report.hardlink_to(source["catalog"])
    else:
        report = source[target]
    before = _snapshot(source["catalog"].parent)
    assert cognistore_cli.main(_args(source) + ["--report", str(report)]) == 1
    assert _output(capsys)["status"] == "error"
    assert _snapshot(source["catalog"].parent) == before


def test_resume_and_export_reject_changed_binding(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    assert cognistore_cli.main(_args(source) + ["--max-items", "1"]) == 0
    _output(capsys)
    original_report = _snapshot(source["report"])
    source["scope"].write_text(source["scope"].read_text().replace("prefix: acme/", "prefix: acme/o"))
    for arguments in (_args(source) + ["--resume"], _args(source, "consistency-export")):
        assert cognistore_cli.main(arguments) == 1
        assert "binding" in _output(capsys)["error"]
    assert _snapshot(source["report"]) == original_report
    assert not source["report"].with_suffix(".jsonl").exists()


def test_export_refuses_existing_files_and_storage_roots(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    assert cognistore_cli.main(_args(source)) == 0
    _output(capsys)
    before = _snapshot(source["catalog"].parent)
    for target in (source["scope"], source["report"], source["hot"] / "new.jsonl"):
        assert cognistore_cli.main(_args(source, "consistency-export") + ["--output", str(target)]) == 1
        assert _output(capsys)["status"] == "error"
    assert _snapshot(source["catalog"].parent) == before


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_invalid_rate_is_a_usage_error(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str], value: str,
) -> None:
    with pytest.raises(SystemExit) as error:
        cognistore_cli.main(_args(source) + ["--requests-per-second", value])
    assert error.value.code == 2
    assert _output(capsys)["error_type"] == "UsageError"
    assert not source["report"].exists()


def test_duplicate_scope_keys_are_rejected(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    source["scope"].write_text("tenants: {}\ntenants: {}\n")
    assert cognistore_cli.main(_args(source)) == 1
    assert "duplicate" in _output(capsys)["error"]
    assert not source["report"].exists()


def test_nested_bucket_binding_is_rejected_even_when_another_tenant_is_selected(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source["scope"].write_text(
        "tenants:\n  acme:\n    bucket: shared\n    prefix: other/\n    tiers: [hot]\n"
        "  other:\n    bucket: shared/other\n    prefix: ''\n    tiers: [hot]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(consistency_commands, "open_catalog", lambda *a, **kw: pytest.fail("source access"))
    assert cognistore_cli.main(_args(source)) == 1
    assert "single path component" in _output(capsys)["error"]
    assert not source["report"].exists()


@pytest.mark.parametrize(("first_bucket", "second_bucket", "first_prefix", "second_prefix"), [
    ("Shared", "shared", "", ""),
    ("shared", "shared", "Docs/", "docs/private/"),
    ("Café", "Cafe\u0301", "", ""),
    ("shared", "shared", "Café/", "Cafe\u0301/private/"),
])
def test_tenant_bindings_reject_case_and_unicode_namespace_aliases(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    first_bucket: str, second_bucket: str, first_prefix: str, second_prefix: str,
) -> None:
    source["scope"].write_text(yaml.safe_dump({"tenants": {
        "acme": {"bucket": first_bucket, "prefix": first_prefix, "tiers": ["hot"]},
        "other": {"bucket": second_bucket, "prefix": second_prefix, "tiers": ["hot"]},
    }}), encoding="utf-8")
    monkeypatch.setattr(consistency_commands, "open_catalog", lambda *a, **kw: pytest.fail("source access"))
    assert cognistore_cli.main(_args(source)) == 1
    assert "overlap" in _output(capsys)["error"]
    assert not source["report"].exists()


def test_export_rejects_real_catalog_journal_through_symlink_locator(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    assert cognistore_cli.main(_args(source)) == 0
    _output(capsys)
    alias = source["catalog"].with_name("catalog-alias.sqlite3")
    alias.symlink_to(source["catalog"])
    target = Path(str(source["catalog"]) + "-journal")
    before = _snapshot(source["report"])
    assert cognistore_cli.main(_args(source, "consistency-export") + [
        "--catalog-db", str(alias), "--output", str(target),
    ]) == 1
    assert "alias" in _output(capsys)["error"]
    assert not target.exists()
    assert _snapshot(source["report"]) == before


def test_relative_driver_root_changes_invalidate_binding(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source["drivers"].write_text("tiers:\n  hot:\n    driver: posix\n    path: hot\n")
    monkeypatch.chdir(source["catalog"].parent)
    assert cognistore_cli.main(_args(source)) == 0
    _output(capsys)
    elsewhere = source["catalog"].parent / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    before = _snapshot(source["report"])
    assert cognistore_cli.main(_args(source, "consistency-export")) == 1
    assert "binding" in _output(capsys)["error"]
    assert _snapshot(source["report"]) == before


def test_hardlinked_report_cannot_mutate_backend_copy(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    assert cognistore_cli.main(_args(source)) == 0
    _output(capsys)
    backend = source["hot"] / "demo" / "acme" / "report.sqlite3"
    backend.hardlink_to(source["report"])
    before = _snapshot(source["catalog"].parent)
    for arguments in (_args(source) + ["--resume"], _args(source, "consistency-export")):
        assert cognistore_cli.main(arguments) == 1
        assert "hardlink" in _output(capsys)["error"]
    assert _snapshot(source["catalog"].parent) == before


@pytest.mark.parametrize("prefix,tiers", [("", "hot"), ("acme/", "hot"), ("acme/one", "cold")])
def test_overlapping_tenant_bindings_are_rejected_even_across_tiers(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str], prefix: str, tiers: str,
) -> None:
    text = source["scope"].read_text().replace("prefix: other/", f"prefix: '{prefix}'")
    text = text.replace("prefix: '" + prefix + "'\n    tiers: [hot]", f"prefix: '{prefix}'\n    tiers: [{tiers}]")
    source["scope"].write_text(text)
    assert cognistore_cli.main(_args(source)) == 1
    assert "overlap" in _output(capsys)["error"]
    assert not source["report"].exists()


def test_same_prefix_in_disjoint_tenant_buckets_is_allowed(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    text = source["scope"].read_text().replace(
        "  other:\n    bucket: demo\n    prefix: other/", "  other:\n    bucket: other\n    prefix: acme/",
    )
    source["scope"].write_text(text)
    assert cognistore_cli.main(_args(source)) == 0
    assert _output(capsys)["summary"]["scope"]["bucket"] == "demo"
