"""Local synthetic HTTP, process and receipt fixtures; no staging qualification."""

from __future__ import annotations

import copy
import hashlib
import http.server
import ipaddress
import json
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from scripts import load_observations as obs


@pytest.fixture
def prometheus(tmp_path, request):
    state = {"now": 1800000000., "age": 1, "status": 200, "count": 1,
             "value": ".5", "type": "vector", "bodies": [], "auth": [], "delay": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            state["auth"].append(self.headers.get("Authorization"))
            query = parse_qs(urlsplit(self.path).query)["query"][0]
            is_timestamp = query.startswith("timestamp:")
            value = str(state["now"] - state["age"]) if is_timestamp else state["value"]
            body = json.dumps({"status": "success", "data": {"resultType": state["type"],
                "result": [{"metric": {"private_label": "secret-series-value"},
                            "value": [state["now"], value]}] * state["count"]}}).encode()
            body = state.get("body", body)
            state["bodies"].append(body)
            time.sleep(state["delay"])
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if state["status"] == 302:
                self.send_header("Location", "/redirect-secret")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = getattr(request, "param", False)
    if tls:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pilot-test-ca")])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                       .public_key(key.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
                       .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                       .sign(key, hashes.SHA256()))
        ca_file, key_file = tmp_path / "ca.pem", tmp_path / "key.pem"
        ca_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(ca_file, key_file)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = tmp_path / "token"
    token.write_text("fixture-secret-token\n")
    config = {"endpoint": f"{'https' if tls else 'http'}://127.0.0.1:{server.server_port}/api/v1/query",
              "token_file": str(token), "total_timeout_seconds": 2,
              "queries": {metric: {"value": f"value:{metric}", "timestamp": f"timestamp:{metric}"}
                          for metric in obs.METRICS}}
    if tls:
        config["trusted_ca_file"] = str(ca_file)
    try:
        yield config, state, token
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def collector(fixture):
    config, state, _ = fixture
    return obs.PrometheusCollector(config, allow_http_local=True, clock=lambda: state["now"])


def resource(seconds=0):
    return {"cohort": "nominal", "seconds": seconds,
            **dict.fromkeys(obs.RATIOS, .3), **dict.fromkeys(obs.DISKS, .5),
            **dict.fromkeys(obs.OBSERVATIONS, 0)}


def controller():
    return obs.StopController({metric.replace("_bytes", "_limit_bytes"): 100 for metric in obs.MEMORY_VOLUMES})


def test_collector_exact_complete_contract_hashes_and_rotation(prometheus, monkeypatch):
    config, state, token = prometheus
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    instance = collector(prometheus)
    row = instance.sample("nominal", 0)
    assert set(row) == {"cohort", "seconds", "_source", *obs.METRICS}
    assert all(row[metric] == .5 for metric in obs.METRICS)
    assert row["_source"]["samples"][obs.METRICS[0]]["value"]["response_sha256"] == hashlib.sha256(state["bodies"][0]).hexdigest()
    assert row["_source"]["samples"][obs.METRICS[0]]["sample_timestamp"] == state["now"] - 1
    assert "secret-series-value" not in json.dumps(row)
    assert config["endpoint"] not in json.dumps(row)
    assert instance.summary("nominal", 45)["max_gap_seconds"] == 45
    token.write_text("rotated-fixture-token")
    state["now"] += 15
    row = instance.sample("nominal", 15)
    assert state["auth"][-1] == "Bearer rotated-fixture-token"
    assert row["_source"]["max_gap_seconds"] == 15
    assert instance.summary("nominal", 16)["samples"] == 2
    instance.close()
    with pytest.raises(obs.ObservationError, match="collector_closed"):
        instance.sample("nominal", 30)


