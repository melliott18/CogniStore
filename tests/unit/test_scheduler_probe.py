from __future__ import annotations

import json

import pytest

from cognistore.jobs import scheduler_probe


def test_completed_failed_cycle_stays_live_but_not_ready(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(scheduler_probe.time, "time", lambda: clock[0])
    path = tmp_path / "health.json"
    assert scheduler_probe.main(["--health-file", str(path)]) == 1
    scheduler_probe.write_status(path, ready=True)
    assert scheduler_probe.main(["--health-file", str(path), "--readiness"]) == 0
    scheduler_probe.write_status(path, ready=False)
    assert scheduler_probe.main(["--health-file", str(path)]) == 0
    assert scheduler_probe.main(["--health-file", str(path), "--readiness"]) == 1
    clock[0] += 31
    assert scheduler_probe.main(["--health-file", str(path)]) == 1
    scheduler_probe.write_status(path, ready=True)
    assert scheduler_probe.main(["--health-file", str(path), "--readiness"]) == 0
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("document", [
    "{", "null", "[]", '{}',
    json.dumps({"completed_at": True, "ready": True}),
    json.dumps({"completed_at": float("nan"), "ready": True}),
    json.dumps({"completed_at": 1000, "ready": "true"}),
    json.dumps({"completed_at": 2000, "ready": True}),
])
def test_probe_fails_closed_on_partial_invalid_or_future_status(tmp_path, monkeypatch, document):
    monkeypatch.setattr(scheduler_probe.time, "time", lambda: 1000.0)
    path = tmp_path / "health.json"
    path.write_text(document)
    assert not scheduler_probe.check_status(path, max_age=30)
