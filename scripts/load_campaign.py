#!/usr/bin/env python3
"""Run the M5 load campaign against an explicitly selected isolated service.

No infrastructure, credentials or operator approvals are inferred. A real run
requires --run-staging and a reviewed configuration. Failed/partial runs retain
their frozen measurement windows and raw evidence; they never become a shorter
passing campaign. Rehearsal uses the real application with disposable local
storage and explicitly cannot qualify the staging topology.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Also support python scripts/load_campaign.py from an uninstalled checkout.
if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import load_corpus, load_qualification, load_workload
from scripts.load_observations import (
    ObservationError,
    PrometheusCollector,
    StopController,
    execute_fault,
)
from scripts.load_service import LoadService, ServiceSettings

HOSTED_CI = "skipped: user instruction; known GitHub billing/spending restriction"
SIZE_LIMIT = 1024 * 1024
SAMPLE_INTERVAL = 15
DRAIN_TIMEOUT = 600
CAPACITY_EXTENSION = 300
MOVEMENT_SECONDS = 5100
MOVEMENT_BATCHES = 51
MOVEMENT_BATCH_INTERVAL = 100
MOVEMENT_BIN = 300
TENANTS = ("pilot-a", "pilot-b")
IDENTIFIERS = {
    "source_revision": r"[0-9a-f]{40}", "image_digest": r"sha256:[0-9a-f]{64}",
    "configuration_sha256": r"[0-9a-f]{64}", "specification_sha256": r"[0-9a-f]{64}",
    "dependency_manifest_sha256": r"[0-9a-f]{64}", "corpus_manifest_sha256": r"[0-9a-f]{64}",
    "environment_id": r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}",
}


class CampaignError(ValueError):
    """Fixed diagnostics only, never service/credential/configuration values."""


def utc():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def read_json(path):
    with Path(path).open("rb") as source:
        raw = source.read(SIZE_LIMIT + 1)
    if len(raw) > SIZE_LIMIT:
        raise CampaignError("configuration_too_large")
    return load_qualification.decode(raw)


def positive(value, *, maximum=86400):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
        raise CampaignError("invalid_duration")
    return value


def path_from(directory, value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CampaignError("invalid_input_path")
    return (directory / value).resolve()


def load_config(path: Path):
    config = read_json(path)
    required = {"schema_version", "run_id", "bindings", "approval", "service",
                "telemetry_file", "faults", "evidence_export_argv", "retry_limit", "induced_alert_ids"}
    if not isinstance(config, dict) or set(config) != required or config["schema_version"] != 1:
        raise CampaignError("invalid_configuration_schema")
    if not isinstance(config["run_id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,47}", config["run_id"]):
        raise CampaignError("invalid_run_id")
    bindings = config["bindings"]
    if not isinstance(bindings, dict) or set(bindings) != set(IDENTIFIERS):
        raise CampaignError("invalid_bindings")
    for name, pattern in IDENTIFIERS.items():
        value = bindings[name]
        if (not isinstance(value, str) or not re.fullmatch(pattern, value)
                or re.search(r"REPLACE|TODO|CHANGEME|pending", value, re.I)):
            raise CampaignError("incomplete_bindings")
    approval = config["approval"]
    if not isinstance(approval, dict) or set(approval) != {
        "operator", "specification_review", "staging_review", "spend_review", "faults_approved",
    }:
        raise CampaignError("missing_reviews")
    if approval["faults_approved"] is not True or any(
        not isinstance(approval[name], str) or not approval[name].strip()
        or re.search(r"REPLACE|TODO|CHANGEME|pending", approval[name], re.I)
        for name in approval if name != "faults_approved"
    ):
        raise CampaignError("missing_reviews")
    service = config["service"]
    if not isinstance(service, dict) or set(service) != {"origin", "ca_file", "token_files", "bucket"}:
        raise CampaignError("invalid_service_configuration")
    if not isinstance(service["token_files"], dict) or set(service["token_files"]) != set(TENANTS):
        raise CampaignError("invalid_tenant_tokens")
    service["ca_file"] = path_from(path.parent, service["ca_file"])
    service["token_files"] = {k: path_from(path.parent, v) for k, v in service["token_files"].items()}
    # Validate origins and scope before creating output or touching the service.
    ServiceSettings(**service, run_id=config["run_id"])
    telemetry_path = path_from(path.parent, config["telemetry_file"])
    telemetry = read_json(telemetry_path)
    for key in ("trusted_ca_file", "token_file"):
        telemetry[key] = str(path_from(telemetry_path.parent, telemetry[key]))
    collector = PrometheusCollector(telemetry)
    collector.close()
    config["telemetry"] = telemetry
    alert_ids = config["induced_alert_ids"]
    if (not isinstance(alert_ids, list) or not alert_ids or len(alert_ids) > 100
            or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", v) for v in alert_ids)
            or len(set(alert_ids)) != len(alert_ids)):
        raise CampaignError("invalid_expected_alerts")
    if type(config["retry_limit"]) is not int or not 2 <= config["retry_limit"] <= 20:
        raise CampaignError("invalid_retry_bound")
    faults = config["faults"]
    if not isinstance(faults, list) or not 3 <= len(faults) <= 20:
        raise CampaignError("missing_faults")
    ids, kinds = set(), set()
    for fault in faults:
        if not isinstance(fault, dict) or set(fault) != {
            "fault_id", "kind", "injection_argv", "recovery_argv", "timeout_seconds",
        }:
            raise CampaignError("invalid_fault")
        if (not isinstance(fault["fault_id"], str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", fault["fault_id"])
                or fault["fault_id"] in ids or fault["fault_id"] == "burst-recovery"
                or fault["kind"] not in {"saturation", "backend_throttle", "bounded_retry"}):
            raise CampaignError("invalid_fault")
        ids.add(fault["fault_id"])
        kinds.add(fault["kind"])
        positive(fault["timeout_seconds"], maximum=600)
        for key in ("injection_argv", "recovery_argv"):
            validate_argv(fault[key])
    if kinds != {"saturation", "backend_throttle", "bounded_retry"}:
        raise CampaignError("missing_fault_kinds")
    validate_argv(config["evidence_export_argv"])
    config["configuration_input_sha256"] = sha(path.read_bytes())
    config["approval_sha256"] = sha(canonical(approval))
    return config


def validate_argv(argv):
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 128
            or not isinstance(argv[0], str) or not os.path.isabs(argv[0])
            or any(not isinstance(v, str) or "\x00" in v or len(v) > 8192 for v in argv)):
        raise CampaignError("invalid_hook_argv")


def expand_argv(argv, output, run_id, binding):
    """Three literal substitutions, without shell parsing or environment expansion."""
    return [v.replace("{output}", str(output)).replace("{run_id}", run_id)
            .replace("{binding_sha256}", binding) for v in argv]


class Evidence:
    def __init__(self, root, run_id, bindings):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.identity = {"run_id": run_id, "binding_sha256": sha(canonical({"run_id": run_id, "bindings": bindings}))}
        self.files = {}

    def row(self, filename, values):
        if Path(filename).name != filename:
            raise CampaignError("invalid_evidence_name")
        stream = self.files.get(filename)
        if stream is None:
            stream = (self.root / filename).open("x", encoding="utf-8")
            self.files[filename] = stream
        stream.write(json.dumps({**values, **self.identity}, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()

    def document(self, filename, values):
        # Our own status document is atomically refreshed; raw streams never rewind.
        temporary = self.root / (filename + ".partial")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(values, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.root / filename)

    def flush(self):
        for stream in self.files.values():
            stream.flush()
            os.fsync(stream.fileno())

    def close(self):
        self.flush()
        for stream in self.files.values():
            stream.close()
        self.files.clear()


class Campaign:
    def __init__(self, config, output, *, service_factory=LoadService,
                 collector_factory=PrometheusCollector, rehearsal=False, duration=10):
        self.config = config
        self.profile = read_json(load_qualification.PROFILE)
        self.evidence = Evidence(output, config["run_id"], config["bindings"])
        self.service_factory = service_factory
        self.collector_factory = collector_factory
        self.rehearsal = rehearsal
        self.duration = positive(duration, maximum=60) if rehearsal else None
        self.controller = StopController({
            "api_tmp_limit_bytes": 256 * 1024**2, "worker_tmp_limit_bytes": 256 * 1024**2,
            "api_shm_limit_bytes": 256 * 1024**2, "worker_shm_limit_bytes": 256 * 1024**2,
        })
        self.started = time.monotonic()
        self.cancel_fault = threading.Event()
        self._exported_jobs = set()
        self._exported_scans = set()
        self.campaign = {
            "schema_version": 1, "run_id": config["run_id"], "bindings": config["bindings"],
            "profile_sha256": sha(load_qualification.PROFILE.read_bytes()),
            "configuration_input_sha256": config.get("configuration_input_sha256"),
            "approval_sha256": config.get("approval_sha256"),
            "execution_scope": "local-rehearsal" if rehearsal else "isolated-staging",
            "started_at": utc(), "ended_at": None, "phases": {}, "movement_windows": [],
            "retry_limit": config["retry_limit"],
            "induced_alert_ids": config["induced_alert_ids"],
            "required_faults": [{"fault_id": "burst-recovery", "kind": "burst"}] + [
                {"fault_id": f["fault_id"], "kind": f["kind"]} for f in config["faults"]],
            "status": "running", "production_qualified": False, "hosted_ci": HOSTED_CI,
        }

    def _event(self, event, **details):
        self.evidence.row("campaign-events.jsonl", {"event": event, "observed_at": utc(), **details})

    def _elapsed(self):
        return time.monotonic() - self.started

    def _assert_admission(self, service):
        if not self.controller.admit or service.stopped:
            raise CampaignError("admissions_stopped")

    async def _sample(self, collector, cohort, elapsed, *, filename="telemetry.jsonl", stage=None):
        try:
            row = await asyncio.to_thread(collector.sample, cohort, elapsed)
        except (ObservationError, OSError, ValueError):
            self._event("telemetry_unavailable", cohort=cohort, stage=stage)
            self.controller.observe(None, self._elapsed())
            return None
        # Evidence writes and control failures must abort rather than masquerade
        # as a missing telemetry sample and permit further admissions.
        if stage is not None:
            row = {**row, "stage": stage}
        self.evidence.row(filename, row)
        self.controller.observe(row, self._elapsed())
        return row

    async def _monitor(self, service, collector, started, done, *, cohort="nominal",
                       stage="auxiliary", measured_duration=None):
        failed_probes, probe_at, next_sample = 0, 0, 0
        while not done.is_set():
            elapsed = time.monotonic() - started
            filename = ("telemetry.jsonl" if measured_duration is not None
                        and elapsed < measured_duration() else "auxiliary-telemetry.jsonl")
            await self._sample(collector, cohort, elapsed, filename=filename, stage=stage)
            self._assert_admission(service)
            if elapsed >= probe_at:
                try:
                    probe = await service.probe()
                    healthy = isinstance(probe, dict) and probe.get("status") == "passed"
                except Exception:
                    healthy = False
                failed_probes = 0 if healthy else failed_probes + 1
                self._event("external_probe", cohort=cohort, stage=stage, healthy=healthy)
                if failed_probes >= 3:
                    self.controller.stop("external_probes_failed", self._elapsed())
                probe_at = elapsed + 30
                self._assert_admission(service)
            # Missed slots remain missing. Never issue several same-slot samples
            # to disguise a slow collector or a blocked event loop.
            next_sample = (math.floor((time.monotonic() - started) / SAMPLE_INTERVAL) + 1) * SAMPLE_INTERVAL
            try:
                await asyncio.wait_for(done.wait(), max(.001, started + next_sample - time.monotonic()))
            except asyncio.TimeoutError:
                pass

    async def _guarded(self, service, operation, stage):
        """Monitor every long operation, including seeding and reconciliation."""
        self._assert_admission(service)
        collector = self.collector_factory(self.config["telemetry"])
        started, done = time.monotonic(), asyncio.Event()
        operation_task = monitor = None
        try:
            # Inspect headroom before admitting the first request of each stage.
            initial = await self._sample(collector, "nominal", 0, filename="auxiliary-telemetry.jsonl", stage=stage)
            if initial is None:
                self.controller.stop("telemetry_unavailable", self._elapsed())
                raise CampaignError("initial_telemetry_unavailable")
            self._assert_admission(service)
            operation_task = asyncio.create_task(operation())

            async def monitoring():
                try:
                    await asyncio.wait_for(done.wait(), SAMPLE_INTERVAL)
                except asyncio.TimeoutError:
                    await self._monitor(service, collector, started, done, stage=stage)

            monitor = asyncio.create_task(monitoring())
            finished, _ = await asyncio.wait({operation_task, monitor}, return_when=asyncio.FIRST_COMPLETED)
            if monitor in finished:
                monitor.result()
                if not operation_task.done():
                    raise CampaignError("monitor_ended_before_operation")
            result = await operation_task
            self._assert_admission(service)
            return result
        finally:
            done.set()
            for task in (operation_task, monitor):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (operation_task, monitor) if task is not None),
                                 return_exceptions=True)
            collector.close()
            self._event("stage_stopped", stage=stage, controller=self.controller.state)

    async def _reconcile(self, service, scope, boundary):
        result = await self._guarded(service, service.reconcile, f"reconcile:{scope}:{boundary}")
        self._event("reconciliation", scope=scope, boundary=boundary, result=result)
        if result.get("status") != "passed":
            self.controller.stop("integrity_failure", self._elapsed())
            raise CampaignError("integrity_failure")
        return result

    async def _checkpoint(self, service, cohort, boundary):
        collector = self.collector_factory(self.config["telemetry"])
        try:
            row = await self._sample(collector, cohort, 0, filename="resource-observations.jsonl",
                                     stage=f"{cohort}:{boundary}")
            self._assert_admission(service)
            self._event(f"resource_{boundary}", cohort=cohort, values=row)
            return row
        finally:
            collector.close()

    async def _export_worker(self, service, dataset):
        observed = await self._guarded(service, service.export_job_observations, f"worker-export:{dataset}")
        for field, filename, identity, retained in (
            ("jobs", "worker-jobs.jsonl", "job_id", self._exported_jobs),
            ("scans", "worker-scans.jsonl", "attempt_id", self._exported_scans),
        ):
            for row in observed[field]:
                if row[identity] not in retained:
                    self.evidence.row(filename, row)
                    retained.add(row[identity])
        self._event("worker_export_gaps", dataset=dataset, gaps=observed["gaps"])

    async def _phase(self, service, collector, cohort, duration, *, measured=True,
                     background=True, warmup=0, capacity_roundtrip=False):
        self._assert_admission(service)
        # Read headroom before starting either the offered-arrival clock or
        # concurrent movement. This separate collector cannot consume slot zero
        # from the measured stream or turn query setup into missed arrivals.
        initial_collector = self.collector_factory(self.config["telemetry"])
        try:
            initial = await self._sample(initial_collector, cohort, 0,
                                         filename="auxiliary-telemetry.jsonl", stage=f"phase-preflight:{cohort}")
            if initial is None:
                self.controller.stop("telemetry_unavailable", self._elapsed())
                raise CampaignError("initial_telemetry_unavailable")
            self._assert_admission(service)
        finally:
            initial_collector.close()
        started, start_utc = time.monotonic(), utc()
        segment_collector = self.collector_factory(self.config["telemetry"])
        planned_duration = duration
        phase = None
        if measured:
            if cohort in self.campaign["phases"]:
                raise CampaignError("duplicate_measured_phase")
            phase = {"started_at": start_utc, "duration_seconds": duration,
                     "ended_at": (datetime.fromisoformat(start_utc) + timedelta(seconds=duration)).isoformat(),
                     "warmup_seconds": warmup, "object_count": service.inventory()["total_objects"],
                     "completed": False}
            self.campaign["phases"][cohort] = phase
            self.evidence.document("campaign.json", self.campaign)
        stop_admissions, monitor_done = asyncio.Event(), asyncio.Event()
        failure_window = []
        rate = load_workload.PROFILES[cohort][0]
        tasks = []
        successful = False

        def record(row):
            self.evidence.row("foreground.jsonl" if measured else "auxiliary-foreground.jsonl", {
                **row, "measurement": cohort if measured else "auxiliary",
            })
            failure_window.append((row["scheduled_seconds"], row["success"]))
            cutoff = max(t for t, _ in failure_window) - 300
            failure_window[:] = [(t, good) for t, good in failure_window if t >= cutoff]
            if measured and cohort != "burst" and len(failure_window) >= 100:
                if sum(not good for _, good in failure_window) / len(failure_window) > .01:
                    self.controller.stop("foreground_failure_ratio", self._elapsed())

        async def transport(request):
            if not self.controller.admit or service.stopped or stop_admissions.is_set():
                return load_workload.Result(False, "adapter_error")
            return await service(request)

        async def roundtrip():
            for direction in ("hot-to-warm", "warm-to-hot"):
                self._assert_admission(service)
                result = await service.move_all(direction)
                self._event("capacity_movement", direction=direction, result=result)
            self._event("capacity_round_trip_complete", cohort=cohort)

        movement = asyncio.create_task(roundtrip()) if capacity_roundtrip else None
        if movement is not None:
            tasks.append(movement)

        async def foreground():
            nonlocal planned_duration
            offset, segment_duration, summaries = 0, duration, []
            while True:
                def shifted_record(row):
                    sequence = row["sequence"] + round(offset * rate)
                    descriptor = load_workload.request_for_sequence(sequence, self.profile["seed"])
                    shifted = {**row, "sequence": sequence,
                               "scheduled_seconds": row["scheduled_seconds"] + offset,
                               "operation": descriptor.operation, "tenant": descriptor.tenant}
                    shifted.pop("size_bytes", None)
                    shifted.pop("put_kind", None)
                    if descriptor.operation in {"get", "head", "put"}:
                        shifted["size_bytes"] = descriptor.size_bytes
                    if descriptor.put_kind:
                        shifted["put_kind"] = descriptor.put_kind
                    record(shifted)

                async def shifted_transport(request):
                    if offset:
                        request = load_workload.request_for_sequence(request.sequence + round(offset * rate), self.profile["seed"])
                    return await transport(request)

                # Explicit common clock anchor preserves original offered slots
                # across capacity extensions, including slow final responses.
                summary = await load_workload.run(
                    load_workload.Settings(cohort, segment_duration, self.profile["seed"]),
                    shifted_transport, shifted_record,
                    start_time=started + offset,
                )
                summaries.append(summary)
                await asyncio.sleep(max(0, started + planned_duration - time.monotonic()))
                if movement is None or movement.done():
                    if movement is not None:
                        movement.result()
                    stop_admissions.set()
                    return summaries
                # Freeze the next expected arrivals before issuing any extension.
                # Every original slot remains in the denominator on later failure.
                offset, segment_duration = planned_duration, CAPACITY_EXTENSION
                planned_duration += CAPACITY_EXTENSION
                if phase is not None:
                    phase["duration_seconds"] = planned_duration
                    phase["ended_at"] = (datetime.fromisoformat(start_utc)
                                         + timedelta(seconds=planned_duration)).isoformat()
                    self.evidence.document("campaign.json", self.campaign)
                self._event("capacity_window_extended", duration_seconds=planned_duration)

        async def background_jobs(kind, period):
            cycle = 0
            while not stop_admissions.is_set():
                self._assert_admission(service)
                scheduled = started + cycle * period
                delay = max(0, scheduled - time.monotonic())
                if delay:
                    try:
                        await asyncio.wait_for(stop_admissions.wait(), delay)
                        return
                    except asyncio.TimeoutError:
                        pass
                # At a capacity extension boundary the foreground task publishes
                # its next frozen window before another job may be submitted.
                while time.monotonic() >= started + planned_duration and not stop_admissions.is_set():
                    try:
                        await asyncio.wait_for(stop_admissions.wait(), .01)
                    except asyncio.TimeoutError:
                        pass
                if stop_admissions.is_set():
                    return
                self._assert_admission(service)
                if kind == "catalog.scan":
                    result = await service.background_once(kind, cycle)
                else:
                    result = await service.background_once(kind, cycle // 2, config={
                        "policy": "simple", "threshold": 1 if cycle % 2 == 0 else 16777217,
                        "allowed_tiers": ["hot", "warm"],
                    })
                self._event("background_scan" if kind == "catalog.scan" else "background_policy",
                            cohort=cohort, cycle=cycle, result=result)
                cycle += 1

        workload = asyncio.create_task(foreground())
        monitor = asyncio.create_task(self._monitor(
            service, segment_collector, started, monitor_done, cohort=cohort,
            stage=f"phase:{cohort}" if measured else f"auxiliary:{cohort}",
            measured_duration=(lambda: planned_duration) if measured else None,
        ))
        tasks.extend((workload, monitor))
        jobs = []
        if background:
            jobs.append(asyncio.create_task(background_jobs("catalog.scan", 300)))
            if not capacity_roundtrip:
                jobs.append(asyncio.create_task(background_jobs("policy.run", 3600)))
            tasks.extend(jobs)
        try:
            active = set(tasks)
            while not workload.done():
                finished, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                for task in finished:
                    task.result()
            summaries = workload.result()
            # Stop future admissions, then drain already accepted work while
            # resource monitoring continues. Cancellation is an abnormal path.
            stop_admissions.set()
            drain_deadline = time.monotonic() + DRAIN_TIMEOUT
            pending = {task for task in jobs if not task.done()}
            while pending:
                finished, _ = await asyncio.wait(pending | {monitor}, timeout=max(0, drain_deadline - time.monotonic()),
                                                return_when=asyncio.FIRST_COMPLETED)
                if not finished:
                    self.controller.stop("background_job_uncertain", self._elapsed())
                    raise CampaignError("background_drain_timeout")
                for task in finished:
                    task.result()
                    if task is monitor:
                        raise CampaignError("monitor_ended_during_drain")
                    pending.discard(task)
            for task in jobs:
                task.result()
            # A monitor may fail just after the foreground wins asyncio.wait,
            # including when no jobs remain to drain. Observe its final result
            # explicitly; cleanup's gather(return_exceptions=True) is not a
            # successful shutdown check. Any in-flight sample is still checked.
            monitor_done.set()
            try:
                await asyncio.wait_for(asyncio.shield(monitor), DRAIN_TIMEOUT)
            except asyncio.TimeoutError:
                self.controller.stop("telemetry_unavailable", self._elapsed())
                raise CampaignError("monitor_shutdown_timeout") from None
            self._assert_admission(service)
            self._event("foreground_complete", cohort=cohort, measured=measured, summaries=summaries)
            if phase is not None:
                phase["completed"] = True
            successful = True
        finally:
            stop_admissions.set()
            monitor_done.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            segment_collector.close()
            if phase is not None:
                phase["actual_ended_at"] = utc()
                phase["completed"] = successful
                self.evidence.document("campaign.json", self.campaign)
            self._event("phase_stopped", cohort=cohort, measured=measured,
                        controller=self.controller.state, inventory=service.inventory())
            self.evidence.flush()

    async def _movement(self, service, collector, direction):
        started, start_utc = time.monotonic(), utc()
        planned_duration = MOVEMENT_SECONDS
        window = {"window_id": direction, "direction": direction, "started_at": start_utc,
                  "ended_at": (datetime.fromisoformat(start_utc) + timedelta(seconds=planned_duration)).isoformat(),
                  "dedicated": True, "completed": False}
        self.campaign["movement_windows"].append(window)
        self.evidence.document("campaign.json", self.campaign)

        async def batches():
            nonlocal planned_duration
            for batch in range(MOVEMENT_BATCHES):
                self._assert_admission(service)
                await asyncio.sleep(max(0, started + batch * MOVEMENT_BATCH_INTERVAL - time.monotonic()))
                result = await service.move_batch(direction, batch)
                self._event("movement_batch", direction=direction, batch=batch, result=result)
                if result.get("verified_objects") != 200 or result.get("verified_moves") != 200:
                    raise CampaignError("movement_batch_incomplete")
            # Preserve all original five-minute bins. Slow work extends coverage
            # to the next complete bin; never backdate a shorter successful run.
            actual = time.monotonic() - started
            planned_duration = max(planned_duration, math.ceil(actual / MOVEMENT_BIN) * MOVEMENT_BIN)
            window["ended_at"] = (datetime.fromisoformat(start_utc) + timedelta(seconds=planned_duration)).isoformat()
            self.evidence.document("campaign.json", self.campaign)
            await asyncio.sleep(max(0, started + planned_duration - time.monotonic()))

        try:
            await self._guarded(service, batches, f"movement:{direction}")
            window["completed"] = True
        finally:
            window["actual_ended_at"] = utc()
            self.evidence.document("campaign.json", self.campaign)

    async def _fault(self, service, collector, fault):
        scope = f"fault:{fault['fault_id']}"
        await self._reconcile(service, scope, "before")
        expanded = [expand_argv(fault[key], self.evidence.root, self.config["run_id"],
                               self.evidence.identity["binding_sha256"])
                    for key in ("injection_argv", "recovery_argv")]
        self.cancel_fault.clear()
        task = asyncio.create_task(asyncio.to_thread(
            execute_fault, fault["fault_id"], *expanded, fault["timeout_seconds"],
            approved=True, cancel_event=self.cancel_fault,
        ))
        self._event("fault_started", fault_id=fault["fault_id"], kind=fault["kind"])
        cancelled = False
        try:
            while not task.done():
                await self._phase(service, collector, "burst", min(10, fault["timeout_seconds"]),
                                  measured=False, background=False)
            result = await asyncio.shield(task)
            self._event("fault_hooks_finished", fault_id=fault["fault_id"], result=result)
            if result["status"] != "passed":
                self.controller.stop("fault_recovery_failed", self._elapsed())
                raise CampaignError("fault_hook_failed")
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            self.cancel_fault.set()
            # Additional user cancellations must not abandon the approved
            # recovery hook. The hook has its own independent bounded timeout.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            result = task.result()
            self._event("fault_recovery_finished", fault_id=fault["fault_id"], result=result)
            if cancelled:
                raise asyncio.CancelledError
        await self._reconcile(service, scope, "after")

    async def run(self):
        self.campaign.update(stop_latched=False, uncertain_mutations=0)
        service = None
        try:
            self.evidence.document("campaign.json", self.campaign)
            for dataset in ("nominal", "capacity"):
                objects = 2000 if self.rehearsal else self.profile["phases"][dataset]["object_count"]
                service_settings = ServiceSettings(**self.config["service"],
                    run_id=f"{self.config['run_id']}-{dataset}", seed=self.profile["seed"])
                async with self.service_factory(service_settings, load_corpus.iter_object_specs(objects),
                        self.evidence.root / f"service-{dataset}.jsonl") as service:
                    try:
                        await self._guarded(service, service.preflight, f"preflight:{dataset}")
                        await self._guarded(service, service.seed, f"seed:{dataset}")
                        self._event("seeded", dataset=dataset, inventory=service.inventory())
                        warmup = 1 if self.rehearsal else self.profile["phases"][dataset]["warmup_seconds"]
                        await self._phase(service, None, dataset, warmup, measured=False,
                                          background=not self.rehearsal)
                        await self._guarded(service, service.normalize_churn, f"normalize-churn:{dataset}")
                        # Warm-up policy jobs may have changed placement. The
                        # measured campaign starts from a verified hot corpus.
                        await self._guarded(service, lambda: service.move_all("warm-to-hot"), f"reset:{dataset}")
                        await self._reconcile(service, f"phase:{dataset}", "before")
                        if service.inventory()["total_objects"] != objects:
                            raise CampaignError("corpus_count_mismatch")
                        await self._checkpoint(service, dataset, "baseline")
                        duration = self.duration if self.rehearsal else self.profile["phases"][dataset]["duration_seconds"]
                        await self._phase(service, None, dataset, duration, warmup=warmup,
                                          background=not self.rehearsal, capacity_roundtrip=dataset == "capacity")
                        await self._reconcile(service, f"phase:{dataset}", "after")
                        await self._checkpoint(service, dataset, "drain")
                        if dataset == "nominal":
                            await self._guarded(service, service.normalize_churn, "normalize-churn:burst")
                            await self._reconcile(service, "phase:burst", "before")
                            await self._reconcile(service, "fault:burst-recovery", "before")
                            burst = self.duration if self.rehearsal else self.profile["phases"]["burst"]["duration_seconds"]
                            await self._phase(service, None, "burst", burst, background=not self.rehearsal)
                            await self._reconcile(service, "phase:burst", "after")
                            await self._phase(service, None, "nominal", 1 if self.rehearsal else 600,
                                              measured=False, background=False)
                            await self._reconcile(service, "fault:burst-recovery", "after")
                            if not self.rehearsal:
                                await self._guarded(service, lambda: service.move_all("warm-to-hot"), "dedicated-movement-reset")
                                for direction in ("hot-to-warm", "warm-to-hot"):
                                    await self._movement(service, None, direction)
                        await self._export_worker(service, dataset)
                        if dataset == "capacity" and not self.rehearsal:
                            # Intentionally triggered admission stops are still
                            # latched. Keep prior measured evidence before drills
                            # that can require an explicit operator continuation.
                            for fault in self.config["faults"]:
                                await self._fault(service, None, fault)
                            await self._export_worker(service, dataset)
                        await self._reconcile(service, f"dataset:{dataset}", "after")
                        if dataset == "nominal":
                            await self._guarded(service, service.cleanup, f"cleanup:{dataset}")
                            self._event("dataset_cleaned", dataset=dataset)
                    finally:
                        self.campaign["uncertain_mutations"] += service.inventory().get("uncertain_mutations", 0)
            self.campaign["status"] = "observations-collected"
        except asyncio.CancelledError:
            self.controller.stop("campaign_interrupted", self._elapsed())
            self.campaign["status"] = "interrupted"
            self._event("campaign_interrupted")
            raise
        except Exception:
            self.campaign["status"] = "failed"
            if service is not None and service.stopped and self.controller.admit:
                self.controller.stop("adapter_integrity_failure", self._elapsed())
            self._event("campaign_failed", controller=self.controller.state)
            if not self.controller.admit:
                self._event("operator_resumption_required", controller=self.controller.state)
        finally:
            self.cancel_fault.set()
            self.campaign["stop_latched"] = not self.controller.admit
            self.campaign["ended_at"] = utc()
            self.evidence.document("campaign.json", self.campaign)
            self.evidence.close()
        return self.campaign


def finalize(output: Path):
    from scripts import load_acceptance
    campaign = read_json(output / "campaign.json")
    artifacts = {}
    for name in load_acceptance.FILES:
        path = output / name
        if path.is_file() and not path.is_symlink():
            checksum = hashlib.sha256()
            with path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    checksum.update(block)
            artifacts[name] = checksum.hexdigest()
    manifest = {"schema_version": 1, "run_id": campaign["run_id"],
                "campaign_sha256": sha((output / "campaign.json").read_bytes()), "artifacts": artifacts}
    (output / "acceptance-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    report = load_acceptance.evaluate(output)
    (output / "acceptance.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "run"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        if name == "run":
            command.add_argument("--run-staging", action="store_true", required=True)
    check = commands.add_parser("evaluate")
    check.add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    campaign = None
    for logger in ("httpx", "httpcore"):
        logging.getLogger(logger).setLevel(logging.CRITICAL)
    try:
        if args.command == "evaluate":
            report = finalize(args.input)
            print(f"load acceptance: {report['status']}")
            return 0 if report["status"] == "passed" else 1
        config = load_config(args.config.resolve())
        if args.command == "preflight":
            report = {"status": "prepared", "production_qualified": False,
                      "configuration_input_sha256": config["configuration_input_sha256"],
                      "run_id": config["run_id"], "hosted_ci": HOSTED_CI,
                      "limitations": ["Offline shape checks; supplied review references are not an attestation."]}
            with args.output.open("x") as target:
                json.dump(report, target, indent=2)
            return 0
        campaign = Campaign(config, args.output.resolve())
        result = asyncio.run(campaign.run())
        # Hooks export worker attempts, actual receipts and platform/cost evidence.
        # The hook gets only explicit arguments, and no shell interpolation.
        if result["status"] == "observations-collected":
            from scripts.load_observations import execute_hook
            argv = expand_argv(config["evidence_export_argv"], args.output.resolve(), config["run_id"],
                               campaign.evidence.identity["binding_sha256"])
            campaign.campaign["export_status"] = "running"
            campaign.evidence.document("campaign.json", campaign.campaign)
            export = execute_hook(argv, timeout_seconds=600, approved=True)
            (args.output / "export-status.json").write_text(json.dumps(export, indent=2) + "\n")
            campaign.campaign["export_status"] = export["outcome"]
            if export["outcome"] != "ok":
                campaign.campaign["status"] = "failed"
            campaign.evidence.document("campaign.json", campaign.campaign)
        report = finalize(args.output)
        print(f"load campaign: {result['status']}; acceptance: {report['status']}")
        return 0 if report["status"] == "passed" else 1
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        parser.exit(2, "load campaign inputs or evidence are invalid; existing evidence retained\n")
    except (KeyboardInterrupt, asyncio.CancelledError):
        if campaign is not None:
            campaign.campaign["status"] = "interrupted"
            campaign.campaign["ended_at"] = utc()
            try:
                campaign.evidence.document("campaign.json", campaign.campaign)
                finalize(args.output)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        parser.exit(130, "load campaign interrupted; partial evidence retained\n")


if __name__ == "__main__":
    raise SystemExit(main())