@pytest.mark.parametrize("change,code", [
    ({"count": 0}, "telemetry_requires_single_sample"),
    ({"count": 2}, "telemetry_requires_single_sample"),
    ({"value": "NaN"}, "invalid_number"),
    ({"value": "+Inf"}, "invalid_number"),
    ({"value": "-1"}, "invalid_number"),
    ({"value": "1.01"}, "telemetry_invalid_ratio"),
    ({"age": 61}, "telemetry_stale_or_future_source"),
    ({"age": -1}, "telemetry_stale_or_future_source"),
    ({"status": 302}, "telemetry_http_status"),
    ({"status": 401}, "telemetry_http_status"),
    ({"type": "matrix"}, "telemetry_query_unsuccessful"),
    ({"body": b'{"private": "secret-series-value"}'}, "telemetry_query_unsuccessful"),
    ({"body": b'{"status":"success","status":"error"}'}, "telemetry_query_failed"),
    ({"body": b"[1]"}, "telemetry_query_failed"),
    ({"body": b"x" * (obs.MAX_RESPONSE_BYTES + 1)}, "telemetry_response_too_large"),
])
def test_collector_fails_closed_without_echoing_content(prometheus, change, code):
    instance = collector(prometheus)
    prometheus[1].update(change)
    with pytest.raises(obs.ObservationError, match=f"^{code}$") as error:
        instance.sample("nominal", 0)
    assert "secret" not in str(error.value)
    assert instance.summary("nominal", 100) == {"cohort": "nominal", "samples": 0, "max_gap_seconds": 100}


def test_collector_deadline_covers_whole_collection(prometheus):
    config, state, _ = prometheus
    config["total_timeout_seconds"] = .08
    state["delay"] = .04
    started = time.monotonic()
    with pytest.raises(obs.ObservationError):
        collector(prometheus).sample("nominal", 0)
    assert time.monotonic() - started < .5
    assert len(state["auth"]) < len(obs.METRICS)


def test_collector_deadline_bounds_blocked_resolver_and_pending_workers(prometheus, monkeypatch):
    prometheus[0]["total_timeout_seconds"] = .05
    instance = collector(prometheus)
    entered, release = threading.Event(), threading.Event()

    def blocked(*_args):
        entered.set()
        release.wait(2)
        raise obs.ObservationError("telemetry_deadline")

    monkeypatch.setattr(instance, "_query", blocked)
    started = time.monotonic()
    try:
        with pytest.raises(obs.ObservationError, match="telemetry_deadline"):
            instance.sample("nominal", 0)
        assert entered.is_set() and time.monotonic() - started < .2
        with pytest.raises(obs.ObservationError, match="telemetry_previous_query_pending"):
            instance.sample("nominal", 15)
        assert instance.summary("nominal", 100)["samples"] == 0
    finally:
        release.set()
        instance._worker.join(timeout=1)
        instance.close()


def test_collector_rejects_missing_mapping_insecure_remote_and_credentials(prometheus):
    config, _, token = prometheus
    with pytest.raises(obs.ObservationError, match="telemetry_requires_verified_https"):
        obs.PrometheusCollector(config)
    remote = {**config, "endpoint": "http://192.0.2.1/api/v1/query"}
    with pytest.raises(obs.ObservationError):
        obs.PrometheusCollector(remote, allow_http_local=True)
    missing = copy.deepcopy(config)
    del missing["queries"][obs.METRICS[0]]
    with pytest.raises(obs.ObservationError, match="incomplete_metric_mapping"):
        obs.PrometheusCollector(missing, allow_http_local=True)
    for secret in ("", "contains newline\nsecret", "x" * 8193):
        token.write_text(secret)
        with pytest.raises(obs.ObservationError, match="invalid_telemetry_credentials"):
            collector(prometheus).sample("nominal", 0)


def test_collector_gap_includes_failed_samples_and_tail(prometheus):
    instance = collector(prometheus)
    instance.sample("nominal", 0)
    with pytest.raises(obs.ObservationError, match="unordered_or_duplicate"):
        instance.sample("nominal", 14)
    prometheus[1]["count"] = 0
    with pytest.raises(obs.ObservationError):
        instance.sample("nominal", 15)
    prometheus[1]["count"] = 1
    row = instance.sample("nominal", 90)
    assert row["_source"]["max_gap_seconds"] == 90
    assert instance.summary("nominal", 200)["max_gap_seconds"] == 110
    assert instance.sample("capacity", 0)["cohort"] == "capacity"


def test_collector_rejects_regressed_source_timestamp_despite_fresh_query(prometheus):
    instance = collector(prometheus)
    instance.sample("nominal", 0)
    prometheus[1]["now"] += 15
    prometheus[1]["age"] = 30
    with pytest.raises(obs.ObservationError, match="telemetry_source_regressed"):
        instance.sample("nominal", 15)


