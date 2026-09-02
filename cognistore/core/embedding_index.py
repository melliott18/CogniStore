"""Versioned embedding indexing and filtered similarity-search contracts.

The orchestration in this module deliberately separates provider calls from
catalog transactions.  A repository implementation may persist each complete
batch atomically, while a retry resumes from the passage identifiers that are
already durable.  Search always names one exact :class:`EmbeddingSpace` so
vectors from incompatible model revisions can never share a result set.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from itertools import islice
from types import MappingProxyType
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from .embeddings import (
    EmbeddingProvider,
    EmbeddingRetryPolicy,
    EmbeddingSpace,
    embed_with_retry,
    validate_embedding_vectors,
)
from .passages import (
    DeterministicPassageChunker,
    NormalizedDocumentIdentity,
    Passage,
    PassageChunkerConfig,
)

EMBEDDING_DOCUMENT_SCHEMA_VERSION = 1
MAX_EMBEDDING_BATCH_SIZE = 1_024
MAX_SIMILARITY_RESULTS = 1_000
MAX_SIMILARITY_FILTER_VALUES = 256
MAX_SIMILARITY_FILTER_TEXT_BYTES = 4_096
MAX_SIMILARITY_KEY_PREFIX_BYTES = 4_096
MAX_SIMILARITY_METADATA_FILTERS = 64
MAX_SIMILARITY_METADATA_KEY_BYTES = 256
MAX_SIMILARITY_METADATA_VALUE_BYTES = 4_096
MAX_SIMILARITY_FILTER_PAYLOAD_BYTES = 64 * 1_024
MAX_SIMILARITY_RESULT_METADATA_BYTES = 16 * 1_024

_EMBEDDING_DOCUMENT_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "https://cognistore.dev/identities/embedding-passage-layouts",
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty string without outer whitespace")
    if "\0" in value:
        raise ValueError(f"{field_name} must not contain NUL characters")
    return value


class EmbeddingIndexError(RuntimeError):
    """Base class for embedding-index orchestration failures."""


class EmbeddingBackendUnsupportedError(EmbeddingIndexError):
    """The selected catalog backend cannot store or search pgvector data."""


class EmbeddingSourceUnavailableError(EmbeddingIndexError):
    """An object has no current successful normalized extraction to embed."""

    def __init__(self, reason: str) -> None:
        self.reason = _required_text(reason, field_name="reason")
        super().__init__(self.reason)


class EmbeddingSourceChangedError(EmbeddingIndexError):
    """The source changed while external embedding work was in progress."""

    retryable = True


class EmbeddingIdentityCollisionError(EmbeddingIndexError):
    """A stable passage, document, or model identity maps to conflicting data."""


class EmbeddingIndexIncompleteError(EmbeddingIndexError):
    """A document cannot be activated until all of its vectors are durable."""


class EmbeddingForceConflictError(EmbeddingIndexError):
    """A forced refresh would invalidate embeddings shared by another object."""


@dataclass(frozen=True)
class EmbeddingDocumentSource:
    """One current, successful normalized extraction loaded from the catalog."""

    bucket: str
    key: str
    extraction_schema_version: int
    source_mime: str
    document: NormalizedDocumentIdentity
    text: str

    def __post_init__(self) -> None:
        _required_text(self.bucket, field_name="bucket")
        _required_text(self.key, field_name="key")
        if (
            isinstance(self.extraction_schema_version, bool)
            or not isinstance(self.extraction_schema_version, int)
            or self.extraction_schema_version < 1
        ):
            raise ValueError("extraction_schema_version must be a positive integer")
        _required_text(self.source_mime, field_name="source_mime")
        if not isinstance(self.document, NormalizedDocumentIdentity):
            raise ValueError("document must be a NormalizedDocumentIdentity")
        if self.extraction_schema_version != self.document.extraction_schema_version:
            raise ValueError(
                "extraction_schema_version must match the normalized document identity"
            )
        if self.source_mime != self.document.source_mime:
            raise ValueError("source_mime must match the normalized document identity")
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")
        try:
            self.text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("text must be valid Unicode") from exc

    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EmbeddingDocument:
    """A deterministic passage layout over one normalized document."""

    source: EmbeddingDocumentSource
    chunker: PassageChunkerConfig
    passages: tuple[Passage, ...]
    fingerprint: str = field(init=False)
    document_id: UUID = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.source, EmbeddingDocumentSource):
            raise ValueError("source must be an EmbeddingDocumentSource")
        if not isinstance(self.chunker, PassageChunkerConfig):
            raise ValueError("chunker must be a PassageChunkerConfig")
        passages = tuple(self.passages)
        object.__setattr__(self, "passages", passages)
        for expected_index, passage in enumerate(passages):
            if not isinstance(passage, Passage):
                raise ValueError("passages must contain only Passage values")
            if passage.document != self.source.document or passage.chunker != self.chunker:
                raise ValueError("passage provenance must match its embedding document")
            if passage.index != expected_index:
                raise ValueError("passage indexes must be contiguous and start at zero")

        payload = {
            "schema_version": EMBEDDING_DOCUMENT_SCHEMA_VERSION,
            "source_document_fingerprint": self.source.document.fingerprint,
            "text_sha256": self.source.text_sha256,
            "chunker": self.chunker.to_metadata(),
            "passage_count": len(passages),
        }
        fingerprint = hashlib.sha256(_canonical_json(payload).encode("ascii")).hexdigest()
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(
            self,
            "document_id",
            uuid5(_EMBEDDING_DOCUMENT_NAMESPACE, fingerprint),
        )

    @classmethod
    def build(
        cls,
        source: EmbeddingDocumentSource,
        chunker: DeterministicPassageChunker,
    ) -> EmbeddingDocument:
        if not isinstance(chunker, DeterministicPassageChunker):
            raise ValueError("chunker must be a DeterministicPassageChunker")
        passages = chunker.chunk(source.text, document=source.document)
        return cls(source=source, chunker=chunker.config, passages=passages)


@dataclass(frozen=True)
class HnswIndexConfig:
    """Persisted pgvector HNSW build and query settings for one space."""

    enabled: bool = True
    m: int = 16
    ef_construction: int = 64
    ef_search: int = 40
    iterative_scan: str = "strict_order"

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        for name, minimum, maximum in (
            ("m", 2, 100),
            ("ef_construction", 4, 1_000),
            ("ef_search", 1, 1_000),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if self.ef_construction < 2 * self.m:
            raise ValueError("ef_construction must be at least twice m")
        if self.iterative_scan not in {"off", "strict_order", "relaxed_order"}:
            raise ValueError("iterative_scan must be off, strict_order, or relaxed_order")


@dataclass(frozen=True)
class SimilaritySearchFilters:
    """Bounded structured filters applied before nearest-neighbor ordering."""

    buckets: frozenset[str] = frozenset()
    key_prefix: str = ""
    tiers: frozenset[str] = frozenset()
    mime_types: frozenset[str] = frozenset()
    metadata: Mapping[str, str] = field(default_factory=dict)
    # Appended to preserve the positional constructor used by earlier releases.
    object_keys: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        payload_bytes = 0
        for field_name in ("buckets", "object_keys", "tiers", "mime_types"):
            raw_values = getattr(self, field_name)
            if isinstance(raw_values, (str, bytes, bytearray, Mapping)) or not isinstance(
                raw_values, Iterable
            ):
                raise ValueError(f"{field_name} must be a finite collection of strings")
            detached_values = tuple(islice(iter(raw_values), MAX_SIMILARITY_FILTER_VALUES + 1))
            if len(detached_values) > MAX_SIMILARITY_FILTER_VALUES:
                raise ValueError(
                    f"{field_name} must contain at most {MAX_SIMILARITY_FILTER_VALUES} values"
                )
            for value in detached_values:
                if not isinstance(value, str) or not value:
                    raise ValueError(f"{field_name} must contain only non-empty strings")
                if field_name == "mime_types" and "\0" in value:
                    raise ValueError("mime_types must not contain NUL characters")
                try:
                    encoded_value = value.encode(
                        "utf-8",
                        errors=("strict" if field_name == "mime_types" else "surrogatepass"),
                    )
                except UnicodeEncodeError as exc:
                    raise ValueError(f"{field_name} must contain valid UTF-8 strings") from exc
                if len(encoded_value) > MAX_SIMILARITY_FILTER_TEXT_BYTES:
                    raise ValueError(
                        f"{field_name} values must be at most "
                        f"{MAX_SIMILARITY_FILTER_TEXT_BYTES} UTF-8 bytes"
                    )
                payload_bytes += len(encoded_value)
            values = frozenset(detached_values)
            object.__setattr__(self, field_name, values)
        if not isinstance(self.key_prefix, str):
            raise ValueError(
                f"key_prefix must be a string of at most "
                f"{MAX_SIMILARITY_KEY_PREFIX_BYTES} UTF-8 bytes"
            )
        key_prefix_bytes = self.key_prefix.encode("utf-8", errors="surrogatepass")
        if len(key_prefix_bytes) > MAX_SIMILARITY_KEY_PREFIX_BYTES:
            raise ValueError(
                "key_prefix must be a string of at most "
                f"{MAX_SIMILARITY_KEY_PREFIX_BYTES} UTF-8 bytes"
            )
        payload_bytes += len(key_prefix_bytes)
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a mapping of string keys to strings")
        if len(self.metadata) > MAX_SIMILARITY_METADATA_FILTERS:
            raise ValueError(
                f"metadata must contain at most {MAX_SIMILARITY_METADATA_FILTERS} filters"
            )
        safe_metadata: dict[str, str] = {}
        for key, value in self.metadata.items():
            if not isinstance(key, str) or not key or "\0" in key:
                raise ValueError("metadata filter keys must be non-empty NUL-free UTF-8 strings")
            if not isinstance(value, str) or "\0" in value:
                raise ValueError("metadata filter values must be NUL-free UTF-8 strings")
            try:
                key_bytes = key.encode("utf-8")
                value_bytes = value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError(
                    "metadata filter keys and values must be valid UTF-8 strings"
                ) from exc
            if len(key_bytes) > MAX_SIMILARITY_METADATA_KEY_BYTES:
                raise ValueError(
                    "metadata filter keys must be at most "
                    f"{MAX_SIMILARITY_METADATA_KEY_BYTES} UTF-8 bytes"
                )
            if len(value_bytes) > MAX_SIMILARITY_METADATA_VALUE_BYTES:
                raise ValueError(
                    "metadata filter values must be at most "
                    f"{MAX_SIMILARITY_METADATA_VALUE_BYTES} UTF-8 bytes"
                )
            payload_bytes += len(key_bytes) + len(value_bytes)
            safe_metadata[key] = value
        if payload_bytes > MAX_SIMILARITY_FILTER_PAYLOAD_BYTES:
            raise ValueError(
                "similarity filter payload must be at most "
                f"{MAX_SIMILARITY_FILTER_PAYLOAD_BYTES} bytes"
            )
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(deepcopy(safe_metadata)),
        )


@dataclass(frozen=True)
class SimilaritySearchResult:
    """One source-backed passage hit in an exact model search space."""

    space_id: UUID
    passage_id: UUID
    document_id: UUID
    source_sha256: str
    document_text_sha256: str
    bucket: str
    key: str
    tier: str
    mime: str
    passage_index: int
    start_codepoint: int
    end_codepoint: int
    text: str
    text_sha256: str
    cosine_distance: float
    object_metadata: Mapping[str, object]
    object_metadata_truncated: bool = False
    indexed_at: str | None = None

    @property
    def score(self) -> float:
        return 1.0 - self.cosine_distance


@dataclass(frozen=True)
class EmbeddingIndexReport:
    """Deterministic progress summary for one object/model indexing pass."""

    document_id: UUID
    space_id: UUID
    total_passages: int
    embedded_passages: int
    reused_passages: int
    batches: int


class EmbeddingIndexRepository(Protocol):
    """Persistence boundary used by :class:`EmbeddingIndexer`."""

    def load_source(self, bucket: str, key: str) -> EmbeddingDocumentSource: ...

    def ensure_space(self, space: EmbeddingSpace, config: HnswIndexConfig) -> None: ...

    def prepare_document(self, document: EmbeddingDocument) -> None: ...

    def existing_passage_ids(
        self,
        document_id: UUID,
        space_id: UUID,
    ) -> frozenset[UUID]: ...

    def reset_document_space(
        self,
        document: EmbeddingDocument,
        space: EmbeddingSpace,
    ) -> None: ...

    def write_embeddings(
        self,
        document: EmbeddingDocument,
        space: EmbeddingSpace,
        values: Sequence[tuple[Passage, Sequence[float]]],
    ) -> None: ...

    def complete_document_space(
        self,
        document: EmbeddingDocument,
        space: EmbeddingSpace,
    ) -> None: ...

    def search(
        self,
        space: EmbeddingSpace,
        query_vector: Sequence[float],
        *,
        filters: SimilaritySearchFilters,
        limit: int,
        exact: bool = False,
    ) -> list[SimilaritySearchResult]: ...


class EmbeddingIndexer:
    """Batch, retry, persist, and query versioned normalized-text passages."""

    def __init__(
        self,
        repository: EmbeddingIndexRepository,
        provider: EmbeddingProvider,
        *,
        chunker: DeterministicPassageChunker | None = None,
        batch_size: int = 32,
        retry_policy: EmbeddingRetryPolicy | None = None,
        hnsw: HnswIndexConfig | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int):
            raise ValueError("batch_size must be an integer")
        if not 1 <= batch_size <= MAX_EMBEDDING_BATCH_SIZE:
            raise ValueError(f"batch_size must be between 1 and {MAX_EMBEDDING_BATCH_SIZE}")
        self.repository = repository
        self.provider = provider
        self.chunker = chunker or DeterministicPassageChunker()
        self.batch_size = batch_size
        self.retry_policy = retry_policy or EmbeddingRetryPolicy()
        self.hnsw = hnsw or HnswIndexConfig()
        self._sleep = sleep

    def _retry(self, operation: Callable[[], object]) -> object:
        if self._sleep is None:
            return embed_with_retry(operation, policy=self.retry_policy)
        return embed_with_retry(
            operation,
            policy=self.retry_policy,
            sleep=self._sleep,
        )

    def index_object(self, bucket: str, key: str, *, force: bool = False) -> EmbeddingIndexReport:
        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")
        source = self.repository.load_source(bucket, key)
        document = EmbeddingDocument.build(source, self.chunker)
        space = self.provider.space
        self.repository.ensure_space(space, self.hnsw)
        self.repository.prepare_document(document)

        existing: frozenset[UUID]
        if force:
            # Hide and clear the old complete set before regenerating. A failed
            # forced pass can then resume from its durable new batches without
            # exposing or mistaking untouched old vectors for refreshed rows.
            self.repository.reset_document_space(document, space)
            existing = frozenset()
        else:
            existing = self.repository.existing_passage_ids(
                document.document_id,
                space.space_id,
            )
        pending = [passage for passage in document.passages if passage.passage_id not in existing]
        batches = 0
        for offset in range(0, len(pending), self.batch_size):
            batch = pending[offset : offset + self.batch_size]
            texts = tuple(passage.text for passage in batch)
            raw_vectors = self._retry(lambda: self.provider.embed_documents(texts))
            vectors = validate_embedding_vectors(
                raw_vectors,
                expected_count=len(batch),
                dimensions=space.dimensions,
                distance_metric=space.distance_metric,
            )
            self.repository.write_embeddings(
                document,
                space,
                list(zip(batch, vectors)),
            )
            batches += 1

        self.repository.complete_document_space(document, space)
        return EmbeddingIndexReport(
            document_id=document.document_id,
            space_id=space.space_id,
            total_passages=len(document.passages),
            embedded_passages=len(pending),
            reused_passages=len(document.passages) - len(pending),
            batches=batches,
        )

    def search(
        self,
        query: str,
        *,
        filters: SimilaritySearchFilters | None = None,
        limit: int = 10,
        exact: bool = False,
    ) -> list[SimilaritySearchResult]:
        _required_text(query, field_name="query")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        if not 1 <= limit <= MAX_SIMILARITY_RESULTS:
            raise ValueError(f"limit must be between 1 and {MAX_SIMILARITY_RESULTS}")
        if not isinstance(exact, bool):
            raise ValueError("exact must be a boolean")
        if filters is None:
            safe_filters = SimilaritySearchFilters()
        elif isinstance(filters, SimilaritySearchFilters):
            safe_filters = filters
        else:
            raise ValueError("filters must be SimilaritySearchFilters or None")
        vector = self.query_vector(query)
        return self.repository.search(
            self.provider.space,
            vector,
            filters=safe_filters,
            limit=limit,
            exact=exact,
        )

    def query_vector(self, query: str) -> tuple[float, ...]:
        """Embed and validate one query in this indexer's exact search space."""

        _required_text(query, field_name="query")
        raw_vector = self._retry(lambda: self.provider.embed_query(query))
        vectors = validate_embedding_vectors(
            [raw_vector],
            expected_count=1,
            dimensions=self.provider.space.dimensions,
            distance_metric=self.provider.space.distance_metric,
        )
        return vectors[0]


def vector_literal(vector: Sequence[float]) -> str:
    """Return pgvector's canonical dense-vector input representation."""

    normalized: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("vector components must be finite numbers")
        component = float(value)
        if not math.isfinite(component):
            raise ValueError("vector components must be finite numbers")
        normalized.append(component)
    if not normalized:
        raise ValueError("vectors must not be empty")
    return "[" + ",".join(format(component, ".17g") for component in normalized) + "]"
