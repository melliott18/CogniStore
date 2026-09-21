"""Synthetic scheduling/transport tests, never staging load qualification."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter

import httpx
import pytest

from scripts import load_workload as workload


class Clock:
    def __init__(self, *, on_sleep=None, first_extra=0):
        self.now = 0.0
        self.on_sleep = on_sleep
        self.first_extra = first_extra

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds + self.first_extra
        self.first_extra = 0
        if self.on_sleep:
            self.on_sleep(self.now)
        # Run ready callbacks without consuming wall-clock time.
        for _ in range(8):
            await asyncio.sleep(0)


async def success(_request):
    return workload.Result(True, "ok")


def execute(settings, transport=success, *, clock=None, record=None):
    clock = clock or Clock()
    rows = []
    report = asyncio.run(workload.run(settings, transport, record or rows.append,
                                      clock=clock, sleep=clock.sleep))
    return report, rows


def test_descriptor_exact_operation_mix_tenants_sizes_and_hotspots():
    expected = {"get": 20, "head": 20, "catalog": 20, "ask": 15,
                "put_replace": 10, "put_create": 5, "delete": 5, "policy_preview": 5}
    assert Counter(workload.OPERATION_CYCLE) == expected
    requests = [workload.request_for_sequence(i) for i in range(20000)]
    assert requests == [workload.request_for_sequence(i) for i in range(20000)]
    assert workload.request_for_sequence(13, "other").selection != requests[13].selection
    for operation in {request.operation for request in requests}:
        selected = [request for request in requests if request.operation == operation]
        assert Counter(request.tenant for request in selected) == {
            "pilot-a": len(selected) // 2, "pilot-b": len(selected) // 2,
        }
        assert Counter(request.size_bytes for request in selected) == {
            4096: len(selected) * 60 // 100,
            65536: len(selected) * 30 // 100,
            1048576: len(selected) * 9 // 100,
            16777216: len(selected) // 100,
        }
        for size in set(workload.SIZE_CYCLE):
            sized = [request for request in selected if request.size_bytes == size]
            assert sum(request.hotspot for request in sized) * 5 == len(sized) * 4
            assert Counter(request.tenant for request in sized) == {
                "pilot-a": len(sized) // 2, "pilot-b": len(sized) // 2,
            }
            for tenant in ("pilot-a", "pilot-b"):
                scoped = [request for request in sized if request.tenant == tenant]
                assert sum(request.hotspot for request in scoped) * 5 == len(scoped) * 4
    assert all(0 <= request.selection < 1 for request in requests)


@pytest.mark.parametrize("sequence", [-1, True, 1.2, "0"])
def test_invalid_descriptor(sequence):
    with pytest.raises(ValueError):
        workload.request_for_sequence(sequence)


@pytest.mark.parametrize("duration", [0, -1, True, float("nan"), float("inf"), "1"])
def test_invalid_settings(duration):
    with pytest.raises(ValueError):
        workload.Settings("nominal", duration)


def test_open_loop_slots_exact_timing_and_sanitized_records():
    report, rows = execute(workload.Settings("nominal", 10))
    assert report["offered"] == report["counts"]["recorded"] == 100
    assert report["counts"]["successes"] == 100
    assert report["production_qualified"] is False
    assert {row["sequence"] for row in rows} == set(range(100))
    for row in rows:
        assert row["scheduled_seconds"] == row["sequence"] / 10
        assert row["elapsed_seconds"] >= row["actual_seconds"] >= 0
        assert row["outcome"] == "ok"
        assert ("put_kind" in row) == (row["operation"] == "put")
        assert ("size_bytes" in row) == (row["operation"] in {"head", "put", "get"})
        assert set(row) <= {
            "sequence", "scheduled_seconds", "elapsed_seconds", "operation", "tenant",
            "cohort", "success", "outcome", "size_bytes", "put_kind", "actual_seconds",
        }


@pytest.mark.parametrize("cohort,duration,limit", [("nominal", 3, 16), ("burst", 2, 32)])
def test_concurrency_rejections_stay_in_offered_denominator(cohort, duration, limit):
    async def scenario():
        finished = asyncio.Event()
        invoked = []

        async def transport(request):
            invoked.append(request.sequence)
            await finished.wait()
            return workload.Result(True, "ok")

        rate = workload.PROFILES[cohort][0]
        clock = Clock(on_sleep=lambda now: finished.set()
                      if now >= duration - 1 / rate - 1e-8 else None)
        rows = []
        report = await workload.run(workload.Settings(cohort, duration), transport, rows.append,
                                    clock=clock, sleep=clock.sleep)
        return report, rows, invoked

    report, rows, invoked = asyncio.run(scenario())
    assert report["max_outstanding_observed"] == limit
    assert report["counts"]["concurrency_limit"] > 0
    assert len(rows) == report["offered"]
    assert len(invoked) == len(set(invoked)) == report["counts"]["issued"]
    rejected = [row for row in rows if row["outcome"] == "concurrency_limit"]
    assert all(not row["success"] and "actual_seconds" not in row for row in rejected)
    assert not {row["sequence"] for row in rejected} & set(invoked)


def test_four_put_limit_separate_from_total_concurrency():
    async def scenario():
        finished = asyncio.Event()

        async def transport(request):
            if request.operation == "put":
                await finished.wait()
            return workload.Result(True, "ok")

        clock = Clock(on_sleep=lambda now: finished.set() if now >= 8.9 else None)
        rows = []
        report = await workload.run(workload.Settings("nominal", 9), transport, rows.append,
                                    clock=clock, sleep=clock.sleep)
        return report, rows

    report, rows = asyncio.run(scenario())
    assert report["max_puts_observed"] == 4
    assert report["max_outstanding_observed"] < 16
    assert report["counts"]["concurrency_limit"] > 0
    assert all(row["operation"] == "put" for row in rows
               if row["outcome"] == "concurrency_limit")


def test_scheduler_delay_records_missed_arrivals_without_catchup_burst():
    invoked = []

    async def transport(request):
        invoked.append(request.sequence)
        return workload.Result(True, "ok")

    report, rows = execute(workload.Settings("nominal", 1), transport,
                           clock=Clock(first_extra=0.25))
    missed = {row["sequence"] for row in rows if row["outcome"] == "missed"}
    assert missed == {1, 2}
    assert not missed & set(invoked)
    assert report["counts"]["recorded"] == report["offered"] == 10
    delayed = next(row for row in rows if row["sequence"] == 3)
    assert delayed["elapsed_seconds"] >= 0.05 - 1e-8


def test_timeout_budget_includes_scheduled_wait(monkeypatch):
    original = asyncio.wait_for
    timeouts = []

    async def deadline(awaitable, timeout):
        timeouts.append(timeout)
        return await original(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", deadline)
    execute(workload.Settings("nominal", 1), clock=Clock(first_extra=0.25))
    assert any(29.9 < value < 29.951 for value in timeouts)
    assert max(timeouts) <= 30


def test_late_adapter_success_is_a_timeout():
    clock = Clock()

    async def transport(_request):
        clock.now = 31
        return workload.Result(True, "ok")

    _, rows = execute(workload.Settings("nominal", 0.1), transport, clock=clock)
    assert rows[0]["success"] is False
    assert rows[0]["outcome"] == "timeout"
    assert rows[0]["actual_seconds"] == rows[0]["elapsed_seconds"] == 31


@pytest.mark.parametrize("result,expected", [
    (workload.Result(False, "http_error"), "http_error"),
    (workload.Result(True, "http_error"), "adapter_error"),
    (workload.Result(False, "private bearer body"), "adapter_error"),
    ({"private": "secret"}, "adapter_error"),
])
def test_invalid_adapter_results_fail_closed_and_are_sanitized(result, expected):
    async def transport(_request):
        return result

    report, rows = execute(workload.Settings("nominal", 0.1), transport)
    assert report["counts"]["failures"] == 1
    assert rows[0]["outcome"] == expected
    assert "private" not in json.dumps(rows)


@pytest.mark.parametrize("error,expected", [
    (TimeoutError("secret"), "timeout"),
    (httpx.ConnectError("private-url-and-token"), "transport_error"),
])
def test_transport_exceptions_record_one_attempt_without_private_details(error, expected):
    calls = []

    async def transport(request):
        calls.append(request.sequence)
        raise error

    _, rows = execute(workload.Settings("nominal", 0.1), transport)
    assert calls == [0]
    assert rows[0]["outcome"] == expected
    assert "secret" not in json.dumps(rows)
    assert "private" not in json.dumps(rows)


def test_mock_http_adapter_consumes_response_before_success():
    consumed = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"synthetic-private-response"
            consumed.append(True)

    async def scenario():
        mock = httpx.MockTransport(lambda _request: httpx.Response(200, stream=Body()))
        async with httpx.AsyncClient(transport=mock, base_url="https://fixture.invalid") as client:
            async def adapter(_request):
                async with client.stream("GET", "/synthetic") as response:
                    body = await response.aread()
                    valid = response.status_code == 200 and body == b"synthetic-private-response"
                    return workload.Result(valid, "ok" if valid else "http_error")

            clock = Clock()
            rows = []
            await workload.run(workload.Settings("nominal", 0.1), adapter, rows.append,
                               clock=clock, sleep=clock.sleep)
            return rows

    rows = asyncio.run(scenario())
    assert consumed == [True]
    assert rows[0]["success"] is True
    assert "synthetic-private-response" not in json.dumps(rows)


def test_evidence_write_failure_aborts_runner():
    def fail(_row):
        raise OSError("disk unavailable")

    with pytest.raises(OSError, match="disk unavailable"):
        execute(workload.Settings("nominal", 1), record=fail)


def test_fixture_cli_is_explicit_short_and_never_qualifies(tmp_path):
    output, summary = tmp_path / "foreground.jsonl", tmp_path / "summary.json"
    assert workload.main([
        "--run-synthetic-fixture", "--duration-seconds", "0.1",
        "--output", str(output), "--summary", str(summary),
    ]) == 0
    report = json.loads(summary.read_text())
    assert report["seed"] == "m5-pilot-v1"
    assert report["seed_sha256"] == workload.hashlib.sha256(b"m5-pilot-v1").hexdigest()
    assert report["generator_source_sha256"] == workload.hashlib.sha256(
        workload.Path(workload.__file__).read_bytes()
    ).hexdigest()
    assert report["execution_scope"] == "synthetic-fixture"
    assert report["qualification_eligible"] is False
    assert report["production_qualified"] is False
    assert len(output.read_text().splitlines()) == 1
    with pytest.raises(SystemExit):
        workload.main(["--output", str(output), "--summary", str(summary)])
    original = output.read_bytes()
    with pytest.raises(SystemExit):
        workload.main(["--run-synthetic-fixture", "--output", str(output),
                       "--summary", str(summary)])
    assert output.read_bytes() == original


def test_runner_records_are_accepted_by_offline_evaluator(tmp_path):
    from scripts import load_qualification as evaluator

    all_rows = []
    for cohort in workload.PROFILES:
        _, rows = execute(workload.Settings(cohort, 1))
        all_rows.extend(rows)
    # Completion order does not need to match scheduled arrival order.
    (tmp_path / "foreground.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in reversed(all_rows)))
    (tmp_path / "telemetry.jsonl").write_text("")
    campaign = {
        "bindings": {
            "source_revision": "a" * 40, "image_digest": "sha256:" + "b" * 64,
            "configuration_sha256": "c" * 64, "specification_sha256": "d" * 64,
            "dependency_manifest_sha256": "e" * 64, "corpus_manifest_sha256": "f" * 64,
            "environment_id": "synthetic-fixture",
        },
        "profile_sha256": hashlib.sha256(evaluator.PROFILE.read_bytes()).hexdigest(),
        "execution_scope": "synthetic-fixture",
        "phases": {
            cohort: {"duration_seconds": 1, "warmup_seconds": 0, "object_count": 0,
                     "started_at": f"2026-09-{20 + index}T00:00:00Z"}
            for index, cohort in enumerate(workload.PROFILES)
        },
    }
    (tmp_path / "campaign.json").write_text(json.dumps(campaign))
    report = evaluator.evaluate(tmp_path)
    assert report["errors"] == []
    assert report["production_qualified"] is False
    for cohort, (rate, _limit) in workload.PROFILES.items():
        assert report["foreground"][cohort]["offered"] == rate
        assert report["foreground"][cohort]["observed"] == rate
        assert report["foreground"][cohort]["availability"] == 1
