"""Deterministic normalized-text passage identities for embedding work.

Passages are deliberately not the source-byte chunks defined by
``content_identity``.  Their offsets and limits are Unicode code points in the
normalized extraction text, and their identities retain the complete source,
parser, normalization, and passage-chunker provenance needed for reproducible
re-embedding.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from cognistore.core.document_extraction import (
    NORMALIZATION_VERSION,
    ParserIdentity,
    normalize_document_text,
)

PASSAGE_SCHEMA_VERSION = 1
PASSAGE_REPRESENTATION = "normalized-text-codepoints"
PASSAGE_CHUNKING_ALGORITHM = "fixed-codepoints"
PASSAGE_CHUNKING_VERSION = 1
DEFAULT_PASSAGE_MAX_CODEPOINTS = 1_000
DEFAULT_PASSAGE_OVERLAP_CODEPOINTS = 100
TEXT_NORMALIZATION_NAME = "document-text"

_PASSAGE_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "https://cognistore.dev/identities/normalized-text-passages",
)
_DOCUMENT_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "https://cognistore.dev/identities/normalized-documents",
)
_LOWERCASE_HEX = frozenset("0123456789abcdef")


def _required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty string without outer whitespace")
    if "\0" in value:
        raise ValueError(f"{field} must not contain NUL characters")
    return value


def _positive_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nonnegative_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _sha256(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _LOWERCASE_HEX for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase 64-character SHA-256 digest")
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class NormalizedDocumentIdentity:
    """Immutable identity of one normalized parser output for a source manifest."""

    manifest_id: UUID
    source_sha256: str
    extraction_schema_version: int
    source_mime: str
    parser_name: str
    parser_implementation_version: str
    parser_runtime_version: str
    normalization_name: str = TEXT_NORMALIZATION_NAME
    normalization_version: int = NORMALIZATION_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.manifest_id, UUID):
            raise ValueError("manifest_id must be a UUID")
        _sha256(self.source_sha256, field="source_sha256")
        _positive_integer(
            self.extraction_schema_version,
            field="extraction_schema_version",
        )
        for field in (
            "source_mime",
            "parser_name",
            "parser_implementation_version",
            "parser_runtime_version",
            "normalization_name",
        ):
            _required_text(getattr(self, field), field=field)
        if self.normalization_name != TEXT_NORMALIZATION_NAME:
            raise ValueError(
                f"unsupported normalization name: {self.normalization_name!r}"
            )
        _positive_integer(
            self.normalization_version,
            field="normalization_version",
        )
        if self.normalization_version != NORMALIZATION_VERSION:
            raise ValueError(
                "unsupported normalization version: "
                f"{self.normalization_version!r}"
            )

    @classmethod
    def from_parser(
        cls,
        *,
        manifest_id: UUID,
        source_sha256: str,
        extraction_schema_version: int,
        source_mime: str,
        parser: ParserIdentity,
    ) -> NormalizedDocumentIdentity:
        """Build the identity from the parser contract used by extraction."""

        if not isinstance(parser, ParserIdentity):
            raise ValueError("parser must be a ParserIdentity")
        return cls(
            manifest_id=manifest_id,
            source_sha256=source_sha256,
            extraction_schema_version=extraction_schema_version,
            source_mime=source_mime,
            parser_name=parser.name,
            parser_implementation_version=parser.implementation_version,
            parser_runtime_version=parser.runtime_version,
        )

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": PASSAGE_SCHEMA_VERSION,
            "representation": PASSAGE_REPRESENTATION,
            "manifest_id": str(self.manifest_id),
            "source_sha256": self.source_sha256,
            "extraction_schema_version": self.extraction_schema_version,
            "source_mime": self.source_mime,
            "parser": {
                "name": self.parser_name,
                "implementation_version": self.parser_implementation_version,
                "runtime_version": self.parser_runtime_version,
            },
            "normalization": {
                "name": self.normalization_name,
                "version": self.normalization_version,
            },
        }

    @property
    def fingerprint(self) -> str:
        """Return the canonical SHA-256 identity fingerprint."""

        return _fingerprint(self._identity_payload())

    @property
    def document_id(self) -> UUID:
        """Return the stable UUID for this normalized document representation."""

        return uuid5(_DOCUMENT_NAMESPACE, self.fingerprint)

    def to_metadata(self) -> dict[str, object]:
        """Return a fresh JSON-safe identity mapping."""

        return {
            **self._identity_payload(),
            "document_id": str(self.document_id),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class PassageChunkerConfig:
    """Immutable configuration for fixed-width Unicode-codepoint passages."""

    algorithm: str = PASSAGE_CHUNKING_ALGORITHM
    version: int = PASSAGE_CHUNKING_VERSION
    max_codepoints: int = DEFAULT_PASSAGE_MAX_CODEPOINTS
    overlap_codepoints: int = DEFAULT_PASSAGE_OVERLAP_CODEPOINTS

    def __post_init__(self) -> None:
        if self.algorithm != PASSAGE_CHUNKING_ALGORITHM:
            raise ValueError(f"unsupported passage chunking algorithm: {self.algorithm!r}")
        _positive_integer(self.version, field="version")
        if self.version != PASSAGE_CHUNKING_VERSION:
            raise ValueError(f"unsupported passage chunking version: {self.version!r}")
        _positive_integer(self.max_codepoints, field="max_codepoints")
        _nonnegative_integer(self.overlap_codepoints, field="overlap_codepoints")
        if self.overlap_codepoints >= self.max_codepoints:
            raise ValueError("overlap_codepoints must be less than max_codepoints")

    @property
    def stride_codepoints(self) -> int:
        return self.max_codepoints - self.overlap_codepoints

    def to_metadata(self) -> dict[str, object]:
        return {
            "algorithm": self.algorithm,
            "version": self.version,
            "max_codepoints": self.max_codepoints,
            "overlap_codepoints": self.overlap_codepoints,
        }

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_metadata())


@dataclass(frozen=True)
class Passage:
    """One immutable normalized-text passage with codepoint offsets."""

    document: NormalizedDocumentIdentity
    chunker: PassageChunkerConfig
    index: int
    start_codepoint: int
    end_codepoint: int
    text: str
    text_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.document, NormalizedDocumentIdentity):
            raise ValueError("document must be a NormalizedDocumentIdentity")
        if not isinstance(self.chunker, PassageChunkerConfig):
            raise ValueError("chunker must be a PassageChunkerConfig")
        _nonnegative_integer(self.index, field="index")
        _nonnegative_integer(self.start_codepoint, field="start_codepoint")
        _positive_integer(self.end_codepoint, field="end_codepoint")
        if not isinstance(self.text, str):
            raise ValueError("passage text must be a string")
        if self.end_codepoint <= self.start_codepoint:
            raise ValueError("end_codepoint must be greater than start_codepoint")
        if self.end_codepoint - self.start_codepoint != len(self.text):
            raise ValueError("passage offsets must match its Unicode codepoint length")
        if not self.text:
            raise ValueError("passage text must not be empty")
        if len(self.text) > self.chunker.max_codepoints:
            raise ValueError("passage text exceeds the configured codepoint limit")
        if self.start_codepoint != self.index * self.chunker.stride_codepoints:
            raise ValueError("passage start offset does not match its chunker index")
        try:
            encoded = self.text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("passage text must be valid Unicode") from exc
        if unicodedata.normalize("NFC", self.text) != self.text or any(
            character != "\n" and unicodedata.category(character) == "Cc"
            for character in self.text
        ):
            raise ValueError(
                "passage text must satisfy the declared normalization contract"
            )
        digest = _sha256(self.text_sha256, field="text_sha256")
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise ValueError("text_sha256 does not match the passage text")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": PASSAGE_SCHEMA_VERSION,
            "representation": PASSAGE_REPRESENTATION,
            "document": self.document._identity_payload(),
            "chunker": self.chunker.to_metadata(),
            "index": self.index,
            "start_codepoint": self.start_codepoint,
            "end_codepoint": self.end_codepoint,
            "text_sha256": self.text_sha256,
        }

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self._identity_payload())

    @property
    def passage_id(self) -> UUID:
        return uuid5(_PASSAGE_NAMESPACE, self.fingerprint)

    def to_metadata(self) -> dict[str, object]:
        return {
            **self._identity_payload(),
            "passage_id": str(self.passage_id),
            "fingerprint": self.fingerprint,
            "text": self.text,
        }


class DeterministicPassageChunker:
    """Split normalized text at deterministic Unicode-codepoint boundaries."""

    def __init__(self, config: PassageChunkerConfig | None = None) -> None:
        self.config = config or PassageChunkerConfig()
        if not isinstance(self.config, PassageChunkerConfig):
            raise ValueError("config must be a PassageChunkerConfig")

    def chunk(
        self,
        text: str,
        *,
        document: NormalizedDocumentIdentity,
    ) -> tuple[Passage, ...]:
        """Return immutable passages for already normalized extraction text."""

        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if not isinstance(document, NormalizedDocumentIdentity):
            raise ValueError("document must be a NormalizedDocumentIdentity")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("text must be valid Unicode") from exc
        if normalize_document_text(text) != text:
            raise ValueError(
                "text must satisfy the declared deterministic normalization contract"
            )
        if not text:
            return ()

        passages: list[Passage] = []
        start = 0
        while start < len(text):
            end = min(len(text), start + self.config.max_codepoints)
            passage_text = text[start:end]
            passages.append(
                Passage(
                    document=document,
                    chunker=self.config,
                    index=len(passages),
                    start_codepoint=start,
                    end_codepoint=end,
                    text=passage_text,
                    text_sha256=hashlib.sha256(
                        passage_text.encode("utf-8")
                    ).hexdigest(),
                )
            )
            if end == len(text):
                break
            start += self.config.stride_codepoints
        return tuple(passages)
