from __future__ import annotations

import importlib
import mimetypes
import re
import threading
from dataclasses import dataclass
from typing import Callable, Literal, Protocol

MimeConfidence = Literal["high", "low", "none"]
MimeProvenance = Literal["content", "filename", "none"]
MimeStatus = Literal["detected", "fallback", "unclassified"]

_MIME_PATTERN = re.compile(
    r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+/[!#$%&'*+.^_`|~0-9A-Za-z-]+$"
)
_GENERIC_CONTENT_TYPES = frozenset(
    {
        "application/octet-stream",
        "application/x-empty",
        "inode/x-empty",
    }
)


class ContentMimeDetector(Protocol):
    """Content-only detector used by the indexing MIME adapter."""

    name: str

    def detect(self, data: bytes) -> object: ...


class MimeDetectionProvider(Protocol):
    """Complete MIME inference contract consumed by :class:`Indexer`."""

    def detect(self, data: bytes, *, filename: str | None = None) -> "MimeDetection": ...


class MimeDetectorUnavailableError(RuntimeError):
    """The optional native MIME detector could not be initialized."""


@dataclass(frozen=True)
class MimeDetection:
    """Selected MIME type and the evidence used to select it.

    ``confidence`` is a qualitative CogniStore trust tier, not a probability
    reported by libmagic. Content signatures are preferred over filename
    inference. Generic content classifications and filename-only guesses are
    intentionally marked ``low``. ``disagreement`` is a literal comparison of
    the two MIME candidates; a filename compression encoding is retained
    separately because it may describe a different layer of the object.
    """

    mime: str | None
    detector: str
    provenance: MimeProvenance
    confidence: MimeConfidence
    content_mime: str | None
    filename_mime: str | None
    filename_encoding: str | None
    disagreement: bool
    status: MimeStatus
    fallback_reason: str | None

    def to_metadata(self) -> dict[str, object]:
        """Return the versioned, JSON-safe catalog representation."""

        return {
            "schema_version": 1,
            "mime": self.mime,
            "detector": self.detector,
            "provenance": self.provenance,
            "confidence": self.confidence,
            "content_mime": self.content_mime,
            "filename_mime": self.filename_mime,
            "filename_encoding": self.filename_encoding,
            "disagreement": self.disagreement,
            "status": self.status,
            "fallback_reason": self.fallback_reason,
        }


class LibmagicMimeDetector:
    """Small, lock-protected adapter around the optional python-magic binding."""

    name = "libmagic"

    def __init__(self, reader: Callable[[bytes], object] | None = None) -> None:
        if reader is None:
            try:
                magic_module = importlib.import_module("magic")
                magic_handle = magic_module.Magic(mime=True)
                reader = magic_handle.from_buffer
            except Exception as exc:
                # Importing python-magic can itself load libmagic and fail when
                # the native library is absent. Keep that optional dependency
                # entirely behind this adapter boundary.
                raise MimeDetectorUnavailableError(
                    "libmagic MIME detection is unavailable"
                ) from exc
        self._reader = reader
        self._lock = threading.Lock()

    def detect(self, data: bytes) -> object:
        # python-magic documents a reusable Magic handle as unsafe for
        # concurrent calls. Indexers may be shared by callers, so serialize it.
        with self._lock:
            return self._reader(data)


class MimeDetectionAdapter:
    """Prefer content detection and safely fall back to filename inference."""

    def __init__(
        self,
        content_detector: ContentMimeDetector | None,
        *,
        unavailable_reason: str | None = None,
    ) -> None:
        self._content_detector = content_detector
        self._unavailable_reason = (
            "content_detector_unavailable"
            if content_detector is None and unavailable_reason is None
            else unavailable_reason
        )

    @classmethod
    def default(cls) -> "MimeDetectionAdapter":
        """Build the default libmagic adapter without leaking load failures."""

        try:
            detector: ContentMimeDetector | None = LibmagicMimeDetector()
        except MimeDetectorUnavailableError:
            detector = None
        return cls(
            detector,
            unavailable_reason="libmagic_unavailable" if detector is None else None,
        )

    def detect(self, data: bytes, *, filename: str | None = None) -> MimeDetection:
        filename_mime, filename_encoding = self._filename_evidence(filename)
        content_mime: str | None = None
        fallback_reason = self._unavailable_reason
        detector_name = (
            self._content_detector.name if self._content_detector is not None else "none"
        )

        if self._content_detector is not None:
            try:
                content_mime = _normalize_mime(self._content_detector.detect(data))
            except Exception:
                # Detection is advisory metadata. A malformed object or a
                # native-library error must not abort the remaining scan.
                fallback_reason = f"{detector_name}_error"
            else:
                if content_mime is None:
                    fallback_reason = f"{detector_name}_no_match"

        disagreement = (
            content_mime is not None
            and filename_mime is not None
            and content_mime != filename_mime
        )
        if content_mime is not None:
            return MimeDetection(
                mime=content_mime,
                detector=detector_name,
                provenance="content",
                confidence=(
                    "low" if content_mime in _GENERIC_CONTENT_TYPES else "high"
                ),
                content_mime=content_mime,
                filename_mime=filename_mime,
                filename_encoding=filename_encoding,
                disagreement=disagreement,
                status="detected",
                fallback_reason=None,
            )
        if filename_mime is not None:
            return MimeDetection(
                mime=filename_mime,
                detector="filename",
                provenance="filename",
                confidence="low",
                content_mime=None,
                filename_mime=filename_mime,
                filename_encoding=filename_encoding,
                disagreement=False,
                status="fallback",
                fallback_reason=fallback_reason,
            )
        return MimeDetection(
            mime=None,
            detector="none",
            provenance="none",
            confidence="none",
            content_mime=None,
            filename_mime=None,
            filename_encoding=filename_encoding,
            disagreement=False,
            status="unclassified",
            fallback_reason=fallback_reason,
        )

    @staticmethod
    def _filename_evidence(filename: str | None) -> tuple[str | None, str | None]:
        try:
            guess_file_type = getattr(mimetypes, "guess_file_type", None)
            if guess_file_type is None:
                # CPython <=3.12 accepts URLs rather than literal paths and
                # special-cases values such as ``data:text/html,...``. A
                # leading slash neutralizes URL schemes while preserving the
                # exact object key for suffix and encoding inference.
                mime, encoding = mimetypes.guess_type(
                    f"/{filename or ''}", strict=True
                )
            else:
                mime, encoding = guess_file_type(filename or "", strict=True)
        except Exception:
            # Filename evidence is advisory too. A platform MIME database
            # failure must not become an object- or scan-level failure.
            return None, None
        normalized_encoding = (
            encoding.strip().lower()
            if isinstance(encoding, str) and encoding.strip()
            else None
        )
        return _normalize_mime(mime), normalized_encoding


def _normalize_mime(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    mime = value.partition(";")[0].strip().lower()
    if not mime.isascii() or _MIME_PATTERN.fullmatch(mime) is None:
        return None
    return mime
