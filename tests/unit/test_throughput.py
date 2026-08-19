from __future__ import annotations

import asyncio
import gc
import threading
import time
import tracemalloc
from pathlib import Path

import pytest

from cognistore.core.throughput import (
    ThroughputConfig,
    ThroughputController,
    ThroughputSaturatedError,
    TierLimits,
    load_throughput_config,
)


async def _eventually(predicate, *, timeout: float = 1.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), timeout=timeout)


def _config(
    *,
    max_queue_depth: int = 8,
    hot: TierLimits | None = None,
    warm: TierLimits | None = None,
    cold: TierLimits | None = None,
) -> ThroughputConfig:
    tiers = {
        "hot": hot or TierLimits(),
        "warm": warm or TierLimits(),
    }
    if cold is not None:
        tiers["cold"] = cold
    return ThroughputConfig(max_queue_depth=max_queue_depth, tiers=tiers)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"source_concurrency": 0}, "source_concurrency"),
        ({"destination_concurrency": True}, "destination_concurrency"),
        ({"bytes_per_second": float("inf")}, "bytes_per_second"),
        ({"operations_per_second": -1}, "operations_per_second"),
    ],
)
def test_tier_limits_reject_invalid_values(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        TierLimits(**kwargs)


def test_yaml_loader_merges_defaults_and_validates_known_tiers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "throughput.yaml"
    path.write_text(
        """
max_queue_depth: 12
defaults:
  source_concurrency: 2
  destination_concurrency: 3
  bytes_per_second: 4096
tiers:
  hot:
    source_concurrency: 5
  warm:
    bytes_per_second: null
    operations_per_second: 20
""",
        encoding="utf-8",
    )

    config = load_throughput_config(path, known_tiers=("hot", "warm", "archive"))

    assert config.max_queue_depth == 12
    assert config.tiers["hot"] == TierLimits(
        source_concurrency=5,
        destination_concurrency=3,
        bytes_per_second=4096,
    )
    assert config.tiers["warm"] == TierLimits(
        source_concurrency=2,
        destination_concurrency=3,
        operations_per_second=20,
    )
    assert config.tiers["archive"] == TierLimits(
        source_concurrency=2,
        destination_concurrency=3,
        bytes_per_second=4096,
    )


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("unexpected: true\ntiers: {hot: {}}\n", "unsupported fields"),
        ("tiers: {hot: {burst: 2}}\n", "tiers.hot has unsupported"),
        ("tiers: {ghost: {}}\n", "unknown tiers: ghost"),
        ("tiers: []\n", "tiers must be a mapping"),
        ("tiers: [\n", "invalid throughput YAML"),
    ],
)
def test_yaml_loader_fails_closed_on_unknown_or_malformed_configuration(
    tmp_path: Path, document: str, message: str
) -> None:
    path = tmp_path / "throughput.yaml"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_throughput_config(path, known_tiers=("hot", "warm"))


def test_source_and_destination_pools_are_independent_and_skip_blocked_lanes() -> None:
    async def scenario() -> None:
        controller = ThroughputController(_config(cold=TierLimits()))
        holder = controller.admit("hot", "warm")
        await holder.__aenter__()

        blocked_entered = asyncio.Event()

        async def blocked() -> None:
            async with controller.admit("hot", "cold"):
                blocked_entered.set()

        blocked_task = asyncio.create_task(blocked())
        await _eventually(lambda: controller.snapshot().queue_depth == 1)

        # hot's source pool is full, but its independent destination pool and
        # cold's source pool remain usable. The blocked lane is skipped.
        async with controller.admit("cold", "hot"):
            snapshot = controller.snapshot()
            assert snapshot.active_jobs == 2
            assert snapshot.tiers["hot"].source.active == 1
            assert snapshot.tiers["hot"].destination.active == 1
            assert blocked_entered.is_set() is False

        await holder.__aexit__(None, None, None)
        await asyncio.wait_for(blocked_task, timeout=1)
        assert blocked_entered.is_set()
        assert controller.snapshot().active_jobs == 0

    asyncio.run(scenario())


def test_reverse_direction_moves_acquire_atomically_without_deadlock() -> None:
    async def scenario() -> None:
        controller = ThroughputController(_config())
        both_entered = asyncio.Event()
        entered = 0
        release = asyncio.Event()

        async def move(source: str, destination: str) -> None:
            nonlocal entered
            async with controller.admit(source, destination):
                entered += 1
                if entered == 2:
                    both_entered.set()
                await release.wait()

        first = asyncio.create_task(move("hot", "warm"))
        second = asyncio.create_task(move("warm", "hot"))
        await asyncio.wait_for(both_entered.wait(), timeout=1)
        assert controller.snapshot().active_jobs == 2
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())


