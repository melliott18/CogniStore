from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import NoReturn, Protocol

from cognistore.core.catalog import CatalogStore, ObjectRecord
from cognistore.core.document_extraction import (
    EXTRACTION_SCHEMA_VERSION,
    NORMALIZATION_VERSION,
)

KEYWORD_INDEX_SCHEMA_VERSION = 1
PASSAGE_CHUNKING_ALGORITHM = "bounded-unicode-word-boundary"
PASSAGE_CHUNKING_VERSION = 1
DEFAULT_PASSAGE_CHARS = 1_200
DEFAULT_PASSAGE_OVERLAP_CHARS = 120
DEFAULT_REBUILD_BATCH_SIZE = 1_000
MAX_SEARCH_LIMIT = 1_000
MAX_SEARCH_OFFSET = 100_000

_LOWERCASE_HEX = frozenset("0123456789abcdef")
_DOCUMENT_METADATA_FIELDS = (
    "format",
    "title",
    "author",
    "subject",
    "keywords",
    "language",
    "created_at",
    "modified_at",
    "page_count",
    "paragraph_count",
    "table_count",
)
_FILTER_FIELDS = frozenset(_DOCUMENT_METADATA_FIELDS)


def _validate_nonempty_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _validate_sha256(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _LOWERCASE_HEX for character in value)
    ):
        return None
    return value


