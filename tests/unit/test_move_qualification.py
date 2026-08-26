from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.perf.qualification import (
    PathConfig,
    QualificationConfig,
    SizeClass,
    _driver_description,
    _FaultController,
    _FaultInjectingDriver,
    _git_details,
    _latency_summary,
    _ObservedRetryAccumulator,
    _payload,
    _QualificationRunner,
    _size_for,
    parse_path,
    parse_size_class,
    run_qualification,
)


def _drivers_file(tmp_path: Path) -> Path:
    path = tmp_path / "drivers.yaml"
    path.write_text(
        """
tiers:
  hot:
    driver: posix
    path: {hot}
    chunk_size: 64
  warm:
    driver: posix
    path: {warm}
    chunk_size: 64
""".format(hot=tmp_path / "hot", warm=tmp_path / "warm"),
        encoding="utf-8",
    )
    return path


def _config(tmp_path: Path, **changes) -> QualificationConfig:
    values = {
        "drivers_path": _drivers_file(tmp_path),
        "catalog_path": tmp_path / "qualification.sqlite3",
        "output_path": tmp_path / "qualification.json",
        "run_id": "unit-run",
        "bucket": "qualification",
        "object_count": 5,
        "size_classes": (SizeClass(0), SizeClass(17, 2), SizeClass(129)),
        "paths": (PathConfig("posix", "hot", "warm"),),
        "workers": 2,
        "faults": "standard",
        "max_attempts": 3,
        "retry_base_delay": 0.03,
        "retry_max_delay": 0.03,
        "lease_seconds": 0.01,
        "worker_termination_timeout": 5.0,
    }
    values.update(changes)
    return QualificationConfig(**cast(Any, values))


def test_workload_parsing_and_generation_are_deterministic() -> None:
    assert parse_size_class("1024") == SizeClass(1024, 1)
    assert parse_size_class("4096:3") == SizeClass(4096, 3)
    assert parse_path("s3:hot:object") == PathConfig("s3", "hot", "object")

    sizes = (SizeClass(1, 2), SizeClass(4, 1))
    assert [_size_for(index, sizes) for index in range(7)] == [1, 1, 4, 1, 1, 4, 1]
    assert _payload(29, "path", 3, 100) == _payload(29, "path", 3, 100)
    assert _payload(29, "path", 3, 100) != _payload(29, "path", 4, 100)
    assert len(_payload(29, "path", 3, 100)) == 100


