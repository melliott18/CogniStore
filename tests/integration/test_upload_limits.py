"""Exercise #163 upload rejection over real, isolated loopback HTTP sockets."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import uvicorn

from cognistore.api.app import MAX_JSON_BODY_BYTES, MAX_OBJECT_UPLOAD_BYTES, create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver
from tests.unit.test_api_authentication import identity_provider as identity_provider
from tests.unit.test_api_authorization import authorization


@dataclass
class _UploadServer:
    address: tuple[str, int]
    catalog: SQLCatalog
    driver: PosixDriver
    token: str = field(repr=False)

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url=f"http://{self.address[0]}:{self.address[1]}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=10,
            trust_env=False,
        )


@pytest.fixture(params=["h11", "httptools"])
def upload_server(tmp_path: Path, request, identity_provider) -> Iterator[_UploadServer]:
    """No ambient services, credentials, fixed ports, or external traffic."""
    protocol = request.param
    if protocol == "httptools":
        pytest.importorskip("httptools")
    authenticator, issue_token, _ = identity_provider
    with SQLCatalog(tmp_path / "catalog.db") as catalog, socket.socket() as listener:
        driver = PosixDriver(str(tmp_path / "hot"))
        app = create_app(
            CogniStoreGateway(catalog, {"hot": driver}),
            authentication=authenticator,
            authorization=authorization("writer"),
        )
        listener.bind(("127.0.0.1", 0))
        address = listener.getsockname()
        server = uvicorn.Server(uvicorn.Config(
            app, http=protocol, loop="asyncio", log_level="error", access_log=False,
        ))
        failures: list[BaseException] = []

        def run() -> None:
            try:
                server.run(sockets=[listener])
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, f"loopback server did not start: {failures}"
            yield _UploadServer(address, catalog, driver, issue_token())
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if thread.is_alive():
                server.force_exit = True
                thread.join(timeout=5)
            assert not thread.is_alive(), "loopback server did not stop"
            assert not failures, failures


def _assert_rejection(response: httpx.Response, request_id: str) -> None:
    assert response.status_code == 413
    assert response.headers["x-request-id"] == request_id
    assert response.headers["connection"] == "close"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "schema_version": 1,
        "request_id": request_id,
        "error": {
            "code": "payload_too_large",
            "message": f"Request bodies must not exceed {MAX_OBJECT_UPLOAD_BYTES} bytes",
            "retryable": False,
            "details": [],
        },
    }


@pytest.mark.parametrize("existing", [False, True], ids=["new", "replacement"])
@pytest.mark.parametrize("encoding", ["eager", "paced-length", "chunked"])
def test_oversized_upload_returns_413_and_preserves_storage(
    upload_server: _UploadServer, existing: bool, encoding: str,
) -> None:
    """Unlike TestClient, an eager sender must finish writing a real TCP body."""
    target = "/v1/objects/hot/bucket/item.bin"
    original = b"original fixture bytes"
    with upload_server.client() as client:
        if existing:
            assert client.put(target, content=original).status_code == 201
        before = upload_server.catalog.get("bucket", "item.bin")
        generation = (
            upload_server.driver.stat_object("bucket", "item.bin") if existing else None
        )
        upload_completed = False

        def chunks() -> Iterator[bytes]:
            nonlocal upload_completed
            for index in range(MAX_OBJECT_UPLOAD_BYTES // (64 * 1024)):
                yield b"x" * (64 * 1024)
                if index == 0 and encoding == "paced-length":
                    # Give the server time to reject the headers before the
                    # client finishes writing, as on a slower connection.
                    time.sleep(0.02)
            yield b"x"
            upload_completed = True

        headers = {"X-Request-ID": "upload-163-oversized"}
        if encoding == "paced-length":
            headers["Content-Length"] = str(MAX_OBJECT_UPLOAD_BYTES + 1)
        response = client.put(
            target,
            content=b"x" * (MAX_OBJECT_UPLOAD_BYTES + 1) if encoding == "eager" else chunks(),
            headers=headers,
        )
        _assert_rejection(response, "upload-163-oversized")
        if encoding != "eager":
            # HTTPX can suppress a broken pipe while sending if this platform
            # preserves the response bytes. Require the bounded body to finish
            # as well so this test detects that early-close race on either OS.
            assert upload_completed, "server closed while the eager client was still sending"
        assert upload_server.catalog.get("bucket", "item.bin") == before
        if existing:
            assert upload_server.driver.get_object("bucket", "item.bin") == original
            assert upload_server.driver.stat_object("bucket", "item.bin") == generation
        else:
            assert before is None
            assert list(upload_server.driver.list_objects("bucket")) == []
        # The client must reconnect cleanly after the explicit close.
        assert client.put(target, content=b"valid retry").status_code == 201
        assert upload_server.driver.get_object("bucket", "item.bin") == b"valid retry"


def test_upload_at_limit_remains_accepted(upload_server: _UploadServer) -> None:
    payload = b"x" * MAX_OBJECT_UPLOAD_BYTES
    with upload_server.client() as client:
        response = client.put("/v1/objects/hot/bucket/at-limit.bin", content=payload)
    assert response.status_code == 201
    assert response.json()["size"] == MAX_OBJECT_UPLOAD_BYTES
    record = upload_server.catalog.get("bucket", "at-limit.bin")
    assert record is not None and record.size == MAX_OBJECT_UPLOAD_BYTES
    assert upload_server.driver.get_object("bucket", "at-limit.bin") == payload


def test_oversized_expect_continue_returns_final_response_without_reading_body(
    upload_server: _UploadServer,
) -> None:
    """An Expect client is waiting for the response and will never send this body."""
    with socket.create_connection(upload_server.address, timeout=2) as connection:
        connection.sendall((
            "PUT /v1/objects/hot/bucket/expect.bin HTTP/1.1\r\n"
            f"Host: {upload_server.address[0]}:{upload_server.address[1]}\r\n"
            f"Authorization: Bearer {upload_server.token}\r\n"
            f"Content-Length: {MAX_OBJECT_UPLOAD_BYTES + 1}\r\n"
            "Expect: 100-continue\r\n"
            "X-Request-ID: upload-163-expect\r\n\r\n"
        ).encode("ascii"))
        # A short timeout catches waiting for a body/continue handshake, while
        # avoiding a fragile assertion about sub-millisecond scheduling.
        response = bytearray()
        while chunk := connection.recv(65536):
            response.extend(chunk)
    header, body = bytes(response).split(b"\r\n\r\n", 1)
    assert header.startswith(b"HTTP/1.1 413 ")
    assert b"100 Continue" not in header
    assert json.loads(body)["error"]["code"] == "payload_too_large"
    assert upload_server.catalog.get("bucket", "expect.bin") is None
    assert list(upload_server.driver.list_objects("bucket")) == []


@pytest.mark.parametrize("chunked", [False, True], ids=["length", "chunked"])
def test_json_rejection_returns_complete_envelope(
    upload_server: _UploadServer, chunked: bool,
) -> None:
    payload = b"x" * (MAX_JSON_BODY_BYTES + 1)
    with upload_server.client() as client:
        response = client.post(
            "/v1/ask", content=iter([payload]) if chunked else payload,
            headers={"Content-Type": "application/json", "X-Request-ID": "json-163-oversized"},
        )
    assert response.status_code == 413
    assert response.headers["x-request-id"] == "json-163-oversized"
    assert response.json()["error"]["code"] == "payload_too_large"
    assert response.json()["error"]["message"] == (
        f"Request bodies must not exceed {MAX_JSON_BODY_BYTES} bytes"
    )
