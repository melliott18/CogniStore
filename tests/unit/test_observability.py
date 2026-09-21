"""Privacy, bounded labels and propagation contracts for process telemetry."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from uuid import UUID

import pytest
from opentelemetry import baggage, context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode, TraceState

from cognistore import observability as telemetry


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_tracer", provider.get_tracer("test"))
    yield exporter
    provider.shutdown()


def sample(name, labels=None):
    return telemetry.REGISTRY.get_sample_value(name, labels or {}) or 0


def test_operation_counts_success_and_failure_without_exception_content(spans):
    labels = {"component": "driver", "operation": "get_object", "outcome": "error"}
    before = sample("cognistore_operations_total", labels)
    with pytest.raises(ValueError, match="PRIVATE_DOCUMENT"):
        with telemetry.observe("driver", "get_object", backend="s3", bucket="SECRET_BUCKET"):
            raise ValueError("PRIVATE_DOCUMENT token=supersecret")
    span = spans.get_finished_spans()[0]
    assert span.name == "driver.get_object"
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description is None
    assert span.events == ()
    assert span.attributes == {"component": "driver", "operation": "get_object", "backend": "s3"}
    assert sample("cognistore_operations_total", labels) == before + 1
    body, content_type = telemetry.metrics_response()
    assert "text/plain" in content_type
    assert b"PRIVATE_DOCUMENT" not in body
    assert b"SECRET_BUCKET" not in body
    assert b"supersecret" not in body


def test_untrusted_labels_collapse_to_bounded_other_and_routes_are_templates(spans, monkeypatch):
    monkeypatch.setattr(telemetry, "_routes", set())
    telemetry.register_http_routes(["/v1/objects/{key}"])
    labels = {"component": "other", "operation": "other", "outcome": "success"}
    before = sample("cognistore_operations_total", labels)
    for index in range(10):
        with telemetry.observe(f"secret-{index}", f"path/{index}", backend=f"bucket-{index}"):
            pass
        telemetry.record_http_request("GET", f"/secret/{index}", 404, 0.02)
    telemetry.record_http_request("GET", "/v1/objects/{key}", 200, 0.01)
    body, _ = telemetry.metrics_response()
    assert sample("cognistore_operations_total", labels) == before + 10
    assert b"secret-" not in body
    assert b"/secret/" not in body
    assert all(span.attributes["backend"] == "other" for span in spans.get_finished_spans())
    assert b'route="/v1/objects/{key}"' in body
    assert b'route="unmatched"' in body


def test_trace_context_propagates_and_preserves_uuid_without_baggage(spans):
    correlation_id = "c24e7e76-0c66-4420-9f70-cea2687ebdbe"
    with telemetry.request_context(correlation_id):
        with telemetry.observe("api", "request", kind=SpanKind.SERVER) as server:
            token = context.attach(baggage.set_baggage("secret", "document-content"))
            try:
                carrier = telemetry.inject_trace_context()
            finally:
                context.detach(token)
            assert set(carrier) == {"traceparent"}
            server_context = server.get_span_context()
    assert telemetry.current_correlation_id() is None
    with telemetry.request_context(correlation_id, **carrier):
        with telemetry.observe("job", "move", kind="consumer") as worker:
            assert baggage.get_all() == {}
            assert worker.get_span_context().trace_id == server_context.trace_id
            with telemetry.observe("driver", "put_object"):
                pass
    server_span, driver_span, worker_span = spans.get_finished_spans()
    assert worker_span.parent.span_id == server_span.context.span_id
    assert worker_span.kind == SpanKind.CONSUMER
    assert driver_span.parent.span_id == worker_span.context.span_id
    assert worker_span.attributes["correlation_id"] == correlation_id


def test_context_without_parent_is_new_root_and_arbitrary_correlation_is_safe_uuid(spans):
    with telemetry.observe("api", "request") as outer:
        with telemetry.request_context("secret bucket/private-file"):
            safe_id = telemetry.current_correlation_id()
            assert str(UUID(safe_id)) == safe_id
            with telemetry.observe("job", "scan") as inner:
                assert inner.get_span_context().trace_id != outer.get_span_context().trace_id
        with telemetry.request_context("secret bucket/private-file", traceparent="secret-invalid"):
            assert telemetry.current_correlation_id() == safe_id
    assert spans.get_finished_spans()[0].parent is None


def test_trace_injection_drops_tracestate(spans):
    parent = trace.SpanContext(
        trace_id=123, span_id=456, is_remote=True, trace_state=TraceState([("vendor", "secret")]),
    )
    token = context.attach(trace.set_span_in_context(trace.NonRecordingSpan(parent)))
    try:
        assert set(telemetry.inject_trace_context()) == {"traceparent"}
    finally:
        context.detach(token)


def test_handled_failure_and_explicit_span_error_affect_metric_outcome(spans):
    labels = {"component": "job", "operation": "move", "outcome": "error"}
    before = sample("cognistore_operations_total", labels)
    with telemetry.observe("job", "move"):
        telemetry.mark_current_span_error()
    with telemetry.observe("job", "move") as span:
        span.set_status(Status(StatusCode.ERROR))
    assert sample("cognistore_operations_total", labels) == before + 2
    assert all(span.status.status_code == StatusCode.ERROR for span in spans.get_finished_spans())


def test_decorators_preserve_sync_async_generator_results_and_exceptions(spans):
    @telemetry.instrument("catalog", "read")
    def sync(value):
        return value

    @telemetry.instrument("queue", "enqueue", kind="producer")
    async def asynchronous(value):
        await asyncio.sleep(0)
        return value

    @telemetry.instrument("driver", "list_objects")
    def generator():
        yield "secret-object"
        raise RuntimeError("secret-contents")

    assert sync(42) == 42
    assert asyncio.run(asynchronous(43)) == 43
    iterator = generator()
    assert len(spans.get_finished_spans()) == 2
    assert next(iterator) == "secret-object"
    assert len(spans.get_finished_spans()) == 2
    with pytest.raises(RuntimeError, match="secret-contents"):
        next(iterator)
    assert len(spans.get_finished_spans()) == 3
    assert spans.get_finished_spans()[1].kind == SpanKind.PRODUCER
    assert spans.get_finished_spans()[2].status.status_code == StatusCode.ERROR


def test_nested_and_concurrent_scopes_do_not_leak_correlation_or_failure(spans):
    async def work(identifier):
        with telemetry.request_context(identifier):
            with telemetry.observe("job", "scan"):
                expected = telemetry.current_correlation_id()
                await asyncio.sleep(0)
                assert telemetry.current_correlation_id() == expected
                return expected

    async def run():
        return await asyncio.gather(work("first"), work("second"))

    first, second = asyncio.run(run())
    assert first != second
    assert telemetry.current_correlation_id() is None
    with telemetry.observe("job", "move"):
        with telemetry.observe("driver", "get_object"):
            telemetry.mark_current_span_error()
    assert spans.get_finished_spans()[-1].status.status_code != StatusCode.ERROR


def test_json_logs_omit_messages_exception_args_content_and_unknown_fields(spans):
    formatter = telemetry.StructuredLogFormatter()
    with telemetry.request_context("secret/request-id"):
        with telemetry.observe("api", "request"):
            record = logging.LogRecord("thirdparty", logging.ERROR, __file__, 1,
                                       "password=%s", ("SECRET_TOKEN",), None)
            record.exc_text = "SECRET_DOCUMENT traceback"
            record.telemetry_fields = {
                "component": "driver", "operation": "read", "backend": "secret-hostname",
                "duration_seconds": 0.1, "query": "SECRET_QUERY", "payload": "SECRET_BYTES",
                "correlation_id": "SECRET_OVERRIDE", "bytes": float("inf"),
            }
            rendered = formatter.format(record)
            data = json.loads(rendered)
    assert "SECRET" not in rendered
    assert "password" not in rendered
    assert data["event"] == "application.log"
    assert data["backend"] == "other"
    assert "trace_id" in data and "span_id" in data
    assert str(UUID(data["correlation_id"])) == data["correlation_id"]
    assert "bytes" not in data


def test_job_and_movement_metrics_have_explicit_units_and_finite_labels():
    labels = {"event": "succeeded", "operation": "move"}
    before = sample("cognistore_job_events_total", labels)
    byte_count = sample("cognistore_movement_bytes_total")
    telemetry.record_job_event("succeeded", "move")
    telemetry.record_job_event("private-job", "secret-operation")
    telemetry.record_movement_bytes(128)
    telemetry.record_movement_bytes(-1)
    telemetry.set_job_queue_depth(3, "pending")
    telemetry.set_job_queue_depth(2, "in_flight")
    telemetry.set_job_queue_depth(300, "private-state")
    telemetry.record_job_queue_latency(1.5, "move")
    telemetry.record_job_queue_latency(-1, "move")
    telemetry.record_job_queue_latency(float("nan"), "move")
    assert sample("cognistore_job_events_total", labels) == before + 1
    assert sample("cognistore_movement_bytes_total") == byte_count + 128
    assert sample("cognistore_job_queue_depth", {"state": "pending"}) == 3
    assert sample("cognistore_job_queue_depth", {"state": "in_flight"}) == 2
    telemetry.set_job_queue_depth(float("nan"), "pending")
    body, _ = telemetry.metrics_response()
    assert b'cognistore_job_queue_depth{state="pending"} NaN' in body
    assert b"private-state" not in body


@pytest.mark.parametrize(
    ("stored_bytes", "max_bytes", "expected"),
    [(0, 100, 0), (50, 100, 0.5), (80, 100, 0.8), (101, 100, 1.01)],
)
def test_queue_byte_capacity_uses_live_limit(stored_bytes, max_bytes, expected):
    telemetry.set_job_queue_byte_utilization(stored_bytes, max_bytes)
    assert sample("cognistore_job_queue_byte_utilization_ratio", {"scope": "main"}) == expected


@pytest.mark.parametrize(
    ("stored_bytes", "max_bytes"),
    [(None, 100), (0, None), (-1, 100), (0, -1), (0, 0), (False, 100), (0, True)],
)
def test_unknown_or_unbounded_queue_bytes_never_report_free_capacity(stored_bytes, max_bytes):
    telemetry.set_job_queue_byte_utilization(stored_bytes, max_bytes)
    assert math.isnan(sample("cognistore_job_queue_byte_utilization_ratio", {"scope": "main"}))


def test_disabled_configuration_does_not_construct_exporter(monkeypatch):
    from opentelemetry.exporter.otlp.proto.http import trace_exporter

    monkeypatch.setattr(telemetry, "_configured", False)
    monkeypatch.delenv("COGNISTORE_OTEL_ENABLED", raising=False)
    monkeypatch.delenv("COGNISTORE_LOG_FORMAT", raising=False)
    def unexpected(**kwargs):
        pytest.fail("disabled telemetry constructed an exporter")
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", unexpected)
    telemetry.configure_observability()
    telemetry.configure_observability()


def test_exporter_failure_and_bad_configuration_do_not_escape(monkeypatch, caplog):
    class BrokenExporter:
        def export(self, spans):
            raise RuntimeError("SECRET collector password")

        def shutdown(self):
            raise RuntimeError("SECRET collector password")

        def force_flush(self, timeout):
            raise RuntimeError("SECRET collector password")

    with caplog.at_level(logging.INFO, logger="cognistore.telemetry"):
        exporter = telemetry._SafeExporter(BrokenExporter())
        assert exporter.export([]) == SpanExportResult.FAILURE
        assert exporter.force_flush() is False
        exporter.shutdown()
        monkeypatch.setattr(telemetry, "_configured", False)
        monkeypatch.setenv("COGNISTORE_OTEL_ENABLED", "true")
        monkeypatch.setenv("COGNISTORE_OTEL_ENDPOINT", "https://SECRET:password@host/traces")
        telemetry.configure_observability()
    assert "SECRET" not in caplog.text
    assert "telemetry.configuration_failed" in caplog.text


def test_enabled_configuration_uses_safe_resource_and_background_exporter(monkeypatch):
    from opentelemetry.exporter.otlp.proto.http import trace_exporter

    exporter = InMemorySpanExporter()
    calls = []
    def construct(**kwargs):
        calls.append(kwargs)
        return exporter
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", construct)
    monkeypatch.setattr(telemetry, "_configured", False)
    monkeypatch.setattr(telemetry, "_provider", None)
    monkeypatch.setattr(telemetry, "_tracer", trace.NoOpTracer())
    monkeypatch.setenv("COGNISTORE_OTEL_ENABLED", "true")
    monkeypatch.setenv("COGNISTORE_OTEL_ENDPOINT", "http://localhost:4318/v1/traces")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "cognistore-worker")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "secret=PRIVATE_TOKEN")
    telemetry.configure_observability()
    telemetry.configure_observability()
    assert len(calls) == 1
    assert calls[0]["timeout"] == 2
    with telemetry.observe("job", "move"):
        pass
    assert telemetry._provider.force_flush(timeout_millis=1000)
    span = exporter.get_finished_spans()[0]
    assert span.resource.attributes == {"service.name": "cognistore-worker"}
    telemetry._provider.shutdown()


def test_registered_http_routes_are_capped_and_reject_invalid_templates(monkeypatch):
    monkeypatch.setattr(telemetry, "_routes", set())
    telemetry.register_http_routes(["invalid", "/" + "a" * 201])
    telemetry.register_http_routes(f"/endpoint-{index}" for index in range(101))
    assert len(telemetry._routes) == 100
    assert "/endpoint-100" not in telemetry._routes


def test_generator_send_throw_and_close_restore_callers_context(spans):
    @telemetry.instrument("driver", "list_objects")
    def iterator():
        value = yield "ready"
        try:
            yield value
        except ValueError:
            yield "recovered"

    with telemetry.observe("api", "request") as parent:
        stream = iterator()
        assert next(stream) == "ready"
        assert trace.get_current_span() is parent
        assert stream.send("value") == "value"
        assert trace.get_current_span() is parent
        assert stream.throw(ValueError("SECRET")) == "recovered"
        assert trace.get_current_span() is parent
        stream.close()
        assert trace.get_current_span() is parent
    assert len(spans.get_finished_spans()) == 2
    assert spans.get_finished_spans()[0].parent.span_id == parent.get_span_context().span_id
    assert spans.get_finished_spans()[0].events == ()


def test_json_configuration_redacts_service_and_exporter_logging(monkeypatch):
    monkeypatch.setattr(telemetry, "_configured", False)
    monkeypatch.setenv("COGNISTORE_LOG_FORMAT", "json")
    monkeypatch.delenv("COGNISTORE_OTEL_ENABLED", raising=False)
    names = ("cognistore.telemetry", "uvicorn", "uvicorn.error", "uvicorn.access", "opentelemetry")
    for name in names:
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "handlers", [])
        monkeypatch.setattr(logger, "level", logger.level)
        monkeypatch.setattr(logger, "propagate", logger.propagate)
    telemetry.configure_observability()
    for name in names:
        logger = logging.getLogger(name)
        assert not logger.propagate
        assert len(logger.handlers) == 1
        record = logging.LogRecord(name, logging.ERROR, __file__, 1, "SECRET %s", ("token",), None)
        assert "SECRET" not in logger.handlers[0].format(record)
    record = logging.LogRecord("exporter", logging.WARNING, __file__, 1,
                               "SECRET %s", ("response",), None)
    record.exc_text = "SECRET traceback"
    assert telemetry._RedactExporterLogs().filter(record)
    assert record.getMessage() == "OpenTelemetry export failed"
    assert record.exc_text is None
