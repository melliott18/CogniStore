from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cognistore.cli import cognistore_cli, orphan_commands
from cognistore.core.audit import AuditContext
from cognistore.core.orphan_cleanup import OrphanCleanup
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.db.migrations import MigrationManager
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.drivers.tenancy import TenantStorageDriver


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
    for tenant in ("acme", "other", "default"):
        catalog = SQLiteCatalog(paths["catalog"], tenant_id=tenant)
        try:
            catalog.upsert("demo", "acme/catalogued", len(tenant), tier="hot")
        finally:
            catalog.close()
        TenantStorageDriver(PosixDriver(str(paths["hot"])), tenant).put_object(
            "demo", "acme/orphan", tenant.encode(),
        )
    return paths


def _args(source: dict[str, Path]) -> list[str]:
    return [
        "--no-config", "--catalog-db", str(source["catalog"]),
        "--drivers", str(source["drivers"]), "orphan-cleanup",
        "--scope-config", str(source["scope"]), "--tenant", "acme",
        "--key", "acme/orphan", "--tier", "hot", "--json",
    ]


def _output(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    output = capsys.readouterr()
    assert output.err == ""
    return json.loads(output.out)


def _snapshot(path: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(file): (file.read_bytes(), file.stat().st_mtime_ns)
        for file in sorted(path.rglob("*")) if file.is_file()
    }


@pytest.mark.parametrize("extra", [[], ["--no-dry-run"], ["--dry-run"], ["--quarantine", "--dry-run"]])
def test_report_and_previews_use_actual_tenant_without_writes(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch, extra: list[str],
) -> None:
    before = _snapshot(source["catalog"].parent)
    observed: dict[str, Any] = {}

    class InspectCleanup:
        def __init__(self, catalog: Any, drivers: Any, scope: Any, binding_id: str) -> None:
            assert catalog.read_only is True
            assert catalog.tenant_id == scope.tenant_id == "acme"
            assert catalog.get("demo", "acme/catalogued").size == len("acme")
            assert drivers["hot"].get_object("demo", "acme/orphan") == b"acme"
            assert len(binding_id) == 64

        def run(self, key: str, tier: str, **kwargs: Any) -> dict[str, Any]:
            assert (key, tier) == ("acme/orphan", "hot")
            observed.update(kwargs)
            return {"stage": kwargs["stage"]}

    monkeypatch.setattr(orphan_commands, "OrphanCleanup", InspectCleanup)
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda *_: pytest.fail("ordinary mutable CLI path"))
    monkeypatch.setattr(MigrationManager, "upgrade", lambda *_: pytest.fail("cleanup migration"))
    assert cognistore_cli.main(_args(source) + extra) == 0
    result = _output(capsys)
    assert result["command"] == "orphan-cleanup"
    assert result["status"] == "planned"
    assert result["dry_run"] is True
    assert observed["stage"] == "report"
    assert observed["context"] is None
    assert _snapshot(source["catalog"].parent) == before


