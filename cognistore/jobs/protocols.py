from __future__ import annotations

from datetime import datetime
from typing import Mapping, Protocol

from .models import (
    DeadLetterReceipt,
    DeadLetterRecord,
    EnqueueReceipt,
    JobEnvelope,
    QueueHealth,
    RedriveReceipt,
)


class JobDelivery(Protocol):
    @property
    def job(self) -> JobEnvelope: ...

    @property
    def raw_data(self) -> bytes: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    @property
    def attempt(self) -> int: ...

    @property
    def source_stream(self) -> str: ...

    @property
    def source_published_at(self) -> datetime: ...

    @property
    def source_consumer(self) -> str: ...

    @property
    def stream_sequence(self) -> int: ...

    @property
    def consumer_sequence(self) -> int: ...

    async def ack(self) -> None: ...

    async def nack(self, delay: float | None = None) -> None: ...

    async def in_progress(self) -> None: ...


class JobQueue(Protocol):
    async def connect(self) -> None: ...

    async def enqueue(
        self, job: JobEnvelope, *, message_id: str | None = None
    ) -> EnqueueReceipt: ...

    async def claim(self, timeout: float) -> JobDelivery | None: ...

    async def publish_dead_letter(
        self, record: DeadLetterRecord
    ) -> DeadLetterReceipt: ...

    async def get_dead_letter(self, dead_letter_id: str) -> DeadLetterRecord: ...

    async def redrive_dead_letter(self, dead_letter_id: str) -> RedriveReceipt: ...

    async def probe(self) -> QueueHealth: ...

    async def close(self, *, graceful: bool = True) -> None: ...
