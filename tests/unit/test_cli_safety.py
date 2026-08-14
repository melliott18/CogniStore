from __future__ import annotations

import json
from pathlib import Path

import pytest

from cognistore.cli import cognistore_cli
from cognistore.core.catalog import Catalog
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.jobs.models import EnqueueReceipt


BUCKET = "bk"
KEY = "small.txt"
DATA = b"12345"


def _configured_policy_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Catalog, PosixDriver, PosixDriver]:
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    warm.put_object(BUCKET, KEY, DATA)

    catalog = Catalog()
    catalog.upsert(BUCKET, KEY, len(DATA), tier="warm")

    monkeypatch.setattr(cognistore_cli, "Catalog", lambda: catalog)
    monkeypatch.setattr(
        cognistore_cli,
        "load_drivers",
        lambda _path: {"hot": hot, "warm": warm},
    )
    return catalog, hot, warm


def _forbid_write(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("dry-run attempted a write")


@pytest.mark.parametrize(
    ("dry_run", "json_output", "expected_status"),
    [
        (True, False, "planned"),
        (False, False, "completed"),
        (True, True, "planned"),
        (False, True, "completed"),
    ],
)
def test_policy_output_distinguishes_planned_from_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
    json_output: bool,
    expected_status: str,
) -> None:
    catalog, hot, warm = _configured_policy_run(tmp_path, monkeypatch)

    if dry_run:
        monkeypatch.setattr(hot, "put_object", _forbid_write)
        monkeypatch.setattr(warm, "delete_object", _forbid_write)
        monkeypatch.setattr(catalog, "upsert", _forbid_write)
        monkeypatch.setattr(catalog, "update_placement", _forbid_write)
        monkeypatch.setattr(catalog, "delete", _forbid_write)

    argv = [
        "--drivers",
        "ignored.yaml",
        "policy-run",
        BUCKET,
        "--threshold",
        str(len(DATA)),
        "--sync",
    ]
    if dry_run:
        argv.append("--dry-run")
    if json_output:
        argv.append("--json")

    assert cognistore_cli.main(argv) == 0
    captured = capsys.readouterr()

    if json_output:
        payload = json.loads(captured.out)
        assert payload == {
            "actions": [
                {
                    "bucket": BUCKET,
                    "from_tier": "warm",
                    "key": KEY,
                    "reason": "small object -> hot tier",
                    "status": expected_status,
                    "to_tier": "hot",
                }
            ],
            "count": 1,
            "dry_run": dry_run,
        }
    else:
        verb = "planned" if dry_run else "moved"
        summary = "planned_actions" if dry_run else "completed_actions"
        assert captured.out.splitlines() == [
            f"{verb} {BUCKET}/{KEY} warm->hot : small object -> hot tier",
            f"{summary}=1",
        ]

    record = catalog.get(BUCKET, KEY)
    assert record is not None
    if dry_run:
        assert record.tier == "warm"
        assert warm.get_object(BUCKET, KEY) == DATA
        with pytest.raises(FileNotFoundError):
            hot.get_object(BUCKET, KEY)
    else:
        assert record.tier == "hot"
        assert hot.get_object(BUCKET, KEY) == DATA
        with pytest.raises(FileNotFoundError):
            warm.get_object(BUCKET, KEY)


def test_direct_move_dry_run_is_planned_and_does_not_create_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog, hot, warm = _configured_policy_run(tmp_path, monkeypatch)
    catalog_path = tmp_path / "missing.sqlite"

    monkeypatch.setattr(hot, "put_object", _forbid_write)
    monkeypatch.setattr(warm, "delete_object", _forbid_write)
    monkeypatch.setattr(catalog, "upsert", _forbid_write)
    monkeypatch.setattr(catalog, "update_placement", _forbid_write)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "--catalog-db",
                str(catalog_path),
                "move",
                "warm",
                "hot",
                BUCKET,
                KEY,
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["count"] == 1
    assert payload["actions"][0]["status"] == "planned"
    assert warm.get_object(BUCKET, KEY) == DATA
    with pytest.raises(FileNotFoundError):
        hot.get_object(BUCKET, KEY)
    assert catalog.get(BUCKET, KEY).tier == "warm"  # type: ignore[union-attr]
    assert not catalog_path.exists()


def test_policy_dry_run_rejects_missing_sqlite_catalog_without_creating_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog_path = tmp_path / "missing.sqlite"
    config_path = tmp_path / "drivers.yaml"
    hot = tmp_path / "hot"
    warm = tmp_path / "warm"
    _write_driver_config(config_path, hot, warm)

    with pytest.raises(SystemExit) as exc_info:
        cognistore_cli.main(
            [
                "--drivers",
                str(config_path),
                "--catalog-db",
                str(catalog_path),
                "policy-run",
                BUCKET,
                "--dry-run",
            ]
        )

    assert exc_info.value.code == 2
    assert "must already exist" in capsys.readouterr().err
    assert not catalog_path.exists()
    assert not hot.exists()
    assert not warm.exists()


