from __future__ import annotations

import asyncio
import json

from cognistore.core.throughput import (
    ThroughputConfig,
    ThroughputController,
    TierLimits,
)
from cognistore.jobs.health import HealthServer
from cognistore.jobs.models import BusState, QueueHealth
from cognistore.jobs.runtime import AsyncWorker, WorkerConfig, WorkerSnapshot, WorkerState


class IdleQueue:
    def __init__(self) -> None:
        self.connected = False
        self.ready = True

    async def connect(self) -> None:
        self.connected = True

    async def enqueue(self, job):  # pragma: no cover - worker-only fake
        raise NotImplementedError

    async def claim(self, timeout: float):
        await asyncio.sleep(timeout)
        return None

    async def probe(self) -> QueueHealth:
        return QueueHealth(
            state=BusState.CONNECTED if self.connected else BusState.DISCONNECTED,
            ready=self.connected and self.ready,
            jetstream=self.connected and self.ready,
            stream="HEALTH_JOBS",
            consumer="health-workers",
        )

    async def close(self, *, graceful: bool = True) -> None:
        self.connected = False


async def _get(port: int, path: str) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
    )
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    headers, encoded = response.split(b"\r\n\r\n", 1)
    status = int(headers.split(b" ", 2)[1])
    return status, json.loads(encoded)


def test_health_snapshots_preserve_legacy_positional_constructors() -> None:
    bus = QueueHealth(
        BusState.DISCONNECTED,
        False,
        False,
        "JOBS",
        "workers",
        1,
        2,
        3,
        "legacy error",
    )
    snapshot = WorkerSnapshot(
        WorkerState.STOPPED,
        False,
        False,
        False,
        0,
        (),
        1,
        2,
        3,
        4,
        None,
        None,
        bus,
    )

    assert bus.error == "legacy error"
    assert bus.stored_messages is None
    assert snapshot.max_in_flight == 1
    assert snapshot.throughput is None


def test_health_and_readiness_expose_worker_and_bus_status() -> None:
    async def scenario() -> None:
        queue = IdleQueue()

        async def handler(job, context) -> None:
            return None

        worker = AsyncWorker(
            queue,
            {"test": handler},
            config=WorkerConfig(
                fetch_timeout=0.05,
                heartbeat_interval=0,
                shutdown_grace=0.1,
                settlement_timeout=0.1,
            ),
            throughput=ThroughputController(
                ThroughputConfig(
                    max_queue_depth=4,
                    tiers={"hot": TierLimits(), "warm": TierLimits()},
                )
            ),
        )
        await worker.start()
        server = HealthServer(worker, port=0)
        await server.start()
        assert server.bound_port is not None
        try:
            health_status, health = await _get(server.bound_port, "/healthz")
            ready_status, ready = await _get(server.bound_port, "/readyz")
            assert health_status == 200
            assert ready_status == 200
            assert health["worker"]["live"] is True
            assert ready["worker"]["ready"] is True
            assert ready["bus"]["state"] == "connected"
            assert ready["bus"]["jetstream"] is True
            assert ready["throughput"]["active_jobs"] == 0
            assert ready["throughput"]["queue_depth"] == 0
            assert ready["throughput"]["queue_capacity"] == 4
            assert ready["throughput"]["tiers"]["hot"]["source"]["capacity"] == 1

            queue.ready = False
            ready_status, ready = await _get(server.bound_port, "/readyz")
            assert ready_status == 503
            assert ready["worker"]["live"] is True
            assert ready["worker"]["ready"] is False
            assert ready["bus"]["ready"] is False

            worker.request_shutdown()
            health_status, health = await _get(server.bound_port, "/healthz")
            ready_status, _ = await _get(server.bound_port, "/readyz")
            assert health_status == 200
            assert health["worker"]["state"] == "draining"
            assert ready_status == 503
            await worker.shutdown()

            health_status, health = await _get(server.bound_port, "/healthz")
            assert health_status == 503
            assert health["worker"]["state"] == "stopped"
        finally:
            await server.close()

    asyncio.run(scenario())
