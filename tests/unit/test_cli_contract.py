from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from uuid import uuid4

import pytest

from cognistore.cli import cognistore_cli

_COGNISTORE_ENVIRONMENT = (
    "COGNISTORE_CONFIG",
    "COGNISTORE_PROFILE",
    "COGNISTORE_BASE",
    "COGNISTORE_DRIVERS",
    "COGNISTORE_CATALOG_DB",
    "COGNISTORE_NATS_URL",
    "COGNISTORE_JOB_STREAM",
    "COGNISTORE_JOB_SUBJECT",
    "COGNISTORE_JOB_CONSUMER",
    "COGNISTORE_ACK_WAIT",
    "COGNISTORE_STREAM_MAX_MESSAGES",
    "COGNISTORE_STREAM_MAX_BYTES",
    "COGNISTORE_DEAD_LETTER_STREAM",
    "COGNISTORE_DEAD_LETTER_SUBJECT",
    "COGNISTORE_DEAD_LETTER_MAX_AGE",
    "COGNISTORE_JSON",
    "COGNISTORE_DRY_RUN",
    "COGNISTORE_VERBOSE",
)


@pytest.fixture(autouse=True)
def _isolated_cli_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in _COGNISTORE_ENVIRONMENT:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def _forbid(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("dry-run attempted a mutation or network operation")


def _json_document(
    capsys: pytest.CaptureFixture[str],
) -> tuple[dict[str, Any], str]:
    captured = capsys.readouterr()
    assert captured.out.endswith("\n")
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    assert payload["schema"] == "cognistore.cli"
    assert payload["schema_version"] == 1
    return payload, captured.err


def test_global_options_are_accepted_before_and_after_the_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """\
version: 1
profiles:
  contract:
    base: configured-but-overridden
""",
        encoding="utf-8",
    )
    real_queue_config = cognistore_cli._queue_config
    observed: list[Any] = []

    def capture_queue_config(
        args: Any,
        *,
        client_name: str,
        one_shot: bool = False,
    ) -> Any:
        config = real_queue_config(
            args,
            client_name=client_name,
            one_shot=one_shot,
        )
        observed.append(config)
        return config

    monkeypatch.setattr(cognistore_cli, "_queue_config", capture_queue_config)
    options = [
        "--config",
        str(config_path),
        "--profile",
        "contract",
        "--base",
        "cli-base",
        "--drivers",
        "cli-drivers.yaml",
        "--catalog-db",
        "cli-catalog.sqlite",
        "--nats-url",
        "nats://operator:flag-secret@nats.example.test:4222",
        "--job-stream",
        "CONTRACT_JOBS",
        "--job-subject",
        "contract.jobs",
        "--job-consumer",
        "contract-workers",
        "--ack-wait",
        "45",
        "--stream-max-messages",
        "1234",
        "--stream-max-bytes",
        "5678",
        "--dead-letter-stream",
        "CONTRACT_DLQ",
        "--dead-letter-subject",
        "contract.jobs.dead",
        "--dead-letter-max-age",
        "600",
        "--json",
        "--dry-run",
        "--verbose",
    ]
    command = ["dead-letter-redrive", str(uuid4())]

    for argv in (options + command, command + options):
        observed.clear()
        assert cognistore_cli.main(argv) == 0
        payload, diagnostics = _json_document(capsys)
        assert payload["command"] == "dead-letter-redrive"
        assert payload["status"] == "planned"
        assert payload["dry_run"] is True
        assert "resolved CLI configuration" in diagnostics
        assert '"base": "command-line"' in diagnostics
        assert '"job_stream": "command-line"' in diagnostics
        assert '"nats_url": "command-line"' in diagnostics
        assert "flag-secret" not in diagnostics
        assert len(observed) == 1
        queue_config = observed[0]
        assert queue_config.servers == (
            "nats://operator:flag-secret@nats.example.test:4222",
        )
        assert queue_config.stream == "CONTRACT_JOBS"
        assert queue_config.subject == "contract.jobs"
        assert queue_config.consumer == "contract-workers"
        assert queue_config.ack_wait == 45
        assert queue_config.stream_max_messages == 1234
        assert queue_config.stream_max_bytes == 5678
        assert queue_config.dead_letter_stream == "CONTRACT_DLQ"
        assert queue_config.dead_letter_subject == "contract.jobs.dead"
        assert queue_config.dead_letter_max_age == 600


