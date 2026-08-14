from __future__ import annotations

import os
import secrets
import stat
from contextlib import AbstractContextManager, contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, Dict, Generator, Mapping, Optional

from .storage_driver import (
    DEFAULT_STREAM_CHUNK_SIZE,
    DriverCapabilities,
    ReadableStream,
    StorageDriver,
)

_STAGING_DIRECTORY = ".cognistore-staging"
_STAGING_FILE_PREFIX = "upload-"
_STAGING_FILE_SUFFIX = ".tmp"
_MAX_STAGING_NAME_ATTEMPTS = 100


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


class PosixDriver(StorageDriver):
    """POSIX filesystem-backed driver.

    Treats files as objects under a base directory, grouping by bucket.
    """

    capabilities = DriverCapabilities(
        range_reads=True,
        range_writes=True,
        atomic_no_overwrite=True,
    )

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
            staging.mkdir(mode=0o700, parents=True, exist_ok=True)
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

        path = self._path(bucket, key)
        path.parent.mkdir(parents=True, exist_ok=True)

        start, end = [int(x) for x in range.replace("bytes=", "").split("-")]
        if not overwrite:
            # Exclusive creation makes collision rejection atomic with the
            # write, rather than relying only on a racy preflight stat.
            with open(path, "xb") as f:
                f.truncate(end + 1)
        elif not path.exists():
            # Pre-size the file to end+1 bytes
            with open(path, "wb") as f:
                f.truncate(end + 1)
        with open(path, "r+b") as f:
            f.seek(start)
            f.write(data[: end - start + 1])

    def get_object(
        self, bucket: str, key: str, range: Optional[str] = None
    ) -> bytes:
        with self.open_object_reader(bucket, key, range=range) as source:
            return source.read()

    def open_object_reader(
        self,
        bucket: str,
        key: str,
        range: Optional[str] = None,
    ) -> AbstractContextManager[ReadableStream]:
        path = self._path(bucket, key)

        @contextmanager
        def reader() -> Generator[ReadableStream, None, None]:
            with open(path, "rb") as stream:
                if not range:
                    yield stream
                    return
                start, end = [int(x) for x in range.replace("bytes=", "").split("-")]
                stream.seek(start)
                yield _RangedReader(stream, end - start + 1)

        return reader()

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
        path.parent.mkdir(parents=True, exist_ok=True)
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

            if overwrite:
                try:
                    existing_mode = stat.S_IMODE(
                        path.stat(follow_symlinks=False).st_mode
                    )
                except FileNotFoundError:
                    pass
                else:
                    temporary_path.chmod(existing_mode, follow_symlinks=False)
                os.replace(temporary_path, path)
            else:
                os.link(temporary_path, path)
            return written
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def delete_object(self, bucket: str, key: str) -> None:
        path = self._path(bucket, key)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

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

    def stat_object(self, bucket: str, key: str) -> Dict[str, Any]:
        path = self._path(bucket, key)
        st = path.stat()
        return {"size": st.st_size, "mtime": st.st_mtime, "path": str(path)}

    def same_backend(self, other: StorageDriver) -> bool:
        return isinstance(other, PosixDriver) and self.base == other.base
