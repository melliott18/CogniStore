from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cognistore.cli import cognistore_cli
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.jobs.models import EnqueueReceipt, JobEnvelope
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
        cognistore_cli.main(
            ["--base", str(storage), "put", "bucket", "nested/key", str(source)]
        )
        == 0
    )

    output = tmp_path / "downloads" / "result.bin"
    assert (
        cognistore_cli.main(
            ["--base", str(storage), "get", "bucket", "nested/key", str(output)]
        )
        == 0
    )
    assert output.read_bytes() == b"payload"

    assert (
        cognistore_cli.main(
            ["--base", str(storage), "ls", "bucket", "--prefix", "nested/"]
        )
        == 0
    )
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
        cognistore_cli.main(
            ["--drivers", "ignored.yaml", "put", "bucket", "key", str(source)]
        )
        == 0
    )
    assert (
        cognistore_cli.main(
            ["--drivers", "ignored.yaml", "ls-tier", "hot", "bucket"]
        )
        == 0
    )
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


def test_serve_worker_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class FakeLoop:
        def add_signal_handler(self, signum, callback) -> None:
            events.append(f"add:{signum.name}")

        def remove_signal_handler(self, signum) -> None:
            events.append(f"remove:{signum.name}")

    class FakeWorker:
        def __init__(self, queue, handlers, *, config) -> None:
            self.state = WorkerState.STARTING

        async def start(self) -> None:
            self.state = WorkerState.RUNNING

        async def wait_for_shutdown_request(self) -> None:
            return None

        async def shutdown(self):
            self.state = WorkerState.STOPPED
            return SimpleNamespace(graceful=True)

        def request_shutdown(self) -> None:
            return None

    class FakeHealth:
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
    monkeypatch.setattr(cognistore_cli, "build_handlers", lambda drivers, catalog: {})
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
    )

    assert asyncio.run(cognistore_cli._serve_worker(args, {}, object())) == 0
    assert events == [
        "health:start",
        "add:SIGINT",
        "add:SIGTERM",
        "remove:SIGINT",
        "remove:SIGTERM",
        "health:close",
    ]
