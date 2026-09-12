"""ASGI telemetry covering the complete response, including streamed bodies."""

from __future__ import annotations

import re
from time import perf_counter
from uuid import uuid4

from opentelemetry.trace import SpanKind
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cognistore.observability import (
    mark_current_span_error,
    observe,
    record_http_request,
    request_context,
)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class TelemetryMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/metrics":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        supplied = headers.get("x-request-id", "")
        request_id = supplied if _REQUEST_ID.fullmatch(supplied) else str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        started = perf_counter()
        status = 500

        async def capture_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        with request_context(request_id, headers.get("traceparent")):
            with observe("api", "request", kind=SpanKind.SERVER) as span:
                try:
                    await self.app(scope, receive, capture_send)
                except BaseException:
                    # Streaming failures may follow a successful response start.
                    status = 500
                    raise
                finally:
                    route = getattr(scope.get("route"), "path", "unmatched")
                    record_http_request(scope["method"], route, status, perf_counter() - started)
                    span.set_attribute("http.response.status_code", status)
                    if status >= 500:
                        mark_current_span_error()
