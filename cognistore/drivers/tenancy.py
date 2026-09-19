"""Explicit tenant namespaces at the storage boundary, including lazy IO."""

from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import AbstractContextManager, contextmanager
from hashlib import sha256
from typing import Any

from cognistore.auth.tenancy import DEFAULT_TENANT_ID, require_tenant, validate_tenant_id

from .storage_driver import (
    MAX_LISTING_PAGE_SIZE,
    ReadableStream,
    StorageDriver,
    StorageListingPage,
    decode_listing_cursor,
    encode_listing_cursor,
    validate_listing_request,
)

TENANT_STORAGE_DIRECTORY = ".cognistore-tenants"


class _TenantReader:
    def __init__(self, reader: ReadableStream, tenant_id: str) -> None:
        self.reader, self.tenant_id = reader, tenant_id

    def read(self, size: int = -1) -> bytes:
        require_tenant(self.tenant_id)
        return self.reader.read(size)


class TenantStorageDriver(StorageDriver):
    """Keep logical coordinates while isolating each tenant's physical keys.

    The default tenant retains legacy paths. Its reserved tenant directory is
    inaccessible, including through listing. Buckets must be single components
    and keys must be relative paths so filesystem backends cannot normalize an
    input into another tenant's namespace.
    """

    def __init__(self, driver: StorageDriver, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self._tenant_id = validate_tenant_id(tenant_id)
        require_tenant(self.tenant_id)
        self.driver: StorageDriver = (
            driver.driver if isinstance(driver, TenantStorageDriver) else driver
        )
        self.capabilities = self.driver.capabilities
        digest = sha256(self.tenant_id.encode("utf-8")).hexdigest()
        self._prefix = (
            "" if self.tenant_id == DEFAULT_TENANT_ID
            else f"{TENANT_STORAGE_DIRECTORY}/{digest}/"
        )
        self._cursor_backend = f"tenant-storage:{digest}"

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    def for_tenant(self, tenant_id: str) -> TenantStorageDriver:
        return self if tenant_id == self.tenant_id else TenantStorageDriver(self.driver, tenant_id)

    def _bucket(self, bucket: str) -> str:
        require_tenant(self.tenant_id)
        if (
            not isinstance(bucket, str) or not bucket or bucket in {".", ".."}
            or any(character in bucket for character in ("/", "\\", "\0"))
            or bucket == TENANT_STORAGE_DIRECTORY
        ):
            raise ValueError("tenant storage bucket must be a single relative path component")
        return bucket

    def _key(self, key: str, *, prefix: bool = False) -> str:
        if (
            not isinstance(key, str) or (not key and not prefix)
            or key.startswith("/") or "\\" in key or "\0" in key
            or any(part in {".", ".."} for part in key.split("/"))
            or key.split("/", 1)[0] == TENANT_STORAGE_DIRECTORY
        ):
            raise ValueError("tenant storage key must remain inside its namespace")
        return self._prefix + key

    def _logical_key(self, key: str, prefix: str) -> str | None:
        if not isinstance(key, str) or not key.startswith(self._prefix):
            return None
        logical = key[len(self._prefix):]
        if not logical.startswith(prefix):
            return None
        try:
            self._key(logical)
        except ValueError:
            return None
        return logical

    def put_object(
        self, bucket: str, key: str, data: bytes, range: str | None = None,
        overwrite: bool = True, **opts: Any,
    ) -> None:
        self.driver.put_object(
            self._bucket(bucket), self._key(key), data, range=range, overwrite=overwrite, **opts
        )

    def get_object(self, bucket: str, key: str, range: str | None = None) -> bytes:
        return self.driver.get_object(self._bucket(bucket), self._key(key), range=range)

    def open_object_reader(
        self, bucket: str, key: str, range: str | None = None,
    ) -> AbstractContextManager[ReadableStream]:
        return self._reader(
            self.driver.open_object_reader(self._bucket(bucket), self._key(key), range=range)
        )

    @contextmanager
    def _reader(
        self, context: AbstractContextManager[ReadableStream],
    ) -> Generator[ReadableStream, None, None]:
        require_tenant(self.tenant_id)
        with context as reader:
            yield _TenantReader(reader, self.tenant_id)

    def open_object_reader_if_generation(
        self, bucket: str, key: str, generation: str, range: str | None = None,
    ) -> AbstractContextManager[ReadableStream]:
        return self._reader(
            self.driver.open_object_reader_if_generation(
                self._bucket(bucket), self._key(key), generation, range=range
            )
        )

    def put_object_stream(
        self, bucket: str, key: str, source: ReadableStream, *, size: int,
        overwrite: bool = True, metadata: Mapping[str, Any] | None = None,
    ) -> int:
        return self.driver.put_object_stream(
            self._bucket(bucket), self._key(key), source, size=size,
            overwrite=overwrite, metadata=metadata,
        )

    def delete_object(self, bucket: str, key: str) -> None:
        self.driver.delete_object(self._bucket(bucket), self._key(key))

    def delete_object_if_generation(self, bucket: str, key: str, generation: str) -> bool:
        return self.driver.delete_object_if_generation(
            self._bucket(bucket), self._key(key), generation
        )

    def list_objects(self, bucket: str, prefix: str = "") -> Generator[str, None, None]:
        physical_bucket, physical_prefix = self._bucket(bucket), self._key(prefix, prefix=True)

        def objects() -> Generator[str, None, None]:
            require_tenant(self.tenant_id)
            iterator = self.driver.list_objects(physical_bucket, physical_prefix)
            try:
                for key in iterator:
                    require_tenant(self.tenant_id)
                    logical = self._logical_key(key, prefix)
                    if logical is not None:
                        yield logical
            finally:
                iterator.close()

        return objects()

    def list_objects_page(
        self, bucket: str, prefix: str = "", *, cursor: str | None = None,
        limit: int = MAX_LISTING_PAGE_SIZE,
    ) -> StorageListingPage:
        validate_listing_request(bucket, prefix, cursor, limit)
        physical_bucket, physical_prefix = self._bucket(bucket), self._key(prefix, prefix=True)
        backend_cursor = (
            decode_listing_cursor(cursor, self._cursor_backend, bucket, prefix)
            if cursor is not None else None
        )
        keys: list[str] = []
        while True:
            page = self.driver.list_objects_page(
                physical_bucket, physical_prefix, cursor=backend_cursor, limit=limit - len(keys)
            )
            keys.extend(
                logical for key in page.keys
                if (logical := self._logical_key(key, prefix)) is not None
            )
            # Never return a cursor positioned on a hidden tenant key: POSIX
            # cursors encode that coordinate. Fill through filtered pages so
            # the terminal coordinate is public, or exhaust the listing.
            if page.next_cursor is None or len(keys) == limit:
                break
            if page.next_cursor == backend_cursor:
                raise RuntimeError("Storage listing cursor did not advance")
            backend_cursor = page.next_cursor
        next_backend_cursor = page.next_cursor
        if not self._prefix and next_backend_cursor is not None:
            probe_cursor = next_backend_cursor
            while True:
                probe = self.driver.list_objects_page(
                    physical_bucket, physical_prefix, cursor=probe_cursor, limit=1
                )
                if any(self._logical_key(key, prefix) is not None for key in probe.keys):
                    break
                if probe.next_cursor is None:
                    # A continuation means another visible object exists,
                    # never merely that another tenant owns remaining keys.
                    next_backend_cursor = None
                    break
                if probe.next_cursor == probe_cursor:
                    raise RuntimeError("Storage listing cursor did not advance")
                probe_cursor = probe.next_cursor
        next_cursor = (
            encode_listing_cursor(self._cursor_backend, bucket, prefix, next_backend_cursor)
            if next_backend_cursor is not None else None
        )
        return StorageListingPage(tuple(keys), next_cursor)

    def stat_object(self, bucket: str, key: str) -> dict[str, Any]:
        return self.driver.stat_object(self._bucket(bucket), self._key(key))

    def object_generation(self, bucket: str, key: str) -> str:
        return self.driver.object_generation(self._bucket(bucket), self._key(key))

    def ensure_object_durable(self, bucket: str, key: str) -> None:
        self.driver.ensure_object_durable(self._bucket(bucket), self._key(key))

    def same_backend(self, other: StorageDriver) -> bool:
        return (
            isinstance(other, TenantStorageDriver)
            and self.tenant_id == other.tenant_id
            and self.driver.same_backend(other.driver)
        )


def scope_storage_drivers(
    drivers: Mapping[str, StorageDriver], tenant_id: str,
) -> dict[str, StorageDriver]:
    return {name: TenantStorageDriver(driver, tenant_id) for name, driver in drivers.items()}
