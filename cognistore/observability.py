"""Bounded, content-free metrics, traces, and structured operational events.

Never pass object identifiers, payloads, paths, query text, or exception messages.
Unknown categorical values collapse to ``other``. Metrics are process-local;
applications opt into OTLP exporting and JSON logging at process startup.
"""

from __future__ import annotations

import inspect
import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar, copy_context
from datetime import datetime, timezone
from functools import wraps
from typing import Any, ParamSpec, TypeVar, cast
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from opentelemetry import context, trace
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client import generate_latest as _generate_latest

_COMPONENTS = frozenset({"api", "driver", "catalog", "index", "policy", "movement", "queue", "job"})
_OPERATIONS = frozenset({
    "request", "put_object", "get_object", "open_object_reader", "open_object_reader_if_generation",
    "put_object_stream", "delete_object", "delete_object_if_generation", "ensure_object_durable",
    "list_objects", "stat_object", "transaction", "read", "extract_bytes", "extract_stream",
    "replace", "delete", "search", "rebuild", "embed", "query", "plan", "evaluate", "execute",
    "move", "recover", "enqueue", "dead_letter", "redrive", "scan", "run_policy",
})
_BACKENDS = frozenset({"posix", "s3", "sqlite", "postgresql", "keyword", "embedding", "document"})
_JOB_OPERATIONS = frozenset({"scan", "move", "run_policy"})
_JOB_EVENTS = frozenset({
    "enqueued", "started", "succeeded", "failed", "retried", "dead_lettered", "redriven", "cancelled",
})
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_EVENTS = frozenset({
    "operation.completed", "http.request.completed", "job.event", "telemetry.configured",
    "telemetry.configuration_failed", "telemetry.export_failed", "application.log",
})
_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300)
REGISTRY = CollectorRegistry()
_OPERATIONS_TOTAL = Counter(
    "cognistore_operations_total", "Completed operations (count).",
    ("component", "operation", "outcome"), registry=REGISTRY,
)
_OPERATION_DURATION = Histogram(
    "cognistore_operation_duration_seconds", "Operation wall-clock duration (seconds).",
    ("component", "operation", "outcome"), buckets=_BUCKETS, registry=REGISTRY,
)
_HTTP_TOTAL = Counter(
    "cognistore_http_requests_total", "Completed HTTP requests (count).",
    ("method", "route", "status_class"), registry=REGISTRY,
)
_HTTP_DURATION = Histogram(
    "cognistore_http_request_duration_seconds", "HTTP response duration (seconds).",
    ("method", "route", "status_class"), buckets=_BUCKETS, registry=REGISTRY,
)
_JOB_TOTAL = Counter(
    "cognistore_job_events_total", "Job lifecycle events (count).",
    ("event", "operation"), registry=REGISTRY,
)
_JOB_DEPTH = Gauge(
    "cognistore_job_queue_depth", "Broker consumer messages (count); NaN when unavailable.",
    ("state",), registry=REGISTRY,
)
_JOB_LATENCY = Histogram(
    "cognistore_job_queue_latency_seconds", "Broker publication-to-current-claim age (seconds).",
    ("operation",), buckets=_BUCKETS, registry=REGISTRY,
)
_MOVEMENT_BYTES = Counter(
    "cognistore_movement_bytes_total", "Successfully transferred movement bytes (bytes).",
    registry=REGISTRY,
)
_correlation_id: ContextVar[str | None] = ContextVar("cognistore_correlation_id", default=None)
_operation_state: ContextVar[dict[str, bool] | None] = ContextVar("cognistore_operation", default=None)
_tracer = trace.get_tracer("cognistore")
_provider: Any = None
_configured = False
_config_lock = threading.Lock()
_routes: set[str] = set()
_propagator = TraceContextTextMapPropagator()
_logger = logging.getLogger("cognistore.telemetry")
P = ParamSpec("P")
R = TypeVar("R")