def test_collector_tls_context_reloaded_and_verified(prometheus, monkeypatch):
    config, state, _ = prometheus
    config["endpoint"] = "https://prometheus.private.example/api/v1/query"
    config["trusted_ca_file"] = "/fixture/trust.pem"
    contexts = []
    original = ssl.create_default_context

    def context(*, cafile):
        assert str(cafile) == "/fixture/trust.pem"
        result = original()
        contexts.append(result)
        return result

    monkeypatch.setattr(obs.ssl, "create_default_context", context)
    instance = collector(prometheus)

    def query(expression, queried_at, deadline, headers, context):
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        value = queried_at - 1 if expression.startswith("timestamp:") else .5
        return value, {"response_timestamp": queried_at, "response_sha256": "a" * 64}

    monkeypatch.setattr(instance, "_query", query)
    instance.sample("nominal", 0)
    state["now"] += 15
    instance.sample("nominal", 15)
    assert len(contexts) == 2 and contexts[0] is not contexts[1]


@pytest.mark.parametrize("prometheus", [True], indirect=True)
def test_collector_real_tls_rejects_bad_trust_hostname_and_reloads_ca(prometheus):
    from pathlib import Path
    config, state, _ = prometheus
    instance = obs.PrometheusCollector(config, clock=lambda: state["now"])
    assert instance.sample("nominal", 0)["api_rss_ratio"] == .5
    no_trust = {key: value for key, value in config.items() if key != "trusted_ca_file"}
    with pytest.raises(obs.ObservationError, match="telemetry_query_failed"):
        obs.PrometheusCollector(no_trust, clock=lambda: state["now"]).sample("nominal", 0)
    wrong_host = {**config, "endpoint": config["endpoint"].replace("127.0.0.1", "localhost")}
    with pytest.raises(obs.ObservationError, match="telemetry_query_failed"):
        obs.PrometheusCollector(wrong_host, clock=lambda: state["now"]).sample("nominal", 0)
    Path(config["trusted_ca_file"]).write_text("invalid rotated CA")
    with pytest.raises(obs.ObservationError, match="telemetry_credentials_or_trust_unavailable"):
        instance.sample("nominal", 15)


@pytest.mark.parametrize("metric", obs.MEMORY_VOLUMES)
def test_memory_volume_warning_stop_and_permanent_latch(metric):
    instance = controller()
    row = resource()
    row[metric] = 60
    assert instance.observe(row, 0)["admit"]
    assert instance.state["warnings"] == [f"{metric}_60_percent"]
    row.update(seconds=15)
    row[metric] = 75
    assert not instance.observe(row, 15)["admit"]
    assert instance.state["stopped_at_seconds"] == 15
    assert not instance.observe(resource(30), 30)["admit"]
    assert instance.state["latched"]


@pytest.mark.parametrize("metric,value,reason", [
    ("queue_pending", 8000, "queue_pending_8000"),
    ("queue_bytes", .8 * 1073741824, "queue_bytes_80_percent"),
    *( (name, .29, f"{name}_below_30_percent") for name in obs.DISKS ),
])
def test_immediate_admission_stops(metric, value, reason):
    instance = controller()
    row = resource()
    row[metric] = value
    assert not instance.observe(row, 0)["admit"]
    assert reason in instance.state["reasons"]


@pytest.mark.parametrize("metric,value", [("api_rss_ratio", .8), ("worker_rss_ratio", .8),
                                           ("queue_oldest_seconds", 301)])
def test_sustained_stops_require_observed_five_minutes(metric, value):
    instance = controller()
    for second in range(0, 315, 15):
        row = resource(second)
        row[metric] = value
        state = instance.observe(row, second)
        assert state["admit"] is (second < 300)


def test_unknown_observations_not_zero_and_interrupted_sustained_window():
    instance = controller()
    for second in range(0, 300, 15):
        row = resource(second)
        row["api_rss_ratio"] = .8
        instance.observe(row, second)
    instance.observe(None, 300)
    row = resource(315)
    row["api_rss_ratio"] = .8
    assert instance.observe(row, 315)["admit"]
    assert instance.observe(None, 615)["admit"]
    assert not instance.observe(None, 616)["admit"]
    assert "telemetry_missing_over_300_seconds" in instance.state["reasons"]


