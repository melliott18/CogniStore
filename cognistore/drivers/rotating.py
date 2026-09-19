"""Storage handles that renew credential bundles without interrupting leased IO."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, TypeVar

from cognistore.secrets import SecretAccessError, SecretReference, SecretResolver
from cognistore.secrets.diagnostics import secret_operation

from .storage_driver import (
    MAX_LISTING_PAGE_SIZE,
    DriverCapabilities,
    ObjectGenerationMismatchError,
    ReadableStream,
    StorageDriver,
    StorageListingPage,
)

_T = TypeVar("_T")
_LOG = logging.getLogger(__name__)


def _safe_error(error: Exception) -> Exception:
    """Keep contract/retry categories without reflecting SDK messages or context."""
    for category in (
        ObjectGenerationMismatchError,
        FileNotFoundError,
        FileExistsError,
        PermissionError,
        ConnectionError,
        TimeoutError,
        NotImplementedError,
        ValueError,
        TypeError,
        OSError,
    ):
        if isinstance(error, category):
            if category is OSError and isinstance(error, OSError):
                return OSError(error.errno, "Secret-backed storage operation failed")
            return category("Secret-backed storage operation failed")
    # Import lazily: the job classifier imports mover/driver definitions. Copy
    # only its fixed category, never a provider-supplied message or error code.
    from cognistore.jobs.retry import FailureCategory, classify_job_error

    classification = classify_job_error(error)
    if classification.category == FailureCategory.AUTHORIZATION:
        return PermissionError("Secret-backed storage operation failed")
    if not classification.retryable:
        return ValueError("Secret-backed storage operation failed")
    if classification.category == FailureCategory.TIMEOUT:
        return TimeoutError("Secret-backed storage operation failed")
    if classification.category == FailureCategory.UNAVAILABLE:
        return ConnectionError("Secret-backed storage operation failed")
    if classification.category in {FailureCategory.THROTTLED, FailureCategory.CONFLICT}:
        failure = _StorageStatusError("Secret-backed storage operation failed")
        failure.status_code = 429 if classification.category == FailureCategory.THROTTLED else 409
        return failure
    return RuntimeError("Secret-backed storage operation failed")


class _StorageStatusError(RuntimeError):
    status_code: int


def _safe_call(function: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    try:
        with secret_operation():
            return function(*args, **kwargs)
    except Exception as error:
        failure = _safe_error(error)
    # Raising outside the handler also removes hidden exception context from
    # classifiers that inspect more than the user-facing traceback.
    raise failure


@dataclass(eq=False)
class _Backend:
    driver: StorageDriver = field(repr=False)
    values: tuple[str, ...] = field(repr=False)
    leases: int = 0
    retired: bool = False


class _SafeReader:
    def __init__(self, reader: ReadableStream) -> None:
        self._reader = reader

    def read(self, size: int = -1) -> bytes:
        return _safe_call(self._reader.read, size)


class RotatingStorageDriver(StorageDriver):
    """Keep one stable driver handle while credentials and backend clients rotate.

    Resolution happens at every operation boundary. Expired credentials cannot
    authorize a new operation if their provider is unavailable. An already-open
    reader, listing, or upload retains its original client until it finishes;
    this is the only grace period. A retired client closes after its last lease.
    References into one JSON secret are extracted from a single provider value.
    """

    def __init__(
        self,
        factory: Callable[..., StorageDriver],
        options: Mapping[str, Any],
        references: Mapping[str, SecretReference],
        resolver: SecretResolver,
        *,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._factory = factory
        self._options = dict(options)
        self._references = dict(references)
        self._resolver = resolver
        self._lock = RLock()
        self._current: _Backend | None = None
        self._closed = False
        self._on_close = on_close
        # Validate and authenticate eagerly, as ordinary configured drivers do.
        with self._lease():
            pass

    def _credentials(self) -> dict[str, str]:
        parents = dict.fromkeys(
            SecretReference(ref.provider, ref.name, ref.version)
            for ref in self._references.values()
        )
        # A single resolution per parent prevents access-key/secret-key fields
        # from straddling a provider rotation at a cache-expiry boundary.
        resolved = {
            ref: value.text()
            for ref, value in zip(parents, self._resolver.resolve_many(parents))
        }
        values: dict[str, str] = {}
        for option, ref in self._references.items():
            value = resolved[SecretReference(ref.provider, ref.name, ref.version)]
            if ref.field is not None:
                invalid = False
                try:
                    payload = json.loads(value)
                    value = payload[ref.field]
                    if not isinstance(value, str) or not value:
                        invalid = True
                except (ValueError, TypeError, KeyError, IndexError):
                    invalid = True
                if invalid:
                    raise SecretAccessError(
                        "Secret credential field must contain a non-empty string"
                    )
            if not value:
                raise SecretAccessError("Secret credential must contain a non-empty string")
            values[option] = value
        return values

    @staticmethod
    def _close_backend(backend: _Backend) -> None:
        close = getattr(backend.driver, "close", None)
        if callable(close):
            try:
                with secret_operation():
                    close()
            except Exception:
                # Retiring a client must neither expose SDK credential material
                # nor invalidate an already installed replacement client.
                _LOG.warning("Could not close retired secret-backed storage client")

    @contextmanager
    def _lease(self) -> Iterator[StorageDriver]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Secret-backed storage driver is closed")
            credentials = self._credentials()
            values = tuple(credentials.values())
            current = self._current
            if current is None or current.values != values:
                driver = _safe_call(self._factory, **{**self._options, **credentials})
                replacement = _Backend(driver, values)
                self._current = replacement
                if current is not None:
                    current.retired = True
                    if current.leases == 0:
                        self._close_backend(current)
                current = replacement
            current.leases += 1
        try:
            yield current.driver
        finally:
            with self._lock:
                current.leases -= 1
                if current.retired and current.leases == 0:
                    self._close_backend(current)

    def close(self) -> None:
        """Reject new operations and close clients once their active IO finishes."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._current is not None:
                current, self._current = self._current, None
                current.retired = True
                if current.leases == 0:
                    self._close_backend(current)
            if self._on_close is not None:
                self._on_close()

    @property
    def capabilities(self) -> DriverCapabilities:  # type: ignore[override]
        with self._lease() as driver:
            return driver.capabilities

    def _invoke(self, method: str, *args: Any, **kwargs: Any) -> Any:
        with self._lease() as driver:
            return _safe_call(getattr(driver, method), *args, **kwargs)

    def encryption_status(self) -> dict[str, Any]:
        with self._lease() as driver:
            return _safe_call(driver.encryption_status)

    def put_object(
        self, bucket: str, key: str, data: bytes, range: str | None = None,
        overwrite: bool = True, **opts: Any,
    ) -> None:
        self._invoke("put_object", bucket, key, data, range=range, overwrite=overwrite, **opts)

    def get_object(self, bucket: str, key: str, range: str | None = None) -> bytes:
        return self._invoke("get_object", bucket, key, range=range)

    @contextmanager
    def _reader(self, method: str, *args: Any, **kwargs: Any) -> Iterator[ReadableStream]:
        with self._lease() as driver:
            context = _safe_call(getattr(driver, method), *args, **kwargs)
            reader = _safe_call(context.__enter__)
            try:
                yield _SafeReader(reader)
            except BaseException:
                # Forward the consumer's exception unchanged; only backend
                # entry/read/exit failures pass through the secret-safe boundary.
                if not _safe_call(context.__exit__, *sys.exc_info()):
                    raise
            else:
                _safe_call(context.__exit__, None, None, None)

    def open_object_reader(
        self, bucket: str, key: str, range: str | None = None,
    ) -> AbstractContextManager[ReadableStream]:
        return self._reader("open_object_reader", bucket, key, range=range)

    def open_object_reader_if_generation(
        self, bucket: str, key: str, generation: str, range: str | None = None,
    ) -> AbstractContextManager[ReadableStream]:
        return self._reader(
            "open_object_reader_if_generation", bucket, key, generation, range=range
        )

    def put_object_stream(
        self, bucket: str, key: str, source: ReadableStream, *, size: int,
        overwrite: bool = True, metadata: Mapping[str, Any] | None = None,
    ) -> int:
        return self._invoke(
            "put_object_stream", bucket, key, source, size=size,
            overwrite=overwrite, metadata=metadata,
        )

    def delete_object(self, bucket: str, key: str) -> None:
        self._invoke("delete_object", bucket, key)

    def delete_object_if_generation(self, bucket: str, key: str, generation: str) -> bool:
        return self._invoke("delete_object_if_generation", bucket, key, generation)

    def list_objects(self, bucket: str, prefix: str = "") -> Generator[str, None, None]:
        with self._lease() as driver:
            iterator = _safe_call(driver.list_objects, bucket, prefix)
            try:
                while True:
                    failure = None
                    try:
                        with secret_operation():
                            key = next(iterator)
                    except StopIteration:
                        break
                    except Exception as error:
                        failure = _safe_error(error)
                    if failure is not None:
                        raise failure
                    yield key
            finally:
                close = getattr(iterator, "close", None)
                if callable(close):
                    _safe_call(close)

    def list_objects_page(
        self, bucket: str, prefix: str = "", *, cursor: str | None = None,
        limit: int = MAX_LISTING_PAGE_SIZE,
    ) -> StorageListingPage:
        return self._invoke("list_objects_page", bucket, prefix, cursor=cursor, limit=limit)

    def stat_object(self, bucket: str, key: str) -> dict[str, Any]:
        return self._invoke("stat_object", bucket, key)

    def object_generation(self, bucket: str, key: str) -> str:
        return self._invoke("object_generation", bucket, key)

    def ensure_object_durable(self, bucket: str, key: str) -> None:
        self._invoke("ensure_object_durable", bucket, key)

    def same_backend(self, other: StorageDriver) -> bool:
        with self._lease() as driver:
            if other is self:
                return True
            if isinstance(other, RotatingStorageDriver):
                with other._lease() as peer:
                    return _safe_call(driver.same_backend, peer)
            return _safe_call(driver.same_backend, other)
