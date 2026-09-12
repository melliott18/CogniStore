"""Protocol failures, streaming bounds, and generation fencing for GCS."""

from __future__ import annotations

import asyncio
import gc
import gzip
import json
import logging
import traceback
import tracemalloc
from collections.abc import Iterator
from io import BytesIO
from typing import Any

import httpx
import pytest

from cognistore.drivers import gcs_driver
from cognistore.drivers.gcs_driver import (
    GCSDriver,
    GCSTransientError,
    GCSUploadCleanupError,
)
from cognistore.drivers.storage_driver import ObjectGenerationMismatchError
from tests.gcs_fake import GCSFake

_CHUNK = 256 * 1024


@pytest.fixture
def server() -> GCSFake:
    return GCSFake()


@pytest.fixture
def driver(server: GCSFake) -> Iterator[GCSDriver]:
    with server.client() as client:
        yield GCSDriver(client=client, emulator_endpoint=server.endpoint, chunk_size=_CHUNK)


def test_capabilities_and_metadata_roundtrip(driver: GCSDriver, server: GCSFake) -> None:
    assert driver.capabilities.range_reads is True
    assert driver.capabilities.range_writes is False
    assert driver.capabilities.atomic_no_overwrite is True
    assert driver.capabilities.conditional_delete is True
    assert driver.put_object_stream(
        "bucket", "folder/雪 space?#%.txt", BytesIO(b"payload"), size=7,
        metadata={"content_type": "text/plain", "metadata": {"owner": "test"}},
    ) == 7
    assert driver.get_object("bucket", "folder/雪 space?#%.txt") == b"payload"
    stat = driver.stat_object("bucket", "folder/雪 space?#%.txt")
    assert stat["size"] == 7
    assert stat["mtime"] == pytest.approx(1735787045.0)
    assert stat["generation"]
    resource = server.objects["bucket", "folder/雪 space?#%.txt"]["resource"]
    assert resource["contentType"] == "text/plain"
    assert resource["metadata"] == {"owner": "test"}


@pytest.mark.parametrize("byte_range,expected", [
    ("bytes=2-6", b"23456"), ("bytes=7-", b"789"), ("bytes=-3", b"789"),
])
def test_get_forwards_ranges(
    driver: GCSDriver, server: GCSFake, byte_range: str, expected: bytes
) -> None:
    server.store("bucket", "key", b"0123456789")
    assert driver.get_object("bucket", "key", range=byte_range) == expected
    request = server.requests[-1]
    assert request.headers["Range"] == byte_range
    assert request.url.params["alt"] == "media"


@pytest.mark.parametrize("byte_range", ["garbage", "bytes=4-2", "bytes=0-1,4-5", "bytes=-"])
def test_invalid_ranges_fail_without_network(
    driver: GCSDriver, server: GCSFake, byte_range: str
) -> None:
    with pytest.raises(ValueError):
        driver.get_object("bucket", "key", range=byte_range)
    assert not server.requests


def test_ranged_writes_fail_without_network(driver: GCSDriver, server: GCSFake) -> None:
    with pytest.raises(NotImplementedError):
        driver.put_object("bucket", "key", b"value", range="bytes=0-4")
    assert not server.requests


def test_generation_reads_and_deletes_are_atomic(driver: GCSDriver, server: GCSFake) -> None:
    original = server.store("bucket", "key", b"original")["generation"]
    with driver.open_object_reader_if_generation("bucket", "key", original) as source:
        request = server.requests[-1]
        assert request.url.params["ifGenerationMatch"] == original
        assert "generation" not in request.url.params
        server.store("bucket", "key", b"replacement")
        assert source.read() == b"original"
    with pytest.raises(ObjectGenerationMismatchError):
        driver.delete_object_if_generation("bucket", "key", original)
    request = server.requests[-1]
    assert request.method == "DELETE"
    assert request.url.params["ifGenerationMatch"] == original
    assert "generation" not in request.url.params
    assert driver.get_object("bucket", "key") == b"replacement"


def test_listing_paginates_and_preserves_literal_prefix(driver: GCSDriver, server: GCSFake) -> None:
    driver = GCSDriver(
        client=server.client(), emulator_endpoint=server.endpoint, list_page_size=2
    )
    for key in ["a ?/1", "a ?/2", "a ?/3", "other"]:
        server.store("bucket", key, b"")
    assert list(driver.list_objects("bucket", prefix="a ?/")) == ["a ?/1", "a ?/2", "a ?/3"]
    assert len(server.requests) == 2
    assert all(r.url.params["prefix"] == "a ?/" for r in server.requests)
    assert all(r.url.params["maxResults"] == "2" for r in server.requests)
    assert server.requests[1].url.params["pageToken"] == "2"


