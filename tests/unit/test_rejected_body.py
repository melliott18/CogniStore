from __future__ import annotations

import asyncio

import pytest

from cognistore.api import rejected_body
from cognistore.api.rejected_body import RejectedBodyMiddleware


def run_rejection(receive, *, status=413, consume=False, scope_type="http"):
    sent = []

    async def send(message):
        sent.append(message)

    async def app(scope, wrapped_receive, wrapped_send):
        if consume:
            await wrapped_receive()
        await wrapped_send({"type": "http.response.start", "status": status, "headers": []})
        await wrapped_send({"type": "http.response.body", "body": b"error"})

    asyncio.run(RejectedBodyMiddleware(app)({"type": scope_type}, receive, send))
    return sent


def test_rejection_sends_error_before_discard_and_closes_after_body():
    sent = []
    chunks = iter([
        {"type": "http.request", "body": b"first", "more_body": True},
        {"type": "http.request", "body": b"last", "more_body": False},
    ])

    async def send(message):
        sent.append(message)

    async def receive():
        # An Expect client must see the final status and complete JSON before
        # any receive call could ask Uvicorn to emit 100 Continue.
        assert sent[0]["status"] == 413
        assert sent[1] == {"type": "http.response.body", "body": b"error", "more_body": True}
        return next(chunks)

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 413, "headers": []})
        await send({"type": "http.response.body", "body": b"error"})

    asyncio.run(RejectedBodyMiddleware(app)({"type": "http"}, receive, send))
    assert list(chunks) == []
    assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}


@pytest.mark.parametrize("status", [200, 401, 403, 422, 500])
def test_other_responses_do_not_read_request_body(status):
    async def receive():
        pytest.fail("non-413 responses must not trigger body cleanup")

    sent = run_rejection(receive, status=status)
    assert sent[-1] == {"type": "http.response.body", "body": b"error"}


def test_non_http_scope_passes_through():
    async def receive():
        pytest.fail("non-HTTP scopes must pass through")

    assert run_rejection(receive, scope_type="websocket")[-1]["body"] == b"error"


@pytest.mark.parametrize("message", [
    {"type": "http.request", "body": b"complete"},
    {"type": "http.disconnect"},
])
def test_already_consumed_or_disconnected_request_needs_no_cleanup(message):
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        assert calls == 1
        return message

    sent = run_rejection(receive, consume=True)
    assert calls == 1
    assert sent[-1] == {"type": "http.response.body", "body": b"error"}


def test_cleanup_stops_at_byte_budget(monkeypatch):
    monkeypatch.setattr(rejected_body, "MAX_REJECTED_BODY_DRAIN_BYTES", 10)
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        assert calls <= 3
        return {"type": "http.request", "body": b"four", "more_body": True}

    sent = run_rejection(receive)
    assert calls == 3  # One transport chunk may straddle the budget.
    assert sent[-1]["more_body"] is False


def test_cleanup_stops_on_disconnect():
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        assert calls == 1
        return {"type": "http.disconnect"}

    assert run_rejection(receive)[-1]["more_body"] is False


def test_cleanup_times_out_and_cancels_stalled_receiver(monkeypatch):
    monkeypatch.setattr(rejected_body, "REJECTED_BODY_DRAIN_TIMEOUT_SECONDS", 0.02)
    cancelled = False

    async def receive():
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        finally:
            cancelled = True

    assert run_rejection(receive)[-1]["more_body"] is False
    assert cancelled


def test_cleanup_deadline_is_total_not_per_chunk(monkeypatch):
    monkeypatch.setattr(rejected_body, "REJECTED_BODY_DRAIN_TIMEOUT_SECONDS", 0.02)
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.005)
        return {"type": "http.request", "body": b"", "more_body": True}

    assert run_rejection(receive)[-1]["more_body"] is False
    assert calls < 10


def test_synchronous_empty_chunks_cannot_starve_deadline(monkeypatch):
    monkeypatch.setattr(rejected_body, "REJECTED_BODY_DRAIN_TIMEOUT_SECONDS", 0.01)
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        # No await: a receiver is allowed to hand back buffered messages.
        assert calls < 500_000, "cleanup must yield to its deadline"
        return {"type": "http.request", "body": b"", "more_body": True}

    assert run_rejection(receive)[-1]["more_body"] is False


def test_external_cancellation_is_not_swallowed():
    async def scenario():
        receiving = asyncio.Event()
        cancelled = False

        async def receive():
            nonlocal cancelled
            receiving.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled = True

        async def send(message):
            pass

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 413})
            await send({"type": "http.response.body", "body": b"error"})

        task = asyncio.create_task(RejectedBodyMiddleware(app)({"type": "http"}, receive, send))
        await receiving.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled

    asyncio.run(scenario())