def _bounded(value: object, allowed: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "other"


def _correlation(value: object) -> str:
    if not isinstance(value, str) or not value:
        return str(uuid4())
    try:
        return str(UUID(value))
    except ValueError:
        return str(uuid5(NAMESPACE_URL, value))


def current_correlation_id() -> str | None:
    """Return the canonical UUID used only in logs/spans and queue propagation."""
    return _correlation_id.get()


@contextmanager
def request_context(
    correlation_id: str | None = None, traceparent: str | None = None,
) -> Iterator[None]:
    """Start an isolated inbound scope, accepting traceparent but no baggage/state.

    Arbitrary incoming correlation strings become deterministic UUIDs, avoiding
    reflection of secrets or content supplied in request headers or job metadata.
    An absent or invalid traceparent starts a new trace, never an ambient parent.
    """
    carrier = {"traceparent": traceparent} if isinstance(traceparent, str) else {}
    extracted = _propagator.extract(carrier, context=context.Context())
    token = context.attach(extracted)
    correlation_token = _correlation_id.set(_correlation(correlation_id))
    try:
        yield
    finally:
        _correlation_id.reset(correlation_token)
        context.detach(token)


def inject_trace_context() -> dict[str, str]:
    """Serialize only W3C traceparent; never propagate baggage or tracestate."""
    carrier: dict[str, str] = {}
    _propagator.inject(carrier)
    return {"traceparent": carrier["traceparent"]} if "traceparent" in carrier else {}


def _context_fields() -> dict[str, str]:
    result = {}
    identifier = current_correlation_id()
    if identifier:
        result["correlation_id"] = identifier
    span = trace.get_current_span().get_span_context()
    if span.is_valid:
        result.update(trace_id=format(span.trace_id, "032x"), span_id=format(span.span_id, "016x"))
    return result


def _safe_fields(fields: Mapping[str, object]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, allowed in (
        ("component", _COMPONENTS), ("operation", _OPERATIONS), ("backend", _BACKENDS),
        ("outcome", frozenset({"success", "error"})), ("method", _METHODS),
        ("status_class", frozenset({"1xx", "2xx", "3xx", "4xx", "5xx"})),
        ("job_event", _JOB_EVENTS),
    ):
        if name in fields:
            result[name] = _bounded(fields[name], allowed)
    if "route" in fields:
        result["route"] = _route(fields["route"])
    for name in ("duration_seconds", "bytes"):
        value = fields.get(name)
        if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
            result[name] = value
    return result


class StructuredLogFormatter(logging.Formatter):
    """JSON allowlist formatter that deliberately omits raw messages/tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "telemetry_fields", {})
        event = _bounded(record.msg, _EVENTS)
        data: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "event": "application.log" if event == "other" else event,
        }
        if isinstance(fields, Mapping):
            data.update(_safe_fields(fields))
        data.update(_context_fields())
        return json.dumps(data, separators=(",", ":"), allow_nan=False)


def log_event(event: str, **fields: object) -> None:
    """Emit an operational event with no raw message, arguments, or exceptions."""
    safe_event = _bounded(event, _EVENTS)
    _logger.info(
        "application.log" if safe_event == "other" else safe_event,
        extra={"telemetry_fields": _safe_fields(fields)},
    )


class _RedactExporterLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = "OpenTelemetry export failed"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


class _SafeExporter(SpanExporter):
    """Keep exporter failures outside application work, including flush/shutdown."""

    def __init__(self, exporter: Any) -> None:
        self.exporter = exporter

    def export(self, spans: Any) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        try:
            return self.exporter.export(spans)
        except Exception:
            log_event("telemetry.export_failed")
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        try:
            self.exporter.shutdown()
        except Exception:
            log_event("telemetry.export_failed")

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return bool(self.exporter.force_flush(timeout_millis))
        except Exception:
            log_event("telemetry.export_failed")
            return False


def configure_observability() -> None:
    """Configure process-local telemetry once, never failing application startup.

    COGNISTORE_LOG_FORMAT=json enables JSON operational events. Traces require
    COGNISTORE_OTEL_ENABLED=true. COGNISTORE_OTEL_ENDPOINT is an OTLP HTTP trace
    endpoint, default http://localhost:4318/v1/traces. OTEL_SERVICE_NAME supports
    cognistore, cognistore-api, and cognistore-worker. No exporter runs by default.
    """
    global _configured, _tracer, _provider
    with _config_lock:
        if _configured:
            return
        _configured = True
        if os.getenv("COGNISTORE_LOG_FORMAT", "").lower() == "json":
            handler = logging.StreamHandler()
            handler.setFormatter(StructuredLogFormatter())
            _logger.addHandler(handler)
            _logger.setLevel(logging.INFO)
            _logger.propagate = False
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "opentelemetry"):
                logger = logging.getLogger(name)
                logger.handlers = [handler]
                logger.propagate = False
        if os.getenv("COGNISTORE_OTEL_ENABLED", "").lower() not in {"true", "1", "yes"}:
            return
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            endpoint = os.getenv(
                "COGNISTORE_OTEL_ENDPOINT", "http://localhost:4318/v1/traces",
            )
            parsed = urlsplit(endpoint)
            if (
                parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment
            ):
                raise ValueError("invalid trace endpoint")
            service = os.getenv("OTEL_SERVICE_NAME", "cognistore")
            if service not in {"cognistore", "cognistore-api", "cognistore-worker"}:
                service = "cognistore"
            exporter_logger = logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter")
            exporter_logger.addFilter(_RedactExporterLogs())
            exporter = _SafeExporter(OTLPSpanExporter(endpoint=endpoint, timeout=2))
            provider = TracerProvider(resource=Resource({"service.name": service}))
            provider.add_span_processor(BatchSpanProcessor(exporter))
            _provider = provider
            _tracer = provider.get_tracer("cognistore")
            log_event("telemetry.configured")
        except Exception:
            log_event("telemetry.configuration_failed")


def mark_current_span_error() -> None:
    """Mark handled failures so execution metrics and span status remain correct."""
    state = _operation_state.get()
    if state is not None:
        state["error"] = True
    trace.get_current_span().set_status(Status(StatusCode.ERROR))


@contextmanager
def observe(
    component: str, operation: str, *, kind: SpanKind | str = SpanKind.INTERNAL,
    **labels: object,
) -> Iterator[Span]:
    """Time an operation and create a safe span, without recording exceptions."""
    component = _bounded(component, _COMPONENTS)
    operation = _bounded(operation, _OPERATIONS)
    if isinstance(kind, str):
        kind = {
            "producer": SpanKind.PRODUCER, "consumer": SpanKind.CONSUMER,
            "server": SpanKind.SERVER, "client": SpanKind.CLIENT,
        }.get(kind, SpanKind.INTERNAL)
    attributes = _safe_fields({"component": component, "operation": operation, **labels})
    if current_correlation_id():
        attributes["correlation_id"] = current_correlation_id()
    state = {"error": False}
    token = _operation_state.set(state)
    started = time.perf_counter()
    try:
        with _tracer.start_as_current_span(
            f"{component}.{operation}", kind=kind, attributes=attributes,
            record_exception=False, set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except BaseException:
                mark_current_span_error()
                raise
            finally:
                status = getattr(span, "status", None)
                failed = state["error"] or getattr(status, "status_code", None) == StatusCode.ERROR
                outcome = "error" if failed else "success"
                duration = time.perf_counter() - started
                _OPERATIONS_TOTAL.labels(component, operation, outcome).inc()
                _OPERATION_DURATION.labels(component, operation, outcome).observe(duration)
                log_event(
                    "operation.completed", component=component, operation=operation,
                    outcome=outcome, duration_seconds=duration, **labels,
                )
    finally:
        _operation_state.reset(token)


def instrument(
    component: str, operation: str, *, kind: SpanKind | str = SpanKind.INTERNAL,
    **labels: object,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Observe sync, async and generator execution; never inspect arguments."""
    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def async_wrapped(*args: P.args, **kwargs: P.kwargs) -> Any:
                with observe(component, operation, kind=kind, **labels):
                    return await function(*args, **kwargs)
            return cast(Callable[P, R], async_wrapped)
        if inspect.isgeneratorfunction(function):
            @wraps(function)
            def generator_wrapped(*args: P.args, **kwargs: P.kwargs) -> Any:
                def run() -> Any:
                    with observe(component, operation, kind=kind, **labels):
                        yield from function(*args, **kwargs)
                return _ContextualGenerator(run())
            return cast(Callable[P, R], generator_wrapped)
        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with observe(component, operation, kind=kind, **labels):
                return function(*args, **kwargs)
        return wrapped
    return decorate


class _ContextualGenerator(Generator[Any, Any, Any]):
    """Keep a stream's active span inside one context across thread/task resumes.

    Streaming responses may enter a storage reader in an AnyIO worker and close
    it in another context. A span must never remain attached in the caller while
    yielding: its token cannot be reset in that other context, and would leak
    the storage span into unrelated work between chunks.
    """

    def __init__(self, generator: Generator[Any, Any, Any]) -> None:
        self._generator = generator
        self._context = copy_context()

    def __next__(self) -> Any:
        return self._context.run(next, self._generator)

    def send(self, value: Any) -> Any:
        return self._context.run(self._generator.send, value)

    def throw(self, *args: Any) -> Any:
        return self._context.run(self._generator.throw, *args)

    def close(self) -> None:
        self._context.run(self._generator.close)

    def __del__(self) -> None:
        # Destructors must not surface errors from abandoned stream cleanup.
        with suppress(Exception):
            self.close()


def register_http_routes(routes: Iterable[str]) -> None:
    """Register trusted application route templates, never request paths (max 100)."""
    for route in routes:
        if len(_routes) >= 100:
            break
        if isinstance(route, str) and route.startswith("/") and len(route) <= 200:
            _routes.add(route)


def _route(route: object) -> str:
    return route if isinstance(route, str) and route in _routes else "unmatched"


def record_http_request(method: str, route: str, status_code: int, duration_seconds: float) -> None:
    method = _bounded(method, _METHODS)
    route = _route(route)
    status = f"{status_code // 100}xx" if 100 <= status_code < 600 else "other"
    _HTTP_TOTAL.labels(method, route, status).inc()
    _HTTP_DURATION.labels(method, route, status).observe(max(0, duration_seconds))
    log_event(
        "http.request.completed", method=method, route=route, status_class=status,
        duration_seconds=duration_seconds,
    )


def record_job_event(event: str, operation: str = "other") -> None:
    event = _bounded(event, _JOB_EVENTS)
    operation = _bounded(operation, _JOB_OPERATIONS)
    _JOB_TOTAL.labels(event, operation).inc()
    log_event("job.event", job_event=event, operation=operation)


def set_job_queue_depth(depth: float, state: str = "pending") -> None:
    if state in {"pending", "in_flight"}:
        _JOB_DEPTH.labels(state).set(float("nan") if math.isnan(depth) else max(0, depth))


def record_job_queue_latency(seconds: float, operation: str = "other") -> None:
    if math.isfinite(seconds) and seconds >= 0:
        _JOB_LATENCY.labels(_bounded(operation, _JOB_OPERATIONS)).observe(seconds)


def record_movement_bytes(amount: int) -> None:
    if amount > 0:
        _MOVEMENT_BYTES.inc(amount)


def metrics_response() -> tuple[bytes, str]:
    """Return a Prometheus text exposition and its complete Content-Type value."""
    return _generate_latest(REGISTRY), CONTENT_TYPE_LATEST