def test_stop_external_failure_and_input_validation():
    instance = controller()
    assert not instance.stop("adapter_integrity_failure", 0)["admit"]
    assert not instance.observe(resource(15), 15)["admit"]
    with pytest.raises(obs.ObservationError, match="invalid_stop_reason"):
        instance.stop("private error text", 30)
    with pytest.raises(obs.ObservationError, match="unordered_stop_observation"):
        instance.observe(resource(0), 0)
    with pytest.raises(obs.ObservationError, match="incomplete_stop_observation"):
        controller().observe({"seconds": 0}, 0)


def test_controller_global_clock_can_span_phase_relative_rows():
    instance = controller()
    assert instance.observe(resource(0), 15)["admit"]
    assert instance.observe(resource(0), 30)["admit"]
    assert instance.observe(None, 330)["admit"]
    assert not instance.observe(None, 331)["admit"]


def hooks(tmp_path, script):
    recovery = tmp_path / "recovered"
    return [sys.executable, "-c", script], [sys.executable, "-c", f"from pathlib import Path;Path({str(recovery)!r}).write_text('done')"], recovery


def test_approved_hooks_run_no_shell_and_sanitize_output(tmp_path):
    inject, recover, marker = hooks(tmp_path, "print('private fixture output')")
    result = obs.execute_fault("worker-restart", inject, recover, 2, approved=True)
    assert result["status"] == "passed"
    assert result["injection"]["returncode"] == 0
    assert result["injection"]["output_bytes"] > 0
    assert result["recovery"]["duration_seconds"] >= 0
    assert "private fixture output" not in json.dumps(result)
    assert marker.read_text() == "done"


@pytest.mark.parametrize("script,timeout,outcome", [
    ("import sys;sys.exit(7)", 2, "exit_nonzero"),
    ("import time;time.sleep(60)", .5, "timeout"),
    ("import sys;sys.stdout.write('x'*1000000);sys.stdout.flush()", 2, "output_limit"),
])
def test_fault_recovery_always_runs_after_failure(tmp_path, script, timeout, outcome):
    inject, recover, marker = hooks(tmp_path, script)
    result = obs.execute_fault("dependency-loss", inject, recover, timeout, approved=True)
    assert result["status"] == "failed"
    assert result["injection"]["outcome"] == outcome
    assert result["recovery"]["outcome"] == "ok"
    assert marker.exists()
    assert result["injection"]["output_bytes"] <= obs.MAX_HOOK_OUTPUT_BYTES + 8192


def test_fault_cancellation_kills_descendants_and_runs_recovery(tmp_path):
    child_marker = tmp_path / "escaped"
    script = ("import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',"
              f"{('import time;from pathlib import Path;time.sleep(1);Path('+repr(str(child_marker))+').touch()')!r}"
              "]);time.sleep(60)")
    inject, recover, marker = hooks(tmp_path, script)
    cancelled = threading.Event()
    timer = threading.Timer(.15, cancelled.set)
    timer.start()
    try:
        result = obs.execute_fault("worker-loss", inject, recover, 2, approved=True, cancel_event=cancelled)
    finally:
        timer.join()
    assert result["injection"]["outcome"] == "cancelled"
    assert result["recovery"]["outcome"] == "ok" and marker.exists()
    time.sleep(1)
    assert not child_marker.exists()


def test_fault_interrupt_recovers_before_propagating(tmp_path, monkeypatch):
    inject, recover, marker = hooks(tmp_path, "pass")
    original = obs._hook
    def interrupted(argv, timeout_seconds, cancel_event):
        if argv == inject:
            raise KeyboardInterrupt
        return original(argv, timeout_seconds, cancel_event)
    monkeypatch.setattr(obs, "_hook", interrupted)
    with pytest.raises(KeyboardInterrupt):
        obs.execute_fault("worker-loss", inject, recover, 2, approved=True)
    assert marker.exists()


def test_failed_launch_still_recovers_and_failed_recovery_never_passes(tmp_path):
    inject, recover, marker = hooks(tmp_path, "pass")
    result = obs.execute_fault("worker-loss", [str(tmp_path / "absent-command")], recover, 2, approved=True)
    assert result["status"] == "failed"
    assert result["injection"]["outcome"] == "execution_failed"
    assert result["recovery"]["outcome"] == "ok" and marker.exists()
    result = obs.execute_fault("worker-loss", inject, [sys.executable, "-c", "raise SystemExit(7)"], 2, approved=True)
    assert result["status"] == "failed"
    assert result["injection"]["outcome"] == "ok"
    assert result["recovery"]["outcome"] == "exit_nonzero"


