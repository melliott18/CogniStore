"""Bounded transport cleanup so eager upload clients can read a 413 response."""

from __future__ import annotations

import asyncio

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Cover the 16 MiB upload boundary, with a small allowance for eager senders.
# These are cleanup budgets, not higher application upload limits.
MAX_REJECTED_BODY_DRAIN_BYTES = 17 * 1024 * 1024
REJECTED_BODY_DRAIN_TIMEOUT_SECONDS = 1.0


class RejectedBodyMiddleware:
    """Send the error immediately, then discard bounded unread request bytes.

    Uvicorn closes a Connection: close response on the final body message.
    Closing with an unread eager upload can reset TCP before HTTPX sees the
    error. Keep the response open at the ASGI layer while discarding the tail.
    The status, headers and error bytes have already been sent, so clients
    waiting for 100 Continue receive the rejection without sending their body.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_finished = False
        rejected = False
        receive_lock = asyncio.Lock()

        def track(message: Message) -> None:
            nonlocal request_finished
            if message["type"] == "http.disconnect" or (
                message["type"] == "http.request" and not message.get("more_body", False)
            ):
                request_finished = True

        async def tracked_receive() -> Message:
            async with receive_lock:
                message = await receive()
                track(message)
                return message

        async def discard_remaining() -> None:
            # Serialize with any downstream disconnect listener. Do not retain
            # body chunks or pass discarded bytes to routes or storage drivers.
            async with receive_lock:
                discarded = 0
                while not request_finished and discarded < MAX_REJECTED_BODY_DRAIN_BYTES:
                    message = await receive()
                    track(message)
                    discarded += len(message.get("body", b""))
                    # ASGI receivers may return buffered (even empty) chunks
                    # synchronously. Yield so the total deadline and task
                    # cancellation still apply to such a stream.
                    await asyncio.sleep(0)

        async def finish_rejection(message: Message) -> None:
            nonlocal rejected
            if message["type"] == "http.response.start":
                rejected = message["status"] == 413
            if (
                rejected
                and message["type"] == "http.response.body"
                and not message.get("more_body", False)
                and not request_finished
            ):
                await send({**message, "more_body": True})
                try:
                    await asyncio.wait_for(
                        discard_remaining(), timeout=REJECTED_BODY_DRAIN_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            else:
                await send(message)

        await self.app(scope, tracked_receive, finish_rejection)
