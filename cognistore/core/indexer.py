from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from typing import Optional


@dataclass
class IndexResult:
    sha256: str
    mime: str | None
    sample: bytes


class Indexer:
    def __init__(self, sample_size: int = 256 * 1024):
        self.sample_size = sample_size

    def index_bytes(self, data: bytes, filename: Optional[str] = None) -> IndexResult:
        digest = hashlib.sha256(data).hexdigest()
        mime, _ = mimetypes.guess_type(filename or "")
        sample = data[: self.sample_size]
        return IndexResult(sha256=digest, mime=mime, sample=sample)