@pytest.mark.parametrize("value", ["", "abc", "1:x", "-1", "1:0"])
def test_invalid_size_specs_are_rejected(value: str) -> None:
    with pytest.raises(Exception):
        parse_size_class(value)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"object_count": 0}, "object_count"),
        ({"workers": True}, "workers"),
        ({"faults": "standard", "object_count": 3}, "at least 4"),
        ({"max_attempts": 1}, "at least 2 attempts"),
        ({"profile": "full", "object_count": 5}, "exactly 1,000,000"),
        (
            {
                "profile": "full",
                "object_count": 1_000_000,
                "size_classes": (SizeClass(0),),
            },
            "canonical mixed-size workload",
        ),
        ({"lease_seconds": 0.03}, "less than retry_base_delay"),
        ({"run_id": "bad/run"}, "run_id"),
    ],
)
def test_qualification_config_rejects_unsafe_values(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _config(tmp_path, **changes)


def test_nearest_rank_latency_summary_is_ordered() -> None:
    summary = _latency_summary([100.0, 1.0, 5.0, 20.0, 10.0])
    assert summary == {
        "min": 1.0,
        "p50": 10.0,
        "p95": 100.0,
        "p99": 100.0,
        "max": 100.0,
    }
    assert _latency_summary([]) == {
        "min": 0.0,
        "p50": 0.0,
        "p95": 0.0,
        "p99": 0.0,
        "max": 0.0,
    }


def test_git_provenance_falls_back_to_verified_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_git(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("tests.perf.qualification.subprocess.run", missing_git)
    monkeypatch.setenv("COGNISTORE_QUALIFICATION_GIT_REVISION", "abc123")
    monkeypatch.setenv("COGNISTORE_QUALIFICATION_GIT_DIRTY", "false")

    assert _git_details() == {
        "revision": "abc123",
        "dirty": False,
        "source": "environment",
    }


def test_driver_description_omits_endpoint_credentials_and_query() -> None:
    driver = SimpleNamespace(
        endpoint_url="https://user:token@example.test:9443/storage?signature=secret#fragment",
        region_name="us-test-1",
    )

    assert _driver_description(cast(Any, driver)) == {
        "type": "SimpleNamespace",
        "endpoint_url": "https://example.test:9443/storage",
        "region_name": "us-test-1",
    }


def test_reduced_posix_qualification_recovers_all_faults(tmp_path: Path) -> None:
    config = _config(tmp_path)

    report = run_qualification(config)

    assert report["schema"] == "cognistore.move-qualification"
    assert report["schema_version"] == 1
    assert report["status"] == "passed"
    assert report["acceptance_status"] == "reduced_scale_only"
    assert report["summary"]["logical_moves"] == 10
    assert report["summary"]["fault_scenarios"] == 4
    assert report["summary"]["recovered_fault_scenarios"] == 4
    assert report["summary"]["injected_failure_events"] == 6
    assert report["summary"]["failed_attempts"] == 6
    assert report["summary"]["observed_failure_objects"] == 0
    assert report["summary"]["silent_loss"] == 0
    assert report["summary"]["corruption"] == 0
    assert report["summary"]["acceptance_criteria"] == {
        "one_million_object_round_trip": "not_evaluated_reduced_scale",
        "injected_failure_idempotency_retry_and_source_retention": "passed",
        "environment_configuration_and_metrics_recorded": "passed",
    }

    path = report["paths"][0]
    assert path["status"] == "passed"
    assert path["current_phase"] == "completed"
    assert {item["kind"] for item in path["failures"]} == {
        "timeout",
        "throttling",
        "backend_unavailable",
        "worker_termination",
    }
    assert all(item["source_retained"] for item in path["failures"])
    assert all(item["recovered"] for item in path["failures"])
    backend_failure = next(
        item for item in path["failures"] if item["kind"] == "backend_unavailable"
    )
    assert backend_failure["bounded_retry_attempts"] == config.max_attempts
    assert backend_failure["retry_limit_reached"] is True
    assert backend_failure["recovery_attempts"] == 1
    timeout_failure = next(
        item for item in path["failures"] if item["kind"] == "timeout"
    )
    assert timeout_failure["after_publication"] is True
    worker_failure = next(
        item for item in path["failures"] if item["kind"] == "worker_termination"
    )
    assert worker_failure["termination_actions"]
    assert worker_failure["terminated_exit_code"] is not None
    if worker_failure["terminated_exit_code"] < 0:
        assert worker_failure["terminated_signal"].startswith("SIG")
    assert report["configuration"]["worker_termination_timeout_seconds"] == 5.0
    assert len(report["configuration"]["drivers_configuration_sha256"]) == 64
    assert report["drivers"]["hot"]["chunk_size"] == 64
    assert path["integrity"]["forward"]["verified_objects"] == 5
    assert path["integrity"]["reverse"]["verified_objects"] == 5
    assert path["integrity"]["reverse"]["absent_objects"] == 0
    assert path["idempotency"]["new_transitions_on_replay"] == 0
    for direction in ("forward", "reverse"):
        latency = path[direction]["latency_ms"]
        assert latency["min"] <= latency["p50"] <= latency["p95"]
        assert latency["p95"] <= latency["p99"] <= latency["max"]

    assert json.loads(config.output_path.read_text(encoding="utf-8")) == report
    assert len(list((tmp_path / "hot" / "qualification").rglob("*.bin"))) == 5
    assert list((tmp_path / "warm" / "qualification").rglob("*.bin")) == []


def test_failed_run_still_writes_evidence(tmp_path: Path) -> None:
    config = _config(tmp_path, faults="none", object_count=1)
    dirty_key = (
        tmp_path
        / "hot"
        / config.bucket
        / "qualification"
        / config.run_id
        / "posix"
        / "000000000000.bin"
    )
    dirty_key.parent.mkdir(parents=True)
    dirty_key.write_bytes(b"unexpected")

    report = run_qualification(config)

    assert report["status"] == "failed"
    assert report["acceptance_status"] == "reduced_scale_failed"
    assert report["error"]["type"] == "FileExistsError"
    assert report["paths"][0]["status"] == "failed"
    assert report["paths"][0]["current_phase"] == "namespace_check"
    assert report["summary"]["paths_started"] == 1
    assert report["summary"]["paths_completed"] == 0
    assert report["summary"]["silent_loss"] is None
    assert json.loads(config.output_path.read_text(encoding="utf-8")) == report


def test_failed_fault_criterion_cannot_emit_scale_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _QualificationRunner._move_with_retry

    def mark_timeout_source_unretained(
        runner: _QualificationRunner,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        outcome, evidence = original(runner, *args, **kwargs)
        if evidence.get("kind") == "timeout":
            evidence["source_retained"] = False
        return outcome, evidence

    monkeypatch.setattr(
        _QualificationRunner,
        "_move_with_retry",
        mark_timeout_source_unretained,
    )

    report = run_qualification(_config(tmp_path))

    assert report["status"] == "failed"
    assert report["acceptance_status"] == "reduced_scale_failed"
    assert report["paths"][0]["status"] == "failed"
    assert report["error"]["type"] == "AssertionError"
    assert (
        report["summary"]["acceptance_criteria"]
        ["injected_failure_idempotency_retry_and_source_retention"]
        == "failed_or_incomplete"
    )


def test_no_fault_campaign_does_not_claim_failure_recovery(tmp_path: Path) -> None:
    report = run_qualification(_config(tmp_path, faults="none", object_count=1))

    assert report["status"] == "passed"
    assert (
        report["summary"]["acceptance_criteria"]
        ["injected_failure_idempotency_retry_and_source_retention"]
        == "not_evaluated_no_faults"
    )


def test_unexpected_retry_exhaustion_preserves_attempt_evidence(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, faults="none", object_count=1, max_attempts=2)
    runner = _QualificationRunner(config)
    runner._prepare_catalog()
    path = config.paths[0]
    prefix = f"qualification/{config.run_id}/{path.name}/"
    controller = _FaultController()
    key = runner._key(prefix, 0)
    controller.arm(
        tier=path.destination_tier,
        operation="put_object_stream",
        bucket=config.bucket,
        key=key,
        kind="backend_unavailable",
        failures=config.max_attempts,
    )
    drivers = dict(runner.drivers)
    drivers[path.destination_tier] = _FaultInjectingDriver(
        path.destination_tier,
        runner.drivers[path.destination_tier],
        controller,
    )
    evidence: list[dict[str, Any]] = []
    try:
        runner._seed(path, prefix)
        with pytest.raises(RuntimeError, match="failed after 2 attempts"):
            runner._move_with_retry(
                path,
                prefix,
                0,
                forward=True,
                drivers=drivers,
                failure_sink=evidence.append,
            )
    finally:
        assert runner.catalog is not None
        runner.catalog.close()
        runner.catalog = None

    assert len(evidence) == 1
    assert evidence[0]["kind"] == "observed_failure"
    assert evidence[0]["attempts"] == 2
    assert evidence[0]["retry_limit_reached"] is True
    assert evidence[0]["source_retained"] is True
    assert evidence[0]["recovered"] is False
    assert evidence[0]["recovery_seconds"] is None
    assert len(evidence[0]["attempt_failures"]) == 2


def test_concurrent_terminal_failures_are_all_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_count = 8
    snapshot_threads: list[threading.Thread] = []
    original_to_dict = _ObservedRetryAccumulator.to_dict

    def track_snapshot_thread(
        accumulator: _ObservedRetryAccumulator,
    ) -> dict[str, Any]:
        snapshot_threads.append(threading.current_thread())
        return original_to_dict(accumulator)

    monkeypatch.setattr(
        _ObservedRetryAccumulator,
        "to_dict",
        track_snapshot_thread,
    )

    def terminal_failure(
        runner: _QualificationRunner,
        path: PathConfig,
        prefix: str,
        index: int,
        **kwargs: Any,
    ) -> Any:
        evidence = {
            "kind": "observed_failure",
            "attempts": 1,
            "retry_limit_reached": True,
            "attempt_failures": [
                {
                    "category": "unavailable",
                    "source_retained": True,
                }
            ],
            "source_retained": True,
            "recovered": False,
            "recovery_seconds": None,
        }
        failure_sink = kwargs["failure_sink"]
        failure_sink(evidence)
        raise RuntimeError(f"terminal failure for object {index}")

    monkeypatch.setattr(
        _QualificationRunner,
        "_move_with_retry",
        terminal_failure,
    )

    report = run_qualification(
        _config(
            tmp_path,
            faults="none",
            object_count=object_count,
            workers=4,
        )
    )

    assert report["status"] == "failed"
    assert report["paths"][0]["observed_retries"]["objects_with_failures"] == 8
    assert report["paths"][0]["observed_retries"]["failed_attempts"] == 8
    assert report["summary"]["observed_failure_objects"] == 8
    assert report["summary"]["failed_attempts"] == 8
    assert snapshot_threads
    assert all(thread is threading.main_thread() for thread in snapshot_threads)
