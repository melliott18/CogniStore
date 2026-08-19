from __future__ import annotations

import asyncio
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Coroutine, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, AsyncIterator

import yaml

DEFAULT_MAX_QUEUE_DEPTH = 64
_DEFAULT_SOURCE_CONCURRENCY = 1
_DEFAULT_DESTINATION_CONCURRENCY = 1
_RATE_WAIT_SLICE = 1.0
_LIMIT_FIELDS = frozenset(
    {
        "source_concurrency",
        "destination_concurrency",
        "bytes_per_second",
        "operations_per_second",
    }
)
_TOP_LEVEL_FIELDS = frozenset({"max_queue_depth", "defaults", "tiers"})


class ThroughputSaturatedError(RuntimeError):
    """Raised when the bounded local admission queue cannot accept more work."""

    retryable = True
    throughput_saturated = True


def _positive_integer(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _optional_positive_rate(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive finite number or null")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{field_name} must be a positive finite number or null")
    return parsed


@dataclass(frozen=True)
class TierLimits:
    source_concurrency: int = _DEFAULT_SOURCE_CONCURRENCY
    destination_concurrency: int = _DEFAULT_DESTINATION_CONCURRENCY
    bytes_per_second: float | None = None
    operations_per_second: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_concurrency",
            _positive_integer(self.source_concurrency, "source_concurrency"),
        )
        object.__setattr__(
            self,
            "destination_concurrency",
            _positive_integer(self.destination_concurrency, "destination_concurrency"),
        )
        object.__setattr__(
            self,
            "bytes_per_second",
            _optional_positive_rate(self.bytes_per_second, "bytes_per_second"),
        )
        object.__setattr__(
            self,
            "operations_per_second",
            _optional_positive_rate(self.operations_per_second, "operations_per_second"),
        )


@dataclass(frozen=True)
class ThroughputConfig:
    max_queue_depth: int = DEFAULT_MAX_QUEUE_DEPTH
    tiers: Mapping[str, TierLimits] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "max_queue_depth",
            _positive_integer(self.max_queue_depth, "max_queue_depth"),
        )
        if not isinstance(self.tiers, Mapping) or not self.tiers:
            raise ValueError("tiers must be a non-empty mapping")
        normalized: dict[str, TierLimits] = {}
        for tier, limits in self.tiers.items():
            if not isinstance(tier, str) or not tier.strip():
                raise ValueError("tier names must be non-empty strings")
            if not isinstance(limits, TierLimits):
                raise ValueError(f"limits for tier {tier!r} must be TierLimits")
            normalized[tier] = limits
        object.__setattr__(self, "tiers", MappingProxyType(normalized))


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    for key in value:
        if not isinstance(key, str):
            raise ValueError(f"{field_name} keys must be strings")
    return value


def _limit_values(value: object, field_name: str) -> dict[str, object]:
    values = dict(_mapping(value, field_name))
    unknown = sorted(set(values).difference(_LIMIT_FIELDS))
    if unknown:
        raise ValueError(f"{field_name} has unsupported fields: {', '.join(unknown)}")
    return values


