from __future__ import annotations

import os
import secrets
import stat
import sys
import threading
from contextlib import AbstractContextManager, contextmanager, nullcontext
from heapq import nsmallest
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, Generator, Iterator, Mapping, Optional

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - unavailable on non-POSIX platforms
    _fcntl = None  # type: ignore[assignment]

from cognistore.observability import instrument

from .storage_driver import (
    DEFAULT_STREAM_CHUNK_SIZE,
    DriverCapabilities,
    ObjectGenerationMismatchError,
    ReadableStream,
    StorageDriver,
    StorageListingPage,
    decode_listing_cursor,
    encode_listing_cursor,
    validate_listing_request,
    validate_object_generation,
)

_STAGING_DIRECTORY = ".cognistore-staging"
_STAGING_FILE_PREFIX = "upload-"
_STAGING_FILE_SUFFIX = ".tmp"
_MAX_STAGING_NAME_ATTEMPTS = 100
_DARWIN_FULL_SYNC = (
    getattr(_fcntl, "F_FULLFSYNC", None)
    if sys.platform == "darwin" and _fcntl is not None
    else None
)


def _sync_descriptor(descriptor: int) -> None:
    """Block until a file descriptor's state reaches durable storage."""

    if _DARWIN_FULL_SYNC is not None:
        # Darwin's fsync(2) only flushes through the host. F_FULLFSYNC also
        # requests that the drive flush its volatile write cache, which is the
        # barrier required before a move may discard its source.
        assert _fcntl is not None
        _fcntl.fcntl(descriptor, _DARWIN_FULL_SYNC)
        return
    os.fsync(descriptor)


class _RangedReader:
    """Restrict reads from an already-positioned file to one byte range."""

    def __init__(self, stream: BinaryIO, length: int) -> None:
        self._stream = stream
        self._remaining = length

    def read(self, size: int = -1) -> bytes:
        if self._remaining == 0:
            return b""
        if size < 0 or size > self._remaining:
            size = self._remaining
        data = self._stream.read(size)
        self._remaining -= len(data)
        return data


class _GenerationBoundReader:
    """Reject a descriptor mutation before exposing bytes from a read."""

    def __init__(
        self,
        reader: ReadableStream,
        validate_generation: Callable[[], None],
    ) -> None:
        self._reader = reader
        self._validate_generation = validate_generation

    def read(self, size: int = -1) -> bytes:
        self._validate_generation()
        data = self._reader.read(size)
        self._validate_generation()
        return data