def test_bounded_queue_rejects_saturation_and_cancellation_removes_waiter() -> None:
    async def scenario() -> None:
        controller = ThroughputController(_config(max_queue_depth=1))
        holder = controller.admit("hot", "warm")
        await holder.__aenter__()

        async def wait_for_hot() -> None:
            async with controller.admit("hot", "warm"):
                raise AssertionError("cancelled waiter must not be admitted")

        waiting = asyncio.create_task(wait_for_hot())
        await _eventually(lambda: controller.snapshot().queue_depth == 1)

        with pytest.raises(ThroughputSaturatedError) as raised:
            async with controller.admit("hot", "warm"):
                pass
        assert raised.value.retryable is True
        assert controller.snapshot().saturation_events == 2

        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert controller.snapshot().queue_depth == 0

        await holder.__aexit__(None, None, None)
        async with controller.admit("hot", "warm"):
            assert controller.snapshot().active_jobs == 1

    asyncio.run(scenario())


def test_repeated_cancellation_cannot_leak_granted_tier_permits() -> None:
    async def scenario() -> None:
        controller = ThroughputController(_config())
        entered = asyncio.Event()

        async def move() -> None:
            async with controller.admit("hot", "warm"):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(move())
        await entered.wait()
        await controller._async_lock.acquire()
        try:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert task.done() is False
        finally:
            controller._async_lock.release()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert controller.snapshot().active_jobs == 0
        async with controller.admit("hot", "warm"):
            assert controller.snapshot().active_jobs == 1

    asyncio.run(scenario())


def test_repeated_cancellation_cannot_strand_a_queued_waiter() -> None:
    async def scenario() -> None:
        controller = ThroughputController(_config())
        holder = controller.admit("hot", "warm")
        await holder.__aenter__()

        async def queued_move() -> None:
            async with controller.admit("hot", "warm"):
                raise AssertionError("cancelled waiter must not be admitted")

        task = asyncio.create_task(queued_move())
        await _eventually(lambda: controller.snapshot().queue_depth == 1)
        await controller._async_lock.acquire()
        try:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert task.done() is False
        finally:
            controller._async_lock.release()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert controller.snapshot().queue_depth == 0
        await holder.__aexit__(None, None, None)
        async with controller.admit("hot", "warm"):
            assert controller.snapshot().active_jobs == 1

    asyncio.run(scenario())


def test_saturation_load_keeps_waiter_count_and_memory_bounded() -> None:
    async def scenario() -> None:
        queue_capacity = 16
        controller = ThroughputController(_config(max_queue_depth=queue_capacity))
        holder = controller.admit("hot", "warm")
        await holder.__aenter__()

        async def blocked() -> None:
            async with controller.admit("hot", "warm"):
                raise AssertionError("blocked load waiter unexpectedly admitted")

        waiters = [asyncio.create_task(blocked()) for _ in range(queue_capacity)]
        await _eventually(lambda: controller.snapshot().queue_depth == queue_capacity)

        async def rejected_batch(count: int) -> None:
            for _ in range(count):
                with pytest.raises(ThroughputSaturatedError):
                    async with controller.admit("hot", "warm"):
                        pass

        tracemalloc.start()
        try:
            await rejected_batch(1_000)
            gc.collect()
            settled_memory, _ = tracemalloc.get_traced_memory()
            await rejected_batch(9_000)
            gc.collect()
            final_memory, peak_memory = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert controller.snapshot().queue_depth == queue_capacity
        # Ten times more rejected load must not create a growing waiter list.
        # Allow allocator/test overhead while keeping growth sub-megabyte.
        assert final_memory - settled_memory < 512 * 1024
        assert peak_memory - settled_memory < 2 * 1024 * 1024

        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        await holder.__aexit__(None, None, None)
        assert controller.snapshot().queue_depth == 0

    asyncio.run(scenario())


def test_lanes_are_fifo_and_round_robin_fair() -> None:
    async def scenario() -> None:
        controller = ThroughputController(
            _config(
                max_queue_depth=4,
                cold=TierLimits(destination_concurrency=2),
            )
        )
        holder = controller.admit("hot", "warm")
        await holder.__aenter__()
        order: list[str] = []
        entered = {name: asyncio.Event() for name in ("a1", "b1", "a2")}
        releases = {name: asyncio.Event() for name in entered}

        async def queued(name: str, destination: str) -> None:
            async with controller.admit("hot", destination):
                order.append(name)
                entered[name].set()
                await releases[name].wait()

        a1 = asyncio.create_task(queued("a1", "warm"))
        await _eventually(lambda: controller.snapshot().queue_depth == 1)
        b1 = asyncio.create_task(queued("b1", "cold"))
        await _eventually(lambda: controller.snapshot().queue_depth == 2)
        a2 = asyncio.create_task(queued("a2", "warm"))
        await _eventually(lambda: controller.snapshot().queue_depth == 3)

        await holder.__aexit__(None, None, None)
        await asyncio.wait_for(entered["a1"].wait(), timeout=1)
        assert order == ["a1"]
        releases["a1"].set()

        await asyncio.wait_for(entered["b1"].wait(), timeout=1)
        assert order == ["a1", "b1"]
        releases["b1"].set()

        await asyncio.wait_for(entered["a2"].wait(), timeout=1)
        assert order == ["a1", "b1", "a2"]
        releases["a2"].set()
        await asyncio.gather(a1, b1, a2)

    asyncio.run(scenario())


