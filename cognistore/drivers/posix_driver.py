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
        self.base = Path(base_path)
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, bucket: str, key: str) -> Path:
        # Normalize to prevent escaping the base dir
        p = Path(bucket) / key
        full = (self.base / p).resolve()
        if not str(full).startswith(str(self.base.resolve())):
            raise ValueError("Key escapes base path")
        return full

    def put_object(
        self, bucket: str, key: str, data: bytes, range: Optional[str] = None, **opts: Any
    ) -> None:
        path = self._path(bucket, key)
        path.parent.mkdir(parents=True, exist_ok=True)

        if range:
            start, end = [int(x) for x in range.replace("bytes=", "").split("-")]
            # Ensure file exists with correct size for r+b
            if not path.exists():
                # Pre-size the file to end+1 bytes
                with open(path, "wb") as f:
                    f.truncate(end + 1)
            with open(path, "r+b") as f:
                f.seek(start)
                f.write(data[: end - start + 1])
        else:
            with open(path, "wb") as f:
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
        base = self.base / bucket
        if not base.exists():
            return
        for root, _, files in os.walk(base):
            for name in files:
                rel = os.path.relpath(os.path.join(root, name), base)
                if rel.startswith(prefix):
                    yield rel

    def stat_object(self, bucket: str, key: str) -> Dict[str, Any]:
        path = self._path(bucket, key)
        st = path.stat()
        return {"size": st.st_size, "mtime": st.st_mtime, "path": str(path)}