class PosixDriver(StorageDriver):
    """POSIX filesystem-backed driver.

    Treats files as objects under a base directory, grouping by bucket.
    """

    capabilities = DriverCapabilities(
        range_reads=True,
        range_writes=True,
        atomic_no_overwrite=True,
        conditional_delete=True,
    )

    _lock_registry_guard = threading.Lock()
    _lock_registry: dict[tuple[str, str, str], threading.RLock] = {}

    def __init__(
        self,
        base_path: str,
        *,
        chunk_size: int = DEFAULT_STREAM_CHUNK_SIZE,
    ) -> None:
        if (
            isinstance(chunk_size, bool)
            or not isinstance(chunk_size, int)
            or chunk_size <= 0
        ):
            raise ValueError("POSIX chunk_size must be a positive integer")
        # Resolve the configured root once so every containment comparison uses
        # the same canonical path. Directories are created lazily by writes;
        # constructing a driver must remain read-only for dry-run workflows.
        self.base = Path(base_path).expanduser().resolve()
        self.chunk_size = chunk_size
        self._durable_directory_entries: set[Path] = set()

    @contextmanager
    def _object_lock(self, bucket: str, key: str) -> Generator[None, None, None]:
        """Serialize mutations for one object across local driver instances."""

        identity = (str(self.base), bucket, key)
        with self._lock_registry_guard:
            lock = self._lock_registry.setdefault(identity, threading.RLock())
        with lock:
            yield

    @staticmethod
    def _sync_directory(path: Path) -> None:
        """Persist namespace changes made in ``path``.

        Directory descriptors and ``O_DIRECTORY`` are POSIX facilities. Any
        failure is deliberately propagated: callers must not report a durable
        publication or deletion when the filesystem cannot provide the
        required namespace barrier.
        """

        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            _sync_descriptor(descriptor)
        finally:
            os.close(descriptor)

    def _ensure_directory(self, path: Path, *, mode: int = 0o777) -> None:
        """Create ``path`` and durably publish every newly created component."""

        path.mkdir(mode=mode, parents=True, exist_ok=True)
        try:
            relative = path.relative_to(self.base)
        except ValueError:
            raise ValueError(
                "POSIX object directories must remain below the configured tier root"
            ) from None

        # Work from the tier root down. Each parent barrier makes its child name
        # durable before that child is used to publish the next component. A
        # successful barrier is cached for this driver instance; failures are
        # not, so a retry cannot mistake a merely visible mkdir for a durable
        # one. A new driver also reconfirms every configured-root edge once.
        directory = self.base
        entries = [directory]
        for part in relative.parts:
            directory /= part
            entries.append(directory)
        for entry in entries:
            if entry in self._durable_directory_entries:
                continue
            self._sync_directory(entry.parent)
            self._durable_directory_entries.add(entry)

    def _publish_staged_file(
        self,
        temporary_path: Path,
        path: Path,
        *,
        overwrite: bool,
    ) -> None:
        """Publish a synced staging inode and persist both namespace changes."""

        staging = temporary_path.parent
        if overwrite:
            os.replace(temporary_path, path)
        else:
            os.link(temporary_path, path)

        # Persist the destination name before removing/persisting the staging
        # name. A crash between these barriers can leave an extra private link,
        # but cannot lose the acknowledged destination.
        self._sync_directory(path.parent)
        if not overwrite:
            temporary_path.unlink()
        self._sync_directory(staging)

    def _sync_existing_parent(self, path: Path) -> None:
        """Barrier a prior unlink when an idempotent retry sees no object."""

        # A never-created bucket remains an idempotent, read-only delete. Once
        # the parent is observed, however, every barrier failure must propagate;
        # do not confuse an fsync error with an absent namespace.
        if not path.parent.exists():
            return
        self._sync_directory(path.parent)

    @staticmethod
    def _generation(st: os.stat_result) -> str:
        return ":".join(
            str(value)
            for value in (
                st.st_dev,
                st.st_ino,
                st.st_size,
                st.st_mtime_ns,
                st.st_ctime_ns,
            )
        )

    @staticmethod
    def _relative_path(value: str, label: str, *, allow_empty: bool = False) -> Path:
        path = Path(value)
        if path.is_absolute() or path.drive:
            raise ValueError("Bucket and key must be relative paths")

        normalized = value.replace(os.altsep, os.sep) if os.altsep else value
        raw_parts = normalized.split(os.sep)
        if allow_empty and value == "":
            return path
        if not value or any(part in {"", ".", ".."} for part in raw_parts):
            raise ValueError(
                f"{label} path escapes configured tier root or contains "
                "ambiguous components"
            )
        return path

    def _path(self, bucket: str, key: str, *, allow_bucket_root: bool = False) -> Path:
        if self.base.is_symlink() or self.base.resolve() != self.base:
            raise ValueError("Configured tier root must not contain symbolic links")

        bucket_path = self._relative_path(bucket, "Bucket")
        if bucket_path.parts[0] == _STAGING_DIRECTORY:
            raise ValueError(
                f"Bucket path uses reserved namespace {_STAGING_DIRECTORY!r}"
            )
        key_path = self._relative_path(
            key,
            "Key",
            allow_empty=allow_bucket_root,
        )

        full = self.base / bucket_path / key_path
        try:
            full.relative_to(self.base)
        except ValueError:
            raise ValueError("Object path escapes configured tier root") from None
        if full == self.base:
            raise ValueError("Object path must be below configured tier root")

        current = self.base
        for part in full.relative_to(self.base).parts:
            current /= part
            if current.is_symlink():
                raise ValueError("Object path must not contain symbolic links")
        return full

    def _staging_directory(self) -> Path:
        """Return a private staging directory outside all object namespaces."""

        if self.base.is_symlink() or self.base.resolve() != self.base:
            raise ValueError("Configured tier root must not contain symbolic links")

        staging = self.base / _STAGING_DIRECTORY
        if staging.is_symlink():
            raise ValueError("POSIX staging directory must not be a symbolic link")
        try:
            self._ensure_directory(staging, mode=0o700)
        except FileExistsError:
            raise ValueError("POSIX staging path must be a directory") from None
        if (
            staging.is_symlink()
            or not staging.is_dir()
            or staging.resolve() != staging
        ):
            raise ValueError(
                "POSIX staging path must be a directory below the configured tier root"
            )
        return staging

    @staticmethod
    def _create_staging_file(staging: Path) -> tuple[int, Path]:
        """Create a private unique file while honoring the process umask."""

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        for _ in range(_MAX_STAGING_NAME_ATTEMPTS):
            name = (
                f"{_STAGING_FILE_PREFIX}{secrets.token_hex(16)}"
                f"{_STAGING_FILE_SUFFIX}"
            )
            path = staging / name
            try:
                # Unlike tempfile.mkstemp's fixed 0o600 mode, passing 0o666 to
                # os.open lets the kernel apply the process's current umask in
                # the same way as the driver's former open(path, "wb") writes.
                return os.open(path, flags, 0o666), path
            except FileExistsError:
                continue
        raise FileExistsError("Could not allocate a unique POSIX staging file")

    @instrument("driver", "put_object", backend="posix")
    def put_object(
        self,
        bucket: str,
        key: str,
        data: bytes,
        range: Optional[str] = None,
        overwrite: bool = True,
        **opts: Any,
    ) -> None:
        if not range:
            self.put_object_stream(
                bucket,
                key,
                BytesIO(data),
                size=len(data),
                overwrite=overwrite,
            )
            return

        with self._object_lock(bucket, key):
            path = self._path(bucket, key)
            self._ensure_directory(path.parent)

            start, end = [int(x) for x in range.replace("bytes=", "").split("-")]
            if path.exists():
                if not overwrite:
                    raise FileExistsError(path)
                with open(path, "r+b") as existing_destination:
                    existing_destination.seek(start)
                    existing_destination.write(data[: end - start + 1])
                    existing_destination.flush()
                    _sync_descriptor(existing_destination.fileno())
                return

            descriptor, temporary_path = self._create_staging_file(
                self._staging_directory()
            )
            try:
                try:
                    destination = os.fdopen(descriptor, "wb")
                except BaseException:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    raise
                with destination:
                    destination.truncate(end + 1)
                    destination.seek(start)
                    destination.write(data[: end - start + 1])
                    destination.flush()
                    _sync_descriptor(destination.fileno())
                    self._publish_staged_file(
                        temporary_path,
                        path,
                        overwrite=overwrite,
                    )
            finally:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

    @instrument("driver", "get_object", backend="posix")
    def get_object(
        self, bucket: str, key: str, range: Optional[str] = None
    ) -> bytes:
        with self.open_object_reader(bucket, key, range=range) as source:
            return source.read()

    @contextmanager
    @instrument("driver", "open_object_reader", backend="posix")
    def open_object_reader(
        self,
        bucket: str,
        key: str,
        range: Optional[str] = None,
    ) -> Iterator[ReadableStream]:
        with self._open_object_reader(
            bucket,
            key,
            range=range,
            generation=None,
        ) as reader:
            yield reader

    @contextmanager
    @instrument("driver", "open_object_reader_if_generation", backend="posix")
    def open_object_reader_if_generation(
        self,
        bucket: str,
        key: str,
        generation: str,
        range: Optional[str] = None,
    ) -> Iterator[ReadableStream]:
        generation = validate_object_generation(generation)
        with self._open_object_reader(
            bucket,
            key,
            range=range,
            generation=generation,
        ) as reader:
            yield reader

    def _open_object_reader(
        self,
        bucket: str,
        key: str,
        *,
        range: Optional[str],
        generation: str | None,
    ) -> AbstractContextManager[ReadableStream]:
        path = self._path(bucket, key)

        @contextmanager
        def reader() -> Generator[ReadableStream, None, None]:
            object_lock = (
                self._object_lock(bucket, key)
                if generation is not None
                else nullcontext()
            )
            with object_lock:
                with open(path, "rb") as stream:
                    opened_stat = os.fstat(stream.fileno())
                    if (
                        generation is not None
                        and self._generation(opened_stat) != generation
                    ):
                        raise ObjectGenerationMismatchError(
                            f"Object generation changed: {bucket}/{key}"
                        )

                    def validate_generation() -> None:
                        if generation is None:
                            return
                        current = os.fstat(stream.fileno())
                        if self._generation(current) == generation:
                            return

                        # Replacing a pathname unlinks the descriptor's inode
                        # and changes only its ctime/link count. The open file
                        # still exposes the accepted immutable bytes, so keep
                        # serving it while all content-relevant fields remain
                        # unchanged. An in-place write changes size or mtime
                        # (and keeps the inode linked) and is rejected before
                        # any bytes can escape.
                        detached_unchanged_generation = (
                            current.st_nlink < opened_stat.st_nlink
                            and (
                                current.st_dev,
                                current.st_ino,
                                current.st_size,
                                current.st_mtime_ns,
                            )
                            == (
                                opened_stat.st_dev,
                                opened_stat.st_ino,
                                opened_stat.st_size,
                                opened_stat.st_mtime_ns,
                            )
                        )
                        if not detached_unchanged_generation:
                            raise ObjectGenerationMismatchError(
                                f"Object generation changed: {bucket}/{key}"
                            )

                    if not range:
                        source: ReadableStream = stream
                    else:
                        start, end = [
                            int(x)
                            for x in range.replace("bytes=", "").split("-")
                        ]
                        stream.seek(start)
                        source = _RangedReader(stream, end - start + 1)
                    if generation is not None:
                        source = _GenerationBoundReader(
                            source,
                            validate_generation,
                        )
                    yield source
                    validate_generation()

        return reader()

    @instrument("driver", "put_object_stream", backend="posix")
    def put_object_stream(
        self,
        bucket: str,
        key: str,
        source: ReadableStream,
        *,
        size: int,
        overwrite: bool = True,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> int:
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("Object size must be a non-negative integer")

        path = self._path(bucket, key)
        self._ensure_directory(path.parent)
        descriptor, temporary_path = self._create_staging_file(
            self._staging_directory()
        )
        written = 0

        try:
            try:
                destination = os.fdopen(descriptor, "wb")
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            with destination:
                while written < size:
                    requested = min(self.chunk_size, size - written)
                    chunk = source.read(requested)
                    if not isinstance(chunk, bytes):
                        raise TypeError("Object stream read() must return bytes")
                    if len(chunk) > requested:
                        raise ValueError(
                            f"Object stream contains more than declared size of {size} bytes"
                        )
                    if not chunk:
                        raise ValueError(
                            f"Object stream ended after {written} bytes; expected {size}"
                        )
                    destination.write(chunk)
                    written += len(chunk)

                extra = source.read(1)
                if not isinstance(extra, bytes):
                    raise TypeError("Object stream read() must return bytes")
                if extra:
                    raise ValueError(
                        f"Object stream contains more than declared size of {size} bytes"
                    )

                with self._object_lock(bucket, key):
                    if overwrite:
                        try:
                            existing_mode = stat.S_IMODE(
                                path.stat(follow_symlinks=False).st_mode
                            )
                        except FileNotFoundError:
                            pass
                        else:
                            temporary_path.chmod(
                                existing_mode,
                                follow_symlinks=False,
                            )
                    destination.flush()
                    _sync_descriptor(destination.fileno())
                    self._publish_staged_file(
                        temporary_path,
                        path,
                        overwrite=overwrite,
                    )
            return written
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    @instrument("driver", "delete_object", backend="posix")
    def delete_object(self, bucket: str, key: str) -> None:
        with self._object_lock(bucket, key):
            path = self._path(bucket, key)
            try:
                path.unlink()
            except FileNotFoundError:
                self._sync_existing_parent(path)
                return
            self._sync_directory(path.parent)

    @instrument("driver", "delete_object_if_generation", backend="posix")
    def delete_object_if_generation(
        self, bucket: str, key: str, generation: str
    ) -> bool:
        with self._object_lock(bucket, key):
            path = self._path(bucket, key)
            try:
                current = self._generation(path.stat(follow_symlinks=False))
            except FileNotFoundError:
                self._sync_existing_parent(path)
                return False
            if current != generation:
                raise ObjectGenerationMismatchError(
                    f"Object generation changed: {bucket}/{key}"
                )
            path.unlink()
            self._sync_directory(path.parent)
            return True

    @instrument("driver", "ensure_object_durable", backend="posix")
    def ensure_object_durable(self, bucket: str, key: str) -> None:
        with self._object_lock(bucket, key):
            path = self._path(bucket, key)
            flags = os.O_RDONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                _sync_descriptor(descriptor)
            finally:
                os.close(descriptor)
            self._sync_directory(path.parent)
            staging = self.base / _STAGING_DIRECTORY
            if staging.exists():
                self._sync_directory(staging)

    @instrument("driver", "list_objects", backend="posix")
    def list_objects(self, bucket: str, prefix: str = "") -> Generator[str, None, None]:
        base = self._path(bucket, "", allow_bucket_root=True)
        if not base.exists():
            return
        for root, dirs, files in os.walk(base):
            dirs[:] = [
                name for name in dirs if not (Path(root) / name).is_symlink()
            ]
            for name in files:
                if (Path(root) / name).is_symlink():
                    continue
                rel = os.path.relpath(os.path.join(root, name), base)
                if rel.startswith(prefix):
                    yield rel

    def list_objects_page(
        self,
        bucket: str,
        prefix: str = "",
        *,
        cursor: str | None = None,
        limit: int = 1000,
    ) -> StorageListingPage:
        """Select a bounded keyset page while pruning unrelated subtrees.

        Filesystems have no portable sorted directory cursor. Candidate
        directories are reread on each call, but only ``limit + 1`` keys are
        retained, regardless of directory size. Completed subtrees and those
        outside the literal prefix are skipped without opening them.
        """

        validate_listing_request(bucket, prefix, cursor, limit)
        base = self._path(bucket, "", allow_bucket_root=True)
        backend = f"posix:{self.base}"
        after = decode_listing_cursor(cursor, backend, bucket, prefix) if cursor else None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            # Anchor every absolute path component, including the tier root's
            # ancestors. A prior _path check alone cannot prevent a directory
            # being replaced with a symlink between validation and traversal.
            descriptor = os.open(base.anchor, flags)
            try:
                for part in base.parts[1:]:
                    child = os.open(part, flags, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
            except BaseException:
                os.close(descriptor)
                raise
        except FileNotFoundError:
            if cursor is not None:
                raise
            return StorageListingPage((), None)

        def keys(directory: int, parent: str = "") -> Generator[str, None, None]:
            # scandir propagates permission and I/O failures; os.walk's default
            # error suppression would misreport these as a complete inventory.
            with os.scandir(directory) as entries:
                for entry in entries:
                    key = f"{parent}{entry.name}"
                    child_prefix = key + "/"
                    if not key.startswith(prefix) and not prefix.startswith(child_prefix):
                        continue
                    if (
                        after is not None
                        and child_prefix <= after
                        and not after.startswith(child_prefix)
                    ):
                        continue
                    mode = entry.stat(follow_symlinks=False).st_mode
                    if stat.S_ISDIR(mode):
                        child = os.open(entry.name, flags, dir_fd=directory)
                        try:
                            yield from keys(child, child_prefix)
                        finally:
                            os.close(child)
                    elif (
                        stat.S_ISREG(mode)
                        and key.startswith(prefix)
                        and (after is None or key > after)
                    ):
                        yield key

        try:
            selected = nsmallest(limit + 1, keys(descriptor))
        finally:
            os.close(descriptor)
        more = len(selected) > limit
        page = tuple(selected[:limit])
        next_cursor = encode_listing_cursor(backend, bucket, prefix, page[-1]) if more else None
        return StorageListingPage(page, next_cursor)

    @instrument("driver", "stat_object", backend="posix")
    def stat_object(self, bucket: str, key: str) -> Dict[str, Any]:
        path = self._path(bucket, key)
        st = path.stat()
        return {
            "size": st.st_size,
            "mtime": st.st_mtime,
            "path": str(path),
            "generation": self._generation(st),
        }

    def same_backend(self, other: StorageDriver) -> bool:
        return isinstance(other, PosixDriver) and self.base == other.base
