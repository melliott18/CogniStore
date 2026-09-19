from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from cognistore.encryption import server_tls_context
from cognistore.observability import metrics_response

from .runtime import AsyncWorker


class HealthServer:
    """Small HTTP health/readiness surface with no web-framework dependency."""

    def __init__(
        self,
        worker: AsyncWorker,
        *,
        host: str = "127.0.0.1",
        port: int = 8081,
        request_timeout: float = 2.0,
    ) -> None:
        self.worker = worker
        self.host = host
        self.port = port
        self.request_timeout = request_timeout
        self._tls = server_tls_context(
            os.environ.get("COGNISTORE_HEALTH_TLS_CERTFILE"),
            os.environ.get("COGNISTORE_HEALTH_TLS_KEYFILE"),
        )
        self._server: asyncio.Server | None = None

    @property
    def scheme(self) -> str:
        return "https" if self._tls is not None else "http"

    @property
    def bound_port(self) -> int | None:
        if self._server is None or not self._server.sockets:
            return None
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("health server has already been started")
        self._server = await asyncio.start_server(
            self._handle_request, self.host, self.port, ssl=self._tls
        )

    async def close(self) -> None:
        server = self._server
        self._server = None
        if server is None:
            return
        server.close()
        await server.wait_closed()

    async def _handle_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        status = 500
        body: dict[str, Any] = {"error": "health check failed"}
        encoded: bytes | None = None
        content_type = "application/json"
        probing_readiness = False
        try:
            request = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=self.request_timeout
            )
            if len(request) > 8192:
                raise ValueError("request headers are too large")
            request_line = request.split(b"\r\n", 1)[0].decode("ascii")
            method, target, _ = request_line.split(" ", 2)
            path = target.split("?", 1)[0]
            if method != "GET":
                status = 405
                body = {"error": "method not allowed"}
            elif path == "/healthz":
                snapshot = self.worker.health_snapshot()
                status = 200 if snapshot.live else 503
                body = snapshot.to_dict()
            elif path == "/readyz":
                probing_readiness = True
                snapshot = await asyncio.wait_for(
                    self.worker.check_readiness(), timeout=self.request_timeout
                )
                status = 200 if snapshot.ready else 503
                body = snapshot.to_dict()
            elif path == "/metrics":
                # Refresh consumer statistics on every scrape. The queue gauge
                # reflects durable broker backlog, including other workers.
                probing_readiness = True
                await asyncio.wait_for(
                    self.worker.check_readiness(), timeout=self.request_timeout
                )
                encoded, content_type = metrics_response()
                status = 200
            else:
                status = 404
                body = {"error": "not found"}
        except asyncio.TimeoutError as exc:
            status = 503 if probing_readiness else 400
            body = {
                "error": "readiness probe timed out"
                if probing_readiness
                else (str(exc) or "request timed out")
            }
        except (asyncio.IncompleteReadError, UnicodeError, ValueError) as exc:
            status = 400
            body = {"error": str(exc) or "bad request"}
        except Exception:
            status = 503
            body = {"error": "worker probe failed"}

        if encoded is None:
            encoded = json.dumps(body, sort_keys=True).encode("utf-8")
        reason = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            405: "Method Not Allowed",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }[status]
        writer.write(
            (
                f"HTTP/1.1 {status} {reason}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(encoded)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            + encoded
        )
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
