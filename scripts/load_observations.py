#!/usr/bin/env python3
"""Private load telemetry, latched admission controls, and observed fault receipts.

No notification is sent here. Queries and fault commands are operator-supplied
configuration; their responses and command output never appear in public errors.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import math
import os
import queue
import re
import selectors
import signal
import ssl
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

try:
    from scripts.load_qualification import DISKS, OBSERVATIONS, PHASES, RATIOS, decode
except ModuleNotFoundError:  # Direct script/importlib invocation.
    from load_qualification import DISKS, OBSERVATIONS, PHASES, RATIOS, decode

METRICS = RATIOS + DISKS + OBSERVATIONS
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_HOOK_OUTPUT_BYTES = 65536
MEMORY_VOLUMES = ("api_tmp_bytes", "worker_tmp_bytes", "api_shm_bytes", "worker_shm_bytes")
SHA256 = re.compile(r"[a-f0-9]{64}")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


class ObservationError(ValueError):
    """Contains a fixed diagnostic code, never private endpoint or response text."""


CollectorError = ObservationError


def _number(value, *, minimum=0, maximum=None):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or value < minimum or maximum is not None and value > maximum):
        raise ObservationError("invalid_number")
    return value


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _identifier(value):
    if (not isinstance(value, str) or not IDENTIFIER.fullmatch(value)
            or re.search("REPLACE|TODO|CHANGEME|pending", value, re.I)):
        raise ObservationError("invalid_evidence_reference")
    return value


class PrometheusCollector:
    """Fetch one complete cohort row; unknown/ambiguous/stale data fails closed.

    Each metric needs ``value`` and ``timestamp`` PromQL expressions. The latter
    returns the oldest source sample's timestamp as its *value*, usually via
    ``min(timestamp(...))``. An instant query's outer timestamp alone cannot
    establish freshness for an aggregation. Both queries must yield one vector
    element. Credentials and trusted CA are reloaded for each collection.

    Connections ignore proxy and authentication environment variables, verify
    TLS and hostnames, refuse redirects, and bound response bytes and deadlines.
    Only literal loopback HTTP is available via an explicit local test option.
    """

    def __init__(self, config, *, allow_http_local=False, clock=time.time):
        try:
            self.endpoint = urlsplit(config["endpoint"])
            if (self.endpoint.username is not None or self.endpoint.password is not None
                    or self.endpoint.query or self.endpoint.fragment
                    or not self.endpoint.hostname or not self.endpoint.path
                    or any(c.isspace() for c in config["endpoint"])):
                raise ObservationError("invalid_telemetry_endpoint")
            if self.endpoint.scheme != "https":
                if not (allow_http_local is True and self.endpoint.scheme == "http"
                        and ipaddress.ip_address(self.endpoint.hostname).is_loopback):
                    raise ObservationError("telemetry_requires_verified_https")
            self.endpoint.port  # Reject malformed ports before collection.
            self.ca_file = Path(config["trusted_ca_file"]) if config.get("trusted_ca_file") else None
            self.token_file = Path(config["token_file"])
            self.interval = _number(config.get("interval_seconds", 15), minimum=0.1, maximum=15)
            self.max_age = _number(config.get("max_sample_age_seconds", 60), minimum=0.1, maximum=60)
            self.total_timeout = _number(config.get("total_timeout_seconds", 15), minimum=0.01, maximum=15)
            self.request_timeout = _number(config.get("request_timeout_seconds", 5), minimum=0.01, maximum=15)
            self.queries = config["queries"]
            if set(self.queries) != set(METRICS):
                raise ObservationError("incomplete_metric_mapping")
            for query in self.queries.values():
                if (not isinstance(query, dict) or set(query) != {"value", "timestamp"}
                        or any(not isinstance(expr, str) or not expr.strip()
                               or len(expr.encode()) > 8192 for expr in query.values())):
                    raise ObservationError("invalid_metric_mapping")
        except ObservationError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ObservationError("invalid_telemetry_configuration") from None
        self.clock = clock
        self._states = {}
        self._closed = False
        self._worker = None
        self._sample_lock = threading.Lock()

    def close(self):
        self._closed = True

    def _remaining(self, deadline):
        if self._closed:
            raise ObservationError("collector_closed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ObservationError("telemetry_deadline")
        return min(remaining, self.request_timeout)

    def _query(self, expression, queried_at, deadline, headers, context):
        connection = None
        try:
            options = {"host": self.endpoint.hostname, "port": self.endpoint.port,
                       "timeout": self._remaining(deadline)}
            if self.endpoint.scheme == "https":
                connection = http.client.HTTPSConnection(**options, context=context)
            else:
                connection = http.client.HTTPConnection(**options)
            connection.connect()
            connection.sock.settimeout(self._remaining(deadline))
            path = self.endpoint.path + "?" + urlencode({"query": expression, "time": queried_at})
            connection.request("GET", path, headers=headers)
            connection.sock.settimeout(self._remaining(deadline))
            response = connection.getresponse()
            if response.status != 200:
                raise ObservationError("telemetry_http_status")
            if (response.getheader("Content-Encoding", "identity") != "identity"
                    or response.getheader("Content-Type", "").split(";", 1)[0] != "application/json"):
                raise ObservationError("telemetry_response_type")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
                raise ObservationError("telemetry_response_too_large")
            chunks, size = [], 0
            while True:
                # HTTP/1.0 can detach connection.sock; the response still owns
                # the original socket until its body has been consumed.
                sock = connection.sock or getattr(getattr(response.fp, "raw", None), "_sock", None)
                if sock is not None:
                    sock.settimeout(self._remaining(deadline))
                self._remaining(deadline)
                chunk = response.read1(min(16384, MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ObservationError("telemetry_response_too_large")
                chunks.append(chunk)
            self._remaining(deadline)
            raw = b"".join(chunks)
            value = decode(raw)
            if (value.get("status") != "success" or value.get("warnings")
                    or value.get("infos") or value["data"]["resultType"] != "vector"):
                raise ObservationError("telemetry_query_unsuccessful")
            result = value["data"]["result"]
            if not isinstance(result, list) or len(result) != 1:
                raise ObservationError("telemetry_requires_single_sample")
            sample = result[0]["value"]
            if not isinstance(sample, list) or len(sample) != 2 or not isinstance(sample[1], str):
                raise ObservationError("telemetry_invalid_sample")
            observed_at = _number(sample[0])
            numeric = _number(float(sample[1]))
            if not 0 <= queried_at - observed_at <= self.max_age:
                raise ObservationError("telemetry_stale_or_future_sample")
            return numeric, {"response_timestamp": observed_at,
                             "response_sha256": hashlib.sha256(raw).hexdigest()}
        except ObservationError:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError,
                http.client.HTTPException, OverflowError):
            raise ObservationError("telemetry_query_failed") from None
        finally:
            if connection is not None:
                connection.close()

    def sample(self, cohort, seconds):
        if not self._sample_lock.acquire(blocking=False):
            raise ObservationError("telemetry_collection_busy")
        try:
            return self._sample(cohort, seconds)
        finally:
            self._sample_lock.release()

    def _sample(self, cohort, seconds):
        if self._closed:
            raise ObservationError("collector_closed")
        if cohort not in PHASES:
            raise ObservationError("unknown_cohort")
        seconds = _number(seconds)
        state = self._states.setdefault(cohort, {"last_attempt": -1, "last_sample": 0,
                                                "last_slot": -1, "max_gap_seconds": 0,
                                                "count": 0, "timestamps": {}})
        slot = math.floor(seconds / self.interval)
        if seconds <= state["last_attempt"] or slot <= state["last_slot"]:
            raise ObservationError("unordered_or_duplicate_telemetry")
        state["last_attempt"], state["last_slot"] = seconds, slot
        deadline = time.monotonic() + self.total_timeout
        if self._worker is not None and self._worker.is_alive():
            # An OS resolver can outlive socket timeouts. Permit at most one
            # pending daemon worker; never accumulate threads or admit stale
            # results. Its next deadline check closes the connection.
            raise ObservationError("telemetry_previous_query_pending")
        completed = queue.Queue(maxsize=1)

        def collect():
            try:
                completed.put(self._collect(cohort, seconds, deadline, state["timestamps"]))
            except ObservationError as error:
                completed.put(error)
            except Exception:
                completed.put(ObservationError("telemetry_collection_failed"))

        self._worker = threading.Thread(target=collect, daemon=True, name="pilot-private-telemetry")
        self._worker.start()
        self._worker.join(max(0, deadline - time.monotonic()))
        if self._worker.is_alive():
            raise ObservationError("telemetry_deadline")
        result = completed.get_nowait()
        if isinstance(result, ObservationError):
            raise result
        self._remaining(deadline)
        row, timestamps = result
        state["max_gap_seconds"] = max(state["max_gap_seconds"], seconds - state["last_sample"])
        state["last_sample"], state["timestamps"] = seconds, timestamps
        state["count"] += 1
        row["_source"]["max_gap_seconds"] = state["max_gap_seconds"]
        return row

    def _collect(self, cohort, seconds, deadline, previous_timestamps):
        try:
            with self.token_file.open("rb") as token_source:
                raw_token = token_source.read(8193)
            token = raw_token.decode("ascii").strip()
            if not token or len(raw_token) > 8192 or any(c.isspace() or ord(c) < 33 for c in token):
                raise ObservationError("invalid_telemetry_credentials")
            context = ssl.create_default_context(cafile=self.ca_file) if self.endpoint.scheme == "https" else None
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
                       "Accept-Encoding": "identity"}
            queried_at = _number(self.clock())
            row, metadata, timestamps = {"cohort": cohort, "seconds": seconds}, {}, {}
            for metric in METRICS:
                numeric, value_meta = self._query(self.queries[metric]["value"], queried_at, deadline, headers, context)
                source_time, time_meta = self._query(self.queries[metric]["timestamp"], queried_at, deadline, headers, context)
                if not 0 <= queried_at - source_time <= self.max_age:
                    raise ObservationError("telemetry_stale_or_future_source")
                if source_time < previous_timestamps.get(metric, 0):
                    raise ObservationError("telemetry_source_regressed")
                if metric in RATIOS + DISKS and numeric > 1:
                    raise ObservationError("telemetry_invalid_ratio")
                row[metric] = numeric
                timestamps[metric] = source_time
                metadata[metric] = {"sample_timestamp": source_time, "value": value_meta,
                                    "timestamp": time_meta}
            self._remaining(deadline)
            row["_source"] = {"queried_at": queried_at, "samples": metadata}
            return row, timestamps
        except ObservationError:
            raise
        except (OSError, ValueError, TypeError):
            raise ObservationError("telemetry_credentials_or_trust_unavailable") from None

    def summary(self, cohort, end_seconds):
        if cohort not in PHASES:
            raise ObservationError("unknown_cohort")
        end_seconds = _number(end_seconds)
        state = self._states.get(cohort, {"count": 0, "last_sample": 0, "max_gap_seconds": 0})
        if end_seconds < state["last_sample"]:
            raise ObservationError("unordered_telemetry_summary")
        return {"cohort": cohort, "samples": state["count"],
                "max_gap_seconds": max(state["max_gap_seconds"], end_seconds - state["last_sample"])}


class StopController:
    """Pure monotonic observations; a stopped controller never resumes itself.

    Call on every control tick, including ``observe(None, seconds)`` when the
    collector has no complete row. A fresh instance requires operator approval
    outside this module; there is deliberately no reset/resume method.
    ``seconds`` uses the campaign-wide monotonic clock; a row's own ``seconds``
    is phase-relative and is validated separately by the collector.
    """

    def __init__(self, limits):
        try:
            self.limits = {metric: _number(limits[metric.replace("_bytes", "_limit_bytes")], minimum=1)
                           for metric in MEMORY_VOLUMES}
            self.queue_max_bytes = _number(limits.get("queue_max_bytes", 1073741824), minimum=1)
        except (KeyError, TypeError):
            raise ObservationError("invalid_stop_limits") from None
        self._last_tick = -1
        self._last_sample = 0
        self._since = {}
        self._stopped_at = None
        self._reasons = set()
        self._warnings = set()

    @property
    def admit(self):
        return self._stopped_at is None

    @property
    def state(self):
        return {"admit": self.admit, "latched": not self.admit,
                "stopped_at_seconds": self._stopped_at,
                "reasons": sorted(self._reasons), "warnings": sorted(self._warnings)}

    def _stop(self, reason, seconds):
        self._reasons.add(reason)
        if self._stopped_at is None:
            self._stopped_at = seconds

    def stop(self, reason, seconds):
        """Latch an externally observed failure using a public fixed code."""
        if reason not in {"adapter_integrity_failure", "background_job_uncertain",
                          "manual_cancellation", "campaign_interrupted",
                          "telemetry_unavailable", "fault_recovery_failed",
                          "integrity_failure", "foreground_failure_ratio", "external_probes_failed"}:
            raise ObservationError("invalid_stop_reason")
        seconds = _number(seconds)
        if seconds < self._last_tick:
            raise ObservationError("unordered_stop_observation")
        self._last_tick = seconds
        self._stop(reason, seconds)
        return self.state

    def observe(self, row, seconds):
        seconds = _number(seconds)
        if seconds < self._last_tick:
            raise ObservationError("unordered_stop_observation")
        self._last_tick = seconds
        if row is None:
            self._warnings = {"telemetry_missing"}
            # Missing observations cannot demonstrate a continuous five-minute
            # RSS/age violation. Their own deadline still stops admission.
            self._since.clear()
            if seconds - self._last_sample > 300:
                self._stop("telemetry_missing_over_300_seconds", seconds)
            return self.state
        try:
            _number(row["seconds"])
            numbers = {metric: _number(row[metric]) for metric in METRICS}
            if any(numbers[metric] > 1 for metric in RATIOS + DISKS):
                raise ObservationError("invalid_ratio")
        except (KeyError, TypeError):
            raise ObservationError("incomplete_stop_observation") from None
        if seconds - self._last_sample > 300:
            self._stop("telemetry_missing_over_300_seconds", seconds)
        if seconds - self._last_sample > 60:
            self._since.clear()
        self._last_sample = seconds
        self._warnings.clear()
        for metric, limit in self.limits.items():
            ratio = numbers[metric] / limit
            if ratio >= .60:
                self._warnings.add(f"{metric}_60_percent")
            if ratio >= .75:
                self._stop(f"{metric}_75_percent", seconds)
        for metric in DISKS:
            if numbers[metric] < .30:
                self._stop(f"{metric}_below_30_percent", seconds)
        if numbers["queue_pending"] >= 8000:
            self._stop("queue_pending_8000", seconds)
        if numbers["queue_pending"] >= 5000:
            self._warnings.add("queue_pending_5000")
        if numbers["queue_bytes"] >= .80 * self.queue_max_bytes:
            self._stop("queue_bytes_80_percent", seconds)
        sustained = {"api_rss_80_percent": numbers["api_rss_ratio"] >= .80,
                     "worker_rss_80_percent": numbers["worker_rss_ratio"] >= .80,
                     "queue_oldest_over_300_seconds": numbers["queue_oldest_seconds"] > 300}
        for name, active in sustained.items():
            if active:
                self._since.setdefault(name, seconds)
                if seconds - self._since[name] >= 300:
                    self._stop(name, seconds)
            else:
                self._since.pop(name, None)
        return self.state


def _argv(argv):
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 128
            or any(not isinstance(arg, str) or "\x00" in arg or len(arg.encode()) > 8192 for arg in argv)
            or not os.path.isabs(argv[0])):
        raise ObservationError("invalid_fault_argv")


def _terminate(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def _hook(argv, timeout_seconds, cancel_event):
    started = time.monotonic()
    result = {"started_at": _utc(), "argv_sha256": hashlib.sha256(json.dumps(argv).encode()).hexdigest(),
              "outcome": "launch_failed", "returncode": None, "output_bytes": 0}
    process = None
    try:
        if cancel_event is not None and cancel_event.is_set():
            result["outcome"] = "cancelled"
            return result
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                                   start_new_session=True, shell=False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            os.set_blocking(process.stdout.fileno(), False)
            while selector.get_map() or process.poll() is None:
                elapsed = time.monotonic() - started
                if cancel_event is not None and cancel_event.is_set():
                    result["outcome"] = "cancelled"
                    _terminate(process)
                    break
                if elapsed >= timeout_seconds:
                    result["outcome"] = "timeout"
                    _terminate(process)
                    break
                for key, _ in selector.select(min(.05, timeout_seconds - elapsed)):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        result["output_bytes"] += len(chunk)
                        if result["output_bytes"] > MAX_HOOK_OUTPUT_BYTES:
                            result["outcome"] = "output_limit"
                            _terminate(process)
                            break
                if result["outcome"] == "output_limit":
                    break
            result["returncode"] = process.wait(timeout=5)
            if result["outcome"] == "launch_failed":
                result["outcome"] = "ok" if process.returncode == 0 else "exit_nonzero"
    except (OSError, ValueError, subprocess.SubprocessError):
        result["outcome"] = "execution_failed"
    finally:
        if process is not None:
            if process.poll() is None:
                _terminate(process)
            if process.stdout is not None:
                process.stdout.close()
        result["finished_at"] = _utc()
        result["duration_seconds"] = time.monotonic() - started
    return result


def execute_fault(name, injection_argv, recovery_argv, timeout_seconds, *, approved, cancel_event=None):
    """Run explicitly approved argv hooks; every started attempt invokes recovery.

    The same bounded timeout applies independently to injection and recovery.
    Cancellation terminates the complete injection process group. Recovery is
    deliberately not cancelled by that event. Hooks receive a minimal environment
    and must use explicit configuration/credential file arguments as necessary.
    """
    if approved is not True:
        raise ObservationError("fault_not_approved")
    _identifier(name)
    _argv(injection_argv)
    _argv(recovery_argv)
    timeout_seconds = _number(timeout_seconds, minimum=.01, maximum=600)
    result = {"name": name, "status": "failed"}
    try:
        result["injection"] = _hook(injection_argv, timeout_seconds, cancel_event)
    finally:
        result["recovery"] = _hook(recovery_argv, timeout_seconds, None)
    if result["injection"]["outcome"] == result["recovery"]["outcome"] == "ok":
        result["status"] = "passed"
    return result


def execute_hook(argv, timeout_seconds=600, *, approved):
    """Run an approved bounded evidence exporter with the same safe argv rules.

    Exporters must receive explicit file destinations in argv. Their stdout and
    stderr are discarded, never treated as evidence or copied into diagnostics.
    """
    if approved is not True:
        raise ObservationError("hook_not_approved")
    _argv(argv)
    timeout_seconds = _number(timeout_seconds, minimum=.01, maximum=600)
    return _hook(argv, timeout_seconds, None)


def _timestamp(value):
    if not isinstance(value, str):
        raise ObservationError("invalid_receipt_timestamp")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.utcoffset() is None or instant.utcoffset().total_seconds() != 0:
            raise ObservationError("invalid_receipt_timestamp")
        return instant.timestamp()
    except (ValueError, OverflowError):
        raise ObservationError("invalid_receipt_timestamp") from None


def validate_receipts(records, *, artifact_ids=None):
    """Validate actual receipt ledger syntax/timing; caller binds artifact hashes.

    Evidence cannot be invented from alert expressions or hooks returning zero.
    The caller must verify receipt/recovery artifact contents and source hashes
    independently. Returned checks contain only indexes, fixed names and timings.
    """
    report = {"status": "incomplete", "checks": [], "errors": []}
    seen, bindings, receipts = set(), None, set()
    try:
        for index, row in enumerate(records):
            alert_id = _identifier(row["alert_id"])
            if alert_id in seen:
                raise ObservationError("duplicate_alert_receipt")
            seen.add(alert_id)
            _identifier(row["receiver_id"])
            run_id = _identifier(row["run_id"])
            campaign = row["binding_sha256"]
            if not isinstance(campaign, str) or not SHA256.fullmatch(campaign):
                raise ObservationError("invalid_receipt_campaign")
            current_binding = (run_id, campaign)
            if bindings is not None and current_binding != bindings:
                raise ObservationError("receipt_binding_mismatch")
            bindings = current_binding
            for key in ("receipt_id", "recovery_receipt_id", "acknowledgement_id", "runbook_evidence_id"):
                evidence_id = _identifier(row[key])
                if evidence_id in receipts:
                    raise ObservationError("reused_receipt_artifact")
                receipts.add(evidence_id)
                if artifact_ids is not None and evidence_id not in artifact_ids:
                    raise ObservationError("receipt_artifact_missing")
            sha = row["source_evidence_sha256"]
            if not isinstance(sha, str) or not SHA256.fullmatch(sha):
                raise ObservationError("invalid_receipt_evidence_hash")
            runbook = urlsplit(row["runbook_url"])
            if (runbook.scheme != "https" or not runbook.hostname or runbook.username is not None
                    or runbook.password is not None or re.search("REPLACE|TODO|CHANGEME", row["runbook_url"], re.I)):
                raise ObservationError("invalid_receipt_runbook")
            stages = ("condition_started_at", "fired_at", "received_at", "acknowledged_at",
                      "runbook_executed_at", "cleared_at", "recovery_received_at")
            stamps = {key: _timestamp(row[key]) for key in stages}
            if any(stamps[left] > stamps[right] for left, right in zip(stages, stages[1:])):
                raise ObservationError("receipt_chronology")
            timings = {"delivery_seconds": stamps["received_at"] - stamps["fired_at"],
                       "acknowledgement_seconds": stamps["acknowledged_at"] - stamps["fired_at"],
                       "recovery_delivery_seconds": stamps["recovery_received_at"] - stamps["cleared_at"]}
            passed = (timings["delivery_seconds"] <= 300 and timings["acknowledgement_seconds"] <= 900
                      and timings["recovery_delivery_seconds"] <= 300)
            report["checks"].append({"record": index, "status": "passed" if passed else "failed", **timings})
    except ObservationError as error:
        report["errors"].append(str(error))
    except (KeyError, TypeError, ValueError, AttributeError):
        report["errors"].append("malformed_receipt_ledger")
    if not seen:
        report["errors"].append("missing_alert_receipts")
    if not report["errors"]:
        report["status"] = "passed" if all(check["status"] == "passed" for check in report["checks"]) else "failed"
    return report
