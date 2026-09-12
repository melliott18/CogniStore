from __future__ import annotations

import errno
import os
import secrets
import stat
import sys
import threading
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _Directory:
    path: Path
    fd: int


# Capture the real functions so syscall instrumentation does not change the
# platform capability test. Runtime filesystem errors still propagate.
_DIR_FD_OPERATIONS = (os.open, os.mkdir, os.stat, os.unlink, os.rename, os.link)
_LIST_DIRECTORY = os.listdir
_SCAN_DIRECTORY = os.scandir
_STAT = os.stat
_LINK = os.link


def _require_containment_support() -> None:
    if (
        os.name != "posix"
        or any(not getattr(os, flag, 0) for flag in (
            "O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK",
        ))
        or not all(operation in os.supports_dir_fd for operation in _DIR_FD_OPERATIONS)
        or _LIST_DIRECTORY not in os.supports_fd
        or _SCAN_DIRECTORY not in os.supports_fd
        or _STAT not in os.supports_follow_symlinks
        or _LINK not in os.supports_follow_symlinks
        or not hasattr(os, "fchmod")
    ):
        raise NotImplementedError(
            "POSIX containment requires no-follow descriptor-relative filesystem operations"
        )


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
        _require_containment_support()
        # Configuration is trusted. Canonicalize once (including system aliases
        # such as /tmp on Darwin), then never resolve object paths by name.
        # Construction remains read-only for dry-run workflows.
        self.base = Path(base_path).expanduser().resolve()
        self.chunk_size = chunk_size
        self._root_identity: tuple[int, int] | None = None
        self._root_identity_guard = threading.Lock()
        self._durable_directory_entries: set[tuple[Path, int, int, int, int]] = set()
        self._creation_boundary = self.base
        while not self._creation_boundary.parent.exists():
            self._creation_boundary = self._creation_boundary.parent
        try:
            with self._directory(self.base):
                pass
        except FileNotFoundError:
            pass

    @contextmanager
    def _object_lock(self, bucket: str, key: str) -> Generator[None, None, None]:
        """Serialize mutations for one object across local driver instances."""

        identity = (str(self.base), bucket, key)
        with self._lock_registry_guard:
            lock = self._lock_registry.setdefault(identity, threading.RLock())
        with lock:
            yield

    @staticmethod
    def _sync_directory(directory: _Directory) -> None:
        """Persist the same directory inode used by the namespace operation."""
        _sync_descriptor(directory.fd)

    def _check_directory(self, descriptor: int, path: Path) -> os.stat_result:
        st = os.fstat(descriptor)
        inside = path == self.base or self.base in path.parents
        owners = {os.geteuid()} if inside else {0, os.geteuid()}
        shared_sticky_ancestor = not inside and bool(st.st_mode & stat.S_ISVTX)
        if st.st_uid not in owners or (
            st.st_mode & 0o022 and not shared_sticky_ancestor
        ):
            raise PermissionError(
                f"POSIX containment requires trusted directory ownership and permissions: {path}"
            )
        if path == self.base:
            identity = (st.st_dev, st.st_ino)
            with self._root_identity_guard:
                if self._root_identity is None:
                    self._root_identity = identity
                elif identity != self._root_identity:
                    raise ValueError("Configured tier root directory was replaced")
        elif inside and self._root_identity is not None:
            if st.st_dev != self._root_identity[0]:
                raise ValueError("POSIX object directories must stay on the tier root filesystem")
        return st

    @contextmanager
    def _child_directory(
        self, parent: _Directory, name: str, *, create: bool = False, mode: int = 0o700,
    ) -> Generator[_Directory, None, None]:
        path = parent.path / name
        self._check_directory(parent.fd, parent.path)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            try:
                descriptor = os.open(name, flags, dir_fd=parent.fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(name, mode=mode, dir_fd=parent.fd)
                except FileExistsError:
                    pass
                descriptor = os.open(name, flags, dir_fd=parent.fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ValueError(
                    "POSIX tier root and object directories must be directories without symbolic links"
                ) from exc
            raise
        try:
            st = self._check_directory(descriptor, path)
            directory = _Directory(path, descriptor)
            if create and (
                path == self._creation_boundary or self._creation_boundary in path.parents
            ):
                parent_st = os.fstat(parent.fd)
                entry = (path, st.st_dev, st.st_ino, parent_st.st_dev, parent_st.st_ino)
                if entry not in self._durable_directory_entries:
                    self._sync_directory(parent)
                    self._durable_directory_entries.add(entry)
            yield directory
        finally:
            os.close(descriptor)

    @contextmanager
    def _directory(
        self, path: Path, *, create: bool = False, mode: int = 0o700,
    ) -> Generator[_Directory, None, None]:
        """Walk from / using only single-component, no-follow opens.

        All descriptors stay live through the operation. Namespace swaps can
        change names but cannot redirect any subsequent relative operation.
        See docs/posix_containment.md for the enforced deployment boundary.
        """
        path.relative_to(self.base)
        descriptor = os.open(
            self.base.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            directory = _Directory(Path(self.base.anchor), descriptor)
            self._check_directory(descriptor, directory.path)
            with ExitStack() as stack:
                for name in path.parts[1:]:
                    directory = stack.enter_context(self._child_directory(
                        directory, name, create=create, mode=mode,
                    ))
                yield directory
        finally:
            os.close(descriptor)

    def _ensure_directory(self, path: Path, *, mode: int = 0o700) -> None:
        with self._directory(path, create=True, mode=mode):
            pass

    def _regular_stat(self, st: os.stat_result) -> os.stat_result:
        if self._root_identity is not None and st.st_dev != self._root_identity[0]:
            raise ValueError("POSIX objects must stay on the tier root filesystem")
        if stat.S_ISLNK(st.st_mode):
            raise ValueError("Object path must not contain symbolic links")
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("POSIX objects must be regular files")
        if st.st_nlink != 1 and not self._only_staging_links(st):
            raise ValueError("POSIX objects must not have multiple hard links")
        return st

    def _only_staging_links(self, st: os.stat_result) -> bool:
        """Account for every extra link left by interrupted publication.

        A process can die after linking the destination but before unlinking
        its staging name. Such an object must remain available to recovery.
        Accept extra links only when all of them are observed in the secured
        staging directory; an unaccounted-for (possibly outside) alias still
        fails closed. This does not remove potentially active staging files.
        """
        if st.st_nlink < 2:
            return False
        with ExitStack() as stack:
            try:
                staging = stack.enter_context(self._directory(self.base / _STAGING_DIRECTORY))
            except FileNotFoundError:
                return False
            links = 0
            for name in os.listdir(staging.fd):
                if not (name.startswith(_STAGING_FILE_PREFIX) and name.endswith(_STAGING_FILE_SUFFIX)):
                    continue
                try:
                    alias = os.stat(name, dir_fd=staging.fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (alias.st_dev, alias.st_ino) == (st.st_dev, st.st_ino):
                    if alias.st_nlink != st.st_nlink:
                        return False
                    links += 1
            return links == st.st_nlink - 1

    def _stat_file(self, parent: _Directory, name: str) -> os.stat_result:
        return self._regular_stat(os.stat(name, dir_fd=parent.fd, follow_symlinks=False))

    @contextmanager
    def _open_file(
        self, parent: _Directory, name: str, *, writable: bool = False,
    ) -> Generator[BinaryIO, None, None]:
        flags = (os.O_RDWR if writable else os.O_RDONLY)
        flags |= os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        try:
            descriptor = os.open(name, flags, dir_fd=parent.fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ValueError("Object path must not contain symbolic links") from exc
            raise
        try:
            self._regular_stat(os.fstat(descriptor))
            stream = os.fdopen(descriptor, "r+b" if writable else "rb")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            yield stream

    def _publish_staged_file(
        self, staging: _Directory, name: str, parent: _Directory, key: str, *, overwrite: bool,
    ) -> None:
        if overwrite:
            os.replace(name, key, src_dir_fd=staging.fd, dst_dir_fd=parent.fd)
        else:
            os.link(
                name, key, src_dir_fd=staging.fd, dst_dir_fd=parent.fd,
                follow_symlinks=False,
            )
        self._sync_directory(parent)
        if not overwrite:
            os.unlink(name, dir_fd=staging.fd)
        self._sync_directory(staging)

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

        return full

    @contextmanager
    def _staged_file(self) -> Generator[tuple[_Directory, str, BinaryIO], None, None]:
        with self._directory(self.base / _STAGING_DIRECTORY, create=True) as staging:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            for _ in range(_MAX_STAGING_NAME_ATTEMPTS):
                name = f"{_STAGING_FILE_PREFIX}{secrets.token_hex(16)}{_STAGING_FILE_SUFFIX}"
                try:
                    descriptor = os.open(name, flags, 0o666, dir_fd=staging.fd)
                    break
                except FileExistsError:
                    continue
            else:
                raise FileExistsError("Could not allocate a unique POSIX staging file")
            try:
                try:
                    destination = os.fdopen(descriptor, "wb")
                except BaseException:
                    os.close(descriptor)
                    raise
                with destination:
                    yield staging, name, destination
            finally:
                try:
                    os.unlink(name, dir_fd=staging.fd)
                except FileNotFoundError:
                    pass

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
            with self._directory(path.parent, create=True) as parent:
                start, end = [int(x) for x in range.replace("bytes=", "").split("-")]
                try:
                    self._stat_file(parent, path.name)
                except FileNotFoundError:
                    pass
                else:
                    if not overwrite:
                        raise FileExistsError(path)
                    with self._open_file(parent, path.name, writable=True) as destination:
                        destination.seek(start)
                        destination.write(data[: end - start + 1])
                        destination.flush()
                        _sync_descriptor(destination.fileno())
                    return

                with self._staged_file() as (staging, name, destination):
                    destination.truncate(end + 1)
                    destination.seek(start)
                    destination.write(data[: end - start + 1])
                    destination.flush()
                    _sync_descriptor(destination.fileno())
                    self._publish_staged_file(
                        staging, name, parent, path.name, overwrite=overwrite,
                    )

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
            with object_lock, self._directory(path.parent) as parent:
                with self._open_file(parent, path.name) as stream:
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
        with self._directory(path.parent, create=True) as parent:
            with self._staged_file() as (staging, name, destination):
                written = 0
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
                            existing_mode = stat.S_IMODE(self._stat_file(parent, path.name).st_mode)
                        except FileNotFoundError:
                            pass
                        else:
                            os.fchmod(destination.fileno(), existing_mode)
                    destination.flush()
                    _sync_descriptor(destination.fileno())
                    self._publish_staged_file(
                        staging, name, parent, path.name, overwrite=overwrite,
                    )
                return written

    @instrument("driver", "delete_object", backend="posix")
    def delete_object(self, bucket: str, key: str) -> None:
        self._delete_object(bucket, key, generation=None)

    @instrument("driver", "delete_object_if_generation", backend="posix")
    def delete_object_if_generation(
        self, bucket: str, key: str, generation: str,
    ) -> bool:
        return self._delete_object(bucket, key, generation=generation)

    def _delete_object(self, bucket: str, key: str, generation: str | None) -> bool:
        with self._object_lock(bucket, key):
            path = self._path(bucket, key)
            with ExitStack() as stack:
                try:
                    parent = stack.enter_context(self._directory(path.parent))
                except FileNotFoundError:
                    return False
                try:
                    current = self._generation(self._stat_file(parent, path.name))
                except FileNotFoundError:
                    self._sync_directory(parent)
                    return False
                if generation is not None and current != generation:
                    raise ObjectGenerationMismatchError(
                        f"Object generation changed: {bucket}/{key}"
                    )
                try:
                    os.unlink(path.name, dir_fd=parent.fd)
                except FileNotFoundError:
                    self._sync_directory(parent)
                    return False
                self._sync_directory(parent)
                return True

    @instrument("driver", "ensure_object_durable", backend="posix")
    def ensure_object_durable(self, bucket: str, key: str) -> None:
        with self._object_lock(bucket, key):
            path = self._path(bucket, key)
            with self._directory(path.parent) as parent:
                with self._open_file(parent, path.name) as stream:
                    _sync_descriptor(stream.fileno())
                self._sync_directory(parent)
            with ExitStack() as stack:
                try:
                    staging = stack.enter_context(self._directory(self.base / _STAGING_DIRECTORY))
                except FileNotFoundError:
                    return
                self._sync_directory(staging)

    @instrument("driver", "list_objects", backend="posix")
    def list_objects(self, bucket: str, prefix: str = "") -> Generator[str, None, None]:
        base = self._path(bucket, "", allow_bucket_root=True)

        def walk(directory: _Directory, relative: str) -> Generator[str, None, None]:
            for name in os.listdir(directory.fd):
                try:
                    st = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                rel = f"{relative}/{name}" if relative else name
                if stat.S_ISDIR(st.st_mode):
                    with ExitStack() as stack:
                        try:
                            child = stack.enter_context(self._child_directory(directory, name))
                        except FileNotFoundError:
                            continue
                        yield from walk(child, rel)
                elif stat.S_ISREG(st.st_mode):
                    self._regular_stat(st)
                    if rel.startswith(prefix):
                        yield rel

        with ExitStack() as stack:
            try:
                directory = stack.enter_context(self._directory(base))
            except FileNotFoundError:
                return
            yield from walk(directory, "")

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

        def keys(directory: _Directory, parent: str = "") -> Generator[str, None, None]:
            # scandir propagates permission and I/O failures; os.walk's default
            # error suppression would misreport these as a complete inventory.
            with os.scandir(directory.fd) as entries:
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
                    st = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(st.st_mode):
                        with self._child_directory(directory, entry.name) as child:
                            yield from keys(child, child_prefix)
                    elif (
                        stat.S_ISREG(st.st_mode)
                        and key.startswith(prefix)
                        and (after is None or key > after)
                    ):
                        self._regular_stat(st)
                        yield key

        with ExitStack() as stack:
            try:
                directory = stack.enter_context(self._directory(base))
            except FileNotFoundError:
                if cursor is not None:
                    raise
                return StorageListingPage((), None)
            selected = nsmallest(limit + 1, keys(directory))
        more = len(selected) > limit
        page = tuple(selected[:limit])
        next_cursor = encode_listing_cursor(backend, bucket, prefix, page[-1]) if more else None
        return StorageListingPage(page, next_cursor)

    @instrument("driver", "stat_object", backend="posix")
    def stat_object(self, bucket: str, key: str) -> Dict[str, Any]:
        path = self._path(bucket, key)
        with self._directory(path.parent) as parent:
            st = self._stat_file(parent, path.name)
        return {
            "size": st.st_size,
            "mtime": st.st_mtime,
            "path": str(path),
            "generation": self._generation(st),
        }

    def same_backend(self, other: StorageDriver) -> bool:
        return isinstance(other, PosixDriver) and self.base == other.base
