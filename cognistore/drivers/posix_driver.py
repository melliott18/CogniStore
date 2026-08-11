from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Generator, Optional

from .storage_driver import StorageDriver


class PosixDriver(StorageDriver):
    """POSIX filesystem-backed driver.

    Treats files as objects under a base directory, grouping by bucket.
    """

    def __init__(self, base_path: str):
        # Resolve the configured root once so every containment comparison uses
        # the same canonical path. Directories are created lazily by writes;
        # constructing a driver must remain read-only for dry-run workflows.
        self.base = Path(base_path).expanduser().resolve()

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

    def put_object(
        self,
        bucket: str,
        key: str,
        data: bytes,
        range: Optional[str] = None,
        overwrite: bool = True,
        **opts: Any,
    ) -> None:
        path = self._path(bucket, key)
        path.parent.mkdir(parents=True, exist_ok=True)

        if range:
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
        else:
            mode = "wb" if overwrite else "xb"
            with open(path, mode) as f:
                f.write(data)

    def get_object(
        self, bucket: str, key: str, range: Optional[str] = None
    ) -> bytes:
        path = self._path(bucket, key)
        with open(path, "rb") as f:
            if range:
                start, end = [int(x) for x in range.replace("bytes=", "").split("-")]
                f.seek(start)
                return f.read(end - start + 1)
            return f.read()

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