def test_config_profile_environment_and_cli_precedence_is_applied_by_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """\
version: 1
default_profile: local
defaults:
  job_stream: FILE_DEFAULT
  nats_url:
    - nats://file-one.example.test:4222
    - nats://file-two.example.test:4222
profiles:
  local:
    job_stream: PROFILE_LOCAL
  remote:
    job_stream: PROFILE_REMOTE
""",
        encoding="utf-8",
    )
    real_queue_config = cognistore_cli._queue_config
    observed: list[Any] = []

    def capture_queue_config(
        args: Any,
        *,
        client_name: str,
        one_shot: bool = False,
    ) -> Any:
        config = real_queue_config(
            args,
            client_name=client_name,
            one_shot=one_shot,
        )
        observed.append(config)
        return config

    monkeypatch.setattr(cognistore_cli, "_queue_config", capture_queue_config)
    dead_letter_id = str(uuid4())

    def invoke(*options: str) -> Any:
        observed.clear()
        argv = [
            "--config",
            str(config_path),
            *options,
            "--dry-run",
            "dead-letter-redrive",
            dead_letter_id,
            "--json",
        ]
        assert cognistore_cli.main(argv) == 0
        payload, diagnostics = _json_document(capsys)
        assert diagnostics == ""
        assert payload["status"] == "planned"
        assert len(observed) == 1
        return observed[0]

    assert invoke().stream == "PROFILE_LOCAL"

    monkeypatch.setenv("COGNISTORE_PROFILE", "remote")
    assert invoke().stream == "PROFILE_REMOTE"
    assert invoke("--profile", "local").stream == "PROFILE_LOCAL"

    monkeypatch.setenv("COGNISTORE_JOB_STREAM", "ENVIRONMENT_JOBS")
    assert invoke("--profile", "local").stream == "ENVIRONMENT_JOBS"
    assert (
        invoke("--profile", "local", "--job-stream", "CLI_JOBS").stream
        == "CLI_JOBS"
    )

    monkeypatch.setenv(
        "COGNISTORE_NATS_URL",
        "nats://environment-one.example.test:4222,"
        "nats://environment-two.example.test:4222",
    )
    config = invoke(
        "--nats-url",
        "nats://cli-one.example.test:4222",
        "--nats-url",
        "nats://cli-two.example.test:4222",
    )
    assert config.servers == (
        "nats://cli-one.example.test:4222",
        "nats://cli-two.example.test:4222",
    )


