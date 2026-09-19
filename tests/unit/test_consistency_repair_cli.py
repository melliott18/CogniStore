from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from cognistore.cli import cognistore_cli, consistency_commands
from cognistore.core.move_jobs import MoveJobState
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in tuple(os.environ):
        if variable.startswith("COGNISTORE_"):
            monkeypatch.delenv(variable)
    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "development")


def _args(source: dict[str, Path], command: str = "consistency-repair") -> list[str]:
    return [
        "--no-config", "--catalog-db", str(source["catalog"]),
        "--drivers", str(source["drivers"]), command,
        "--scope-config", str(source["scope"]), "--tenant", "acme",
        "--report", str(source["report"]), "--json",
        "--requests-per-second", "1000000", "--bytes-per-second", "1000000000",
    ]


def _output(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    output = capsys.readouterr()
    assert output.err == ""
    return json.loads(output.out)


def _snapshot(path: Path) -> dict[str, tuple[bytes, int]]:
    files = sorted(path.rglob("*")) if path.is_dir() else [path]
    return {str(file): (file.read_bytes(), file.stat().st_mtime_ns)
            for file in files if file.is_file()}


@pytest.fixture
def source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, Path]:
    paths = {name: tmp_path / filename for name, filename in {
        "catalog": "catalog.sqlite3", "drivers": "drivers.yaml",
        "scope": "tenants.yaml", "report": "report.sqlite3", "hot": "hot", "warm": "warm",
    }.items()}
    paths["drivers"].write_text(
        "tiers:\n" + "".join(
            f"  {tier}:\n    driver: posix\n    path: {paths[tier]}\n"
            for tier in ("hot", "warm")
        ), encoding="utf-8",
    )
    paths["scope"].write_text(
        "tenants:\n  acme:\n    bucket: demo\n    prefix: acme/\n    tiers: [hot, warm]\n"
        "  other:\n    bucket: demo\n    prefix: other/\n    tiers: [hot, warm]\n",
        encoding="utf-8",
    )
    driver = PosixDriver(paths["hot"])
    data = b"repair me"
    driver.put_object("demo", "acme/one", data)
    catalog = SQLiteCatalog(paths["catalog"])
    try:
        catalog.upsert("demo", "acme/one", len(data), tier="hot", metadata={
            "sha256": hashlib.sha256(data).hexdigest(), "sample_len": len(data),
        })
        catalog.claim_move_job(
            "existing-move", src_tier="hot", dst_tier="warm", bucket="demo", key="acme/one",
            expected_size=len(data), source_metadata=driver.stat_object("demo", "acme/one"),
            owner_id="original-worker", now="2020-01-01T00:00:00.000000Z",
            lease_expires_at="2020-01-01T00:01:00.000000Z",
        )
    finally:
        catalog.close()
    assert cognistore_cli.main(_args(paths, "consistency-scan")) == 0
    assert _output(capsys)["summary"]["complete"] is True
    return paths


def test_default_plan_is_audited_without_catalog_or_backend_changes(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = {name: _snapshot(source[name]) for name in ("catalog", "hot")}
    report_before = _snapshot(source["report"])
    original_open = consistency_commands.open_catalog
    opened: list[dict[str, Any]] = []

    def open_catalog(*args: Any, **kwargs: Any) -> Any:
        opened.append(kwargs)
        return original_open(*args, **kwargs)

    monkeypatch.setattr(consistency_commands, "open_catalog", open_catalog)
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda *_: pytest.fail("ordinary CLI setup"))
    assert cognistore_cli.main(_args(source)) == 0
    result = _output(capsys)
    assert result["status"] == "planned"
    assert result["dry_run"] is False
    assert result["summary"]["plan_only"] is True
    assert result["summary"]["counts"] == {"planned": 1}
    assert result["summary"]["actions"][0]["job_id"] == "existing-move"
    assert opened == [{"read_only": True, "migrate": False}]
    assert {name: _snapshot(source[name]) for name in before} == before
    assert _snapshot(source["report"]) != report_before
    assert not source["warm"].exists()


@pytest.mark.parametrize("enable", [False, True])
def test_dry_run_prevents_all_writes_even_when_repair_is_enabled(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str], enable: bool,
) -> None:
    before = _snapshot(source["catalog"].parent)
    extra = ["--dry-run"] + (["--enable-repair"] if enable else [])
    assert cognistore_cli.main(_args(source) + extra) == 0
    result = _output(capsys)
    assert result["status"] == "planned"
    assert result["summary"]["plan_only"] is True
    assert result["summary"]["counts"] == {"planned": 1}
    assert _snapshot(source["catalog"].parent) == before


