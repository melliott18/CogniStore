"""Recovery waits must retain persistence failures and a finite deadline."""

import hashlib
import json
import urllib.error

import pytest

from scripts.kubernetes import exercise, fixtures


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_restart_read_records_transient_failures_before_first_response(tmp_path, monkeypatch):
    clock = Clock()
    calls = []
    failures = [TimeoutError(), urllib.error.HTTPError("fixture", 503, "unavailable", {}, None)]

    def request(base, path, **kwargs):
        calls.append((base, path, kwargs))
        if failures:
            raise failures.pop(0)
        return b"persisted"

    monkeypatch.setattr(exercise, "request", request)
    output = tmp_path / "recovery.json"
    assert exercise.read_after_restart("fixture", "/object", output,
                                       clock=clock, sleep=clock.sleep) == b"persisted"
    assert calls == [("fixture", "/object", {"raw": True, "timeout": 10})] * 3
    report = json.loads(output.read_text())
    assert report["status"] == "response_received"
    assert report["attempts"][0]["error_type"] == "TimeoutError"
    assert report["attempts"][1]["http_status"] == 503
    assert report["elapsed_seconds"] == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
def test_restart_read_does_not_retry_permanent_or_application_errors(tmp_path, monkeypatch, status):
    calls = []

    def request(*_args, **_kwargs):
        calls.append(True)
        raise urllib.error.HTTPError("fixture", status, "failure", {}, None)

    monkeypatch.setattr(exercise, "request", request)
    output = tmp_path / "recovery.json"
    with pytest.raises(urllib.error.HTTPError):
        exercise.read_after_restart("fixture", "/object", output)
    assert calls == [True]
    assert json.loads(output.read_text())["status"] == "failed"


@pytest.mark.parametrize("late_success", [False, True])
def test_restart_recovery_deadline_is_finite_and_records_failure(tmp_path, monkeypatch, late_success):
    clock = Clock()
    timeouts = []

    def request(*_args, timeout, **_kwargs):
        timeouts.append(timeout)
        clock.sleep(timeout)
        if late_success:
            clock.sleep(20)
            return b"too late"
        raise TimeoutError()

    monkeypatch.setattr(exercise, "request", request)
    output = tmp_path / "recovery.json"
    with pytest.raises(TimeoutError, match="deadline"):
        exercise.read_after_restart("fixture", "/object", output, timeout=25,
                                    clock=clock, sleep=clock.sleep)
    assert timeouts == ([10] if late_success else [10, 10, 3])
    report = json.loads(output.read_text())
    assert report["status"] == "failed" and report["deadline_seconds"] == 25


def test_recovered_read_cannot_hide_persisted_payload_corruption(tmp_path, monkeypatch):
    calls = []

    def request(*_args, **_kwargs):
        calls.append(True)
        return b"corrupted"

    monkeypatch.setattr(exercise, "request", request)
    output = tmp_path / "invariants.json"
    output.write_text(json.dumps({"sha256": hashlib.sha256(b"original").hexdigest()}))
    with pytest.raises(AssertionError):
        exercise.verify("fixture", output, recover_backends=True)
    assert calls == [True]


def test_minio_fixture_waits_for_http_readiness(capsys):
    fixtures.main()
    resources = json.loads(capsys.readouterr().out)["items"]
    deployment = next(item for item in resources
                      if item["kind"] == "Deployment" and item["metadata"]["name"] == "minio")
    probe = deployment["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
    assert probe == {"httpGet": {"port": 9000, "path": "/minio/health/ready"}, "periodSeconds": 2}