def _stable_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_int(
    value: object,
    *,
    field_name: str,
    minimum: int,
    maximum: int = 2**63 - 1,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ValueError(
            f"{field_name} must be an integer between {minimum} and {maximum}"
        )
    return value


@dataclass(frozen=True)
class KeywordPassage:
    """One deterministic normalized-text passage indexed as a Tantivy document."""

    passage_id: str
    ordinal: int
    char_start: int
    char_end: int
    text: str

    def __post_init__(self) -> None:
        _validate_sha256(self.passage_id) or _raise_invalid_digest("passage_id")
        _validate_int(self.ordinal, field_name="passage ordinal", minimum=0)
        start = _validate_int(self.char_start, field_name="passage char_start", minimum=0)
        end = _validate_int(self.char_end, field_name="passage char_end", minimum=0)
        if end < start:
            raise ValueError("passage char_end must not precede char_start")
        if not isinstance(self.text, str):
            raise ValueError("passage text must be a string")
        if len(self.text) != end - start:
            raise ValueError("passage text length must match its character extent")


def _raise_invalid_digest(field_name: str) -> NoReturn:
    raise ValueError(f"{field_name} must be a lowercase 64-character SHA-256 digest")


@dataclass(frozen=True)
class KeywordObject:
    """Backend-neutral projection of one current catalog object."""

    object_id: str
    bucket: str
    key: str
    tier: str
    size: int
    mime: str | None
    content_sha256: str | None
    document_metadata: Mapping[str, object]
    passages: tuple[KeywordPassage, ...]
    extraction_identity: str | None = None
    passage_chunking_algorithm: str = PASSAGE_CHUNKING_ALGORITHM
    passage_chunking_version: int = PASSAGE_CHUNKING_VERSION
    passage_max_chars: int = DEFAULT_PASSAGE_CHARS
    passage_overlap_chars: int = DEFAULT_PASSAGE_OVERLAP_CHARS

    def __post_init__(self) -> None:
        _validate_sha256(self.object_id) or _raise_invalid_digest("object_id")
        _validate_nonempty_text(self.bucket, field_name="bucket")
        _validate_nonempty_text(self.key, field_name="key")
        expected_object_id = _stable_digest(
            ["catalog-object", self.bucket, self.key]
        )
        if self.object_id != expected_object_id:
            raise ValueError("object_id does not match bucket and key")
        _validate_nonempty_text(self.tier, field_name="tier")
        _validate_int(self.size, field_name="size", minimum=0)
        if self.mime is not None and not isinstance(self.mime, str):
            raise ValueError("mime must be a string or None")
        if self.content_sha256 is not None and _validate_sha256(self.content_sha256) is None:
            _raise_invalid_digest("content_sha256")
        if self.extraction_identity is not None and _validate_sha256(
            self.extraction_identity
        ) is None:
            _raise_invalid_digest("extraction_identity")
        if self.passage_chunking_algorithm != PASSAGE_CHUNKING_ALGORITHM:
            raise ValueError("unsupported passage chunking algorithm")
        if self.passage_chunking_version != PASSAGE_CHUNKING_VERSION:
            raise ValueError("unsupported passage chunking version")
        _validate_int(
            self.passage_max_chars,
            field_name="passage_max_chars",
            minimum=1,
            maximum=2**31 - 1,
        )
        _validate_int(
            self.passage_overlap_chars,
            field_name="passage_overlap_chars",
            minimum=0,
            maximum=2**31 - 1,
        )
        if self.passage_overlap_chars >= self.passage_max_chars:
            raise ValueError("passage_overlap_chars must be smaller than passage_max_chars")
        if not isinstance(self.document_metadata, Mapping):
            raise ValueError("document_metadata must be a mapping")
        metadata = deepcopy(dict(self.document_metadata))
        if any(not isinstance(name, str) for name in metadata):
            raise ValueError("document metadata names must be strings")
        unknown_metadata = sorted(set(metadata) - _FILTER_FIELDS)
        if unknown_metadata:
            raise ValueError(
                "unsupported document metadata fields: " + ", ".join(unknown_metadata)
            )
        if any(not _is_json_scalar(value) for value in metadata.values()):
            raise ValueError("document_metadata values must be JSON scalars")
        # Ensure the projection is safe for persistent backends before any
        # catalog-driven mutation starts.
        json.dumps(metadata, ensure_ascii=False, allow_nan=False, sort_keys=True)
        object.__setattr__(self, "document_metadata", metadata)
        passages = tuple(self.passages)
        if not passages:
            raise ValueError("a keyword object must contain at least one passage")
        if any(passage.ordinal != ordinal for ordinal, passage in enumerate(passages)):
            raise ValueError("passage ordinals must be contiguous from zero")
        object.__setattr__(self, "passages", passages)


@dataclass(frozen=True)
class KeywordSearchFilters:
    bucket: str | None = None
    tier: str | None = None
    size: int | None = None
    mime: str | None = None
    content_sha256: str | None = None
    document_metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("bucket", "tier", "mime"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} filter must be a non-empty string or None")
        if self.size is not None:
            _validate_int(self.size, field_name="size filter", minimum=0)
        if self.content_sha256 is not None and _validate_sha256(
            self.content_sha256
        ) is None:
            _raise_invalid_digest("content_sha256 filter")
        if not isinstance(self.document_metadata, Mapping):
            raise ValueError("document_metadata filters must be a mapping")
        filters = deepcopy(dict(self.document_metadata))
        if any(not isinstance(name, str) for name in filters):
            raise ValueError("document metadata filter names must be strings")
        unknown = sorted(set(filters) - _FILTER_FIELDS)
        if unknown:
            raise ValueError(
                "unsupported document metadata filter fields: " + ", ".join(unknown)
            )
        for name, value in filters.items():
            if isinstance(value, (Mapping, Sequence)) and not isinstance(
                value, (str, bytes, bytearray)
            ):
                raise ValueError(f"document metadata filter {name} must be scalar")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"document metadata filter {name} must be finite")
            try:
                json.dumps(value, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"document metadata filter {name} must be JSON scalar"
                ) from exc
        object.__setattr__(self, "document_metadata", filters)


@dataclass(frozen=True)
class KeywordSearchQuery:
    text: str = ""
    filters: KeywordSearchFilters = field(default_factory=KeywordSearchFilters)
    limit: int = 20
    offset: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("query text must be a string")
        _validate_int(
            self.limit,
            field_name="query limit",
            minimum=1,
            maximum=MAX_SEARCH_LIMIT,
        )
        _validate_int(
            self.offset,
            field_name="query offset",
            minimum=0,
            maximum=MAX_SEARCH_OFFSET,
        )
        if not isinstance(self.filters, KeywordSearchFilters):
            raise ValueError("query filters must be KeywordSearchFilters")


