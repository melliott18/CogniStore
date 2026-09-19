from __future__ import annotations

import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cognistore.cli import cognistore_cli
from cognistore.core.catalog import Catalog
from cognistore.core.mover import Mover
from cognistore.core.policy import SimplePolicy
from cognistore.core.policy_dataset import export_policy_dataset
from cognistore.core.policy_runner import PolicyRunner
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in tuple(os.environ):
        if variable.startswith("COGNISTORE_"):
            monkeypatch.delenv(variable)
    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "development")


def _forbid(*args: object, **kwargs: object) -> None:
    raise AssertionError("dataset commands must not load drivers or open unrelated services")


def _report(capsys: pytest.CaptureFixture[str]) -> dict:
    output = capsys.readouterr()
    assert output.err == ""
    return json.loads(output.out)


@pytest.fixture
def dataset_catalog(tmp_path: Path) -> tuple[Path, str]:
    database = tmp_path / "catalog.sqlite"
    drivers = {"hot": PosixDriver(tmp_path / "hot")}
    catalog = SQLiteCatalog(database)
    try:
        catalog.upsert("private-bucket", "confidential/customer.txt", 3, tier="hot")
        runner = PolicyRunner(
            catalog, drivers, Mover(drivers, catalog),
            SimplePolicy(allowed_tiers=("hot",)),
        )
        assert runner.plan_once("private-bucket") == []
    finally:
        catalog.close()
    return database, (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()


def _export_args(database: Path, output: Path, as_of: str) -> list[str]:
    return [
        "--no-config", "--catalog-db", str(database), "policy-dataset-export",
        "--output", str(output), "--as-of", as_of, "--json",
    ]


def test_export_is_private_reproducible_and_read_only(
    dataset_catalog: tuple[Path, str], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    database, as_of = dataset_catalog
    original = database.read_bytes()
    modified_at = database.stat().st_mtime_ns
    monkeypatch.setattr(cognistore_cli, "load_drivers", _forbid)
    monkeypatch.setattr(cognistore_cli, "load_policy_feature_loader", _forbid)
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    for output in (first, second):
        arguments = _export_args(database, output, as_of)
        arguments += ["--exclude-field", "snapshot.object.size", "--seed", "repeatable"]
        assert cognistore_cli.main(arguments) == 0
        report = _report(capsys)
        assert report["count"] == 1
        assert report["output"] == str(output)
    assert first.read_bytes() == second.read_bytes()
    exported_text = first.read_text()
    assert "private-bucket" not in exported_text
    assert "confidential/customer.txt" not in exported_text
    dataset = json.loads(exported_text)
    assert "size" not in dataset["rows"][0]["snapshot"]["object"]
    assert database.read_bytes() == original
    assert database.stat().st_mtime_ns == modified_at
    assert cognistore_cli.main([
        "--no-config", "policy-dataset-validate", "--input", str(first), "--json",
    ]) == 0
    assert _report(capsys)["valid"] is True


def test_export_dry_run_does_not_create_output_directories(
    dataset_catalog: tuple[Path, str], tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, as_of = dataset_catalog
    output = tmp_path / "absent" / "dataset.json"
    assert cognistore_cli.main(_export_args(database, output, as_of) + ["--dry-run"]) == 0
    assert _report(capsys)["status"] == "planned"
    assert not output.parent.exists()


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_export_cannot_overwrite_catalog_files(
    dataset_catalog: tuple[Path, str], capsys: pytest.CaptureFixture[str], suffix: str,
) -> None:
    database, as_of = dataset_catalog
    original = database.read_bytes()
    with pytest.raises(SystemExit) as failure:
        cognistore_cli.main(_export_args(database, Path(str(database) + suffix), as_of))
    assert failure.value.code == 2
    assert _report(capsys)["error_type"] == "UsageError"
    assert database.read_bytes() == original


def test_export_missing_catalog_is_not_created(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    database, output = tmp_path / "missing.sqlite", tmp_path / "dataset.json"
    with pytest.raises(SystemExit) as failure:
        cognistore_cli.main(_export_args(database, output, "2026-09-09T00:00:00Z"))
    assert failure.value.code == 2
    _report(capsys)
    assert not database.exists()
    assert not output.exists()


def test_failed_publication_preserves_previous_export_and_removes_temporary_file(
    dataset_catalog: tuple[Path, str], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    database, as_of = dataset_catalog
    output = tmp_path / "dataset.json"
    output.write_text("previous export")

    def fail_replace(source: Path, destination: Path) -> None:
        assert json.loads(source.read_text())["schema_version"] == 1
        assert destination == output
        raise OSError("publication failed")

    monkeypatch.setattr(cognistore_cli.os, "replace", fail_replace)
    assert cognistore_cli.main(_export_args(database, output, as_of)) == 1
    assert _report(capsys)["error_type"] == "OSError"
    assert output.read_text() == "previous export"
    assert list(tmp_path.glob(".dataset.json.*.tmp")) == []


def test_nonfinite_export_does_not_replace_previous_output(
    dataset_catalog: tuple[Path, str], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    database, as_of = dataset_catalog
    output = tmp_path / "dataset.json"
    output.write_text("previous export")
    monkeypatch.setattr(
        cognistore_cli, "export_policy_dataset",
        lambda *args, **kwargs: {"manifest": {}, "rows": [], "invalid": float("nan")},
    )
    assert cognistore_cli.main(_export_args(database, output, as_of)) == 1
    assert _report(capsys)["error_type"] == "ValueError"
    assert output.read_text() == "previous export"
    assert list(tmp_path.glob(".dataset.json.*.tmp")) == []


def test_validation_reads_stdin_without_opening_catalog_or_drivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = export_policy_dataset(Catalog(), as_of="2026-09-09T00:00:00Z")
    monkeypatch.setattr(cognistore_cli, "open_catalog", _forbid)
    monkeypatch.setattr(cognistore_cli, "load_drivers", _forbid)
    monkeypatch.setattr(cognistore_cli.sys, "stdin", io.StringIO(json.dumps(dataset)))
    unused_database = tmp_path / "unused.sqlite"
    assert cognistore_cli.main([
        "--no-config", "--catalog-db", str(unused_database),
        "policy-dataset-validate", "--json",
    ]) == 0
    assert _report(capsys)["valid"] is True
    assert not unused_database.exists()


def test_pending_labels_require_explicit_exploratory_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "catalog.sqlite"
    drivers = {
        "hot": PosixDriver(tmp_path / "hot"),
        "warm": PosixDriver(tmp_path / "warm"),
    }
    drivers["warm"].put_object("bucket", "small.txt", b"abc")
    catalog = SQLiteCatalog(database)
    try:
        catalog.upsert("bucket", "small.txt", 3, tier="warm")
        runner = PolicyRunner(catalog, drivers, Mover(drivers, catalog), SimplePolicy())
        assert len(runner.plan_once("bucket")) == 1
    finally:
        catalog.close()
    as_of = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    output = tmp_path / "pending.json"
    assert cognistore_cli.main(_export_args(database, output, as_of)) == 0
    _report(capsys)
    arguments = [
        "--no-config", "policy-dataset-validate", "--input", str(output), "--json",
    ]
    assert cognistore_cli.main(arguments) == 1
    assert "missing_label" in {issue["code"] for issue in _report(capsys)["issues"]}
    assert cognistore_cli.main(arguments + ["--allow-missing-labels"]) == 0
    assert _report(capsys)["valid"] is True


@pytest.mark.parametrize("allow_missing", [False, True])
def test_validation_reports_schema_issues_and_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], allow_missing: bool,
) -> None:
    monkeypatch.setattr(cognistore_cli.sys, "stdin", io.StringIO('{"schema_version":999}'))
    arguments = ["--no-config", "policy-dataset-validate", "--json"]
    if allow_missing:
        arguments.append("--allow-missing-labels")
    assert cognistore_cli.main(arguments) == 1
    report = _report(capsys)
    assert report["valid"] is False
    assert report["count"] == len(report["issues"]) > 0
    assert set(report["issues"][0]) == {"code", "path", "message"}


@pytest.mark.parametrize("input_text", ['{"rows":', '{"value":NaN}', '{"value":Infinity}'])
def test_validation_rejects_malformed_or_nonfinite_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], input_text: str,
) -> None:
    monkeypatch.setattr(cognistore_cli.sys, "stdin", io.StringIO(input_text))
    assert cognistore_cli.main(["--no-config", "policy-dataset-validate", "--json"]) == 1
    assert _report(capsys)["status"] == "error"
