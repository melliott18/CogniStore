from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cognistore.cli import cognistore_cli
from cognistore.jobs.scheduler import ScheduledJob, SQLiteScheduleStore


def _stale_run(database: Path) -> tuple[str, str]:
    started_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    schedule = ScheduledJob(
        schedule_id="scan-reports",
        job_type="catalog.scan",
        interval_seconds=60,
        enabled=True,
        payload={"tier": "hot", "bucket": "demo", "prefix": "reports/"},
        scope="test-schedule-scope",
    )
    store = SQLiteScheduleStore(database)
    try:
        store.sync((schedule,), started_at)
        run = store.reserve_due(
            schedule,
            started_at,
            publisher_id="publisher",
            publication_lease_seconds=30,
        )
        assert run is not None
        store.mark_enqueued(
            run.job.job_id,
            publisher_id="publisher",
            now=started_at,
        )
        claim = store.begin_execution(
            run.job,
            scope=run.scope,
            schedule_id=run.schedule_id,
            owner_id="worker-owner",
            now=started_at,
            lease_seconds=1,
        )
        assert claim.execute is True
        return run.job.job_id, "worker-owner"
    finally:
        store.close()


def _json(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def _recovery_arguments(
    database: Path,
    job_id: str,
    owner: str,
    *,
    dry_run: bool = False,
    reason: str = "worker process was killed",
) -> list[str]:
    arguments = [
        "--catalog-db",
        str(database),
        "schedule-run-recover",
        job_id,
        "--recovery-id",
        "486b55d1-dd25-4810-8235-19f789be2d44",
        "--expected-owner",
        owner,
        "--operator",
        "test-operator",
        "--reason",
        reason,
        "--fence-evidence",
        "PID 4242 exited from SIGKILL",
        "--confirm-former-worker-fenced",
        "--json",
    ]
    if dry_run:
        arguments.append("--dry-run")
    return arguments


def test_schedule_run_inspection_is_read_only_and_recovery_is_audited(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "catalog.sqlite"
    job_id, owner = _stale_run(database)
    original_bytes = database.read_bytes()
    original_mtime = database.stat().st_mtime_ns

    assert (
        cognistore_cli.main(
            [
                "--catalog-db",
                str(database),
                "schedule-run-list",
                "--stale",
                "--json",
            ]
        )
        == 0
    )
    listed = _json(capsys)
    assert listed["count"] == 1
    listed_run = listed["runs"][0]
    assert listed_run["job_id"] == job_id
    assert listed_run["state"] == "running"
    assert listed_run["stale"] is True

    assert (
        cognistore_cli.main(
            [
                "--catalog-db",
                str(database),
                "schedule-run-status",
                job_id,
                "--json",
            ]
        )
        == 0
    )
    status = _json(capsys)
    assert status["run"]["execution_owner"] == owner
    assert status["recoveries"] == []

    assert cognistore_cli.main(
        _recovery_arguments(database, job_id, owner, dry_run=True)
    ) == 0
    preview = _json(capsys)
    assert preview["status"] == "planned"
    assert preview["preconditions"]["readiness"] == "confirmed_at_preview_time"
    assert preview["preconditions"]["atomic_recheck_on_write"] is True
    assert preview["candidate"]["execution_owner"] == owner
    assert database.read_bytes() == original_bytes
    assert database.stat().st_mtime_ns == original_mtime

    assert cognistore_cli.main(
        _recovery_arguments(database, job_id, owner)
    ) == 0
    recovered = _json(capsys)
    assert recovered["status"] == "recovered"
    assert recovered["run"]["state"] == "retry_wait"
    assert recovered["run"]["execution_owner"] is None
    assert recovered["run"]["execution_generation"] == 0
    assert recovered["recovery"]["prior_execution_owner"] == owner
    assert recovered["recovery"]["reason"] == "worker process was killed"

    # An uncertain client may repeat the exact request after process restart.
    assert cognistore_cli.main(
        _recovery_arguments(database, job_id, owner)
    ) == 0
    replay = _json(capsys)
    assert replay["recovery"] == recovered["recovery"]

    assert (
        cognistore_cli.main(
            [
                "--catalog-db",
                str(database),
                "schedule-run-status",
                job_id,
                "--json",
            ]
        )
        == 0
    )
    final_status = _json(capsys)
    assert final_status["run"]["state"] == "retry_wait"
    assert final_status["recoveries"] == [recovered["recovery"]]


def test_schedule_run_recovery_rejects_changed_idempotent_request(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "catalog.sqlite"
    job_id, owner = _stale_run(database)

    assert cognistore_cli.main(
        _recovery_arguments(database, job_id, owner)
    ) == 0
    _json(capsys)

    assert cognistore_cli.main(
        _recovery_arguments(
            database,
            job_id,
            owner,
            reason="different recovery reason",
        )
    ) == 1
    failure = _json(capsys)
    assert failure["status"] == "error"
    assert failure["error_type"] == "ScheduledRunRecoveryError"
    assert "different request" in failure["error"]


def test_schedule_run_recovery_requires_explicit_fence_confirmation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "catalog.sqlite"
    job_id, owner = _stale_run(database)
    arguments = _recovery_arguments(database, job_id, owner)
    arguments.remove("--confirm-former-worker-fenced")

    with pytest.raises(SystemExit) as exc_info:
        cognistore_cli.main(arguments)

    assert exc_info.value.code == 2
    failure = _json(capsys)
    assert failure["status"] == "error"
    assert failure["error_type"] == "UsageError"
    assert "--confirm-former-worker-fenced" in failure["error"]


def test_schedule_run_commands_fail_cleanly_for_missing_database_and_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(SystemExit) as exc_info:
        cognistore_cli.main(
            [
                "--catalog-db",
                str(missing),
                "schedule-run-list",
                "--json",
            ]
        )
    assert exc_info.value.code == 2
    missing_database = _json(capsys)
    assert missing_database["error_type"] == "UsageError"
    assert "does not exist" in missing_database["error"]

    database = tmp_path / "catalog.sqlite"
    store = SQLiteScheduleStore(database)
    store.close()
    unknown_job = "cc91f2e2-cbe1-4694-b9f0-b47b53386440"
    assert (
        cognistore_cli.main(
            [
                "--catalog-db",
                str(database),
                "schedule-run-status",
                unknown_job,
                "--json",
            ]
        )
        == 1
    )
    missing_run = _json(capsys)
    assert missing_run["error_type"] == "ScheduledRunNotFound"
    assert missing_run["job_id"] == unknown_job