@dataclass(frozen=True)
class KeywordSearchHit:
    score: float
    object_id: str
    passage_id: str
    bucket: str
    key: str
    tier: str
    size: int
    mime: str | None
    content_sha256: str | None
    passage_ordinal: int
    char_start: int
    char_end: int
    text: str
    document_metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.score, (int, float)) or not math.isfinite(self.score):
            raise ValueError("search score must be finite")
        object.__setattr__(self, "document_metadata", deepcopy(dict(self.document_metadata)))


@dataclass(frozen=True)
class KeywordRebuildReport:
    objects_indexed: int
    passages_indexed: int

    def __post_init__(self) -> None:
        _validate_int(self.objects_indexed, field_name="objects_indexed", minimum=0)
        _validate_int(self.passages_indexed, field_name="passages_indexed", minimum=0)


class KeywordIndexAdapter(Protocol):
    """Derived-index operations; implementations must never mutate the catalog."""

    def replace_object(self, document: KeywordObject) -> int: ...

    def delete_object(self, bucket: str, key: str) -> None: ...

    def search(self, query: KeywordSearchQuery) -> list[KeywordSearchHit]: ...

    def rebuild(self, documents: Iterable[KeywordObject]) -> KeywordRebuildReport: ...

    def close(self) -> None: ...


