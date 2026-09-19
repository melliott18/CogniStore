from __future__ import annotations

import argparse
import asyncio
import json
import signal
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cognistore.cli import cognistore_cli
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.jobs.models import EnqueueReceipt, JobEnvelope, RedriveReceipt
from cognistore.jobs.runtime import WorkerState
from cognistore.utils.device_info import DeviceInfo
from cognistore.utils.tier_profiler import TierMetrics


def _metrics(path: Path, *, latency: float = 0.2, read: float = 500.0) -> TierMetrics:
    return TierMetrics(
        path=str(path),
        filesystem_block_size=4096,
        total_bytes=10_000,
        free_bytes=8_000,
        seq_write_MBps=400.0,
        seq_read_MBps=read,
        random_read_IOPS=1_000.0,
        first_byte_latency_ms=latency,
    )


def _device(tier: str, path: Path, media_type: str = "ssd") -> DeviceInfo:
    return DeviceInfo(
        tier=tier,
        base_path=str(path),
        device="/dev/test1",
        base_device="/dev/test",
        model="Test Disk",
        transport="sata",
        rotational=media_type == "hdd",
        solid_state=media_type != "hdd",
        size_bytes=10_000,
        media_type=media_type,
    )


def test_base_driver_put_get_and_list_commands(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    storage = tmp_path / "storage"

    assert (
        cognistore_cli.main(["--base", str(storage), "put", "bucket", "nested/key", str(source)])
        == 0
    )

    output = tmp_path / "downloads" / "result.bin"
    assert (
        cognistore_cli.main(["--base", str(storage), "get", "bucket", "nested/key", str(output)])
        == 0
    )
    assert output.read_bytes() == b"payload"

    assert cognistore_cli.main(["--base", str(storage), "ls", "bucket", "--prefix", "nested/"]) == 0
    assert capsys.readouterr().out == "nested/key\n"


def test_configured_driver_put_and_tier_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hot = PosixDriver(tmp_path / "hot")
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda _path: {"hot": hot})
    source = tmp_path / "source.bin"
    source.write_bytes(b"configured")

    assert (
        cognistore_cli.main(["--drivers", "ignored.yaml", "put", "bucket", "key", str(source)]) == 0
    )
    assert cognistore_cli.main(["--drivers", "ignored.yaml", "ls-tier", "hot", "bucket"]) == 0
    assert capsys.readouterr().out == "key\n"


def test_sync_catalog_scan_indexes_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hot = PosixDriver(tmp_path / "hot")
    hot.put_object("bucket", "reports/one.txt", b"one")
    hot.put_object("bucket", "other.txt", b"other")
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda _path: {"hot": hot})

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
                "--sync",
            ]
        )
        == 0
    )
    assert "indexed hot:bucket/reports/one.txt size=3" in capsys.readouterr().out


def test_profile_and_device_scan_commands_write_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hot_path = tmp_path / "hot"
    drivers = {"hot": PosixDriver(hot_path), "remote": object()}
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda _path: drivers)
    monkeypatch.setattr(cognistore_cli, "profile_path", lambda path: _metrics(Path(path)))
    monkeypatch.setattr(
        cognistore_cli,
        "discover_device_for_tier",
        lambda tier, path: _device(tier, Path(path)),
    )

    metrics_path = tmp_path / "out" / "metrics.json"
    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "tier-profile",
                "--metrics-out",
                str(metrics_path),
            ]
        )
        == 0
    )
    assert json.loads(metrics_path.read_text())["hot"]["seq_read_MBps"] == 500.0

    hardware_path = tmp_path / "out" / "hardware.json"
    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "devices-scan",
                "--hardware-out",
                str(hardware_path),
            ]
        )
        == 0
    )
    assert json.loads(hardware_path.read_text())["hot"]["model"] == "Test Disk"
    captured = capsys.readouterr()
    assert "skip remote: unsupported driver for profiling" in captured.err
    assert "skip remote: unsupported driver for devices-scan" in captured.err


