from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.core.access import AccessConfig, AccessEvent
from cognistore.core.catalog import Catalog
from cognistore.drivers.observed import (
    ObservedStorageDriver,
    access_operation,
    suppress_access_capture,
)
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.drivers.s3_driver import S3Driver


class _RecordingCatalog(Catalog):
    def __init__(self) -> None:
        super().__init__()
        self.events: dict[str, AccessEvent] = {}
        self.fail_access = False

    def append_access_event(self, event: AccessEvent) -> AccessEvent:
        if self.fail_access:
            raise ConnectionError("history backend unavailable")
        persisted = super().append_access_event(event)
        self.events[persisted.event_id] = persisted
        return persisted


def test_posix_boundary_captures_once_and_retries_share_identity(tmp_path: Path) -> None:
    catalog = _RecordingCatalog()
    raw = PosixDriver(str(tmp_path))
    observed = ObservedStorageDriver(raw, catalog, tier="hot")
    nested = ObservedStorageDriver(observed, catalog, tier="hot")
    with access_operation(operation_id="upload", correlation_id="trace"):
        observed.put_object("bucket", "key", b"payload")
        nested.put_object("bucket", "key", b"payload")
    with access_operation(operation_id="read", correlation_id="trace"):
        assert observed.get_object("bucket", "key") == b"payload"
        with nested.open_object_reader("bucket", "key") as reader:
            assert reader.read(2) == b"pa"
            assert reader.read() == b"yload"
    with access_operation(operation_id="touch", correlation_id="trace"):
        assert observed.stat_object("bucket", "key")["size"] == 7
        generation = observed.object_generation("bucket", "key")
    assert len(catalog.events) == 3
    assert {event.kind for event in catalog.events.values()} == {"write", "read", "touch"}
    assert {event.correlation_id for event in catalog.events.values()} == {"trace"}
    assert {event.source for event in catalog.events.values()} == {"driver"}
    assert observed.capabilities == raw.capabilities
    assert observed.same_backend(raw)
    assert observed.same_backend(nested)
    assert not observed.same_backend(PosixDriver(str(tmp_path / "other")))

    observed.ensure_object_durable("bucket", "key")
    assert observed.delete_object_if_generation("bucket", "key", generation)
    observed.delete_object("bucket", "key")
    assert len(catalog.events) == 3


def test_failed_read_retry_and_stream_writes(tmp_path: Path) -> None:
    catalog = _RecordingCatalog()
    raw = PosixDriver(str(tmp_path))
    observed = ObservedStorageDriver(raw, catalog)
    with access_operation(operation_id="read-retry"):
        with pytest.raises(FileNotFoundError):
            observed.get_object("bucket", "key")
        raw.put_object("bucket", "key", b"ok")
        assert observed.get_object("bucket", "key") == b"ok"
        assert observed.get_object("bucket", "key") == b"ok"
    assert len(catalog.events) == 1
    with pytest.raises(ValueError):
        observed.put_object_stream("bucket", "bad", BytesIO(b"short"), size=100)
    assert len(catalog.events) == 1
    assert observed.put_object_stream("bucket", "stream", BytesIO(b"ok"), size=2) == 2
    generation = raw.object_generation("bucket", "stream")
    with observed.open_object_reader_if_generation("bucket", "stream", generation) as reader:
        assert reader.read() == b"ok"
    assert sorted(event.kind for event in catalog.events.values()) == ["read", "read", "write"]


def test_lazy_stream_freezes_context_across_threads_and_empty_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _RecordingCatalog()
    raw = PosixDriver(str(tmp_path))
    raw.put_object("bucket", "key", b"")
    observed = ObservedStorageDriver(raw, catalog)
    with access_operation(operation_id="original", correlation_id="original-trace"):
        stream = observed.open_object_reader("bucket", "key")
    observed_at = "2030-01-01T00:00:00.000000Z"
    monkeypatch.setattr("cognistore.drivers.observed.access_timestamp", lambda: observed_at)

    def consume() -> None:
        with access_operation(operation_id="unrelated"):
            with stream as reader:
                assert reader.read(0) == b""
                assert not catalog.events
                assert reader.read() == b""
                assert reader.read() == b""

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(consume).result()
    event, = catalog.events.values()
    assert event.operation_id == "original"
    assert event.correlation_id == "original-trace"
    assert event.occurred_at == observed_at


