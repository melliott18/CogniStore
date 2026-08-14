from __future__ import annotations

from typing import Protocol

from .models import EnqueueReceipt, JobEnvelope, QueueHealth


class JobDelivery(Protocol):
    job: JobEnvelope
    attempt: int
    stream_sequence: int
    consumer_sequence: int

    async def ack(self) -> None: ...

    async def nack(self) -> None: ...

    async def in_progress(self) -> None: ...

class JobQueue(Protocol):
    async def connect(self) -> None: ...

    async def enqueue(self, job: JobEnvelope) -> EnqueueReceipt: ...

    async def claim(self, timeout: float) -> JobDelivery | None: ...

    async def probe(self) -> QueueHealth: ...

    async def close(self, *, graceful: bool = True) -> None: ...