@pytest.mark.parametrize("interval", [0, 1])
def test_auto_refresh_writes_cache_once_or_until_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interval: int,
) -> None:
    hot_path = tmp_path / "hot"
    drivers = {"hot": PosixDriver(hot_path), "remote": object()}
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda _path: drivers)
    monkeypatch.setattr(cognistore_cli, "profile_path", lambda path: _metrics(Path(path)))
    monkeypatch.setattr(
        cognistore_cli,
        "discover_device_for_tier",
        lambda tier, path: _device(tier, Path(path)),
    )
    if interval:
        monkeypatch.setattr(
            cognistore_cli.time,
            "sleep",
            lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt),
        )

    cache = tmp_path / f"cache-{interval}"
    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "auto-refresh",
                "--cache-dir",
                str(cache),
                "--interval",
                str(interval),
            ]
        )
        == 0
    )
    assert json.loads((cache / "hardware.json").read_text())["hot"]["tier"] == "hot"
    assert json.loads((cache / "tier_metrics.json").read_text())["hot"]["free_bytes"] == 8000


def test_policy_uses_metrics_and_hardware_threshold_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    drivers = {
        "hot": PosixDriver(tmp_path / "hot"),
        "warm": PosixDriver(tmp_path / "warm"),
    }
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda _path: drivers)
    monkeypatch.setattr(
        cognistore_cli,
        "load_metrics_json",
        lambda _path: {
            "hot": _metrics(tmp_path / "hot", latency=0.1, read=2_000.0),
            "warm": _metrics(tmp_path / "warm", latency=5.0, read=180.0),
        },
    )

    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "policy-run",
                "bucket",
                "--sync",
                "--dry-run",
                "--metrics-in",
                "metrics.json",
            ]
        )
        == 0
    )
    assert "derived hot/warm threshold from metrics" in capsys.readouterr().out

    monkeypatch.setattr(
        cognistore_cli,
        "load_hardware_json",
        lambda _path: {
            "hot": _device("hot", tmp_path / "hot", "nvme"),
            "warm": _device("warm", tmp_path / "warm", "hdd"),
        },
    )
    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "policy-run",
                "bucket",
                "--sync",
                "--dry-run",
                "--hardware-in",
                "hardware.json",
            ]
        )
        == 0
    )
    assert "derived hot/warm threshold from hardware" in capsys.readouterr().out


def test_writable_auto_discovery_populates_policy_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hot_path = tmp_path / "hot"
    warm_path = tmp_path / "warm"
    drivers = {"hot": PosixDriver(hot_path), "warm": PosixDriver(warm_path)}
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda _path: drivers)
    monkeypatch.setattr(cognistore_cli, "profile_path", lambda path: _metrics(Path(path)))
    monkeypatch.setattr(
        cognistore_cli,
        "discover_device_for_tier",
        lambda tier, path: _device(tier, Path(path)),
    )

    cache = tmp_path / "policy-cache"
    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "ignored.yaml",
                "policy-run",
                "bucket",
                "--sync",
                "--auto-discover",
                "--cache-dir",
                str(cache),
            ]
        )
        == 0
    )
    assert (cache / "hardware.json").exists()
    assert (cache / "tier_metrics.json").exists()


def test_queue_configuration_and_plain_enqueue_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COGNISTORE_NATS_URL", "nats://environment:4222")
    args = argparse.Namespace(
        nats_url=None,
        job_stream="JOBS",
        job_subject="jobs.subject",
        job_consumer="workers",
        ack_wait=15.0,
    )
    config = cognistore_cli._queue_config(args, client_name="test-client")
    assert config.servers == ("nats://environment:4222",)
    assert config.client_name == "test-client"

    job = JobEnvelope.create("test.job", {})
    receipt = EnqueueReceipt(
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        stream="JOBS",
        sequence=1,
    )
    cognistore_cli._render_enqueue(job, receipt, json_output=False)
    assert capsys.readouterr().out.startswith("queued test.job job_id=")