def test_stream_first_failure_is_not_demand_and_late_abort_retains_demand(tmp_path: Path) -> None:
    class FailingReader:
        def __init__(self, succeed_first: bool) -> None:
            self.succeed_first = succeed_first

        def read(self, size: int = -1) -> bytes:
            if self.succeed_first:
                self.succeed_first = False
                return b"x"
            raise OSError("stream failed")

    class Driver(PosixDriver):
        succeed_first = False

        @contextmanager
        def open_object_reader(self, bucket, key, range=None):
            yield FailingReader(self.succeed_first)

    catalog = _RecordingCatalog()
    raw = Driver(str(tmp_path))
    observed = ObservedStorageDriver(raw, catalog)
    with observed.open_object_reader("bucket", "key") as reader:
        with pytest.raises(OSError):
            reader.read()
    assert not catalog.events
    raw.succeed_first = True
    with observed.open_object_reader("bucket", "key") as reader:
        assert reader.read(1) == b"x"
        with pytest.raises(OSError):
            reader.read()
    assert len(catalog.events) == 1


def test_list_records_one_namespace_event_only_after_exhaustion(tmp_path: Path) -> None:
    catalog = _RecordingCatalog()
    raw = PosixDriver(str(tmp_path))
    raw.put_object("bucket", "prefix/one", b"1")
    raw.put_object("bucket", "prefix/two", b"2")
    observed = ObservedStorageDriver(raw, catalog)
    partial = observed.list_objects("bucket", "prefix/")
    next(partial)
    partial.close()
    assert not catalog.events
    with access_operation(operation_id="listing", correlation_id="list-trace"):
        deferred = observed.list_objects("bucket", "prefix/")
    assert len(list(deferred)) == 2
    event, = catalog.events.values()
    assert event.kind == "list"
    assert event.key is None
    assert event.operation_id == "listing"


def test_suppression_excludes_internal_work_and_sampling_is_reproducible(tmp_path: Path) -> None:
    catalog = _RecordingCatalog()
    raw = PosixDriver(str(tmp_path))
    observed = ObservedStorageDriver(raw, catalog)
    with suppress_access_capture():
        observed.put_object("bucket", "key", b"payload")
        assert observed.get_object("bucket", "key") == b"payload"
        assert list(observed.list_objects("bucket")) == ["key"]
    assert not catalog.events
    catalogs = [_RecordingCatalog(), _RecordingCatalog()]
    for sink in catalogs:
        sampled = ObservedStorageDriver(raw, sink, config=AccessConfig(sample_rate=0.5))
        for index in range(100):
            with access_operation(operation_id=f"sample-{index}"):
                sampled.get_object("bucket", "key")
                sampled.get_object("bucket", "key")
    assert set(catalogs[0].events) == set(catalogs[1].events)
    assert 25 < len(catalogs[0].events) < 75
    assert {event.sample_rate for event in catalogs[0].events.values()} == {0.5}


class _S3Client:
    """Protocol fake: no AWS network calls or credential resolution."""

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        return {"ETag": '"etag"'}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        return {"Body": BytesIO(b"payload")}

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "ContentLength": 7,
            "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "ETag": '"etag"',
        }

    def get_paginator(self, operation: str) -> _S3Client:
        assert operation == "list_objects_v2"
        return self

    def paginate(self, **kwargs: Any):
        yield {"Contents": [{"Key": "key"}], "IsTruncated": False}


def test_s3_wrapper_captures_public_operations_without_backend_amplification() -> None:
    catalog = _RecordingCatalog()
    raw = S3Driver(client=_S3Client())
    observed = ObservedStorageDriver(raw, catalog, tier="cold")
    observed.put_object("bucket", "key", b"payload")
    assert observed.get_object("bucket", "key") == b"payload"
    assert observed.stat_object("bucket", "key")["size"] == 7
    assert list(observed.list_objects("bucket")) == ["key"]
    assert sorted(event.kind for event in catalog.events.values()) == ["list", "read", "touch", "write"]
    assert {event.tier for event in catalog.events.values()} == {"cold"}