def test_unapproved_or_malformed_hook_never_executes(tmp_path):
    inject, recover, marker = hooks(tmp_path, "pass")
    with pytest.raises(obs.ObservationError, match="fault_not_approved"):
        obs.execute_fault("worker-loss", inject, recover, 2, approved=False)
    with pytest.raises(obs.ObservationError, match="invalid_fault_argv"):
        obs.execute_fault("worker-loss", "echo secret; rm anything", recover, 2, approved=True)
    assert not marker.exists()


def test_exporter_hook_requires_approval_and_bounds_private_output(tmp_path):
    inject, _recover, _marker = hooks(tmp_path, "print('fixture secret output')")
    with pytest.raises(obs.ObservationError, match="hook_not_approved"):
        obs.execute_hook(inject, approved=False)
    result = obs.execute_hook(inject, approved=True)
    assert result["outcome"] == "ok"
    assert "fixture secret" not in json.dumps(result)


def receipt():
    start = datetime(2026, 9, 29, tzinfo=timezone.utc)
    times = {field: (start + timedelta(seconds=seconds)).isoformat() for field, seconds in (
        ("condition_started_at", 0), ("fired_at", 120), ("received_at", 150),
        ("acknowledged_at", 180), ("runbook_executed_at", 200),
        ("cleared_at", 300), ("recovery_received_at", 330))}
    return {"alert_id": "queue-stop", "receiver_id": "operator-a", "run_id": "test-run",
            "binding_sha256": "b" * 64, "source_evidence_sha256": "c" * 64,
            "receipt_id": "receipt-1", "recovery_receipt_id": "recovery-1",
            "acknowledgement_id": "ack-1", "runbook_evidence_id": "runbook-1",
            "runbook_url": "https://runbook.example/queue", **times}


def test_receipt_real_timing_and_artifact_references_required():
    row = receipt()
    result = obs.validate_receipts([row], artifact_ids={"receipt-1", "recovery-1", "ack-1", "runbook-1"})
    assert result["status"] == "passed"
    assert result["checks"] == [{"record": 0, "status": "passed", "delivery_seconds": 30,
                                  "acknowledgement_seconds": 60, "recovery_delivery_seconds": 30}]
    assert obs.validate_receipts([row], artifact_ids=set())["status"] == "incomplete"
    assert obs.validate_receipts([row], artifact_ids={"receipt-1", "recovery-1"})["status"] == "incomplete"
    assert obs.validate_receipts([])["status"] == "incomplete"
    row["recovery_received_at"] = "2026-09-29T00:11:00Z"
    assert obs.validate_receipts([row])["status"] == "failed"


@pytest.mark.parametrize("field,value", [
    ("received_at", "2026-09-29T00:00:01Z"),
    ("received_at", "2026-09-29T00:02:30"),
    ("received_at", True),
    ("source_evidence_sha256", "private error text"),
    ("runbook_url", "http://unsafe.example/runbook"),
    ("runbook_url", "https://user:secret@example.test/runbook"),
    ("receiver_id", "TODO"),
    ("receipt_id", "private error text"),
    ("recovery_receipt_id", "receipt-1"),
])
def test_receipt_rejects_false_synthetic_claims_and_redacts_errors(field, value):
    row = receipt()
    row[field] = value
    result = obs.validate_receipts([row])
    assert result["status"] == "incomplete"
    assert "private error text" not in json.dumps(result)


def test_receipt_rejects_mixed_bindings_and_duplicates():
    first, second = receipt(), receipt()
    assert obs.validate_receipts([first, second])["errors"] == ["duplicate_alert_receipt"]
    second.update(alert_id="rss-stop", run_id="another-run")
    assert obs.validate_receipts([first, second])["errors"] == ["receipt_binding_mismatch"]


def test_example_telemetry_mapping_matches_all_required_fields():
    from pathlib import Path
    example = json.loads((Path(__file__).resolve().parents[2] / "release/load/telemetry.example.json").read_text())
    assert set(obs.PrometheusCollector(example).queries) == set(obs.METRICS)