def load_throughput_config(
    config_path: str | Path,
    *,
    known_tiers: Iterable[str] | None = None,
) -> ThroughputConfig:
    """Load strict per-tier throughput limits from a standalone YAML file.

    The top-level ``defaults`` mapping is applied before each entry in ``tiers``.
    When ``known_tiers`` is provided, every known tier receives a configuration
    (including tiers omitted from the file), and unknown configured tiers fail
    closed instead of being silently ignored.
    """

    path = Path(config_path)
    try:
        with path.open("r", encoding="utf-8") as stream:
            loaded = yaml.safe_load(stream)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid throughput YAML in {path}: {exc}") from exc
    if loaded is None:
        loaded = {}
    document = dict(_mapping(loaded, "throughput configuration"))
    unknown_top_level = sorted(set(document).difference(_TOP_LEVEL_FIELDS))
    if unknown_top_level:
        raise ValueError(
            "throughput configuration has unsupported fields: " + ", ".join(unknown_top_level)
        )

    default_values = _limit_values(document.get("defaults", {}), "defaults")
    configured_tiers = _mapping(document.get("tiers", {}), "tiers")
    overrides: dict[str, dict[str, object]] = {}
    for tier, raw_limits in configured_tiers.items():
        if not tier.strip():
            raise ValueError("tier names must be non-empty strings")
        overrides[tier] = _limit_values(raw_limits, f"tiers.{tier}")

    if known_tiers is None:
        tier_names = tuple(overrides)
    else:
        tier_names_list: list[str] = []
        seen: set[str] = set()
        for tier in known_tiers:
            if not isinstance(tier, str) or not tier.strip():
                raise ValueError("known tier names must be non-empty strings")
            if tier in seen:
                raise ValueError(f"known tier {tier!r} is duplicated")
            seen.add(tier)
            tier_names_list.append(tier)
        unknown_tiers = sorted(set(overrides).difference(seen))
        if unknown_tiers:
            raise ValueError(
                "throughput configuration contains unknown tiers: " + ", ".join(unknown_tiers)
            )
        tier_names = tuple(tier_names_list)

    tiers: dict[str, TierLimits] = {}
    for tier in tier_names:
        values: dict[str, object] = {
            "source_concurrency": _DEFAULT_SOURCE_CONCURRENCY,
            "destination_concurrency": _DEFAULT_DESTINATION_CONCURRENCY,
            "bytes_per_second": None,
            "operations_per_second": None,
        }
        values.update(default_values)
        values.update(overrides.get(tier, {}))
        tiers[tier] = TierLimits(**values)  # type: ignore[arg-type]

    return ThroughputConfig(
        max_queue_depth=document.get("max_queue_depth", DEFAULT_MAX_QUEUE_DEPTH),
        tiers=tiers,
    )


@dataclass(frozen=True)
class RoleSnapshot:
    active: int
    waiting: int
    capacity: int

    @property
    def saturated(self) -> bool:
        return self.active >= self.capacity or self.waiting > 0

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "active": self.active,
            "waiting": self.waiting,
            "capacity": self.capacity,
            "saturated": self.saturated,
        }


@dataclass(frozen=True)
class TierThroughputSnapshot:
    source: RoleSnapshot
    destination: RoleSnapshot
    bytes_per_second: float | None
    operations_per_second: float | None
    bytes_consumed: int
    operations_consumed: int
    throttled: int
    throttle_events: int
    throttle_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "destination": self.destination.to_dict(),
            "bytes_per_second": self.bytes_per_second,
            "operations_per_second": self.operations_per_second,
            "bytes_consumed": self.bytes_consumed,
            "operations_consumed": self.operations_consumed,
            "throttled": self.throttled,
            "throttle_events": self.throttle_events,
            "throttle_seconds": self.throttle_seconds,
        }


@dataclass(frozen=True)
class ThroughputSnapshot:
    active_jobs: int
    queue_depth: int
    queue_capacity: int
    throttled: int
    throttle_events: int
    throttle_seconds: float
    saturation_events: int
    saturated: bool
    tiers: Mapping[str, TierThroughputSnapshot]

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_jobs": self.active_jobs,
            "queue_depth": self.queue_depth,
            "queue_capacity": self.queue_capacity,
            "throttled": self.throttled,
            "throttle_events": self.throttle_events,
            "throttle_seconds": self.throttle_seconds,
            "saturation_events": self.saturation_events,
            "saturated": self.saturated,
            "tiers": {tier: snapshot.to_dict() for tier, snapshot in self.tiers.items()},
        }


@dataclass(eq=False)
class _AdmissionWaiter:
    source: str
    destination: str
    future: asyncio.Future[None]
    queued_at: float
    granted: bool = False
    operation_throttle_started: float | None = None
    operation_throttle_tiers: tuple[str, ...] = ()


@dataclass(eq=False)
class _ByteWaiter:
    amount: int
    remaining: float


async def _drain_cleanup(awaitable: Coroutine[Any, Any, None]) -> None:
    """Finish async bookkeeping even if the caller is cancelled repeatedly."""

    cleanup: asyncio.Task[None] = asyncio.create_task(awaitable)
    cancellation_requested = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancellation_requested = True
            continue
    cleanup.result()
    if cancellation_requested:
        raise asyncio.CancelledError