def _write_driver_config(config_path: Path, hot: Path, warm: Path) -> None:
    config_path.write_text(
        "\n".join(
            [
                "tiers:",
                "  hot:",
                "    driver: posix",
                f"    path: {hot}",
                "  warm:",
                "    driver: posix",
                f"    path: {warm}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_policy_dry_run_does_not_create_missing_posix_roots(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hot = tmp_path / "hot"
    warm = tmp_path / "warm"
    config_path = tmp_path / "drivers.yaml"
    _write_driver_config(config_path, hot, warm)

    assert (
        cognistore_cli.main(
            ["--drivers", str(config_path), "policy-run", BUCKET, "--dry-run"]
        )
        == 0
    )

    assert capsys.readouterr().out == "planned_actions=0\n"
    assert not hot.exists()
    assert not warm.exists()


def test_auto_discover_dry_run_does_not_refresh_cache_or_profile_tiers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hot = tmp_path / "hot"
    warm = tmp_path / "warm"
    cache = tmp_path / "cache"
    config_path = tmp_path / "drivers.yaml"
    _write_driver_config(config_path, hot, warm)

    monkeypatch.setattr(cognistore_cli, "profile_path", _forbid_write)
    monkeypatch.setattr(cognistore_cli, "discover_device_for_tier", _forbid_write)
    monkeypatch.setattr(cognistore_cli, "save_metrics_json", _forbid_write)
    monkeypatch.setattr(cognistore_cli, "save_hardware_json", _forbid_write)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                str(config_path),
                "policy-run",
                BUCKET,
                "--dry-run",
                "--auto-discover",
                "--cache-dir",
                str(cache),
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.out == "planned_actions=0\n"
    assert "skipped auto-discovery refresh" in captured.err
    assert not cache.exists()
    assert not hot.exists()
    assert not warm.exists()


@pytest.mark.parametrize("policy_mode", ["simple", "llm", "content"])
def test_each_policy_mode_respects_allowed_destinations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    policy_mode: str,
) -> None:
    catalog, hot, warm = _configured_policy_run(tmp_path, monkeypatch)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "policy-run",
                BUCKET,
                "--policy",
                policy_mode,
                "--threshold",
                str(len(DATA)),
                "--allowed-tiers",
                "warm",
                "--dry-run",
            ]
        )
        == 0
    )

    assert capsys.readouterr().out == "planned_actions=0\n"
    assert warm.get_object(BUCKET, KEY) == DATA
    with pytest.raises(FileNotFoundError):
        hot.get_object(BUCKET, KEY)
    assert catalog.get(BUCKET, KEY).tier == "warm"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("allowed_tiers", "message"),
    [
        (",,", "must contain at least one tier"),
        ("hot,archive", "unknown allowed tier(s): archive"),
    ],
)
def test_policy_run_rejects_invalid_or_unknown_allowed_tiers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    allowed_tiers: str,
    message: str,
) -> None:
    _configured_policy_run(tmp_path, monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "policy-run",
                BUCKET,
                "--allowed-tiers",
                allowed_tiers,
                "--dry-run",
            ]
        )

    assert exc_info.value.code == 2
    assert message in capsys.readouterr().err


def test_policy_run_enqueues_by_default_without_moving_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog, hot, warm = _configured_policy_run(tmp_path, monkeypatch)
    captured_jobs = []

    async def enqueue(_config, job):
        captured_jobs.append(job)
        return EnqueueReceipt(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            stream="TEST_JOBS",
            sequence=12,
        )

    monkeypatch.setattr(cognistore_cli, "_enqueue_job", enqueue)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "policy-run",
                BUCKET,
                "--threshold",
                str(len(DATA)),
                "--correlation-id",
                "request-18",
                "--json",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "queued"
    assert output["job_type"] == "policy.run"
    assert output["correlation_id"] == "request-18"
    assert output["sequence"] == 12
    assert len(captured_jobs) == 1
    assert captured_jobs[0].payload["bucket"] == BUCKET
    assert captured_jobs[0].payload["allowed_tiers"] == ["hot", "warm"]
    assert catalog.get(BUCKET, KEY).tier == "warm"  # type: ignore[union-attr]
    assert warm.get_object(BUCKET, KEY) == DATA
    with pytest.raises(FileNotFoundError):
        hot.get_object(BUCKET, KEY)


def test_catalog_scan_enqueues_job_and_correlation_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda _path: {"hot": hot})
    captured_jobs = []

    async def enqueue(_config, job):
        captured_jobs.append(job)
        return EnqueueReceipt(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            stream="TEST_JOBS",
            sequence=4,
        )

    monkeypatch.setattr(cognistore_cli, "_enqueue_job", enqueue)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "catalog-scan",
                "hot",
                BUCKET,
                "--prefix",
                "reports/",
                "--correlation-id",
                "scan-request",
                "--json",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["job_type"] == "catalog.scan"
    assert output["correlation_id"] == "scan-request"
    assert captured_jobs[0].payload == {
        "tier": "hot",
        "bucket": BUCKET,
        "prefix": "reports/",
    }