def test_scheduler_once_accepts_sqlite_catalog_url_without_connecting_to_nats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule_path = tmp_path / "schedules.yaml"
    schedule_path.write_text(
        """
jobs:
  scan-reports:
    type: catalog.scan
    enabled: true
    interval_seconds: 60
    payload:
      tier: hot
      bucket: reports
      prefix: incoming/
""".lstrip()
    )
    seen: dict[str, object] = {}

    def load_drivers(path: str):
        seen["drivers_path"] = path
        return {"hot": object()}

    async def serve_scheduler(args, schedules):
        seen["args"] = args
        seen["schedules"] = schedules
        return 0

    def unexpected_queue(*_args, **_kwargs):  # pragma: no cover - safety assertion
        raise AssertionError("CLI validation must not connect to NATS")

    monkeypatch.setattr(cognistore_cli, "load_drivers", load_drivers)
    monkeypatch.setattr(cognistore_cli, "_serve_scheduler", serve_scheduler)
    monkeypatch.setattr(cognistore_cli, "NatsJetStreamQueue", unexpected_queue)

    catalog_path = tmp_path / "catalog.db"
    catalog_url = f"sqlite:///{catalog_path.as_posix()}"
    assert (
        cognistore_cli.main(
            [
                "--drivers",
                "drivers.yaml",
                "--catalog-db",
                catalog_url,
                "scheduler",
                "--schedule-config",
                str(schedule_path),
                "--once",
            ]
        )
        == 0
    )

    assert seen["drivers_path"] == "drivers.yaml"
    args = seen["args"]
    assert isinstance(args, argparse.Namespace)
    assert args.once is True
    assert args.schedule_config == str(schedule_path)
    assert args.catalog_db == catalog_url
    schedules = seen["schedules"]
    assert isinstance(schedules, tuple)
    assert len(schedules) == 1
    schedule = schedules[0]
    assert schedule.schedule_id == "scan-reports"
    assert schedule.job_type == "catalog.scan"
    assert schedule.interval_seconds == 60.0
    assert schedule.enabled is True
    assert dict(schedule.payload) == {
        "tier": "hot",
        "bucket": "reports",
        "prefix": "incoming/",
    }
    assert len(schedule.scope) == 64
    assert not catalog_path.exists()


@pytest.mark.parametrize(
    ("catalog_locator", "command", "message"),
    (
        (
            ":memory:",
            ["worker"],
            "--catalog-db must be a persistent catalog for worker",
        ),
        (
            "sqlite:///:memory:",
            ["worker"],
            "--catalog-db must be a persistent catalog for worker",
        ),
        (
            "sqlite:///:memory:?cache=shared",
            ["worker"],
            "--catalog-db must be a persistent catalog for worker",
        ),
        (
            ":memory:",
            ["scheduler", "--schedule-config", "unused-schedules.yaml"],
            "--schedule-db must be a persistent SQLite file",
        ),
        (
            "sqlite:///:memory:",
            ["scheduler", "--schedule-config", "unused-schedules.yaml"],
            "--schedule-db must be a persistent SQLite file",
        ),
        (
            "sqlite:///:memory:?cache=shared",
            ["scheduler", "--schedule-config", "unused-schedules.yaml"],
            "--schedule-db must be a persistent SQLite file",
        ),
    ),
)
def test_worker_and_scheduler_reject_in_memory_catalog_state(
    catalog_locator: str,
    command: list[str],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda _path: {"hot": object()})

    with pytest.raises(SystemExit) as raised:
        cognistore_cli.main(
            ["--drivers", "drivers.yaml", "--catalog-db", catalog_locator, *command]
        )

    assert raised.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    "command",
    (
        ["worker"],
        ["scheduler", "--schedule-config", "unused-schedules.yaml"],
    ),
)
def test_worker_and_scheduler_reject_in_memory_schedule_db(
    tmp_path: Path,
    command: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda _path: {"hot": object()})

    with pytest.raises(SystemExit) as raised:
        cognistore_cli.main(
            [
                "--drivers",
                "drivers.yaml",
                "--catalog-db",
                str(tmp_path / "catalog.db"),
                "--schedule-db",
                ":memory:",
                *command,
            ]
        )

    assert raised.value.code == 2
    assert "--schedule-db must be a persistent SQLite file" in capsys.readouterr().err


