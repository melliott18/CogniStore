from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cognistore.cli import cognistore_cli
from cognistore.core.move_jobs import MoveJobFailedError, MoveJobState
from cognistore.core.mover import Mover
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver

BUCKET = "move-recovery"
EXPIRED_TIME = datetime(2000, 1, 1, tzinfo=timezone.utc)
MOVE_TRANSITIONS = [
    "prepared",
    "transferred",
    "verified",
    "committed",
    "cleanup",
    "completed",
]


class InjectedCrash(BaseException):
    """Model process loss without allowing the mover to handle the failure."""


def _drivers_and_config(
    tmp_path: Path,
) -> tuple[Path, PosixDriver, PosixDriver]:
    hot_path = tmp_path / "hot"
    warm_path = tmp_path / "warm"
    config_path = tmp_path / "drivers.yaml"
    config_path.write_text(
        "\n".join(
            [
                "tiers:",
                "  hot:",
                "    driver: posix",
                f"    path: {hot_path}",
                "  warm:",
                "    driver: posix",
                f"    path: {warm_path}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path, PosixDriver(str(hot_path)), PosixDriver(str(warm_path))


def _cli_prefix(config_path: Path, database: Path) -> list[str]:
    return [
        "--no-config",
        "--drivers",
        str(config_path),
        "--catalog-db",
        str(database),
    ]


def _seed_source(
    catalog: SQLiteCatalog,
    hot: PosixDriver,
    *,
    key: str,
    data: bytes,
) -> None:
    hot.put_object(BUCKET, key, data)
    catalog.upsert(BUCKET, key, len(data), "hot", {"test": "cli-recovery"})


def _placement_tier(catalog: SQLiteCatalog, key: str) -> str:
    placement = catalog.get(BUCKET, key)
    assert placement is not None
    return placement.tier


def _interrupt_move(
    catalog: SQLiteCatalog,
    hot: PosixDriver,
    warm: PosixDriver,
    *,
    move_id: str,
    key: str,
    data: bytes,
    crash_state: MoveJobState,
) -> None:
    _seed_source(catalog, hot, key=key, data=data)

    def crash_after_checkpoint(job) -> None:
        if job.state == crash_state:
            raise InjectedCrash(job.state.value)

    mover = Mover(
        {"hot": hot, "warm": warm},
        catalog,
        owner_id=f"interrupted-{move_id}",
        lease_seconds=1,
        clock=lambda: EXPIRED_TIME,
        transition_hook=crash_after_checkpoint,
    )
    with pytest.raises(InjectedCrash, match=crash_state.value):
        mover.move("hot", "warm", BUCKET, key, idempotency_key=move_id)


def test_keyed_move_output_persists_job_status_and_transitions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, hot, warm = _drivers_and_config(tmp_path)
    database = tmp_path / "catalog.sqlite"
    move_id = "manual:completed"
    key = "reports/completed.bin"
    data = b"persist the manual move journal"
    catalog = SQLiteCatalog(database)
    _seed_source(catalog, hot, key=key, data=data)
    catalog.close()

    assert (
        cognistore_cli.main(
            [
                *_cli_prefix(config_path, database),
                "move",
                "hot",
                "warm",
                BUCKET,
                key,
                "--idempotency-key",
                move_id,
                "--json",
            ]
        )
        == 0
    )
    move_payload = json.loads(capsys.readouterr().out)
    assert move_payload["command"] == "move"
    assert move_payload["status"] == "completed"
    assert move_payload["idempotency_key"] == move_id
    assert move_payload["would_resume"] is False
    assert move_payload["verification"]["status"] == "verified"
    assert warm.get_object(BUCKET, key) == data
    with pytest.raises(FileNotFoundError):
        hot.stat_object(BUCKET, key)

    assert (
        cognistore_cli.main(
            [
                *_cli_prefix(config_path, database),
                "move-status",
                move_id,
                "--json",
            ]
        )
        == 0
    )
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["job"]["idempotency_key"] == move_id
    assert status_payload["job"]["state"] == "completed"
    assert [item["to_state"] for item in status_payload["transitions"]] == (
        MOVE_TRANSITIONS
    )

    catalog = SQLiteCatalog(database, read_only=True)
    persisted = catalog.get_move_job(move_id)
    assert persisted is not None
    assert persisted.state == MoveJobState.COMPLETED
    transitions_before = catalog.list_move_job_transitions(move_id)
    placement = catalog.get(BUCKET, key)
    assert placement is not None
    assert placement.tier == "warm"
    catalog.close()

    database_before = database.read_bytes()
    assert (
        cognistore_cli.main(
            [
                *_cli_prefix(config_path, database),
                "move",
                "hot",
                "warm",
                BUCKET,
                key,
                "--idempotency-key",
                move_id,
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    completed_preview = json.loads(capsys.readouterr().out)
    assert completed_preview["outcome"] == "already_completed"
    assert completed_preview["would_resume"] is False
    assert database.read_bytes() == database_before

    # A terminal replay is a successful no-op and does not append transitions.
    assert (
        cognistore_cli.main(
            [
                *_cli_prefix(config_path, database),
                "move-resume",
                move_id,
                "--json",
            ]
        )
        == 0
    )
    resume_payload = json.loads(capsys.readouterr().out)
    assert resume_payload["outcome"] == "already_completed"
    assert resume_payload["job"]["state"] == "completed"
    catalog = SQLiteCatalog(database, read_only=True)
    assert catalog.list_move_job_transitions(move_id) == transitions_before
    catalog.close()


@pytest.mark.parametrize("recovery_command", ["move", "move-resume"])
def test_cli_recovers_transferred_move_despite_visible_destination(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    recovery_command: str,
) -> None:
    config_path, hot, warm = _drivers_and_config(tmp_path)
    database = tmp_path / "catalog.sqlite"
    move_id = f"manual:recover:{recovery_command}"
    key = f"interrupted/{recovery_command}.bin"
    data = b"destination publication survived the interrupted process"
    catalog = SQLiteCatalog(database)
    _interrupt_move(
        catalog,
        hot,
        warm,
        move_id=move_id,
        key=key,
        data=data,
        crash_state=MoveJobState.TRANSFERRED,
    )
    interrupted = catalog.get_move_job(move_id)
    assert interrupted is not None
    assert interrupted.state == MoveJobState.TRANSFERRED
    assert _placement_tier(catalog, key) == "hot"
    catalog.close()

    # Publication already happened, so an ordinary new-move preflight would
    # reject this path as a destination collision. The stable key identifies
    # the transferred journal and lets the CLI resume from its checkpoint.
    assert hot.get_object(BUCKET, key) == data
    assert warm.get_object(BUCKET, key) == data

    command = (
        [
            "move",
            "hot",
            "warm",
            BUCKET,
            key,
            "--idempotency-key",
            move_id,
        ]
        if recovery_command == "move"
        else ["move-resume", move_id]
    )
    assert (
        cognistore_cli.main(
            [*_cli_prefix(config_path, database), *command, "--json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert "resume_preconditions" not in payload
    if recovery_command == "move":
        assert payload["idempotency_key"] == move_id
        assert payload["would_resume"] is True
    else:
        assert payload["outcome"] == "completed"
        assert payload["job"]["idempotency_key"] == move_id

    assert warm.get_object(BUCKET, key) == data
    with pytest.raises(FileNotFoundError):
        hot.stat_object(BUCKET, key)
    catalog = SQLiteCatalog(database, read_only=True)
    completed = catalog.get_move_job(move_id)
    assert completed is not None
    assert completed.state == MoveJobState.COMPLETED
    assert _placement_tier(catalog, key) == "warm"
    assert [
        transition.to_state.value
        for transition in catalog.list_move_job_transitions(move_id)
    ] == MOVE_TRANSITIONS
    catalog.close()


@pytest.mark.parametrize("recovery_command", ["move", "move-resume"])
def test_recovery_preview_validates_the_recorded_driver_pair(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    recovery_command: str,
) -> None:
    config_path, hot, warm = _drivers_and_config(tmp_path)
    database = tmp_path / "catalog.sqlite"
    move_id = f"manual:missing-tier:{recovery_command}"
    key = f"invalid-driver/{recovery_command}.bin"
    catalog = SQLiteCatalog(database)
    _interrupt_move(
        catalog,
        hot,
        warm,
        move_id=move_id,
        key=key,
        data=b"validate recorded tiers before claiming recovery",
        crash_state=MoveJobState.PREPARED,
    )
    catalog.close()
    database_before = database.read_bytes()
    config_path.write_text(
        "\n".join(
            [
                "tiers:",
                "  hot:",
                "    driver: posix",
                f"    path: {tmp_path / 'hot'}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    command = (
        ["move-resume", move_id]
        if recovery_command == "move-resume"
        else [
            "move",
            "hot",
            "warm",
            BUCKET,
            key,
            "--idempotency-key",
            move_id,
        ]
    )

    with pytest.raises(SystemExit) as exc_info:
        cognistore_cli.main(
            [*_cli_prefix(config_path, database), *command, "--dry-run", "--json"]
        )

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 2
    assert payload["status"] == "error"
    assert payload["error_type"] == "UsageError"
    assert "Unknown destination tier: warm" in payload["error"]
    assert database.read_bytes() == database_before


def test_move_resume_dry_run_does_not_mutate_transferred_job_or_storage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, hot, warm = _drivers_and_config(tmp_path)
    database = tmp_path / "catalog.sqlite"
    move_id = "manual:dry-run"
    key = "interrupted/dry-run.bin"
    data = b"a dry run must leave both interrupted copies intact"
    catalog = SQLiteCatalog(database)
    _interrupt_move(
        catalog,
        hot,
        warm,
        move_id=move_id,
        key=key,
        data=data,
        crash_state=MoveJobState.TRANSFERRED,
    )
    job_before = catalog.get_move_job(move_id)
    transitions_before = catalog.list_move_job_transitions(move_id)
    catalog.close()
    database_before = database.read_bytes()

    assert (
        cognistore_cli.main(
            [
                *_cli_prefix(config_path, database),
                "move-resume",
                move_id,
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "planned"
    assert payload["dry_run"] is True
    assert payload["outcome"] == "would_resume"
    assert payload["job"]["state"] == "transferred"
    assert payload["verification"] is None
    preconditions = payload["resume_preconditions"]
    assert preconditions["readiness"] == "not_confirmed"
    assert preconditions["journal"] == {
        "checked": True,
        "satisfied": True,
        "state": "transferred",
    }
    assert preconditions["driver_pair"] == {
        "checked": True,
        "satisfied": True,
        "source_tier": "hot",
        "destination_tier": "warm",
    }
    assert preconditions["ownership"]["checked"] is False
    assert preconditions["ownership"]["satisfied"] is None
    assert preconditions["ownership"]["claim_attempted"] is False
    assert [check["name"] for check in preconditions["storage"]] == [
        "source_integrity",
        "destination_integrity",
    ]
    assert all(check["checked"] is False for check in preconditions["storage"])
    assert all(check["satisfied"] is None for check in preconditions["storage"])

    assert database.read_bytes() == database_before
    assert hot.get_object(BUCKET, key) == data
    assert warm.get_object(BUCKET, key) == data
    catalog = SQLiteCatalog(database, read_only=True)
    assert catalog.get_move_job(move_id) == job_before
    assert catalog.list_move_job_transitions(move_id) == transitions_before
    assert _placement_tier(catalog, key) == "hot"
    catalog.close()


@pytest.mark.parametrize(
    ("state", "expected_checks"),
    [
        (
            MoveJobState.PREPARED,
            ["source_generation", "destination_publication"],
        ),
        (
            MoveJobState.VERIFIED,
            [
                "destination_generation_for_cleanup",
                "source_generation_for_cleanup",
            ],
        ),
        (
            MoveJobState.COMMITTED,
            [
                "destination_generation_for_cleanup",
                "source_generation_for_cleanup",
            ],
        ),
        (
            MoveJobState.CLEANUP,
            [
                "destination_generation_for_cleanup",
                "source_generation_for_cleanup",
            ],
        ),
    ],
)
def test_move_resume_preview_names_unchecked_phase_storage_preconditions(
    tmp_path: Path,
    state: MoveJobState,
    expected_checks: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, hot, warm = _drivers_and_config(tmp_path)
    database = tmp_path / "catalog.sqlite"
    move_id = f"manual:preview:{state.value}"
    catalog = SQLiteCatalog(database)
    _interrupt_move(
        catalog,
        hot,
        warm,
        move_id=move_id,
        key=f"preview/{state.value}.bin",
        data=f"preview {state.value}".encode(),
        crash_state=state,
    )
    catalog.close()
    database_before = database.read_bytes()

    assert (
        cognistore_cli.main(
            [
                *_cli_prefix(config_path, database),
                "move-resume",
                move_id,
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    preconditions = payload["resume_preconditions"]
    assert preconditions["readiness"] == "not_confirmed"
    assert [item["name"] for item in preconditions["storage"]] == expected_checks
    assert all(item["checked"] is False for item in preconditions["storage"])
    assert database.read_bytes() == database_before


def test_move_list_is_deterministic_and_filterable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, hot, warm = _drivers_and_config(tmp_path)
    database = tmp_path / "catalog.sqlite"
    catalog = SQLiteCatalog(database)
    # All three jobs intentionally share a timestamp. Stable key ordering is
    # therefore the observable tie-breaker, independent of insertion order.
    for move_id, key in [
        ("batch:zeta", "list/zeta.bin"),
        ("other:middle", "list/middle.bin"),
        ("batch:alpha", "list/alpha.bin"),
    ]:
        _interrupt_move(
            catalog,
            hot,
            warm,
            move_id=move_id,
            key=key,
            data=move_id.encode(),
            crash_state=MoveJobState.PREPARED,
        )
    catalog.close()

    argv = [
        *_cli_prefix(config_path, database),
        "move-list",
        "--state",
        "prepared",
        "--idempotency-prefix",
        "batch:",
        "--json",
    ]
    assert cognistore_cli.main(argv) == 0
    first_output = capsys.readouterr().out
    assert cognistore_cli.main(argv) == 0
    second_output = capsys.readouterr().out
    assert second_output == first_output

    payload = json.loads(first_output)
    assert payload["count"] == 2
    assert [job["idempotency_key"] for job in payload["jobs"]] == [
        "batch:alpha",
        "batch:zeta",
    ]
    assert {job["state"] for job in payload["jobs"]} == {"prepared"}

    assert (
        cognistore_cli.main(
            [
                *_cli_prefix(config_path, database),
                "move-list",
                "--state",
                "completed",
                "--idempotency-prefix",
                "batch:",
                "--json",
            ]
        )
        == 0
    )
    empty_payload = json.loads(capsys.readouterr().out)
    assert empty_payload["count"] == 0
    assert empty_payload["jobs"] == []


def test_missing_and_failed_move_commands_return_structured_failures(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, hot, warm = _drivers_and_config(tmp_path)
    database = tmp_path / "catalog.sqlite"
    failed_id = "manual:failed"
    key = "failed/missing-source.bin"
    catalog = SQLiteCatalog(database)
    _interrupt_move(
        catalog,
        hot,
        warm,
        move_id=failed_id,
        key=key,
        data=b"remove the source before recovery",
        crash_state=MoveJobState.PREPARED,
    )
    hot.delete_object(BUCKET, key)
    with pytest.raises(MoveJobFailedError):
        Mover(
            {"hot": hot, "warm": warm},
            catalog,
            owner_id="failure-recorder",
            lease_seconds=1,
            clock=lambda: EXPIRED_TIME + timedelta(seconds=2),
        ).move("hot", "warm", BUCKET, key, idempotency_key=failed_id)
    failed_job = catalog.get_move_job(failed_id)
    assert failed_job is not None
    assert failed_job.state == MoveJobState.FAILED
    catalog.close()

    assert (
        cognistore_cli.main(
            [
                *_cli_prefix(config_path, database),
                "move-resume",
                failed_id,
                "--json",
            ]
        )
        == 1
    )
    failed_payload = json.loads(capsys.readouterr().out)
    assert failed_payload["status"] == "error"
    assert failed_payload["error_type"] == "MoveJobFailedError"
    assert failed_payload["job"]["state"] == "failed"

    database_before = database.read_bytes()
    assert (
        cognistore_cli.main(
            [
                *_cli_prefix(config_path, database),
                "move-resume",
                failed_id,
                "--dry-run",
                "--json",
            ]
        )
        == 1
    )
    failed_preview = json.loads(capsys.readouterr().out)
    assert failed_preview["status"] == "error"
    assert failed_preview["error_type"] == "MoveJobFailedError"
    assert database.read_bytes() == database_before

    assert (
        cognistore_cli.main(
            [
                *_cli_prefix(config_path, database),
                "move",
                "hot",
                "warm",
                BUCKET,
                key,
                "--idempotency-key",
                failed_id,
                "--dry-run",
                "--json",
            ]
        )
        == 1
    )
    failed_move_preview = json.loads(capsys.readouterr().out)
    assert failed_move_preview["status"] == "error"
    assert failed_move_preview["error_type"] == "MoveJobFailedError"
    assert database.read_bytes() == database_before

    assert (
        cognistore_cli.main(
            [
                *_cli_prefix(config_path, database),
                "move-status",
                "manual:missing",
                "--json",
            ]
        )
        == 1
    )
    missing_status = json.loads(capsys.readouterr().out)
    assert missing_status["status"] == "error"
    assert missing_status["error_type"] == "MoveJobNotFound"
    assert missing_status["idempotency_key"] == "manual:missing"

    assert (
        cognistore_cli.main(
            [
                *_cli_prefix(config_path, database),
                "move-resume",
                "manual:missing",
                "--json",
            ]
        )
        == 1
    )
    missing_resume = json.loads(capsys.readouterr().out)
    assert missing_resume["status"] == "error"
    assert missing_resume["error_type"] == "MoveJobNotFound"