def test_large_upload_uses_multiple_resumable_chunks(driver: GCSDriver, server: GCSFake) -> None:
    payload = b"a" * (_CHUNK * 2) + b"tail"
    assert driver.put_object_stream("bucket", "key", BytesIO(payload), size=len(payload)) == len(payload)
    assert driver.get_object("bucket", "key") == payload
    writes = [r for r in server.requests if r.method == "PUT"]
    assert [len(r.content) for r in writes] == [_CHUNK, _CHUNK, 4]
    assert all(r.headers["Content-Range"].endswith(f"/{len(payload)}") for r in writes)
    assert len([r for r in server.requests if r.method == "POST"]) == 1


@pytest.mark.parametrize("failure", ["429", "503", "transport"])
def test_interrupted_upload_resumes_from_server_accepted_offset(
    driver: GCSDriver, server: GCSFake, failure: str
) -> None:
    payload = b"x" * (_CHUNK * 2) + b"tail"
    interrupted = False

    def interrupt(request: httpx.Request) -> httpx.Response | None:
        nonlocal interrupted
        if request.method == "PUT" and not interrupted:
            interrupted = True
            server.sessions[request.url.path]["data"].extend(request.content[:_CHUNK // 2])
            if failure == "transport":
                raise httpx.ReadTimeout("secret upload URL", request=request)
            return GCSFake.error(int(failure))
        return None

    server.hook = interrupt
    driver.put_object_stream("bucket", "key", BytesIO(payload), size=len(payload))
    assert server.objects["bucket", "key"]["payload"] == payload
    puts = [r for r in server.requests if r.method == "PUT"]
    assert puts[1].headers["Content-Range"] == f"bytes */{len(payload)}"
    assert puts[2].headers["Content-Range"].startswith(f"bytes {_CHUNK // 2}-")
    assert len([r for r in server.requests if r.method == "POST"]) == 1


@pytest.mark.parametrize(
    "payload", [b"", b"exclusive", b"x" * (_CHUNK + 1)], ids=["empty", "small", "multichunk"]
)
def test_lost_commit_response_is_resolved_by_status_query(
    driver: GCSDriver, server: GCSFake, payload: bytes
) -> None:
    lost = False

    def lose_response(request: httpx.Request) -> httpx.Response | None:
        nonlocal lost
        if request.method == "PUT" and not lost:
            response = server.respond(request)
            if response.status_code == 200:
                lost = True
                raise httpx.ReadError("provider-secret", request=request)
            return response
        return None

    server.hook = lose_response
    driver.put_object("bucket", "key", payload, overwrite=False)
    assert server.objects["bucket", "key"]["payload"] == payload
    assert len([r for r in server.requests if r.method == "POST"]) == 1
    puts = [r for r in server.requests if r.method == "PUT"]
    assert len(puts) == max(1, (len(payload) + _CHUNK - 1) // _CHUNK) + 1
    assert puts[-1].headers["Content-Range"] == f"bytes */{len(payload)}"


@pytest.mark.parametrize("cause", [RuntimeError("interrupted"), asyncio.CancelledError()])
def test_interrupted_upload_cancels_session(
    driver: GCSDriver, server: GCSFake, cause: BaseException
) -> None:
    def interrupt(request: httpx.Request) -> httpx.Response | None:
        if request.method == "PUT":
            raise cause
        return None

    server.hook = interrupt
    with pytest.raises(type(cause)):
        driver.put_object("bucket", "key", b"payload")
    assert not server.sessions
    assert ("bucket", "key") not in server.objects
    assert any(r.method == "DELETE" and r.url.path.startswith("/session/") for r in server.requests)


def test_failed_cancellation_is_reported_explicitly(server: GCSFake) -> None:
    def fail(request: httpx.Request) -> httpx.Response | None:
        if request.method == "PUT":
            raise InterruptedError("interrupted")
        if request.method == "DELETE":
            return GCSFake.error(503)
        return None

    server.hook = fail
    driver = GCSDriver(client=server.client(), emulator_endpoint=server.endpoint, max_retries=0)
    with pytest.raises(GCSUploadCleanupError):
        driver.put_object("bucket", "key", b"payload")
    assert ("bucket", "key") not in server.objects


def test_exclusive_publication_race_keeps_competing_object(driver: GCSDriver, server: GCSFake) -> None:
    def race(request: httpx.Request) -> httpx.Response | None:
        if request.method == "PUT":
            server.store("bucket", "key", b"competitor")
        return None

    server.hook = race
    with pytest.raises(FileExistsError):
        driver.put_object("bucket", "key", b"mine", overwrite=False)
    assert server.objects["bucket", "key"]["payload"] == b"competitor"
    initiation = next(r for r in server.requests if r.method == "POST")
    assert initiation.url.params["ifGenerationMatch"] == "0"
    assert not any(r.method == "GET" for r in server.requests)
    assert not server.sessions


@pytest.mark.parametrize("payload,size", [(b"short", 6), (b"long", 3)])
def test_invalid_source_size_never_starts_upload(
    driver: GCSDriver, server: GCSFake, payload: bytes, size: int
) -> None:
    server.store("bucket", "key", b"original")
    with pytest.raises(ValueError):
        driver.put_object_stream("bucket", "key", BytesIO(payload), size=size)
    assert server.objects["bucket", "key"]["payload"] == b"original"
    assert not server.requests


def test_source_failure_does_not_publish_or_start_session(driver: GCSDriver, server: GCSFake) -> None:
    class BrokenSource:
        def read(self, size: int = -1) -> bytes:
            raise OSError("source failed")

    with pytest.raises(OSError, match="source failed"):
        driver.put_object_stream("bucket", "key", BrokenSource(), size=1)
    assert not server.requests


@pytest.mark.parametrize("status,exception", [
    (404, FileNotFoundError), (401, PermissionError), (403, PermissionError),
    (429, GCSTransientError), (503, GCSTransientError), (400, OSError),
])
@pytest.mark.parametrize("operation", ["get_object", "stat_object"])
def test_provider_errors_are_normalized_and_redacted(
    server: GCSFake, status: int, exception: type[Exception], operation: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    server.hook = lambda request: GCSFake.error(status)
    driver = GCSDriver(client=server.client(), emulator_endpoint=server.endpoint, max_retries=0)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(exception) as captured:
            getattr(driver, operation)("bucket", "key")
    formatted = "".join(traceback.format_exception(captured.value))
    assert "provider-secret" not in formatted + caplog.text
    assert "http://" not in str(captured.value)
    if status in {429, 503}:
        assert isinstance(captured.value, ConnectionError)
    assert len(server.requests) == 1


def test_exhausted_upload_throttling_aborts_with_bounded_requests(server: GCSFake) -> None:
    server.hook = lambda request: GCSFake.error(429) if request.method == "PUT" else None
    driver = GCSDriver(client=server.client(), emulator_endpoint=server.endpoint, max_retries=1)
    with pytest.raises(GCSTransientError):
        driver.put_object("bucket", "key", b"payload")
    assert not server.sessions
    assert len(server.requests) <= 8


class _TrackedBody(httpx.SyncByteStream):
    def __init__(self, payload: bytes, *, fail: bool = False) -> None:
        self.payload = payload
        self.fail = fail
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield self.payload[:2]
        if self.fail:
            raise httpx.ReadError("provider-secret")
        yield self.payload[2:]

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("failure", ["none", "consumer", "network"])
def test_reader_closes_response_on_all_exit_paths(server: GCSFake, failure: str) -> None:
    body = _TrackedBody(b"payload", fail=failure == "network")
    server.hook = lambda request: httpx.Response(200, stream=body)
    driver = GCSDriver(client=server.client(), emulator_endpoint=server.endpoint)

    def consume() -> None:
        with driver.open_object_reader("bucket", "key") as source:
            assert len(server.requests) == 1
            if failure == "consumer":
                raise RuntimeError("consumer failed")
            assert source.read() == b"payload"

    if failure == "none":
        consume()
    else:
        with pytest.raises(RuntimeError if failure == "consumer" else OSError):
            consume()
    assert body.closed


def test_reader_preserves_compressed_stored_bytes(server: GCSFake) -> None:
    compressed = gzip.compress(b"stored compressed content")
    body = _TrackedBody(compressed)
    server.hook = lambda request: httpx.Response(
        200, headers={"Content-Encoding": "gzip"}, stream=body
    )
    driver = GCSDriver(client=server.client(), emulator_endpoint=server.endpoint)
    assert driver.get_object("bucket", "key") == compressed
    assert "gzip" in server.requests[-1].headers["Accept-Encoding"]
    assert body.closed


def test_upload_staging_and_transfer_stay_bounded_in_memory() -> None:
    object_size = 512 * _CHUNK

    class GeneratedSource:
        remaining = object_size
        largest_read = 0

        def read(self, size: int = -1) -> bytes:
            assert size > 0, "source must be read in bounded chunks"
            self.largest_read = max(self.largest_read, size)
            length = min(size, self.remaining)
            self.remaining -= length
            return b"x" * length

    received = 0

    def discard(request: httpx.Request) -> httpx.Response:
        nonlocal received
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": "http://gcs.test/session/1"})
        assert request.method == "PUT"
        received += len(request.content)
        if received < object_size:
            return httpx.Response(308, headers={"Range": f"bytes=0-{received - 1}"})
        return httpx.Response(200, json={"generation": "1"})

    source = GeneratedSource()
    with httpx.Client(transport=httpx.MockTransport(discard)) as client:
        driver = GCSDriver(client=client, emulator_endpoint=GCSFake.endpoint, chunk_size=_CHUNK)
        gc.collect()
        was_enabled = gc.isenabled()
        gc.disable()
        tracemalloc.start()
        try:
            assert driver.put_object_stream("bucket", "key", source, size=object_size) == object_size
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            if was_enabled:
                gc.enable()
    assert received == object_size
    assert source.largest_read <= _CHUNK
    # Bound memory even when a caller disables cyclic collection: completed
    # httpx responses must release their request body as each chunk finishes.
    assert peak < 16 * _CHUNK


@pytest.mark.parametrize("chunk_size", [True, 0, -1, _CHUNK - 1, _CHUNK + 1, "262144"])
def test_invalid_chunk_size_is_rejected(chunk_size: Any, server: GCSFake) -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        GCSDriver(client=server.client(), emulator_endpoint=server.endpoint, chunk_size=chunk_size)


def test_auto_create_bucket_is_explicit_and_only_for_writes(server: GCSFake) -> None:
    driver = GCSDriver(
        client=server.client(), emulator_endpoint=server.endpoint,
        project="project", auto_create_bucket=True,
    )
    with pytest.raises(FileNotFoundError):
        driver.get_object("new-bucket", "key")
    assert "new-bucket" not in server.buckets
    driver.put_object("new-bucket", "key", b"value")
    assert server.objects["new-bucket", "key"]["payload"] == b"value"
    create = next(r for r in server.requests if r.url.path == "/storage/v1/b")
    assert create.url.params["project"] == "project"
    assert json.loads(create.content)["name"] == "new-bucket"


@pytest.mark.parametrize("size", [0, 3])
def test_status_queries_at_eof_cannot_loop_forever(server: GCSFake, size: int) -> None:
    puts = 0

    def never_finalizes(request: httpx.Request) -> httpx.Response | None:
        nonlocal puts
        if request.method != "PUT":
            return None
        puts += 1
        assert puts <= 5, "upload status queries were not bounded"
        headers = {"Range": f"bytes=0-{size - 1}"} if size else {}
        return httpx.Response(308, headers=headers)

    server.hook = never_finalizes
    driver = GCSDriver(client=server.client(), emulator_endpoint=server.endpoint, max_retries=1)
    with pytest.raises(GCSTransientError):
        driver.put_object("bucket", "key", b"x" * size)
    assert not server.sessions
    assert ("bucket", "key") not in server.objects


@pytest.mark.parametrize("acknowledgement", ["nonsense", "bytes=1-5", "bytes=0-9999999"])
def test_upload_rejects_invalid_acknowledged_offsets(server: GCSFake, acknowledgement: str) -> None:
    server.hook = lambda request: (
        httpx.Response(308, headers={"Range": acknowledgement})
        if request.method == "PUT" else None
    )
    driver = GCSDriver(client=server.client(), emulator_endpoint=server.endpoint, max_retries=0)
    with pytest.raises(OSError):
        driver.put_object("bucket", "key", b"payload")
    assert not server.sessions
    assert ("bucket", "key") not in server.objects


def test_upload_rejects_regressing_acknowledged_offsets(driver: GCSDriver, server: GCSFake) -> None:
    uploads = 0

    def regress(request: httpx.Request) -> httpx.Response | None:
        nonlocal uploads
        if request.method == "PUT":
            uploads += 1
            if uploads == 2:
                return httpx.Response(308, headers={"Range": "bytes=0-10"})
        return None

    server.hook = regress
    with pytest.raises(OSError):
        driver.put_object("bucket", "key", b"x" * (_CHUNK + 1))
    assert not server.sessions


def test_reader_rejects_ignored_range_and_closes_response(server: GCSFake) -> None:
    body = _TrackedBody(b"0123456789")
    server.hook = lambda request: httpx.Response(200, stream=body)
    driver = GCSDriver(client=server.client(), emulator_endpoint=server.endpoint)
    with pytest.raises(OSError):
        driver.get_object("bucket", "key", range="bytes=2-3")
    assert body.closed


def test_reader_rejects_transcoded_object_and_closes_response(server: GCSFake) -> None:
    body = _TrackedBody(b"decoded data")
    server.hook = lambda request: httpx.Response(
        200, headers={"X-Goog-Stored-Content-Encoding": "gzip"}, stream=body
    )
    driver = GCSDriver(client=server.client(), emulator_endpoint=server.endpoint)
    with pytest.raises(OSError):
        driver.get_object("bucket", "key")
    assert body.closed


def test_transport_logging_never_exposes_session_urls_or_tokens(
    server: GCSFake, caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "secret-session-token"

    def log_transport(request: httpx.Request) -> httpx.Response:
        logging.getLogger("httpcore.http11").debug(
            "headers=%s url=%s", {"Location": f"http://gcs.test/session/1?upload_id={secret}"},
            request.url,
        )
        if request.method == "POST":
            return httpx.Response(
                200, headers={"Location": f"http://gcs.test/session/1?upload_id={secret}"}
            )
        assert request.method == "PUT"
        return httpx.Response(200, json={"generation": "1"})

    with httpx.Client(transport=httpx.MockTransport(log_transport)) as client:
        driver = GCSDriver(client=client, emulator_endpoint=server.endpoint)
        with caplog.at_level(logging.DEBUG):
            driver.put_object("bucket", "key", b"value")
            logging.getLogger("httpx").info("unrelated HTTP client event")
    assert caplog.records
    assert secret not in caplog.text
    assert "/session/" not in caplog.text
    assert "unrelated HTTP client event" in caplog.text


@pytest.mark.parametrize("strategy", ["adc", "file"])
def test_credentials_are_loaded_using_google_auth_and_sent_as_headers(
    monkeypatch: pytest.MonkeyPatch, server: GCSFake, strategy: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Credentials:
        def before_request(self, auth_request: Any, method: str, url: str, headers: dict) -> None:
            headers["authorization"] = "Bearer private-access-token"

    credentials = Credentials()
    calls: list[tuple[tuple, dict]] = []

    def load(*args: Any, **kwargs: Any) -> tuple[Any, str]:
        calls.append((args, kwargs))
        return credentials, "inferred-project"

    monkeypatch.setattr(gcs_driver.google.auth, "default", load)
    monkeypatch.setattr(gcs_driver.google.auth, "load_credentials_from_file", load)
    server.store("bucket", "key", b"payload")
    with server.client() as client:
        with caplog.at_level(logging.DEBUG):
            driver = GCSDriver(
                client=client,
                credentials_file="/configured/credential-file.json" if strategy == "file" else None,
            )
            assert driver.project == "inferred-project"
            assert driver.get_object("bucket", "key") == b"payload"
            driver.close()
        assert not client.is_closed, "injected clients remain caller-owned"
    assert len(calls) == 1
    assert calls[0][0] == (("/configured/credential-file.json",) if strategy == "file" else ())
    assert "https://www.googleapis.com/auth/devstorage.read_write" in calls[0][1]["scopes"]
    assert server.requests[0].headers["authorization"] == "Bearer private-access-token"
    assert "private-access-token" not in caplog.text
    assert "/configured/credential-file.json" not in caplog.text


def test_anonymous_emulator_never_loads_ambient_credentials(
    monkeypatch: pytest.MonkeyPatch, server: GCSFake,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("emulator attempted to load ambient credentials")

    monkeypatch.setattr(gcs_driver.google.auth, "default", forbidden)
    monkeypatch.setattr(gcs_driver.google.auth, "load_credentials_from_file", forbidden)
    server.store("bucket", "key", b"payload")
    driver = GCSDriver(client=server.client(), emulator_endpoint=server.endpoint)
    assert driver.get_object("bucket", "key") == b"payload"
    assert "authorization" not in server.requests[0].headers


@pytest.mark.parametrize("stage", ["load", "refresh"])
def test_authentication_errors_do_not_expose_provider_secrets(
    monkeypatch: pytest.MonkeyPatch, server: GCSFake, stage: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "private-refresh-token"

    class Credentials:
        def before_request(self, *args: Any, **kwargs: Any) -> None:
            logging.getLogger("urllib3.connectionpool").debug("refresh token: %s", secret)
            raise RuntimeError(secret)

    def fail_load(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(secret)

    if stage == "load":
        monkeypatch.setattr(gcs_driver.google.auth, "default", fail_load)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(PermissionError) as captured:
            driver = GCSDriver(
                client=server.client(), credentials=Credentials() if stage == "refresh" else None,
            )
            driver.get_object("bucket", "key")
    formatted = "".join(traceback.format_exception(captured.value))
    assert secret not in formatted + caplog.text
    assert not server.requests
