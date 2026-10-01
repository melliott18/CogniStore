"""Synthetic orchestration controls; these tests never qualify a live campaign."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from scripts import load_campaign as campaign
from scripts import load_observations as observations
from scripts import load_workload as workload


def configuration(tmp_path):
    telemetry = json.loads((Path(__file__).resolve().parents[2] / "release/load/telemetry.example.json").read_text())
    telemetry.update(trusted_ca_file="ca.pem", token_file="metrics.token")
    (tmp_path / "telemetry.json").write_text(json.dumps(telemetry))
    config = {
        "schema_version": 1, "run_id": "local-test-run", "retry_limit": 3,
        "bindings": {"source_revision": "a" * 40, "image_digest": "sha256:" + "b" * 64,
                     "configuration_sha256": "c" * 64, "specification_sha256": "d" * 64,
                     "dependency_manifest_sha256": "e" * 64, "corpus_manifest_sha256": "f" * 64,
                     "environment_id": "local-fixture"},
        "approval": {"operator": "fixture-operator", "specification_review": "fixture-spec",
                     "staging_review": "fixture-scope", "spend_review": "fixture-budget",
                     "faults_approved": True},
        "service": {"origin": "https://fixture.invalid", "ca_file": "ca.pem", "bucket": "fixture-bucket",
                    "token_files": {"pilot-a": "a.token", "pilot-b": "b.token"}},
        "telemetry_file": "telemetry.json", "induced_alert_ids": ["queue-stop"],
        "faults": [{"fault_id": name, "kind": name, "injection_argv": [sys.executable, "-c", "pass"],
                    "recovery_argv": [sys.executable, "-c", "pass"], "timeout_seconds": 2}
                   for name in ("saturation", "backend_throttle", "bounded_retry")],
        "evidence_export_argv": [sys.executable, "-c", "pass"],
    }
    # Public fault IDs follow the CLI's hyphenated identifier contract.
    for fault in config["faults"]:
        fault["fault_id"] = fault["fault_id"].replace("_", "-")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path, config


def resource(cohort, seconds):
    return {"cohort": cohort, "seconds": seconds,
            **dict.fromkeys(observations.RATIOS, .2), **dict.fromkeys(observations.DISKS, .7),
            **dict.fromkeys(observations.OBSERVATIONS, 0)}


class Collector:
    def __init__(self, _config):
        self.closed = False

    def sample(self, cohort, seconds):
        return resource(cohort, seconds)

    def close(self):
        self.closed = True


class Service:
    def __init__(self):
        self.stopped = False
        self.requests = []
        self.jobs = []
        self.cancelled_jobs = 0
        self.objects = 100000

    def inventory(self):
        return {"total_objects": self.objects, "uncertain_mutations": 0, "unsettled_jobs": 0}

    async def probe(self):
        return {"status": "passed"}

    async def __call__(self, request):
        self.requests.append(request)
        return workload.Result(True, "ok")

    async def background_once(self, kind, cycle, config=None):
        self.jobs.append((kind, cycle, config))
        return [{"status": "succeeded"}]

    async def move_all(self, direction):
        return {"status": "passed", "direction": direction}

    async def move_batch(self, direction, batch):
        return {"verified_objects": 200, "verified_moves": 200}


@pytest.fixture
def runner(tmp_path, monkeypatch):
    path, _ = configuration(tmp_path)
    config = campaign.load_config(path)
    monkeypatch.setattr(campaign, "SAMPLE_INTERVAL", .02)
    instance = campaign.Campaign(config, tmp_path / "run", collector_factory=Collector)
    try:
        yield instance
    finally:
        instance.evidence.close()


def records(runner, filename):
    path = runner.evidence.root / filename
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_preflight_validates_configuration_without_network_or_credentials(tmp_path, monkeypatch):
    path, _ = configuration(tmp_path)
    output = tmp_path / "preflight.json"
    monkeypatch.setattr(campaign.PrometheusCollector, "sample", lambda *_: pytest.fail("offline preflight contacted telemetry"))
    assert campaign.main(["preflight", "--config", str(path), "--output", str(output)]) == 0
    assert json.loads(output.read_text())["production_qualified"] is False
    before = output.read_bytes()
    with pytest.raises(SystemExit) as error:
        campaign.main(["preflight", "--config", str(path), "--output", str(output)])
    assert error.value.code == 2 and output.read_bytes() == before


@pytest.mark.parametrize("change", [
    lambda value: value["approval"].update(faults_approved=False),
    lambda value: value["service"].update(origin="http://fixture.invalid"),
    lambda value: value["faults"][0].update(injection_argv="echo private; unsafe"),
    lambda value: value.update(induced_alert_ids=["same", "same"]),
    lambda value: value["bindings"].update(source_revision="private-secret"),
])
def test_preflight_rejects_missing_scope_unsafe_transport_and_shell_strings(tmp_path, change):
    path, config = configuration(tmp_path)
    change(config)
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError) as error:
        campaign.load_config(path)
    assert "private-secret" not in str(error.value)


def test_normal_phase_waits_full_window_and_drains_accepted_jobs(runner):
    async def scenario():
        service = Service()
        release = asyncio.Event()

        async def background(kind, cycle, config=None):
            service.jobs.append((kind, cycle, config))
            try:
                await release.wait()
                return [{"status": "succeeded"}]
            except asyncio.CancelledError:
                service.cancelled_jobs += 1
                raise

        service.background_once = background
        started = time.monotonic()
        task = asyncio.create_task(runner._phase(service, None, "nominal", .06))
        await asyncio.sleep(.10)
        assert not task.done()  # Foreground ended, accepted background work still drains.
        release.set()
        await task
        assert time.monotonic() - started >= .10
        assert service.cancelled_jobs == 0 and len(service.jobs) == 2
        return service

    asyncio.run(scenario())
    phase = runner.campaign["phases"]["nominal"]
    assert phase["completed"]
    assert datetime.fromisoformat(phase["actual_ended_at"]) >= datetime.fromisoformat(phase["ended_at"])
    assert len(records(runner, "foreground.jsonl")) == 1
    assert records(runner, "auxiliary-telemetry.jsonl")  # Monitoring continued during drain.


def test_short_fast_phase_never_backdates_end_before_planned_boundary(runner):
    started = time.monotonic()
    asyncio.run(runner._phase(Service(), None, "burst", .05, background=False))
    assert time.monotonic() - started >= .05
    row = runner.campaign["phases"]["burst"]
    assert datetime.fromisoformat(row["actual_ended_at"]) >= datetime.fromisoformat(row["ended_at"])


def test_guarded_long_stage_stops_mutations_and_retains_resource_evidence(runner):
    collectors = []

    class Pressure(Collector):
        def __init__(self, config):
            super().__init__(config)
            self.calls = 0
            collectors.append(self)

        def sample(self, cohort, seconds):
            self.calls += 1
            row = resource(cohort, seconds)
            if self.calls >= 2:
                row["api_tmp_bytes"] = 256 * 1024**2 * .8
            return row

    runner.collector_factory = Pressure
    interrupted = []

    async def slow_seed():
        try:
            await asyncio.sleep(60)
        finally:
            interrupted.append(True)

    with pytest.raises(campaign.CampaignError, match="admissions_stopped"):
        asyncio.run(runner._guarded(Service(), slow_seed, "seed:fixture"))
    assert interrupted and not runner.controller.admit
    assert all(item.closed for item in collectors)
    assert records(runner, "auxiliary-telemetry.jsonl")[-1]["api_tmp_bytes"] > 0


@pytest.mark.parametrize("stage", ["guarded", "phase"])
def test_missing_initial_telemetry_never_admits_stage_or_phase_work(runner, stage):
    class Unknown(Collector):
        def sample(self, *_args):
            raise observations.ObservationError("telemetry_requires_single_sample")
    runner.collector_factory = Unknown
    entered = []
    async def operation():
        entered.append(True)
    service = Service()
    service.move_all = operation
    with pytest.raises(campaign.CampaignError, match="initial_telemetry_unavailable"):
        if stage == "guarded":
            asyncio.run(runner._guarded(service, operation, "seed:unknown"))
        else:
            asyncio.run(runner._phase(service, None, "capacity", .05, capacity_roundtrip=True))
    assert not entered and not service.requests
    assert not runner.controller.admit
    assert "telemetry_unavailable" in runner.controller.state["reasons"]
    assert "capacity" not in runner.campaign["phases"]


def test_monitor_failure_during_final_shutdown_prevents_completed_phase(runner, monkeypatch):
    async def late_monitor(_service, _collector, _started, done, **_kwargs):
        # The workload wins the primary wait. A final collector failure only
        # becomes visible after the completion path requests monitor shutdown.
        await done.wait()
        raise campaign.CampaignError("late_monitor_failure")
    monkeypatch.setattr(runner, "_monitor", late_monitor)
    with pytest.raises(campaign.CampaignError, match="late_monitor_failure"):
        asyncio.run(runner._phase(Service(), None, "nominal", .03, background=False))
    assert not runner.campaign["phases"]["nominal"]["completed"]


def test_phase_exception_preserves_original_planned_denominator(runner, monkeypatch):
    async def failing(settings, transport, record, **_kwargs):
        result = await transport(workload.request_for_sequence(0))
        record({"cohort": settings.cohort, "sequence": 0, "scheduled_seconds": 0,
                "success": result.success, "outcome": result.outcome, "operation": "get",
                "tenant": "pilot-a", "size_bytes": 4096, "elapsed_seconds": .01, "actual_seconds": .01})
        raise RuntimeError("private runtime details")

    monkeypatch.setattr(campaign.load_workload, "run", failing)
    with pytest.raises(RuntimeError):
        asyncio.run(runner._phase(Service(), None, "nominal", 7200, background=False))
    phase = runner.campaign["phases"]["nominal"]
    assert phase["duration_seconds"] == 7200 and not phase["completed"]
    assert len(records(runner, "foreground.jsonl")) == 1
    assert "private runtime details" not in (runner.evidence.root / "campaign.json").read_text()


def test_evidence_write_failure_is_not_reported_as_missing_telemetry(runner, monkeypatch):
    def failed(*_args, **_kwargs):
        raise OSError("fixture unwritable output")
    monkeypatch.setattr(runner.evidence, "row", failed)
    with pytest.raises(OSError, match="unwritable"):
        asyncio.run(runner._sample(Collector({}), "nominal", 0))


def test_background_drain_timeout_latches_and_records_incomplete_phase(runner, monkeypatch):
    monkeypatch.setattr(campaign, "DRAIN_TIMEOUT", .04)
    service = Service()
    async def stuck(*_args, **_kwargs):
        await asyncio.sleep(60)
    service.background_once = stuck
    with pytest.raises(campaign.CampaignError, match="background_drain_timeout"):
        asyncio.run(runner._phase(service, None, "nominal", .03))
    assert not runner.controller.admit
    assert not runner.campaign["phases"]["nominal"]["completed"]


@pytest.mark.parametrize("dispatch_delay", [0, .15])
def test_capacity_movement_extends_load_with_contiguous_original_slots(runner, monkeypatch, dispatch_delay):
    monkeypatch.setattr(campaign, "CAPACITY_EXTENSION", .1)
    service = Service()
    service.objects = 200000
    directions = []
    anchors = []
    forward_complete = asyncio.Event()
    reverse_started = asyncio.Event()
    reverse_complete = asyncio.Event()
    movement_complete = asyncio.Event()

    async def move(direction):
        directions.append(direction)
        if direction == "hot-to-warm":
            await forward_complete.wait()
        else:
            reverse_started.set()
            await reverse_complete.wait()
            movement_complete.set()
        return {"status": "passed"}

    original_run = workload.run

    async def segment(settings, transport, record, *, start_time):
        anchors.append(start_time)
        assert settings.offered_count == 1
        await asyncio.sleep(dispatch_delay)
        # Keep the real request generator, but give it a deterministic clock.
        # Scheduler lateness is covered separately by test_load_workload.
        summary = await original_run(settings, transport, record,
                                     clock=lambda: start_time, start_time=start_time)
        if len(anchors) == 2:
            forward_complete.set()
            await reverse_started.wait()
        elif len(anchors) == 3:
            reverse_complete.set()
            await movement_complete.wait()
        return summary

    monkeypatch.setattr(campaign.load_workload, "run", segment)
    service.move_all = move
    async def scenario():
        await asyncio.wait_for(runner._phase(service, None, "capacity", .1,
                                            background=False, capacity_roundtrip=True), 10)
    asyncio.run(scenario())
    rows = records(runner, "foreground.jsonl")
    assert directions == ["hot-to-warm", "warm-to-hot"]
    assert runner.campaign["phases"]["capacity"]["duration_seconds"] == pytest.approx(.3)
    assert [row["sequence"] for row in rows] == list(range(3))
    assert [row["scheduled_seconds"] for row in rows] == pytest.approx([0, .1, .2])
    assert [anchor - anchors[0] for anchor in anchors] == pytest.approx([0, .1, .2])
    assert service.requests == [workload.request_for_sequence(i) for i in range(3)]
    for row in rows:
        assert row["size_bytes"] == workload.request_for_sequence(row["sequence"]).size_bytes
    assert runner.campaign["phases"]["capacity"]["completed"]


def test_slow_movement_extends_complete_bins_and_never_shortens_evidence(runner, monkeypatch):
    monkeypatch.setattr(campaign, "MOVEMENT_SECONDS", .06)
    monkeypatch.setattr(campaign, "MOVEMENT_BIN", .05)
    monkeypatch.setattr(campaign, "MOVEMENT_BATCHES", 2)
    monkeypatch.setattr(campaign, "MOVEMENT_BATCH_INTERVAL", .02)
    service = Service()
    async def move(*_args):
        await asyncio.sleep(.045)
        return {"verified_objects": 200, "verified_moves": 200}
    service.move_batch = move
    asyncio.run(runner._movement(service, None, "hot-to-warm"))
    row = runner.campaign["movement_windows"][0]
    duration = (datetime.fromisoformat(row["ended_at"]) - datetime.fromisoformat(row["started_at"])).total_seconds()
    assert duration >= .1 and duration / .05 == pytest.approx(round(duration / .05))
    assert row["completed"]
    assert datetime.fromisoformat(row["actual_ended_at"]) >= datetime.fromisoformat(row["ended_at"])


def test_movement_already_at_destination_cannot_count_as_success(runner, monkeypatch):
    monkeypatch.setattr(campaign, "MOVEMENT_BATCHES", 1)
    service = Service()
    async def no_move(*_args):
        return {"verified_objects": 200, "verified_moves": 0}
    service.move_batch = no_move
    with pytest.raises(campaign.CampaignError, match="movement_batch_incomplete"):
        asyncio.run(runner._movement(service, None, "hot-to-warm"))
    assert not runner.campaign["movement_windows"][0]["completed"]


def test_repeated_fault_cancellation_still_waits_for_recovery(runner, monkeypatch):
    started, recovered = threading.Event(), threading.Event()

    def hooks(_name, _inject, _recover, _timeout, *, approved, cancel_event):
        assert approved
        started.set()
        try:
            cancel_event.wait(2)
        finally:
            time.sleep(.1)
            recovered.set()
        return {"status": "failed", "recovery": {"outcome": "ok"}}

    async def reconcile(*_args):
        return {"status": "passed"}

    async def phase(*_args, **_kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(campaign, "execute_fault", hooks)
    monkeypatch.setattr(runner, "_reconcile", reconcile)
    monkeypatch.setattr(runner, "_phase", phase)

    async def scenario():
        task = asyncio.create_task(runner._fault(Service(), None, runner.config["faults"][0]))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert recovered.is_set()

    asyncio.run(scenario())
    assert any(row["event"] == "fault_recovery_finished" for row in records(runner, "campaign-events.jsonl"))


def test_export_hook_nonzero_cannot_pass_even_if_it_wrote_valid_files(tmp_path, monkeypatch):
    path, _ = configuration(tmp_path)
    output = tmp_path / "run"

    class AlreadyCollected:
        def __init__(self, config, destination):
            self.evidence = campaign.Evidence(destination, config["run_id"], config["bindings"])
            self.campaign = {"status": "observations-collected"}

        async def run(self):
            self.evidence.document("campaign.json", self.campaign)
            self.evidence.close()
            return self.campaign

    monkeypatch.setattr(campaign, "Campaign", AlreadyCollected)
    monkeypatch.setattr(observations, "execute_hook", lambda *_args, **_kwargs: {"outcome": "exit_nonzero"})
    monkeypatch.setattr(campaign, "finalize", lambda directory: {
        "status": "passed" if json.loads((directory / "campaign.json").read_text())["status"] == "observations-collected" else "failed"})
    assert campaign.main(["run", "--config", str(path), "--output", str(output), "--run-staging"]) == 1
    assert json.loads((output / "campaign.json").read_text())["status"] == "failed"
    assert json.loads((output / "export-status.json").read_text())["outcome"] == "exit_nonzero"


def test_final_fault_stop_preserves_primary_phases_and_never_auto_resumes(runner, monkeypatch):
    ordering = []

    class Lifecycle(Service):
        def __init__(self, settings, _specs, _journal):
            super().__init__()
            self.objects = 200000 if settings.run_id.endswith("capacity") else 100000
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            pass
        async def preflight(self):
            return {"status": "passed"}
        seed = preflight
        normalize_churn = preflight
        reconcile = preflight
        async def cleanup(self):
            self.objects = 0
        async def export_job_observations(self):
            return {"jobs": [], "scans": [], "gaps": []}

    async def phase(_service, _collector, cohort, duration, *, measured=True, **_kwargs):
        ordering.append(f"phase:{cohort}:{measured}")
        if measured:
            runner.campaign["phases"][cohort] = {"duration_seconds": duration, "completed": True}

    async def movement(*_args):
        ordering.append("movement")

    async def fault(*_args):
        ordering.append("fault")
        runner.controller.stop("background_job_uncertain", runner._elapsed())
        raise campaign.CampaignError("admissions_stopped")

    runner.service_factory = Lifecycle
    monkeypatch.setattr(runner, "_phase", phase)
    monkeypatch.setattr(runner, "_movement", movement)
    monkeypatch.setattr(runner, "_fault", fault)
    result = asyncio.run(runner.run())
    assert ordering.index("phase:capacity:True") < ordering.index("fault")
    assert result["status"] == "failed" and result["stop_latched"] is True
    assert result["uncertain_mutations"] == 0
    assert all(result["phases"][name]["completed"] for name in ("nominal", "burst", "capacity"))
    assert any(row["event"] == "operator_resumption_required" for row in records(runner, "campaign-events.jsonl"))


def test_literal_hook_expansion_does_not_evaluate_shell_or_environment():
    value = campaign.expand_argv(["/bin/tool", "{output}", "{run_id}", "{binding_sha256}", "$(touch /tmp/bad)", "$TOKEN"],
                                 "/fixture/output", "fixture-run", "a" * 64)
    assert value[-2:] == ["$(touch /tmp/bad)", "$TOKEN"]
    assert value[1:4] == ["/fixture/output", "fixture-run", "a" * 64]