def test_reconfigure_lowers_capacity_without_revoking_active_holders() -> None:
    async def scenario() -> None:
        controller = ThroughputController(
            _config(
                hot=TierLimits(source_concurrency=2),
                warm=TierLimits(destination_concurrency=2),
            )
        )
        first = controller.admit("hot", "warm")
        second = controller.admit("hot", "warm")
        await first.__aenter__()
        await second.__aenter__()
        assert controller.snapshot().active_jobs == 2

        await controller.reconfigure(_config())
        snapshot = controller.snapshot()
        assert snapshot.active_jobs == 2
        assert snapshot.tiers["hot"].source.capacity == 1

        third_entered = asyncio.Event()

        async def third_move() -> None:
            async with controller.admit("hot", "warm"):
                third_entered.set()

        third = asyncio.create_task(third_move())
        await _eventually(lambda: controller.snapshot().queue_depth == 1)

        await first.__aexit__(None, None, None)
        await asyncio.sleep(0)
        assert third_entered.is_set() is False
        assert controller.snapshot().active_jobs == 1

        await second.__aexit__(None, None, None)
        await asyncio.wait_for(third, timeout=1)
        assert third_entered.is_set()

    asyncio.run(scenario())


def test_reconfigure_rejects_tier_set_changes() -> None:
    async def scenario() -> None:
        controller = ThroughputController(_config())
        with pytest.raises(ValueError, match="cannot add or remove tiers"):
            await controller.reconfigure(ThroughputConfig(tiers={"hot": TierLimits()}))

    asyncio.run(scenario())


def test_reconfigure_rejects_queue_capacity_changes() -> None:
    async def scenario() -> None:
        controller = ThroughputController(_config(max_queue_depth=2))
        with pytest.raises(ValueError, match="cannot change max_queue_depth"):
            await controller.reconfigure(_config(max_queue_depth=3))

    asyncio.run(scenario())


def test_operation_rate_is_enforced_asynchronously_and_measured() -> None:
    async def scenario() -> None:
        limits = TierLimits(operations_per_second=100)
        controller = ThroughputController(_config(hot=limits, warm=limits))

        async with controller.admit("hot", "warm"):
            pass

        entered = asyncio.Event()
        started = time.monotonic()

        async def second_move() -> None:
            async with controller.admit("hot", "warm"):
                entered.set()

        task = asyncio.create_task(second_move())
        await asyncio.wait_for(entered.wait(), timeout=1)
        elapsed = time.monotonic() - started
        await task

        snapshot = controller.snapshot()
        assert elapsed >= 0.005
        assert snapshot.throttle_events == 1
        assert snapshot.throttle_seconds > 0
        assert snapshot.tiers["hot"].operations_consumed == 2
        assert snapshot.tiers["warm"].operations_consumed == 2
        assert snapshot.throttled == 0

    asyncio.run(scenario())


def test_byte_limiter_unblocks_on_live_reconfiguration_and_reports_metrics() -> None:
    controller = ThroughputController(
        _config(
            hot=TierLimits(bytes_per_second=1000),
            warm=TierLimits(),
        )
    )
    # Spend the initial one-second token-bucket burst.
    controller.consume_bytes("hot", 1000)
    waiting = threading.Event()
    finished = threading.Event()
    callback_count = 0

    def on_wait() -> None:
        nonlocal callback_count
        callback_count += 1
        waiting.set()

    def consume() -> None:
        controller.consume_bytes("hot", 2000, on_wait=on_wait)
        finished.set()

    thread = threading.Thread(target=consume)
    thread.start()
    assert waiting.wait(timeout=1)
    snapshot = controller.snapshot()
    assert snapshot.throttled == 1
    assert snapshot.tiers["hot"].throttled == 1

    asyncio.run(
        controller.reconfigure(_config(hot=TierLimits(bytes_per_second=None), warm=TierLimits()))
    )
    thread.join(timeout=1)

    assert finished.is_set()
    assert callback_count >= 1
    snapshot = controller.snapshot()
    assert snapshot.throttled == 0
    assert snapshot.throttle_events == 1
    assert snapshot.throttle_seconds > 0
    assert snapshot.tiers["hot"].bytes_consumed == 3000


def test_snapshot_dictionary_exposes_global_and_per_tier_capacity() -> None:
    async def scenario() -> None:
        controller = ThroughputController(
            _config(
                max_queue_depth=3,
                hot=TierLimits(source_concurrency=2, bytes_per_second=2048),
            )
        )
        async with controller.admit("hot", "warm"):
            payload = controller.snapshot().to_dict()
            assert payload["active_jobs"] == 1
            assert payload["queue_depth"] == 0
            assert payload["queue_capacity"] == 3
            assert payload["tiers"]["hot"]["source"] == {
                "active": 1,
                "waiting": 0,
                "capacity": 2,
                "saturated": False,
            }
            assert payload["tiers"]["hot"]["bytes_per_second"] == 2048.0

    asyncio.run(scenario())
