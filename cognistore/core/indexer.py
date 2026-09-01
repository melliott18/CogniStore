from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from cognistore.drivers.storage_driver import ReadableStream

from .content_identity import ContentIdentityBuilder, ObjectContent
from .document_extraction import DocumentExtraction, DocumentExtractionPipeline
from .mime_detection import MimeDetection, MimeDetectionAdapter, MimeDetectionProvider


@dataclass
class IndexResult:
    sha256: str
    mime: str | None
    sample: bytes
    mime_detection: MimeDetection
    document_extraction: DocumentExtraction
    content: ObjectContent | None = None


class _DocumentCaptureReader:
    """Capture one bounded document while another consumer reads its stream."""

    def __init__(self, reader: ReadableStream) -> None:
        self._reader = reader
        self._data = bytearray()

    def read(self, size: int = -1) -> bytes:
        data = self._reader.read(size)
        if isinstance(data, bytes):
            self._data.extend(data)
        return data

    def data(self) -> bytes:
        return bytes(self._data)


class Indexer:
    def __init__(
        self,
        sample_size: int = 256 * 1024,
        *,
        mime_detector: MimeDetectionProvider | None = None,
        document_extractor: DocumentExtractionPipeline | None = None,
        content_builder: ContentIdentityBuilder | None = None,
    ):
        self.sample_size = sample_size
        self.mime_detector = (
            mime_detector if mime_detector is not None else MimeDetectionAdapter.default()
        )
        self.document_extractor = document_extractor or DocumentExtractionPipeline()
        self.content_builder = content_builder or ContentIdentityBuilder()

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

    def index_stream(
        self,
        reader: ReadableStream,
        *,
        sample: bytes,
        filename: str | None = None,
        source_size: int,
    ) -> IndexResult:
        """Index one complete object stream and derive its canonical identity.

        MIME detection remains sample-bounded. Supported documents within the
        extraction limit are captured while the identity builder consumes the
        stream, so hashing, chunking, and document loading share one complete
        object read. Other objects retain no bytes beyond the sample and the
        builder's bounded chunk buffer.
        """

        bounded_sample = sample[: self.sample_size]
        mime_detection = self.mime_detector.detect(
            bounded_sample,
            filename=filename,
        )
        capture_document = self.document_extractor.needs_document_bytes(
            mime_detection.mime,
            source_size=source_size,
        )
        content_reader: ReadableStream = reader
        capturing_reader: _DocumentCaptureReader | None = None
        if capture_document:
            capturing_reader = _DocumentCaptureReader(reader)
            content_reader = capturing_reader

        content = self.content_builder.build(
            content_reader,
            expected_size=source_size,
        )
        document_data = (
            capturing_reader.data()
            if capturing_reader is not None
            else bounded_sample
        )
        document_extraction = self.document_extractor.extract(
            document_data,
            mime=mime_detection.mime,
            source_size=source_size,
        )
        return IndexResult(
            sha256=content.sha256,
            mime=mime_detection.mime,
            sample=bounded_sample,
            mime_detection=mime_detection,
            document_extraction=document_extraction,
            content=content,
        )
