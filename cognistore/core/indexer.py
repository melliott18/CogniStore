from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .mime_detection import MimeDetection, MimeDetectionAdapter, MimeDetectionProvider


@dataclass
class IndexResult:
    sha256: str
    mime: str | None
    sample: bytes
    mime_detection: MimeDetection


class Indexer:
    def __init__(
        self,
        sample_size: int = 256 * 1024,
        *,
        mime_detector: MimeDetectionProvider | None = None,
    ):
        self.sample_size = sample_size
        self.mime_detector = (
            mime_detector if mime_detector is not None else MimeDetectionAdapter.default()
        )

    def index_bytes(self, data: bytes, filename: str | None = None) -> IndexResult:
        digest = hashlib.sha256(data).hexdigest()
        sample = data[: self.sample_size]
        mime_detection = self.mime_detector.detect(sample, filename=filename)
        return IndexResult(
            sha256=digest,
            mime=mime_detection.mime,
            sample=sample,
            mime_detection=mime_detection,
        )