def test_json_success_is_one_versioned_stdout_document(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class ListingDriver:
        def __init__(self, _base: str | Path) -> None:
            pass

        def list_objects(self, _bucket: str, prefix: str = "") -> Iterator[str]:
            assert prefix == ""
            return iter(("a", "b"))

    monkeypatch.setattr(cognistore_cli, "PosixDriver", ListingDriver)

    assert cognistore_cli.main(["--base", "unused", "ls", "bucket", "--json"]) == 0
    payload, diagnostics = _json_document(capsys)

    assert diagnostics == ""
    assert payload["command"] == "ls"
    assert payload["status"] == "success"
    assert payload["keys"] == ["a", "b"]


def test_json_runtime_error_is_one_versioned_stdout_document_with_nonzero_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingDriver:
        def __init__(self, _base: str | Path) -> None:
            pass

        def list_objects(self, _bucket: str, prefix: str = "") -> Iterator[str]:
            raise RuntimeError("storage listing failed")

    monkeypatch.setattr(cognistore_cli, "PosixDriver", FailingDriver)

    exit_code = cognistore_cli.main(
        ["--base", "unused", "ls", "bucket", "--json"]
    )
    payload, diagnostics = _json_document(capsys)

    assert exit_code == 1
    assert diagnostics == ""
    assert payload["command"] == "ls"
    assert payload["status"] == "error"
    assert payload["error_type"] == "RuntimeError"
    assert payload["exit_code"] == exit_code


def test_json_usage_error_is_one_versioned_stdout_document_with_nonzero_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cognistore_cli.main(["get", "bucket", "key", "--json"])

    payload, diagnostics = _json_document(capsys)
    assert exc_info.value.code == 2
    assert diagnostics == ""
    assert payload["command"] == "get"
    assert payload["status"] == "error"
    assert payload["error_type"] == "UsageError"
    assert payload["exit_code"] == exc_info.value.code


@pytest.mark.parametrize(
    ("argv", "command", "expected_usage"),
    [
        (["--json", "--help"], None, "usage: cognistore"),
        (["ls", "--json", "--help"], "ls", "usage: cognistore ls"),
    ],
)
def test_json_help_is_one_versioned_stdout_document(
    argv: list[str],
    command: str | None,
    expected_usage: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cognistore_cli.main(argv)

    payload, diagnostics = _json_document(capsys)
    assert exc_info.value.code == 0
    assert diagnostics == ""
    assert payload["command"] == command
    assert payload["status"] == "success"
    assert payload["help_format"] == "text"
    assert expected_usage in payload["help"]


def test_json_config_error_is_one_versioned_stdout_document_with_nonzero_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_config = tmp_path / "missing.yaml"

    exit_code = cognistore_cli.main(
        ["ls", "bucket", "--config", str(missing_config), "--json"]
    )
    payload, diagnostics = _json_document(capsys)

    assert exit_code == 2
    assert diagnostics == ""
    assert payload["command"] == "ls"
    assert payload["status"] == "error"
    assert payload["error_type"] == "ConfigurationError"
    assert payload["exit_code"] == exit_code


def test_verbose_diagnostics_stay_on_stderr_and_redact_urls_and_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingDriver:
        def __init__(self, _base: str | Path) -> None:
            pass

        def list_objects(self, _bucket: str, prefix: str = "") -> Iterator[str]:
            try:
                raise RuntimeError("Authorization: Bearer log-trace-secret")
            except RuntimeError:
                logging.getLogger("cognistore.jobs.runtime").exception(
                    "backend log failed nkey_seed=log-message-secret"
                )
            raise RuntimeError(
                "request to https://alice:http-secret@storage.example.test/object "
                "failed password=runtime-secret"
            )

    monkeypatch.setattr(cognistore_cli, "PosixDriver", FailingDriver)
    argv = [
        "--base",
        "unused",
        "--nats-url",
        "nats://queue-user-unique:nats-secret@nats.example.test:4222?token=query-secret",
        "ls",
        "bucket",
        "--json",
        "--verbose",
    ]

    assert cognistore_cli.main(argv) == 1
    payload, diagnostics = _json_document(capsys)

    assert payload["status"] == "error"
    assert payload["error"] == (
        "request to https://[REDACTED]@storage.example.test/object "
        "failed password=[REDACTED]"
    )
    assert "resolved CLI configuration" in diagnostics
    assert "command failed" in diagnostics
    for secret in (
        "alice",
        "http-secret",
        "runtime-secret",
        "queue-user-unique",
        "nats-secret",
        "query-secret",
        "log-trace-secret",
        "log-message-secret",
    ):
        assert secret not in json.dumps(payload)
        assert secret not in diagnostics


def test_put_dry_run_does_not_write_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    storage_root = tmp_path / "missing-storage-root"
    unused_catalog = tmp_path / "must-not-be-created.sqlite"

    class PutSentinelDriver:
        def __init__(self, base: str | Path) -> None:
            assert Path(base) == storage_root

        def stat_object(self, bucket: str, key: str) -> dict[str, Any]:
            assert (bucket, key) == ("bucket", "key")
            raise FileNotFoundError(key)

        put_object = _forbid

    monkeypatch.setattr(cognistore_cli, "PosixDriver", PutSentinelDriver)

    assert (
        cognistore_cli.main(
            [
                "--base",
                str(storage_root),
                "--catalog-db",
                str(unused_catalog),
                "put",
                "bucket",
                "key",
                str(source),
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    payload, diagnostics = _json_document(capsys)

    assert diagnostics == ""
    assert payload["status"] == "planned"
    assert payload["dry_run"] is True
    assert payload["would_overwrite"] is False
    assert not storage_root.exists()
    assert not unused_catalog.exists()


def test_put_dry_run_reports_an_existing_destination_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"replacement")

    class ExistingDestinationDriver:
        def __init__(self, _base: str | Path) -> None:
            pass

        def stat_object(self, bucket: str, key: str) -> dict[str, Any]:
            assert (bucket, key) == ("bucket", "key")
            return {"size": 7, "generation": "existing-generation"}

        put_object = _forbid

    monkeypatch.setattr(
        cognistore_cli,
        "PosixDriver",
        ExistingDestinationDriver,
    )

    assert (
        cognistore_cli.main(
            [
                "--base",
                "unused",
                "put",
                "bucket",
                "key",
                str(source),
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    payload, diagnostics = _json_document(capsys)

    assert diagnostics == ""
    assert payload["status"] == "planned"
    assert payload["would_overwrite"] is True


def test_background_submission_ignores_profile_catalog_without_creating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    drivers_path = tmp_path / "drivers.yaml"
    drivers_path.write_text(
        "tiers:\n  hot:\n    driver: posix\n    path: /unused/hot\n",
        encoding="utf-8",
    )
    unused_catalog = tmp_path / "profile-catalog.sqlite"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "version: 1",
                "default_profile: shared",
                "profiles:",
                "  shared:",
                f"    drivers: {drivers_path}",
                f"    catalog_db: {unused_catalog}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    def submit(args: Any, job: Any) -> int:
        assert args.catalog_db == str(unused_catalog)
        assert job.job_type == "catalog.scan"
        cognistore_cli._emit_result(
            "catalog-scan",
            "queued",
            json_output=args.json,
            job_id=job.job_id,
        )
        return 0

    monkeypatch.setattr(cognistore_cli, "_submit_job", submit)

    assert (
        cognistore_cli.main(
            [
                "--config",
                str(config_path),
                "catalog-scan",
                "hot",
                "bucket",
                "--json",
            ]
        )
        == 0
    )
    payload, diagnostics = _json_document(capsys)

    assert diagnostics == ""
    assert payload["status"] == "queued"
    assert not unused_catalog.exists()


def test_background_submission_rejects_an_explicit_catalog(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    drivers_path = tmp_path / "drivers.yaml"
    drivers_path.write_text(
        "tiers:\n  hot:\n    driver: posix\n    path: /unused/hot\n",
        encoding="utf-8",
    )
    unused_catalog = tmp_path / "explicit-catalog.sqlite"

    with pytest.raises(SystemExit) as exc_info:
        cognistore_cli.main(
            [
                "--drivers",
                str(drivers_path),
                "--catalog-db",
                str(unused_catalog),
                "catalog-scan",
                "hot",
                "bucket",
                "--json",
            ]
        )

    payload, diagnostics = _json_document(capsys)
    assert exc_info.value.code == 2
    assert diagnostics == ""
    assert payload["error_type"] == "UsageError"
    assert not unused_catalog.exists()


def test_get_dry_run_does_not_read_payload_or_write_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "missing-parent" / "download.bin"

    class GetSentinelDriver:
        def __init__(self, _base: str | Path) -> None:
            pass

        def stat_object(self, bucket: str, key: str) -> dict[str, Any]:
            assert (bucket, key) == ("bucket", "key")
            return {"size": 17, "generation": "generation-1"}

        get_object = _forbid

    monkeypatch.setattr(cognistore_cli, "PosixDriver", GetSentinelDriver)

    assert (
        cognistore_cli.main(
            [
                "--base",
                "unused",
                "get",
                "bucket",
                "key",
                str(output),
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    payload, diagnostics = _json_document(capsys)

    assert diagnostics == ""
    assert payload["status"] == "planned"
    assert payload["size"] == 17
    assert not output.exists()
    assert not output.parent.exists()


def test_catalog_scan_dry_run_does_not_enqueue_or_write_storage_or_catalog(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class ScanDriver:
        def list_objects(self, bucket: str, prefix: str = "") -> Iterator[str]:
            assert (bucket, prefix) == ("bucket", "reports/")
            return iter(("reports/one.txt",))

        def stat_object(self, bucket: str, key: str) -> dict[str, Any]:
            assert (bucket, key) == ("bucket", "reports/one.txt")
            return {"size": 3, "generation": "generation-1"}

        def get_object(
            self,
            bucket: str,
            key: str,
            range: str | None = None,
        ) -> bytes:
            assert (bucket, key, range) == (
                "bucket",
                "reports/one.txt",
                "bytes=0-2",
            )
            return b"one"

        def object_generation(self, bucket: str, key: str) -> str:
            assert (bucket, key) == ("bucket", "reports/one.txt")
            return "generation-1"

        put_object = _forbid
        delete_object = _forbid

    class CatalogSentinel:
        def __getattr__(self, _name: str) -> Any:
            return _forbid

    monkeypatch.setattr(
        cognistore_cli,
        "load_drivers",
        lambda _path: {"hot": ScanDriver()},
    )
    monkeypatch.setattr(cognistore_cli, "Catalog", CatalogSentinel)
    monkeypatch.setattr(cognistore_cli, "_submit_job", _forbid)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "catalog-scan",
                "hot",
                "bucket",
                "--prefix",
                "reports/",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    payload, diagnostics = _json_document(capsys)

    assert diagnostics == ""
    assert payload["status"] == "planned"
    assert payload["objects"] == [
        {"bucket": "bucket", "key": "reports/one.txt", "size": 3, "tier": "hot"}
    ]


def test_tier_profile_dry_run_does_not_benchmark_or_write_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "metrics.json"
    tier_root = tmp_path / "tier-root"
    monkeypatch.setattr(
        cognistore_cli,
        "load_drivers",
        lambda _path: {"hot": SimpleNamespace(base=tier_root)},
    )
    monkeypatch.setattr(cognistore_cli, "profile_path", _forbid)
    monkeypatch.setattr(cognistore_cli, "save_metrics_json", _forbid)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "tier-profile",
                "--metrics-out",
                str(output),
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    payload, diagnostics = _json_document(capsys)

    assert diagnostics == ""
    assert payload["status"] == "planned"
    assert payload["planned_tiers"] == [{"path": str(tier_root), "tier": "hot"}]
    assert not output.exists()


def test_devices_scan_dry_run_does_not_write_hardware_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "hardware.json"
    tier_root = tmp_path / "tier-root"
    discoveries: list[tuple[str, str]] = []
    device = SimpleNamespace(
        device="/dev/test1",
        base_device="/dev/test",
        media_type="ssd",
        model="Test Disk",
        transport="sata",
        to_dict=lambda: {"tier": "hot", "media_type": "ssd"},
    )

    def discover(tier: str, path: str) -> Any:
        discoveries.append((tier, path))
        return device

    monkeypatch.setattr(
        cognistore_cli,
        "load_drivers",
        lambda _path: {"hot": SimpleNamespace(base=tier_root)},
    )
    monkeypatch.setattr(cognistore_cli, "discover_device_for_tier", discover)
    monkeypatch.setattr(cognistore_cli, "save_hardware_json", _forbid)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "devices-scan",
                "--hardware-out",
                str(output),
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    payload, diagnostics = _json_document(capsys)

    assert diagnostics == ""
    assert payload["status"] == "planned"
    assert payload["devices"] == {"hot": {"media_type": "ssd", "tier": "hot"}}
    assert discoveries == [("hot", str(tier_root))]
    assert not output.exists()


def test_auto_refresh_dry_run_does_not_discover_profile_or_write_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(
        cognistore_cli,
        "load_drivers",
        lambda _path: {"hot": SimpleNamespace(base=tmp_path / "tier-root")},
    )
    monkeypatch.setattr(cognistore_cli, "discover_device_for_tier", _forbid)
    monkeypatch.setattr(cognistore_cli, "profile_path", _forbid)
    monkeypatch.setattr(cognistore_cli, "save_hardware_json", _forbid)
    monkeypatch.setattr(cognistore_cli, "save_metrics_json", _forbid)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "auto-refresh",
                "--cache-dir",
                str(cache_dir),
                "--interval",
                "10",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    payload, diagnostics = _json_document(capsys)

    assert diagnostics == ""
    assert payload["status"] == "planned"
    assert payload["outputs"] == {
        "hardware": str(cache_dir / "hardware.json"),
        "metrics": str(cache_dir / "tier_metrics.json"),
    }
    assert not cache_dir.exists()


def test_dead_letter_redrive_dry_run_does_not_connect(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dead_letter_id = str(uuid4())
    monkeypatch.setattr(cognistore_cli, "_redrive_dead_letter", _forbid)
    monkeypatch.setattr(cognistore_cli.asyncio, "run", _forbid)

    assert (
        cognistore_cli.main(
            ["dead-letter-redrive", dead_letter_id, "--dry-run", "--json"]
        )
        == 0
    )
    payload, diagnostics = _json_document(capsys)

    assert diagnostics == ""
    assert payload["status"] == "planned"
    assert payload["dead_letter_id"] == dead_letter_id
    assert payload["existence_checked"] is False


def test_scheduler_dry_run_does_not_open_state_or_connect(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    schedule = SimpleNamespace(
        schedule_id="scan-hot",
        job_type="catalog.scan",
        interval_seconds=60.0,
        enabled=True,
        payload={"tier": "hot", "bucket": "bucket", "prefix": ""},
    )
    monkeypatch.setattr(
        cognistore_cli,
        "load_drivers",
        lambda _path: {"hot": SimpleNamespace(base="unused")},
    )
    monkeypatch.setattr(
        cognistore_cli,
        "load_schedule_config",
        lambda _path, *, known_tiers: [schedule],
    )
    monkeypatch.setattr(cognistore_cli, "SQLiteCatalog", _forbid)
    monkeypatch.setattr(cognistore_cli, "SQLiteScheduleStore", _forbid)
    monkeypatch.setattr(cognistore_cli, "_serve_scheduler", _forbid)
    monkeypatch.setattr(cognistore_cli.asyncio, "run", _forbid)

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "scheduler",
                "--schedule-config",
                "ignored-schedules.yaml",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    payload, diagnostics = _json_document(capsys)

    assert diagnostics == ""
    assert payload["status"] == "planned"
    assert payload["due_state_checked"] is False
    assert payload["schedules"] == [
        {
            "enabled": True,
            "interval_seconds": 60.0,
            "job_type": "catalog.scan",
            "payload": {"bucket": "bucket", "prefix": "", "tier": "hot"},
            "schedule_id": "scan-hot",
        }
    ]


def test_worker_rejects_dry_run_before_loading_drivers_or_connecting(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cognistore_cli, "load_drivers", _forbid)
    monkeypatch.setattr(cognistore_cli, "SQLiteCatalog", _forbid)
    monkeypatch.setattr(cognistore_cli, "_serve_worker", _forbid)
    monkeypatch.setattr(cognistore_cli.asyncio, "run", _forbid)

    with pytest.raises(SystemExit) as exc_info:
        cognistore_cli.main(
            ["--drivers", "ignored.yaml", "worker", "--dry-run", "--json"]
        )

    payload, diagnostics = _json_document(capsys)
    assert exc_info.value.code == 2
    assert diagnostics == ""
    assert payload["command"] == "worker"
    assert payload["status"] == "error"
    assert payload["error_type"] == "UsageError"
    assert payload["exit_code"] == exc_info.value.code
    assert "does not support --dry-run" in payload["error"]
