"""Opt-in observation at the caller-facing storage boundary.

Backends remain unmodified so their internal stat/read calls and maintenance
work performed through raw drivers do not look like application demand.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any, Protocol
from uuid import uuid4

from cognistore.core.access import AccessConfig, AccessEvent, AccessKind, access_timestamp

from .storage_driver import ReadableStream, StorageDriver, StorageListingPage


class AccessEventSink(Protocol):
    def append_access_event(self, event: AccessEvent) -> AccessEvent: ...


@dataclass(frozen=True)
class AccessOperation:
    operation_id: str
    correlation_id: str | None = None
    source: str = "driver"


_operation: ContextVar[AccessOperation | None] = ContextVar("access_operation", default=None)
_suppressed: ContextVar[bool] = ContextVar("access_capture_suppressed", default=False)


@contextmanager
def access_operation(
    *,
    operation_id: str | None = None,
    correlation_id: str | None = None,
    source: str = "driver",
) -> Iterator[AccessOperation]:
    """Share one logical identity across retries and nested access boundaries."""

    operation = AccessOperation(operation_id or correlation_id or str(uuid4()), correlation_id, source)
    token = _operation.set(operation)
    try:
        yield operation
    finally:
        _operation.reset(token)


@contextmanager
def suppress_access_capture() -> Iterator[None]:
    """Exclude implementation details and maintenance from demand history."""

    token = _suppressed.set(True)
    try:
        yield
    finally:
        _suppressed.reset(token)


class AccessRecorder:
    """Prepare reproducibly sampled events and persist acknowledged accesses."""

    def __init__(self, sink: AccessEventSink, config: AccessConfig | None = None) -> None:
        self.sink = sink
        self.config = config or AccessConfig()

    def event(
        self,
        kind: AccessKind,
        bucket: str,
        key: str | None = None,
        *,
        tier: str | None = None,
        source: str = "driver",
    ) -> AccessEvent | None:
        """Freeze context now, even when a lazy stream runs on another thread."""

        if _suppressed.get():
            return None
        operation = _operation.get()
        event = AccessEvent.create(
            kind=kind,
            bucket=bucket,
            key=key,
            tier=tier,
            source=operation.source if operation is not None else source,
            operation_id=operation.operation_id if operation is not None else None,
            correlation_id=operation.correlation_id if operation is not None else None,
            sample_rate=self.config.sample_rate,
        )
        sample = int.from_bytes(sha256(event.event_id.encode("utf-8")).digest(), "big")
        return event if sample < int(self.config.sample_rate * (1 << 256)) else None

    def persist(self, event: AccessEvent | None) -> None:
        # Never silently turn a persistence outage into apparently complete
        # history. A caller may retry with the same operation identity.
        if event is not None:
            # A stream can be consumed long after it was prepared. Freeze its
            # identity early, but timestamp the successful observation now.
            self.sink.append_access_event(replace(event, occurred_at=access_timestamp()))


class _ObservedReader:
    def __init__(self, reader: ReadableStream, recorder: AccessRecorder, event: AccessEvent | None):
        self.reader = reader
        self.recorder = recorder
        self.event = event

    def read(self, size: int = -1) -> bytes:
        data = self.reader.read(size)
        if size != 0 and self.event is not None:
            # Reading an empty object is still demand. Persist before exposing
            # bytes; failed opens and failed first reads never create events.
            self.recorder.persist(self.event)
            self.event = None
        return data


class ObservedStorageDriver(StorageDriver):
    """Capture one successful public access, including POSIX and S3 streams.

    Use the raw backend for scans, moves, and policy work. For retries, reuse an
    ``access_operation`` identity. Deletion and durability checks are maintenance
    operations, so they do not contribute read/write/touch demand.
    """

    def __init__(
        self,
        driver: StorageDriver,
        sink: AccessEventSink,
        *,
        tier: str | None = None,
        config: AccessConfig | None = None,
        source: str = "driver",
    ) -> None:
        self.raw_driver: StorageDriver = (
            driver.raw_driver if isinstance(driver, ObservedStorageDriver) else driver
        )
        self.capabilities = self.raw_driver.capabilities
        self.recorder = AccessRecorder(sink, config)
        self.tier = tier
        self.source = source

    def encryption_status(self) -> dict[str, Any]:
        return self.raw_driver.encryption_status()

    def _event(self, kind: AccessKind, bucket: str, key: str | None = None) -> AccessEvent | None:
        return self.recorder.event(kind, bucket, key, tier=self.tier, source=self.source)

    def put_object(
        self,
        bucket: str,
        key: str,
        data: bytes,
        range: str | None = None,
        overwrite: bool = True,
        **opts: Any,
    ) -> None:
        event = self._event("write", bucket, key)
        self.raw_driver.put_object(bucket, key, data, range=range, overwrite=overwrite, **opts)
        self.recorder.persist(event)

    def get_object(self, bucket: str, key: str, range: str | None = None) -> bytes:
        event = self._event("read", bucket, key)
        data = self.raw_driver.get_object(bucket, key, range=range)
        self.recorder.persist(event)
        return data

    @contextmanager
    def _reader(
        self, reader: AbstractContextManager[ReadableStream], event: AccessEvent | None
    ) -> Iterator[ReadableStream]:
        with reader as opened:
            yield _ObservedReader(opened, self.recorder, event)

    def open_object_reader(
        self, bucket: str, key: str, range: str | None = None
    ) -> AbstractContextManager[ReadableStream]:
        event = self._event("read", bucket, key)
        return self._reader(self.raw_driver.open_object_reader(bucket, key, range=range), event)

    def open_object_reader_if_generation(
        self, bucket: str, key: str, generation: str, range: str | None = None
    ) -> AbstractContextManager[ReadableStream]:
        event = self._event("read", bucket, key)
        return self._reader(
            self.raw_driver.open_object_reader_if_generation(bucket, key, generation, range=range),
            event,
        )

    def put_object_stream(
        self,
        bucket: str,
        key: str,
        source: ReadableStream,
        *,
        size: int,
        overwrite: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        event = self._event("write", bucket, key)
        written = self.raw_driver.put_object_stream(
            bucket, key, source, size=size, overwrite=overwrite, metadata=metadata
        )
        self.recorder.persist(event)
        return written

    def list_objects(self, bucket: str, prefix: str = "") -> Generator[str, None, None]:
        event = self._event("list", bucket)

        def objects() -> Generator[str, None, None]:
            yield from self.raw_driver.list_objects(bucket, prefix)
            self.recorder.persist(event)

        return objects()

    def list_objects_page(
        self,
        bucket: str,
        prefix: str = "",
        *,
        cursor: str | None = None,
        limit: int = 1000,
    ) -> StorageListingPage:
        """Inventory maintenance never contributes application demand."""

        return self.raw_driver.list_objects_page(bucket, prefix, cursor=cursor, limit=limit)

    def stat_object(self, bucket: str, key: str) -> dict[str, Any]:
        event = self._event("touch", bucket, key)
        metadata = self.raw_driver.stat_object(bucket, key)
        self.recorder.persist(event)
        return metadata

    def object_generation(self, bucket: str, key: str) -> str:
        event = self._event("touch", bucket, key)
        generation = self.raw_driver.object_generation(bucket, key)
        self.recorder.persist(event)
        return generation

    def delete_object(self, bucket: str, key: str) -> None:
        self.raw_driver.delete_object(bucket, key)

    def delete_object_if_generation(self, bucket: str, key: str, generation: str) -> bool:
        return self.raw_driver.delete_object_if_generation(bucket, key, generation)

    def ensure_object_durable(self, bucket: str, key: str) -> None:
        self.raw_driver.ensure_object_durable(bucket, key)

    def same_backend(self, other: StorageDriver) -> bool:
        raw = other.raw_driver if isinstance(other, ObservedStorageDriver) else other
        return self.raw_driver.same_backend(raw)