def test_api_records_logical_accesses_and_deduplicates_retries(tmp_path: Path) -> None:
    catalog = _RecordingCatalog()
    gateway = CogniStoreGateway(catalog, {"hot": PosixDriver(str(tmp_path))})
    path = "/v1/objects/hot/bucket/key"
    with TestClient(create_app(gateway)) as client:
        for attempt in ("upload-first", "upload-retry"):
            response = client.put(
                path,
                content=b"payload",
                headers={"X-Request-ID": attempt, "Idempotency-Key": "upload"},
            )
            assert response.status_code == 201
            assert response.headers["X-Request-ID"] == attempt
        for attempt in ("read-first", "read-retry"):
            response = client.get(
                path,
                headers={"X-Request-ID": attempt, "Idempotency-Key": "download"},
            )
            assert response.status_code == 200
            assert response.content == b"payload"
        for _ in range(2):
            assert client.head(path, headers={"X-Request-ID": "head"}).status_code == 200
        assert client.get(
            "/v1/catalog/objects/bucket/key", headers={"X-Request-ID": "catalog-touch"}
        ).status_code == 200
        assert client.get(
            "/v1/catalog/objects?bucket=bucket", headers={"X-Request-ID": "list"}
        ).status_code == 200
        before_failures = len(catalog.events)
        assert client.get(path, headers={"Range": "bytes=999-"}).status_code == 416
        assert client.get(path + "-missing").status_code == 404
        assert client.put(path + "?overwrite=false", content=b"fail").status_code == 409
        assert client.get(path, headers={"Idempotency-Key": "bad key"}).status_code == 422
        assert client.delete(path).status_code == 204
        assert len(catalog.events) == before_failures
    assert sorted(event.kind for event in catalog.events.values()) == ["list", "read", "touch", "touch", "write"]
    assert {event.source for event in catalog.events.values()} == {"api"}
    read = next(event for event in catalog.events.values() if event.kind == "read")
    assert read.operation_id == "download"
    assert read.correlation_id == "read-first"
    listing = next(event for event in catalog.events.values() if event.kind == "list")
    assert listing.key is None


def test_api_history_failure_is_unavailable_and_retry_can_record(tmp_path: Path) -> None:
    catalog = _RecordingCatalog()
    raw = PosixDriver(str(tmp_path))
    raw.put_object("bucket", "key", b"payload")
    catalog.upsert("bucket", "key", 7, "hot")
    gateway = CogniStoreGateway(catalog, {"hot": raw})
    catalog.fail_access = True
    with TestClient(create_app(gateway)) as client:
        failed = client.get(
            "/v1/objects/hot/bucket/key", headers={"X-Request-ID": "read-outage"}
        )
        assert failed.status_code == 503
        assert failed.json()["error"]["code"] == "backend_unavailable"
        assert not catalog.events
        catalog.fail_access = False
        success = client.get(
            "/v1/objects/hot/bucket/key", headers={"X-Request-ID": "read-outage"}
        )
        assert success.status_code == 200
        assert len(catalog.events) == 1


def test_api_initial_stream_failure_has_no_touch_or_read(tmp_path: Path) -> None:
    class FailingDriver(PosixDriver):
        @contextmanager
        def open_object_reader_if_generation(self, bucket, key, generation, range=None):
            raise ConnectionError("open failed")
            yield  # pragma: no cover

    catalog = _RecordingCatalog()
    raw = FailingDriver(str(tmp_path))
    raw.put_object("bucket", "key", b"payload")
    catalog.upsert("bucket", "key", 7, "hot")
    with TestClient(create_app(CogniStoreGateway(catalog, {"hot": raw}))) as client:
        response = client.get("/v1/objects/hot/bucket/key")
        assert response.status_code == 503
    assert not catalog.events