class NormalizedPassageChunker:
    """Split already-normalized extraction text at stable character boundaries."""

    def __init__(
        self,
        *,
        max_chars: int = DEFAULT_PASSAGE_CHARS,
        overlap_chars: int = DEFAULT_PASSAGE_OVERLAP_CHARS,
    ) -> None:
        self.max_chars = _validate_int(
            max_chars,
            field_name="max_chars",
            minimum=1,
            maximum=2**31 - 1,
        )
        self.overlap_chars = _validate_int(
            overlap_chars,
            field_name="overlap_chars",
            minimum=0,
            maximum=2**31 - 1,
        )
        if self.overlap_chars >= self.max_chars:
            raise ValueError("overlap_chars must be smaller than max_chars")

    def chunk(
        self,
        text: str,
        *,
        object_id: str,
        extraction_identity: str,
    ) -> tuple[KeywordPassage, ...]:
        if not isinstance(text, str):
            raise ValueError("normalized extraction text must be a string")
        _validate_sha256(object_id) or _raise_invalid_digest("object_id")
        _validate_sha256(extraction_identity) or _raise_invalid_digest(
            "extraction_identity"
        )
        passages: list[KeywordPassage] = []
        cursor = 0
        while cursor < len(text):
            hard_end = min(len(text), cursor + self.max_chars)
            boundary = self._boundary(text, cursor, hard_end)
            content_start = cursor
            while content_start < boundary and text[content_start].isspace():
                content_start += 1
            content_end = boundary
            while content_end > content_start and text[content_end - 1].isspace():
                content_end -= 1
            if content_start < content_end:
                ordinal = len(passages)
                passage_text = text[content_start:content_end]
                passages.append(
                    KeywordPassage(
                        passage_id=_stable_digest(
                            [
                                "keyword-passage",
                                KEYWORD_INDEX_SCHEMA_VERSION,
                                PASSAGE_CHUNKING_ALGORITHM,
                                PASSAGE_CHUNKING_VERSION,
                                self.max_chars,
                                self.overlap_chars,
                                object_id,
                                extraction_identity,
                                ordinal,
                                content_start,
                                content_end,
                                hashlib.sha256(passage_text.encode("utf-8")).hexdigest(),
                            ]
                        ),
                        ordinal=ordinal,
                        char_start=content_start,
                        char_end=content_end,
                        text=passage_text,
                    )
                )
            if boundary >= len(text):
                break
            next_cursor = max(cursor + 1, boundary - self.overlap_chars)
            cursor = next_cursor
        return tuple(passages)

    def _boundary(self, text: str, start: int, hard_end: int) -> int:
        if hard_end == len(text):
            return hard_end
        minimum = start + (self.max_chars // 2)
        for separator in ("\n\n", "\n", " "):
            candidate = text.rfind(separator, minimum, hard_end)
            if candidate >= minimum:
                return candidate + len(separator)
        return hard_end


def _object_id(bucket: str, key: str) -> str:
    return _stable_digest(["catalog-object", bucket, key])


def _document_metadata(extraction: Mapping[str, object] | None) -> dict[str, object]:
    if extraction is None:
        return {}
    raw = extraction.get("document_metadata")
    if not isinstance(raw, Mapping):
        return {}
    return {
        name: deepcopy(raw[name])
        for name in _DOCUMENT_METADATA_FIELDS
        if name in raw and _is_json_scalar(raw[name]) and raw[name] is not None
    }


def _is_json_scalar(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _current_content_sha256(record: ObjectRecord) -> str | None:
    content = record.metadata.get("content_identity")
    if not isinstance(content, Mapping):
        return None
    content_sha256 = _validate_sha256(content.get("sha256"))
    if content_sha256 is None or record.metadata.get("sha256") != content_sha256:
        return None
    if content.get("size") != record.size:
        return None
    return content_sha256


def _current_extraction(
    record: ObjectRecord,
    *,
    content_sha256: str,
) -> tuple[str, str, Mapping[str, object]] | None:
    """Return trusted text, digest, and extraction metadata for current bytes.

    ``content_identity`` is catalog-owned and is removed when a generic write
    invalidates the active source mapping. Requiring it prevents a rebuild from
    reviving stale extraction JSON that may remain after a placement mutation.
    """

    metadata = record.metadata
    extraction = metadata.get("document_extraction")
    if not isinstance(extraction, Mapping):
        return None
    if extraction.get("status") != "succeeded":
        return None
    if extraction.get("source_size") != record.size:
        return None
    text = extraction.get("text")
    if not isinstance(text, str):
        return None
    schema_version = extraction.get("schema_version")
    normalization_version = extraction.get("normalization_version")
    parser = extraction.get("parser")
    if isinstance(schema_version, bool) or schema_version != EXTRACTION_SCHEMA_VERSION:
        return None
    if (
        isinstance(normalization_version, bool)
        or normalization_version != NORMALIZATION_VERSION
        or not isinstance(parser, Mapping)
    ):
        return None
    if extraction.get("failure_code") is not None:
        return None
    text_bytes = extraction.get("text_bytes")
    if (
        isinstance(text_bytes, bool)
        or not isinstance(text_bytes, int)
        or text_bytes != len(text.encode("utf-8"))
    ):
        return None
    if not isinstance(extraction.get("document_metadata"), Mapping):
        return None
    source_mime = extraction.get("source_mime")
    catalog_mime = metadata.get("mime")
    if not isinstance(source_mime, str) or source_mime != catalog_mime:
        return None
    parser_identity = {
        name: parser.get(name)
        for name in ("name", "implementation_version", "runtime_version")
    }
    if any(not isinstance(value, str) or not value for value in parser_identity.values()):
        return None
    extraction_identity = _stable_digest(
        [
            "normalized-extraction",
            content_sha256,
            schema_version,
            normalization_version,
            parser_identity,
            source_mime,
        ]
    )
    return text, extraction_identity, extraction


def project_catalog_record(
    record: ObjectRecord,
    *,
    chunker: NormalizedPassageChunker | None = None,
) -> KeywordObject:
    """Create the search projection for one detached catalog record."""

    if not isinstance(record, ObjectRecord):
        raise ValueError("record must be an ObjectRecord")
    bucket = _validate_nonempty_text(record.bucket, field_name="record bucket")
    key = _validate_nonempty_text(record.key, field_name="record key")
    tier = _validate_nonempty_text(record.tier, field_name="record tier")
    size = _validate_int(record.size, field_name="record size", minimum=0)
    if not isinstance(record.metadata, Mapping):
        raise ValueError("record metadata must be a mapping")
    object_id = _object_id(bucket, key)
    active_chunker = chunker or NormalizedPassageChunker()
    content_sha256 = _current_content_sha256(record)
    current = (
        None
        if content_sha256 is None
        else _current_extraction(record, content_sha256=content_sha256)
    )
    metadata: dict[str, object] = {}
    mime_value = record.metadata.get("mime")
    mime = mime_value if isinstance(mime_value, str) and mime_value else None
    extraction_identity: str | None = None
    passages: tuple[KeywordPassage, ...] = ()
    if current is not None:
        text, extraction_identity, trusted_extraction = current
        metadata = _document_metadata(trusted_extraction)
        passages = active_chunker.chunk(
            text,
            object_id=object_id,
            extraction_identity=extraction_identity,
        )
    if not passages:
        synthetic_identity = extraction_identity or _stable_digest(
            ["metadata-only", object_id, content_sha256, mime, metadata]
        )
        passages = (
            KeywordPassage(
                passage_id=_stable_digest(
                    [
                        "keyword-passage",
                        KEYWORD_INDEX_SCHEMA_VERSION,
                        PASSAGE_CHUNKING_ALGORITHM,
                        PASSAGE_CHUNKING_VERSION,
                        active_chunker.max_chars,
                        active_chunker.overlap_chars,
                        object_id,
                        synthetic_identity,
                        0,
                    ]
                ),
                ordinal=0,
                char_start=0,
                char_end=0,
                text="",
            ),
        )
    return KeywordObject(
        object_id=object_id,
        bucket=bucket,
        key=key,
        tier=tier,
        size=size,
        mime=mime,
        content_sha256=content_sha256,
        document_metadata=metadata,
        passages=passages,
        extraction_identity=extraction_identity,
        passage_chunking_algorithm=PASSAGE_CHUNKING_ALGORITHM,
        passage_chunking_version=PASSAGE_CHUNKING_VERSION,
        passage_max_chars=active_chunker.max_chars,
        passage_overlap_chars=active_chunker.overlap_chars,
    )


class KeywordSearchService:
    """Coordinate catalog-to-index projection without writing catalog state."""

    def __init__(
        self,
        catalog: CatalogStore,
        adapter: KeywordIndexAdapter,
        *,
        chunker: NormalizedPassageChunker | None = None,
        rebuild_batch_size: int = DEFAULT_REBUILD_BATCH_SIZE,
    ) -> None:
        self.catalog = catalog
        self.adapter = adapter
        self.chunker = chunker or NormalizedPassageChunker()
        self.rebuild_batch_size = _validate_int(
            rebuild_batch_size,
            field_name="rebuild_batch_size",
            minimum=1,
            maximum=2**31 - 1,
        )

    def sync_object(self, bucket: str, key: str) -> int:
        """Replace one derived object after its catalog transaction commits."""

        record = self.catalog.get(bucket, key)
        if record is None:
            self.adapter.delete_object(bucket, key)
            return 0
        return self.adapter.replace_object(
            project_catalog_record(record, chunker=self.chunker)
        )

    def delete_object(self, bucket: str, key: str) -> None:
        """Remove one already-deleted catalog coordinate from the derived index."""

        if self.catalog.get(bucket, key) is not None:
            raise ValueError("catalog object still exists; delete it before its search entry")
        self.adapter.delete_object(bucket, key)

    def search(self, query: KeywordSearchQuery) -> list[KeywordSearchHit]:
        return self.adapter.search(query)

    def rebuild(self) -> KeywordRebuildReport:
        """Reconstruct the complete derived index from one catalog iteration."""

        iterator = getattr(self.catalog, "iter_objects", None)
        if not callable(iterator):
            raise TypeError("catalog does not support bounded full-object iteration")
        records: Iterator[ObjectRecord] = iterator(batch_size=self.rebuild_batch_size)
        documents = (
            project_catalog_record(record, chunker=self.chunker) for record in records
        )
        try:
            return self.adapter.rebuild(documents)
        finally:
            close = getattr(records, "close", None)
            if callable(close):
                close()