class ThroughputController:
    """Bounded, fair admission and pacing shared by all moves in one worker.

    Admission is asynchronous so work waiting for tier capacity never occupies a
    storage executor thread. Byte pacing is deliberately synchronous because it
    is called from streamed driver reads already running in those threads.
    """

    def __init__(
        self,
        config: ThroughputConfig,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(config, ThroughputConfig):
            raise TypeError("config must be ThroughputConfig")
        self._config = config
        self._clock = clock or time.monotonic
        self._async_lock = asyncio.Lock()
        self._state_lock = threading.RLock()
        self._rate_condition = threading.Condition(self._state_lock)
        self._rate_generation = 0

        self._active_source = {tier: 0 for tier in config.tiers}
        self._active_destination = {tier: 0 for tier in config.tiers}
        self._lanes: dict[tuple[str, str], deque[_AdmissionWaiter]] = {}
        self._lane_order: list[tuple[str, str]] = []
        self._lane_cursor = 0
        self._operation_wakeup: asyncio.TimerHandle | None = None

        now = self._clock()
        self._operation_tokens: dict[str, float] = {}
        self._operation_updated = {tier: now for tier in config.tiers}
        self._byte_tokens: dict[str, float] = {}
        self._byte_updated = {tier: now for tier in config.tiers}
        self._byte_waiters: dict[str, deque[_ByteWaiter]] = {tier: deque() for tier in config.tiers}
        for tier, limits in config.tiers.items():
            self._operation_tokens[tier] = self._operation_capacity(limits.operations_per_second)
            self._byte_tokens[tier] = float(limits.bytes_per_second or 0)

        self._bytes_consumed = {tier: 0 for tier in config.tiers}
        self._operations_consumed = {tier: 0 for tier in config.tiers}
        self._tier_throttled = {tier: 0 for tier in config.tiers}
        self._tier_throttle_events = {tier: 0 for tier in config.tiers}
        self._tier_throttle_seconds = {tier: 0.0 for tier in config.tiers}
        self._throttled = 0
        self._throttle_events = 0
        self._throttle_seconds = 0.0
        self._saturation_events = 0

    @property
    def config(self) -> ThroughputConfig:
        return self._config

    @asynccontextmanager
    async def admit(self, source: str, destination: str) -> AsyncIterator[None]:
        waiter = await self._acquire(source, destination)
        try:
            yield None
        finally:
            await _drain_cleanup(self._release(waiter))

    async def _acquire(self, source: str, destination: str) -> _AdmissionWaiter:
        loop = asyncio.get_running_loop()
        waiter = _AdmissionWaiter(
            source=source,
            destination=destination,
            future=loop.create_future(),
            queued_at=self._clock(),
        )
        async with self._async_lock:
            with self._state_lock:
                self._validate_tier(source)
                self._validate_tier(destination)
                now = self._clock()
                self._dispatch_locked(now)
                lane = (source, destination)
                lane_queue = self._lanes.get(lane)
                if (
                    not lane_queue
                    and self._has_concurrency_locked(source, destination)
                    and self._operation_delay_locked(source, destination, now) <= 0
                ):
                    self._grant_locked(waiter, now)
                    return waiter

                if self._queue_depth_locked() >= self._config.max_queue_depth:
                    self._saturation_events += 1
                    raise ThroughputSaturatedError(
                        "throughput admission queue is saturated "
                        f"({self._config.max_queue_depth} waiting jobs)"
                    )

                if lane_queue is None:
                    lane_queue = deque()
                    self._lanes[lane] = lane_queue
                    self._lane_order.append(lane)
                lane_queue.append(waiter)
                self._saturation_events += 1
                self._dispatch_locked(now)

        try:
            await waiter.future
            return waiter
        except asyncio.CancelledError:
            await _drain_cleanup(self._cancel_waiter(waiter))
            raise

    async def _cancel_waiter(self, waiter: _AdmissionWaiter) -> None:
        async with self._async_lock:
            with self._state_lock:
                now = self._clock()
                if waiter.granted:
                    self._release_counts_locked(waiter)
                else:
                    lane_queue = self._lanes.get((waiter.source, waiter.destination))
                    if lane_queue is not None:
                        try:
                            lane_queue.remove(waiter)
                        except ValueError:
                            pass
                self._finish_operation_throttle_locked(waiter, now)
                self._dispatch_locked(now)

    async def _release(self, waiter: _AdmissionWaiter) -> None:
        async with self._async_lock:
            with self._state_lock:
                if not waiter.granted:
                    return
                self._release_counts_locked(waiter)
                waiter.granted = False
                self._dispatch_locked(self._clock())

    def _release_counts_locked(self, waiter: _AdmissionWaiter) -> None:
        self._active_source[waiter.source] -= 1
        self._active_destination[waiter.destination] -= 1

    def _validate_tier(self, tier: str) -> None:
        if tier not in self._config.tiers:
            raise ValueError(f"unknown throughput tier: {tier}")

    def _queue_depth_locked(self) -> int:
        return sum(len(queue) for queue in self._lanes.values())

    def _has_concurrency_locked(self, source: str, destination: str) -> bool:
        source_limit = self._config.tiers[source].source_concurrency
        destination_limit = self._config.tiers[destination].destination_concurrency
        return (
            self._active_source[source] < source_limit
            and self._active_destination[destination] < destination_limit
        )

    @staticmethod
    def _operation_capacity(rate: float | None) -> float:
        # Operations use a one-token burst. This paces small-object workloads
        # instead of permitting a full second's worth to arrive at once.
        return 0.0 if rate is None else 1.0

    def _refill_operation_locked(self, tier: str, now: float) -> None:
        rate = self._config.tiers[tier].operations_per_second
        previous = self._operation_updated[tier]
        self._operation_updated[tier] = now
        if rate is None:
            self._operation_tokens[tier] = 0.0
            return
        elapsed = max(0.0, now - previous)
        capacity = self._operation_capacity(rate)
        self._operation_tokens[tier] = min(capacity, self._operation_tokens[tier] + elapsed * rate)

    @staticmethod
    def _operation_tiers(source: str, destination: str) -> tuple[str, ...]:
        return (source,) if source == destination else (source, destination)

    def _operation_delay_locked(self, source: str, destination: str, now: float) -> float:
        delay = 0.0
        for tier in self._operation_tiers(source, destination):
            self._refill_operation_locked(tier, now)
            rate = self._config.tiers[tier].operations_per_second
            if rate is None:
                continue
            deficit = max(0.0, 1.0 - self._operation_tokens[tier])
            delay = max(delay, deficit / rate)
        return delay

    def _operation_blocked_tiers_locked(
        self, source: str, destination: str, now: float
    ) -> tuple[str, ...]:
        blocked: list[str] = []
        for tier in self._operation_tiers(source, destination):
            self._refill_operation_locked(tier, now)
            rate = self._config.tiers[tier].operations_per_second
            if rate is not None and self._operation_tokens[tier] < 1.0:
                blocked.append(tier)
        return tuple(blocked)

    def _consume_operation_locked(self, source: str, destination: str, now: float) -> None:
        for tier in self._operation_tiers(source, destination):
            self._refill_operation_locked(tier, now)
            if self._config.tiers[tier].operations_per_second is not None:
                self._operation_tokens[tier] = max(0.0, self._operation_tokens[tier] - 1.0)
            self._operations_consumed[tier] += 1

    def _mark_operation_throttle_locked(self, waiter: _AdmissionWaiter, now: float) -> None:
        if waiter.operation_throttle_started is not None:
            return
        blocked = self._operation_blocked_tiers_locked(waiter.source, waiter.destination, now)
        if not blocked:
            return
        waiter.operation_throttle_started = now
        waiter.operation_throttle_tiers = blocked
        self._throttled += 1
        self._throttle_events += 1
        for tier in blocked:
            self._tier_throttled[tier] += 1
            self._tier_throttle_events[tier] += 1

    def _finish_operation_throttle_locked(self, waiter: _AdmissionWaiter, now: float) -> None:
        started = waiter.operation_throttle_started
        if started is None:
            return
        elapsed = max(0.0, now - started)
        self._throttled -= 1
        self._throttle_seconds += elapsed
        for tier in waiter.operation_throttle_tiers:
            self._tier_throttled[tier] -= 1
            self._tier_throttle_seconds[tier] += elapsed
        waiter.operation_throttle_started = None

    def _grant_locked(self, waiter: _AdmissionWaiter, now: float) -> None:
        self._consume_operation_locked(waiter.source, waiter.destination, now)
        self._finish_operation_throttle_locked(waiter, now)
        self._active_source[waiter.source] += 1
        self._active_destination[waiter.destination] += 1
        waiter.granted = True
        if not waiter.future.done():
            waiter.future.set_result(None)

    def _dispatch_locked(self, now: float) -> None:
        wake_delay: float | None = None
        while self._lane_order:
            selected: tuple[int, _AdmissionWaiter] | None = None
            lane_count = len(self._lane_order)
            for offset in range(lane_count):
                index = (self._lane_cursor + offset) % lane_count
                lane = self._lane_order[index]
                queue = self._lanes[lane]
                while queue and queue[0].future.cancelled():
                    cancelled = queue.popleft()
                    self._finish_operation_throttle_locked(cancelled, now)
                if not queue:
                    continue
                waiter = queue[0]
                if not self._has_concurrency_locked(waiter.source, waiter.destination):
                    continue
                delay = self._operation_delay_locked(waiter.source, waiter.destination, now)
                if delay > 0:
                    self._mark_operation_throttle_locked(waiter, now)
                    wake_delay = delay if wake_delay is None else min(wake_delay, delay)
                    continue
                selected = (index, waiter)
                break

            if selected is None:
                break
            index, waiter = selected
            self._lanes[(waiter.source, waiter.destination)].popleft()
            self._lane_cursor = (index + 1) % len(self._lane_order)
            self._grant_locked(waiter, now)
            now = self._clock()

        self._schedule_operation_wakeup_locked(wake_delay)

    def _schedule_operation_wakeup_locked(self, delay: float | None) -> None:
        if self._operation_wakeup is not None:
            self._operation_wakeup.cancel()
            self._operation_wakeup = None
        if delay is None or self._queue_depth_locked() == 0:
            return
        loop = asyncio.get_running_loop()
        self._operation_wakeup = loop.call_later(
            max(0.0, delay),
            lambda: asyncio.create_task(self._wake_operation_waiters()),
        )

    async def _wake_operation_waiters(self) -> None:
        async with self._async_lock:
            with self._state_lock:
                self._operation_wakeup = None
                self._dispatch_locked(self._clock())

    async def reconfigure(self, config: ThroughputConfig) -> None:
        """Apply new limits without cancelling or revoking admitted work."""

        if not isinstance(config, ThroughputConfig):
            raise TypeError("config must be ThroughputConfig")
        async with self._async_lock:
            with self._state_lock:
                if set(config.tiers) != set(self._config.tiers):
                    raise ValueError("throughput reconfiguration cannot add or remove tiers")
                if config.max_queue_depth != self._config.max_queue_depth:
                    raise ValueError("throughput reconfiguration cannot change max_queue_depth")
                now = self._clock()
                old_config = self._config
                for tier in old_config.tiers:
                    self._refill_operation_locked(tier, now)
                    self._refill_bytes_locked(tier, now)

                self._config = config
                for tier, limits in config.tiers.items():
                    old_limits = old_config.tiers[tier]
                    if limits.operations_per_second is None:
                        self._operation_tokens[tier] = 0.0
                    elif old_limits.operations_per_second is None:
                        self._operation_tokens[tier] = self._operation_capacity(
                            limits.operations_per_second
                        )
                    else:
                        self._operation_tokens[tier] = min(
                            self._operation_capacity(limits.operations_per_second),
                            self._operation_tokens[tier],
                        )
                    self._operation_updated[tier] = now

                    if limits.bytes_per_second is None:
                        self._byte_tokens[tier] = 0.0
                    elif old_limits.bytes_per_second is None:
                        self._byte_tokens[tier] = limits.bytes_per_second
                    else:
                        self._byte_tokens[tier] = min(
                            limits.bytes_per_second, self._byte_tokens[tier]
                        )
                    self._byte_updated[tier] = now

                self._rate_generation += 1
                self._rate_condition.notify_all()
                self._dispatch_locked(now)

    def _refill_bytes_locked(self, tier: str, now: float) -> None:
        rate = self._config.tiers[tier].bytes_per_second
        previous = self._byte_updated[tier]
        self._byte_updated[tier] = now
        if rate is None:
            self._byte_tokens[tier] = 0.0
            return
        elapsed = max(0.0, now - previous)
        self._byte_tokens[tier] = min(rate, self._byte_tokens[tier] + elapsed * rate)

    def consume_bytes(
        self,
        tier: str,
        amount: int,
        on_wait: Callable[[], None] | None = None,
    ) -> None:
        """Synchronously consume bytes from one tier's fair token bucket.

        ``on_wait`` is called at least once per second while throttled. A mover
        can use it to renew a durable lease without coupling this controller to
        catalog persistence.
        """

        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise ValueError("amount must be a non-negative integer")
        with self._state_lock:
            self._validate_tier(tier)
            if amount == 0:
                return
            if self._config.tiers[tier].bytes_per_second is None:
                self._bytes_consumed[tier] += amount
                return
            waiter = _ByteWaiter(amount=amount, remaining=float(amount))
            queue: deque[_ByteWaiter] = self._byte_waiters[tier]
            queue.append(waiter)

        marked = False
        started = 0.0
        try:
            while True:
                callback_needed = False
                generation = 0
                timeout = _RATE_WAIT_SLICE
                with self._rate_condition:
                    now = self._clock()
                    self._refill_bytes_locked(tier, now)
                    rate = self._config.tiers[tier].bytes_per_second
                    queue = self._byte_waiters[tier]
                    if rate is None:
                        waiter.remaining = 0
                    elif queue and queue[0] is waiter:
                        consumed = min(waiter.remaining, self._byte_tokens[tier])
                        waiter.remaining -= consumed
                        self._byte_tokens[tier] -= consumed

                    if waiter.remaining <= 0:
                        try:
                            queue.remove(waiter)
                        except ValueError:
                            pass
                        self._bytes_consumed[tier] += amount
                        if marked:
                            self._finish_byte_throttle_locked(tier, started, now)
                        self._rate_generation += 1
                        self._rate_condition.notify_all()
                        return

                    if not marked:
                        marked = True
                        started = now
                        self._throttled += 1
                        self._throttle_events += 1
                        self._tier_throttled[tier] += 1
                        self._tier_throttle_events[tier] += 1
                    callback_needed = True
                    generation = self._rate_generation
                    if queue and queue[0] is waiter and rate is not None:
                        timeout = min(
                            _RATE_WAIT_SLICE,
                            max(0.000_001, waiter.remaining / rate),
                        )

                if callback_needed and on_wait is not None:
                    on_wait()

                with self._rate_condition:
                    if generation == self._rate_generation:
                        self._rate_condition.wait(timeout=timeout)
        except BaseException:
            with self._rate_condition:
                now = self._clock()
                queue = self._byte_waiters[tier]
                try:
                    queue.remove(waiter)
                except ValueError:
                    pass
                if marked:
                    self._finish_byte_throttle_locked(tier, started, now)
                self._rate_generation += 1
                self._rate_condition.notify_all()
            raise

    def _finish_byte_throttle_locked(self, tier: str, started: float, now: float) -> None:
        elapsed = max(0.0, now - started)
        self._throttled -= 1
        self._throttle_seconds += elapsed
        self._tier_throttled[tier] -= 1
        self._tier_throttle_seconds[tier] += elapsed

    def snapshot(self) -> ThroughputSnapshot:
        with self._state_lock:
            source_waiting = {tier: 0 for tier in self._config.tiers}
            destination_waiting = {tier: 0 for tier in self._config.tiers}
            for queue in self._lanes.values():
                for waiter in queue:
                    source_waiting[waiter.source] += 1
                    destination_waiting[waiter.destination] += 1

            tiers: dict[str, TierThroughputSnapshot] = {}
            for tier, limits in self._config.tiers.items():
                tiers[tier] = TierThroughputSnapshot(
                    source=RoleSnapshot(
                        active=self._active_source[tier],
                        waiting=source_waiting[tier],
                        capacity=limits.source_concurrency,
                    ),
                    destination=RoleSnapshot(
                        active=self._active_destination[tier],
                        waiting=destination_waiting[tier],
                        capacity=limits.destination_concurrency,
                    ),
                    bytes_per_second=limits.bytes_per_second,
                    operations_per_second=limits.operations_per_second,
                    bytes_consumed=self._bytes_consumed[tier],
                    operations_consumed=self._operations_consumed[tier],
                    throttled=self._tier_throttled[tier],
                    throttle_events=self._tier_throttle_events[tier],
                    throttle_seconds=self._tier_throttle_seconds[tier],
                )

            queue_depth = self._queue_depth_locked()
            active_jobs = sum(self._active_source.values())
            saturated = queue_depth >= self._config.max_queue_depth or any(
                snapshot.source.saturated or snapshot.destination.saturated
                for snapshot in tiers.values()
            )
            return ThroughputSnapshot(
                active_jobs=active_jobs,
                queue_depth=queue_depth,
                queue_capacity=self._config.max_queue_depth,
                throttled=self._throttled,
                throttle_events=self._throttle_events,
                throttle_seconds=self._throttle_seconds,
                saturation_events=self._saturation_events,
                saturated=saturated,
                tiers=MappingProxyType(tiers),
            )