def test_postgres_worker_requires_separate_sqlite_scheduler_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda _path: {"hot": object()})

    with pytest.raises(SystemExit) as raised:
        cognistore_cli.main(
            [
                "--drivers",
                "drivers.yaml",
                "--catalog-url",
                "postgresql://catalog.example/cognistore",
                "worker",
            ]
        )

    assert raised.value.code == 2
    assert "--schedule-db is required with a PostgreSQL worker catalog" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("ttl", "message"),
    (("0.0000006", "at least one microsecond"), ("0.0000011", "whole-microsecond")),
)
def test_worker_rejects_unrepresentable_schedule_lock_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ttl: str,
    message: str,
) -> None:
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda _path: {"hot": object()})

    with pytest.raises(SystemExit) as raised:
        cognistore_cli.main(
            [
                "--drivers",
                "drivers.yaml",
                "--catalog-db",
                str(tmp_path / "catalog.db"),
                "worker",
                "--schedule-lock-ttl",
                ttl,
            ]
        )

    assert raised.value.code == 2
    assert message in capsys.readouterr().err


def test_dead_letter_redrive_does_not_require_storage_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dead_letter_id = str(uuid4())
    job_id = str(uuid4())
    seen = {}

    async def redrive(config, requested_id):
        seen["config"] = config
        seen["dead_letter_id"] = requested_id
        return RedriveReceipt(
            dead_letter_id=dead_letter_id,
            job_id=job_id,
            correlation_id="request-24",
            stream="JOBS",
            sequence=42,
            redrive_count=2,
            audit_chain=(dead_letter_id,),
        )

    monkeypatch.setattr(cognistore_cli, "_redrive_dead_letter", redrive)

    assert (
        cognistore_cli.main(
            [
                "--nats-url",
                "nats://test:4222",
                "--job-stream",
                "JOBS",
                "--dead-letter-stream",
                "JOBS_FAILED",
                "dead-letter-redrive",
                dead_letter_id,
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["job_id"] == job_id
    assert payload["dead_letter_id"] == dead_letter_id
    assert payload["redrive_count"] == 2
    assert seen["dead_letter_id"] == dead_letter_id
    assert seen["config"].resolved_dead_letter_stream == "JOBS_FAILED"
    assert seen["config"].max_reconnect_attempts == 1
    assert seen["config"].allow_reconnect is False


def test_dead_letter_redrive_reports_operational_failure_as_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def redrive(config, requested_id):
        raise ConnectionError("NATS is unavailable")

    monkeypatch.setattr(cognistore_cli, "_redrive_dead_letter", redrive)

    assert cognistore_cli.main(["dead-letter-redrive", str(uuid4()), "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload == {
        "schema": "cognistore.cli",
        "schema_version": 1,
        "command": "dead-letter-redrive",
        "error": "NATS is unavailable",
        "error_type": "ConnectionError",
        "exit_code": 1,
        "operation": "dead-letter-redrive",
        "retryable": False,
        "status": "error",
    }
    assert captured.err == ""


def test_dead_letter_redrive_suppresses_nats_traceback_callback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def unavailable_connect(**options):
        assert options["allow_reconnect"] is False
        await options["error_cb"](ConnectionRefusedError("connection refused"))
        raise ConnectionError("NATS is unavailable")

    monkeypatch.setattr("cognistore.jobs.nats_queue.nats.connect", unavailable_connect)

    assert cognistore_cli.main(["dead-letter-redrive", str(uuid4()), "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "error"
    assert payload["error"] == "NATS is unavailable"
    assert captured.err == ""


def test_dead_letter_redrive_rejects_bad_id_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def unexpected_connect(**options):  # pragma: no cover - safety assertion
        raise AssertionError("invalid ID must not connect")

    monkeypatch.setattr("cognistore.jobs.nats_queue.nats.connect", unexpected_connect)

    assert cognistore_cli.main(["dead-letter-redrive", "not-a-uuid", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["error_type"] == "DeadLetterRecordError"


def test_plain_redrive_output_distinguishes_existing_completion(
    capsys: pytest.CaptureFixture[str],
) -> None:
    dead_letter_id = str(uuid4())
    receipt = RedriveReceipt(
        dead_letter_id=dead_letter_id,
        job_id=str(uuid4()),
        correlation_id="request-24",
        stream="JOBS",
        sequence=42,
        redrive_count=1,
        audit_chain=(dead_letter_id,),
        duplicate=True,
    )

    cognistore_cli._render_redrive(receipt, json_output=False)

    assert capsys.readouterr().out.startswith("already redriven ")


@pytest.mark.parametrize("protected", [False, True])
def test_serve_worker_lifecycle_and_live_limit_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protected: bool,
) -> None:
    events: list[str] = []
    callbacks = {}
    catalog_sentinel = object()
    feature_loader_sentinel = object()
    seen_feature_loader: object | None = None
    limits_path = tmp_path / "limits.yaml"
    limits_path.write_text("tiers:\n  hot:\n    source_concurrency: 1\n")
    reload_started = threading.Event()
    release_reload = threading.Event()
    reload_calls = 0
    real_load = cognistore_cli.load_throughput_config
    authorization_path = tmp_path / "authorization.json"
    authorization_path.write_text('{"bindings": []}')

    def delayed_load(*args, **kwargs):
        nonlocal reload_calls
        config = real_load(*args, **kwargs)
        reload_calls += 1
        if reload_calls == 1:
            reload_started.set()
            assert release_reload.wait(timeout=2)
        return config

    class FakeLoop:
        def add_signal_handler(self, signum, callback) -> None:
            events.append(f"add:{signum.name}")
            callbacks[signum] = callback

        def remove_signal_handler(self, signum) -> None:
            events.append(f"remove:{signum.name}")

        def run_in_executor(self, executor, function):
            assert executor is not None
            return asyncio.wrap_future(executor.submit(function))

    class FakeWorker:
        def __init__(
            self,
            queue,
            handlers,
            *,
            config,
            throughput=None,
            coordinator=None,
            audit_catalog=None,
            authorization=None,
            tenant_resolver=None,
        ) -> None:
            self.state = WorkerState.STARTING
            self.throughput = throughput
            assert coordinator is not None
            assert audit_catalog is catalog_sentinel
            assert isinstance(authorization, cognistore_cli.RBACAuthorizer) == protected

        async def start(self) -> None:
            self.state = WorkerState.RUNNING

        async def wait_for_shutdown_request(self) -> None:
            limits_path.write_text("tiers:\n  hot:\n    source_concurrency: 2\n")
            callbacks[signal.SIGHUP]()
            while not reload_started.is_set():
                await asyncio.sleep(0)
            limits_path.write_text("tiers:\n  hot:\n    source_concurrency: 3\n")
            callbacks[signal.SIGHUP]()
            release_reload.set()
            while self.throughput.config.tiers["hot"].source_concurrency != 3:
                await asyncio.sleep(0)

        async def shutdown(self):
            self.state = WorkerState.STOPPED
            return SimpleNamespace(graceful=True)

        def request_shutdown(self) -> None:
            return None

    class FakeHealth:
        scheme = "http"
        bound_port = 8123

        def __init__(self, worker, *, host, port) -> None:
            pass

        async def start(self) -> None:
            events.append("health:start")

        async def close(self) -> None:
            events.append("health:close")

    monkeypatch.setattr(cognistore_cli, "NatsJetStreamQueue", lambda config: object())
    monkeypatch.setattr(cognistore_cli, "AsyncWorker", FakeWorker)
    monkeypatch.setattr(cognistore_cli, "HealthServer", FakeHealth)
    monkeypatch.setattr(cognistore_cli, "load_throughput_config", delayed_load)
    monkeypatch.setattr(
        cognistore_cli,
        "load_policy_feature_loader",
        lambda path, catalog: feature_loader_sentinel,
    )

    def handlers(
        drivers,
        catalog,
        *,
        throughput=None,
        policy_feature_loader=None,
    ):
        nonlocal seen_feature_loader
        seen_feature_loader = policy_feature_loader
        return {}

    monkeypatch.setattr(
        cognistore_cli,
        "build_handlers",
        handlers,
    )
    monkeypatch.setattr(cognistore_cli.asyncio, "get_running_loop", lambda: FakeLoop())
    args = argparse.Namespace(
        nats_url=["nats://test:4222"],
        job_stream="JOBS",
        job_subject="jobs",
        job_consumer="workers",
        ack_wait=30.0,
        fetch_timeout=0.1,
        heartbeat_interval=1.0,
        shutdown_grace=2.0,
        settlement_timeout=1.0,
        once=True,
        health_host="127.0.0.1",
        health_port=0,
        drivers="runtime.yaml",
        authorization_policy=str(authorization_path) if protected else None,
        schedule_db=str(tmp_path / "schedule.db"),
        tier_limits=str(limits_path),
        _throughput_config=cognistore_cli.ThroughputConfig(
            tiers={"hot": cognistore_cli.TierLimits()}
        ),
    )

    assert (
        asyncio.run(
            cognistore_cli._serve_worker(
                args,
                {"hot": object()},
                catalog_sentinel,
            )
        )
        == 0
    )
    assert events == [
        "health:start",
        "add:SIGINT",
        "add:SIGTERM",
        "add:SIGHUP",
        "remove:SIGINT",
        "remove:SIGTERM",
        "remove:SIGHUP",
        "health:close",
    ]
    assert reload_calls == 2
    assert seen_feature_loader is feature_loader_sentinel


@pytest.mark.parametrize("explicit", [False, True])
def test_worker_authorization_policy_flag_overrides_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool,
) -> None:
    from cognistore.core.catalog import Catalog

    environment_path = tmp_path / "environment.json"
    explicit_path = tmp_path / "explicit.json"
    monkeypatch.setenv("COGNISTORE_AUTHORIZATION_POLICY", str(environment_path))
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda path: {"hot": object()})
    monkeypatch.setattr(cognistore_cli, "open_catalog", lambda *args, **kwargs: Catalog())
    seen = []

    async def serve_worker(args, drivers, catalog):
        seen.append(args.authorization_policy)
        return 0

    monkeypatch.setattr(cognistore_cli, "_serve_worker", serve_worker)
    args = [
        "--no-config", "--drivers", "ignored.yaml", "--catalog-db",
        str(tmp_path / "catalog.db"), "worker", "--once",
    ]
    if explicit:
        args.extend(["--authorization-policy", str(explicit_path)])
    assert cognistore_cli.main(args) == 0
    assert seen == [str(explicit_path if explicit else environment_path)]


@pytest.mark.parametrize("contents", [None, "", "not-json", '{"bindings": "invalid"}'])
def test_worker_rejects_invalid_authorization_before_queue_or_schedule_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str | None,
) -> None:
    path = tmp_path / "authorization.json"
    if contents is not None:
        path.write_text(contents)

    def forbidden(*args, **kwargs):
        raise AssertionError("worker must validate authorization before acquiring resources")

    monkeypatch.setattr(cognistore_cli, "NatsJetStreamQueue", forbidden)
    monkeypatch.setattr(cognistore_cli, "SQLiteScheduleStore", forbidden)
    with pytest.raises(ValueError, match="Invalid RBAC policy"):
        asyncio.run(cognistore_cli._serve_worker(
            argparse.Namespace(authorization_policy=str(path)), {}, object(),
        ))


@pytest.mark.parametrize("explicit", [False, True])
def test_worker_tenant_policy_flag_overrides_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool,
) -> None:
    from cognistore.core.catalog import Catalog

    environment_path = tmp_path / "tenants-environment.json"
    explicit_path = tmp_path / "tenants-explicit.json"
    monkeypatch.setenv("COGNISTORE_TENANT_POLICY", str(environment_path))
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda path: {"hot": object()})
    monkeypatch.setattr(cognistore_cli, "open_catalog", lambda *args, **kwargs: Catalog())
    seen = []

    async def serve_worker(args, drivers, catalog):
        seen.append(args.tenant_policy)
        return 0

    monkeypatch.setattr(cognistore_cli, "_serve_worker", serve_worker)
    args = [
        "--no-config", "--drivers", "ignored.yaml", "--catalog-db",
        str(tmp_path / "catalog.db"), "worker", "--once",
    ]
    if explicit:
        args.extend(["--tenant-policy", str(explicit_path)])
    assert cognistore_cli.main(args) == 0
    assert seen == [str(explicit_path if explicit else environment_path)]


def test_worker_rejects_invalid_tenant_policy_before_acquiring_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("worker must validate tenant policy before acquiring resources")

    monkeypatch.setattr(cognistore_cli, "NatsJetStreamQueue", forbidden)
    monkeypatch.setattr(cognistore_cli, "SQLiteScheduleStore", forbidden)
    with pytest.raises(ValueError, match="Invalid tenant policy"):
        asyncio.run(cognistore_cli._serve_worker(
            argparse.Namespace(tenant_policy=str(tmp_path / "missing.json")), {}, object(),
        ))
