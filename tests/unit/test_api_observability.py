from __future__ import annotations

import asyncio
import json
from contextvars import copy_context
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from cognistore import observability as telemetry
from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.jobs.handlers import build_handlers
from cognistore.jobs.models import JobEnvelope
from cognistore.jobs.runtime import AsyncWorker, WorkerConfig
from tests.unit.test_rest_api import (
    _RaisingGateway,
    _RecordingSubmissionQueue,
    _SingleDeliveryQueue,
    _WorkerDelivery,
)

TRACE_ID = "1234567890abcdef1234567890abcdef"
TRACEPARENT = f"00-{TRACE_ID}-1234567890abcdef-01"


@pytest.fixture
def spans(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_tracer", provider.get_tracer("test"))
    yield exporter
    provider.shutdown()


def test_request_to_serialized_job_to_storage_and_catalog(tmp_path, spans):
    driver = PosixDriver(str(tmp_path / "hot"))
    driver.put_object("private-bucket", "private-key.txt", b"private content sentinel")
    with SQLCatalog(tmp_path / "catalog.sqlite3") as catalog:
        queue = _RecordingSubmissionQueue()
        gateway = CogniStoreGateway(catalog, {"hot": driver}, queue=queue)
        with TestClient(create_app(gateway)) as client:
            response = client.post(
                "/v1/actions/catalog-scans",
                json={"tier": "hot", "bucket": "private-bucket"},
                headers={
                    "traceparent": TRACEPARENT,
                    "X-Request-ID": "private-request-id",
                    "Authorization": "Bearer secret-token-sentinel",
                    "baggage": "secret=private-baggage",
                    "tracestate": "vendor=private-tracestate",
                },
            )
            assert response.status_code == 202, response.text
            assert response.headers["X-Request-ID"] == "private-request-id"
            correlation_id = response.json()["correlation_id"]
            UUID(correlation_id)
            queued = JobEnvelope.from_bytes(queue.enqueued[0][0].to_bytes())
            assert queued.correlation_id == correlation_id
            assert queued.metadata["traceparent"].split("-")[1] == TRACE_ID

            async def consume():
                delivery = _WorkerDelivery(queued)
                worker = AsyncWorker(
                    _SingleDeliveryQueue(delivery),
                    build_handlers({"hot": driver}, catalog),
                    config=WorkerConfig(
                        fetch_timeout=0.01, heartbeat_interval=0, stop_after_jobs=1,
                        shutdown_grace=1, settlement_timeout=1,
                    ),
                    audit_catalog=catalog,
                )
                await worker.start()
                await asyncio.wait_for(worker.wait_for_shutdown_request(), timeout=5)
                report = await worker.shutdown()
                assert report.completed == 1
                assert delivery.ack_count == 1

            asyncio.run(consume())
            assert catalog.get("private-bucket", "private-key.txt") is not None
            scrape = client.get("/metrics")
            assert scrape.status_code == 200
            assert "text/plain" in scrape.headers["content-type"]

        trace = [s for s in spans.get_finished_spans() if s.context.trace_id == int(TRACE_ID, 16)]
        names = {s.name for s in trace}
        assert {"api.request", "job.scan", "driver.list_objects", "catalog.transaction",
                "index.extract_stream"} <= names
        request = next(s for s in trace if s.kind == SpanKind.SERVER)
        consumer = next(s for s in trace if s.kind == SpanKind.CONSUMER)
        assert consumer.parent.span_id == request.context.span_id
        assert all(s.attributes["correlation_id"] == correlation_id for s in trace)
        span_ids = {s.context.span_id for s in trace}
        assert all(s is request or s.parent.span_id in span_ids for s in trace)
        rendered = json.dumps([json.loads(s.to_json()) for s in trace]) + scrape.text
        for secret in ("private-bucket", "private-key", "private content", "private-request-id",
                       "secret-token", "private-baggage", "private-tracestate"):
            assert secret not in rendered
        assert telemetry.current_correlation_id() is None


def test_streaming_and_failures_have_bounded_routes_and_no_content(tmp_path, spans):
    driver = PosixDriver(str(tmp_path / "hot"))
    with SQLCatalog(tmp_path / "catalog.sqlite3") as catalog:
        app = create_app(CogniStoreGateway(catalog, {"hot": driver}))
        with TestClient(app) as client:
            content = b"secret document content" * 1000
            created = client.put(
                "/v1/objects/hot/private-bucket/private-key.txt", content=content,
                headers={"traceparent": TRACEPARENT},
            )
            assert created.status_code == 201
            read = client.get(
                "/v1/objects/hot/private-bucket/private-key.txt",
                headers={"traceparent": TRACEPARENT},
            )
            assert read.content == content
            for i in range(12):
                assert client.get(f"/private-unmatched-{i}").status_code == 404
            scrape = client.get("/metrics").text
        assert 'route="/v1/objects/{tier}/{bucket}/{key:path}"' in scrape
        assert 'route="unmatched"' in scrape
        assert "private-unmatched" not in scrape
        assert "private-key" not in scrape
        readers = [s for s in spans.get_finished_spans()
                   if s.name == "driver.open_object_reader_if_generation"]
        assert readers
        assert all(s.status.status_code != StatusCode.ERROR for s in readers)


def test_storage_reader_can_be_resumed_in_another_context(tmp_path, spans):
    driver = PosixDriver(str(tmp_path / "hot"))
    driver.put_object("bucket", "key", b"payload")
    reader = driver.open_object_reader("bucket", "key")
    first_context, second_context = copy_context(), copy_context()
    stream = first_context.run(reader.__enter__)
    assert second_context.run(stream.read) == b"payload"
    second_context.run(reader.__exit__, None, None, None)
    assert telemetry.current_correlation_id() is None


def test_handled_http_failure_counts_as_error_without_tracing(monkeypatch):
    from opentelemetry.trace import NoOpTracer

    monkeypatch.setattr(telemetry, "_tracer", NoOpTracer())
    labels = {"component": "api", "operation": "request", "outcome": "error"}
    before = telemetry.REGISTRY.get_sample_value("cognistore_operations_total", labels) or 0
    app = create_app(_RaisingGateway(RuntimeError("private exception content")))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/v1/catalog/objects/private-bucket/private-key")
        assert response.status_code == 500
        assert "private exception content" not in response.text
    after = telemetry.REGISTRY.get_sample_value("cognistore_operations_total", labels)
    assert after == before + 1