@pytest.mark.parametrize("stage", ["quarantine", "execute"])
def test_mutating_stages_open_existing_tenant_and_attribute_local_operator(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    candidate_id = str(uuid4())
    observed: dict[str, Any] = {}

    class InspectCleanup:
        def __init__(self, catalog: Any, drivers: Any, scope: Any, binding_id: str) -> None:
            assert catalog.read_only is False
            assert catalog.tenant_id == "acme"
            assert drivers["hot"].tenant_id == "acme"

        def run(self, key: str, tier: str, **kwargs: Any) -> dict[str, Any]:
            observed.update(kwargs)
            return {"stage": kwargs["stage"]}

    monkeypatch.setattr(orphan_commands, "OrphanCleanup", InspectCleanup)
    monkeypatch.setattr(orphan_commands.getpass, "getuser", lambda: "local-operator")
    monkeypatch.setattr(MigrationManager, "upgrade", lambda *_: pytest.fail("cleanup migration"))
    extra = [f"--{stage}", "--grace-period-seconds", "12", "--retention-seconds", "34"]
    if stage == "execute":
        extra += ["--candidate-id", candidate_id]
    assert cognistore_cli.main(_args(source) + extra) == 0
    result = _output(capsys)
    assert result["status"] == "success"
    assert result["dry_run"] is False
    assert observed["stage"] == stage
    assert observed["grace_period_seconds"] == 12
    assert observed["retention_seconds"] == 34
    context = observed["context"]
    assert isinstance(context, AuditContext)
    assert (context.actor_type, context.actor_id) == ("user", "local-operator")
    if stage == "execute":
        assert observed["candidate_id"] == candidate_id


def test_execute_preview_retains_candidate_without_opening_writable_catalog(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_id = str(uuid4())

    class InspectCleanup:
        def __init__(self, catalog: Any, *args: Any, **kwargs: Any) -> None:
            assert catalog.read_only is True

        def run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["stage"] == "report"
            assert kwargs["candidate_id"] == candidate_id
            return {"stage": "report"}

    monkeypatch.setattr(orphan_commands, "OrphanCleanup", InspectCleanup)
    before = _snapshot(source["catalog"].parent)
    assert cognistore_cli.main(_args(source) + [
        "--execute", "--candidate-id", candidate_id, "--dry-run",
    ]) == 0
    assert _output(capsys)["dry_run"] is True
    assert _snapshot(source["catalog"].parent) == before


@pytest.mark.parametrize("extra", [
    ["--tenant", "unknown"], ["--tier", "cold"], ["--key", "other/private"],
    ["--key", "acme/../other/private"], ["--key", "acme//orphan"],
    ["--key", "acme/./orphan"], ["--key", "/acme/orphan"], ["--key", "acme/orphan/"],
])
def test_invalid_scope_rejected_before_catalog_or_storage_access(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch, extra: list[str],
) -> None:
    monkeypatch.setattr(orphan_commands, "open_catalog", lambda *a, **k: pytest.fail("catalog access"))
    monkeypatch.setattr(orphan_commands, "load_drivers", lambda *a, **k: pytest.fail("storage access"))
    assert cognistore_cli.main(_args(source) + extra) == 1
    assert _output(capsys)["status"] == "error"


@pytest.mark.parametrize("option", ["--grace-period-seconds", "--retention-seconds"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_cleanup_intervals_must_be_finite_and_strictly_positive(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str], option: str, value: str,
) -> None:
    with pytest.raises(SystemExit) as error:
        cognistore_cli.main(_args(source) + [option, value])
    assert error.value.code == 2
    assert _output(capsys)["error_type"] == "UsageError"


@pytest.mark.parametrize("extra", [["--execute"], ["--execute", "--dry-run"], ["--execute", "--quarantine"]])
def test_execute_requires_candidate_and_stages_are_mutually_exclusive(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str], extra: list[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        cognistore_cli.main(_args(source) + extra)
    assert error.value.code == 2
    assert _output(capsys)["error_type"] == "UsageError"


@pytest.mark.parametrize("extra", [[], ["--quarantine"], ["--execute", "--candidate-id", str(uuid4())]])
def test_missing_tenant_catalog_never_creates_partition(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str], extra: list[str],
) -> None:
    source["scope"].write_text(source["scope"].read_text().replace("  acme:", "  missing:"))
    before = _snapshot(source["catalog"].parent)
    assert cognistore_cli.main(_args(source) + ["--tenant", "missing", *extra]) == 1
    assert _output(capsys)["status"] == "error"
    assert _snapshot(source["catalog"].parent) == before


def test_memory_catalog_is_rejected(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    assert cognistore_cli.main(_args(source) + ["--catalog-db", ":memory:"]) == 1
    assert "persistent" in _output(capsys)["error"]


def test_real_cleanup_preserves_foreign_tenants_and_retries_backend_failure(
    source: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc) + timedelta(days=30)
    monkeypatch.setattr(
        orphan_commands, "OrphanCleanup",
        lambda *args, **kwargs: OrphanCleanup(*args, **kwargs, clock=lambda: now),
    )
    monkeypatch.setattr(orphan_commands.getpass, "getuser", lambda: "local-operator")
    arguments = _args(source) + ["--grace-period-seconds", "1", "--retention-seconds", "1"]
    before = _snapshot(source["catalog"].parent)
    assert cognistore_cli.main(arguments) == 0
    report = _output(capsys)
    assert report["dry_run"] is True
    assert report["summary"]["status"] == "eligible"
    assert _snapshot(source["catalog"].parent) == before

    assert cognistore_cli.main(arguments + ["--quarantine"]) == 0
    quarantine = _output(capsys)["summary"]
    assert quarantine["status"] == "quarantined"
    candidate_id = quarantine["candidate_id"]
    execution = arguments + ["--execute", "--candidate-id", candidate_id]
    assert cognistore_cli.main(execution) == 0
    assert "grace_period" in _output(capsys)["summary"]["blockers"]
    now += timedelta(seconds=2)

    before = _snapshot(source["catalog"].parent)
    assert cognistore_cli.main(execution + ["--dry-run"]) == 0
    assert _output(capsys)["summary"]["status"] == "eligible"
    assert _snapshot(source["catalog"].parent) == before

    original_delete = PosixDriver.delete_object_if_generation

    def unavailable(*_args: Any, **_kwargs: Any) -> bool:
        raise OSError("backend unavailable")

    monkeypatch.setattr(PosixDriver, "delete_object_if_generation", unavailable)
    assert cognistore_cli.main(execution) == 1
    failed = _output(capsys)
    assert failed["status"] == "error"
    assert failed["retryable"] is True
    assert failed["summary"]["status"] == "failed"
    assert failed["summary"]["candidate_id"] == candidate_id
    monkeypatch.setattr(PosixDriver, "delete_object_if_generation", original_delete)
    assert cognistore_cli.main(execution) == 0
    assert _output(capsys)["summary"]["status"] == "deleted"
    assert cognistore_cli.main(execution) == 0
    assert _output(capsys)["summary"]["status"] == "already_deleted"

    raw = PosixDriver(str(source["hot"]))
    with pytest.raises(FileNotFoundError):
        TenantStorageDriver(raw, "acme").get_object("demo", "acme/orphan")
    for tenant in ("other", "default"):
        assert TenantStorageDriver(raw, tenant).get_object("demo", "acme/orphan") == tenant.encode()
    catalog = SQLiteCatalog(source["catalog"], tenant_id="acme", read_only=True)
    try:
        events = catalog.list_audit_events()
        assert {event.event_type for event in events} == {
            "orphan.quarantined", "orphan.delete_started", "orphan.delete_failed", "orphan.deleted",
        }
        assert {event.actor_id for event in events} == {"local-operator"}
        assert {event.correlation_id for event in events} == {candidate_id}
    finally:
        catalog.close()
