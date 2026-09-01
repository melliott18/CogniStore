from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from .document_extraction import DocumentExtraction, DocumentExtractionPipeline
from .mime_detection import MimeDetection, MimeDetectionAdapter, MimeDetectionProvider


@dataclass
class IndexResult:
    sha256: str
    mime: str | None
    sample: bytes
    mime_detection: MimeDetection
    document_extraction: DocumentExtraction


class Indexer:
    def __init__(
        self,
        sample_size: int = 256 * 1024,
        *,
        mime_detector: MimeDetectionProvider | None = None,
        document_extractor: DocumentExtractionPipeline | None = None,
    ):
        self.sample_size = sample_size
        self.mime_detector = (
            mime_detector if mime_detector is not None else MimeDetectionAdapter.default()
        )
        self.document_extractor = document_extractor or DocumentExtractionPipeline()

    @property
    def max_document_file_bytes(self) -> int:
        return self.document_extractor.limits.max_file_bytes

    def index_bytes(
        self,
        data: bytes,
        filename: str | None = None,
        *,
        source_size: int | None = None,
        document_loader: Callable[[], bytes] | None = None,
    ) -> IndexResult:
        digest = hashlib.sha256(data).hexdigest()
        sample = data[: self.sample_size]
        mime_detection = self.mime_detector.detect(sample, filename=filename)
        effective_size = len(data) if source_size is None else source_size
        document_data = data
        if document_loader is not None and self.document_extractor.needs_document_bytes(
            mime_detection.mime,
            source_size=effective_size,
        ):
            document_data = document_loader()
        document_extraction = self.document_extractor.extract(
            document_data,
            mime=mime_detection.mime,
            source_size=effective_size,
        )
        return IndexResult(
            sha256=digest,
            mime=mime_detection.mime,
            sample=sample,
            mime_detection=mime_detection,
            document_extraction=document_extraction,
        )