def test_explicit_enable_resumes_original_move_and_repeated_run_is_safe(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open = consistency_commands.open_catalog
    opened: list[dict[str, Any]] = []

    def open_catalog(*args: Any, **kwargs: Any) -> Any:
        opened.append(kwargs)
        return original_open(*args, **kwargs)

    monkeypatch.setattr(consistency_commands, "open_catalog", open_catalog)
    assert cognistore_cli.main(_args(source) + ["--enable-repair"]) == 0
    result = _output(capsys)
    assert result["status"] == "success"
    assert result["summary"]["plan_only"] is False
    assert result["summary"]["counts"] == {"completed": 1}
    assert opened == [{"read_only": False, "migrate": False}]
    assert not (source["hot"] / "demo" / "acme" / "one").exists()
    assert (source["warm"] / "demo" / "acme" / "one").read_bytes() == b"repair me"
    assert cognistore_cli.main(_args(source) + ["--enable-repair"]) == 0
    assert _output(capsys)["summary"]["counts"] == {"resolved": 1}
    catalog = SQLiteCatalog(source["catalog"], read_only=True)
    try:
        jobs = catalog.list_move_jobs()
        assert len(jobs) == 1
        assert jobs[0].idempotency_key == "existing-move"
        assert jobs[0].state == MoveJobState.COMPLETED
        assert catalog.get("demo", "acme/one").tier == "warm"
    finally:
        catalog.close()


@pytest.mark.parametrize("invalid", ["tenant", "binding", "prefix", "tier", "incomplete", "integrity"])
def test_invalid_report_or_binding_is_rejected_before_catalog_open(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    invalid: str,
) -> None:
    extra: list[str] = []
    if invalid == "tenant":
        extra = ["--tenant", "other"]
    elif invalid == "binding":
        source["scope"].write_text(source["scope"].read_text().replace("prefix: acme/", "prefix: acme/o"))
    elif invalid == "prefix":
        extra = ["--prefix", "acme/o"]
    elif invalid == "tier":
        extra = ["--tier", "hot"]
    else:
        with sqlite3.connect(source["report"]) as connection:
            if invalid == "incomplete":
                state = json.loads(connection.execute("SELECT state FROM checkpoint").fetchone()[0])
                state["phase"] = "inspection"
                connection.execute("UPDATE checkpoint SET state=?", (json.dumps(state),))
            else:
                connection.execute("DROP TRIGGER audit_no_update")
                connection.execute("UPDATE audit SET entry_hash=? WHERE sequence=1", ("0" * 64,))
    before = _snapshot(source["catalog"].parent)
    monkeypatch.setattr(consistency_commands, "open_catalog", lambda *a, **kw: pytest.fail("catalog opened"))
    assert cognistore_cli.main(_args(source) + ["--enable-repair"] + extra) == 1
    assert _output(capsys)["status"] == "error"
    assert _snapshot(source["catalog"].parent) == before


@pytest.mark.parametrize("target", ["catalog", "scope", "drivers", "hot", "hardlink"])
def test_repair_rejects_unsafe_report_paths_before_catalog_open(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    if target == "hot":
        report = source["hot"] / "scan.sqlite3"
    elif target == "hardlink":
        report = source["report"].with_name("linked-report.sqlite3")
        report.hardlink_to(source["report"])
    else:
        report = source[target]
    before = _snapshot(source["catalog"].parent)
    monkeypatch.setattr(consistency_commands, "open_catalog", lambda *a, **kw: pytest.fail("catalog opened"))
    assert cognistore_cli.main(_args(source) + ["--enable-repair", "--report", str(report)]) == 1
    assert _output(capsys)["status"] == "error"
    assert _snapshot(source["catalog"].parent) == before


def test_enabled_repair_does_not_recreate_missing_catalog(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source["catalog"].unlink()
    monkeypatch.setattr(consistency_commands, "open_catalog", lambda *a, **kw: pytest.fail("catalog opened"))
    assert cognistore_cli.main(_args(source) + ["--enable-repair"]) == 1
    assert "existing" in _output(capsys)["error"]
    assert not source["catalog"].exists()
