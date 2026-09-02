"""Grounded hybrid retrieval over catalog, keyword, and vector signals.

The keyword and vector indexes intentionally use different passage layouts.
This module therefore fuses their ranked object coordinates, not their opaque
passage identifiers.  Evidence keeps the source-qualified passage identity so
callers can inspect and cite exactly what each backend returned.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from itertools import islice
from types import MappingProxyType
from typing import Protocol

from cognistore.core.catalog import CatalogStore, ObjectRecord
from cognistore.core.document_extraction import (
    EXTRACTION_SCHEMA_VERSION,
    NORMALIZATION_VERSION,
)
from cognistore.core.embedding_index import (
    EmbeddingBackendUnsupportedError,
    SimilaritySearchFilters,
    SimilaritySearchResult,
)
from cognistore.core.embeddings import (
    EmbeddingProviderConfigurationError,
    TransientEmbeddingProviderError,
)

from .keyword import (
    DOCUMENT_METADATA_FIELDS,
    KeywordSearchFilters,
    KeywordSearchHit,
    KeywordSearchQuery,
)

ASK_SCHEMA_VERSION = 1
DEFAULT_CANDIDATE_LIMIT = 100
DEFAULT_PASSAGES_PER_RESULT = 3
DEFAULT_RRF_RANK_CONSTANT = 60.0
MAX_ASK_RESULTS = 100
MAX_ASK_CANDIDATES = 1_000
MAX_PASSAGES_PER_RESULT = 10
MAX_QUERY_BYTES = 16 * 1_024
MAX_FILTER_KEY_BYTES = 256
MAX_FILTER_VALUE_BYTES = 4 * 1_024
MAX_FILTER_ITEMS = 64
MAX_FILTER_PAYLOAD_BYTES = 64 * 1_024
MAX_GENERATED_ANSWER_BYTES = 64 * 1_024
MAX_GENERATED_CITATION_BYTES = 1_024
MAX_GENERATED_CITATIONS = MAX_ASK_RESULTS * (MAX_PASSAGES_PER_RESULT + 1)
MAX_KEY_PREFIX_BYTES = 4 * 1_024
MAX_RESULT_METADATA_BYTES = 16 * 1_024

JSONScalar = str | int | float | bool | None
_LOWERCASE_HEX = frozenset("0123456789abcdef")
_TOKEN = re.compile(r"\w+", flags=re.UNICODE)
_INTERNAL_METADATA_KEYS = frozenset(
    {
        "content_identity",
        "document_extraction",
        "etag",
        "mime_detection",
        "mtime",
        "path",
        "sample_len",
        "version_id",
    }
)
_SIGNAL_ORDER = {
    "metadata": 0,
    "keyword": 1,
    "vector": 2,
}


class RetrievalProviderUnavailableError(RuntimeError):
    """An optional retrieval provider is not available in this deployment."""


class AnswerProviderUnavailableError(RuntimeError):
    """The configured answer provider cannot currently serve requests."""


class AnswerCitationError(ValueError):
    """A generated answer cited context that was not supplied to its provider."""


class RetrievalSignal(str, Enum):
    METADATA = "metadata"
    KEYWORD = "keyword"
    VECTOR = "vector"


class RetrievalMode(str, Enum):
    METADATA = "metadata"
    METADATA_KEYWORD = "metadata+keyword"
    METADATA_VECTOR = "metadata+vector"
    HYBRID = "metadata+keyword+vector"

    @property
    def signals(self) -> tuple[RetrievalSignal, ...]:
        return {
            RetrievalMode.METADATA: (RetrievalSignal.METADATA,),
            RetrievalMode.METADATA_KEYWORD: (
                RetrievalSignal.METADATA,
                RetrievalSignal.KEYWORD,
            ),
            RetrievalMode.METADATA_VECTOR: (
                RetrievalSignal.METADATA,
                RetrievalSignal.VECTOR,
            ),
            RetrievalMode.HYBRID: (
                RetrievalSignal.METADATA,
                RetrievalSignal.KEYWORD,
                RetrievalSignal.VECTOR,
            ),
        }[self]


class ProviderState(str, Enum):
    SUCCEEDED = "succeeded"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"
    NOT_REQUESTED = "not_requested"
    NO_CONTEXT = "no_context"


class GenerationStatus(str, Enum):
    NOT_REQUESTED = "not_requested"
    PROVIDER_MISSING = "provider_missing"
    NO_CONTEXT = "no_context"
    SUCCEEDED = "succeeded"
    PROVIDER_UNAVAILABLE = "provider_unavailable"


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty string without outer whitespace")
    if "\0" in value:
        raise ValueError(f"{field_name} must not contain NUL characters")
    return value


def _identity_text(value: object, *, field_name: str) -> str:
    """Validate a non-empty catalog identity without changing its bytes."""

    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_identity_text(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _identity_text(value, field_name=field_name)


def _optional_text(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name=field_name)


def _bounded_integer(
    value: object,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return value


def _finite_number(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number")
    try:
        converted = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(converted):
        raise ValueError(f"{field_name} must be a finite number")
    return converted


def _sha256(value: object, *, field_name: str, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _LOWERCASE_HEX for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase 64-character SHA-256 digest")
    return value


def _is_json_scalar(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _scalar_mapping(
    value: object,
    *,
    field_name: str,
    allowed_names: frozenset[str] | None = None,
) -> Mapping[str, JSONScalar]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping of string keys to JSON scalars")
    if len(value) > MAX_FILTER_ITEMS:
        raise ValueError(f"{field_name} must contain at most {MAX_FILTER_ITEMS} entries")
    detached: dict[str, JSONScalar] = {}
    payload_bytes = 0
    for name, item in value.items():
        if not isinstance(name, str) or not name or "\0" in name:
            raise ValueError(f"{field_name} keys must be non-empty NUL-free strings")
        if allowed_names is not None and name not in allowed_names:
            raise ValueError(f"unsupported {field_name} field: {name}")
        if not _is_json_scalar(item):
            raise ValueError(f"{field_name} values must be finite JSON scalars")
        try:
            name_bytes = name.encode("utf-8")
            item_json = json.dumps(item, ensure_ascii=False, allow_nan=False)
            item_bytes = item_json.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(f"{field_name} keys and values must be valid UTF-8") from exc
        if len(name_bytes) > MAX_FILTER_KEY_BYTES:
            raise ValueError(
                f"{field_name} keys must be at most {MAX_FILTER_KEY_BYTES} UTF-8 bytes"
            )
        if len(item_bytes) > MAX_FILTER_VALUE_BYTES:
            raise ValueError(
                f"{field_name} values must be at most {MAX_FILTER_VALUE_BYTES} UTF-8 bytes"
            )
        payload_bytes += len(name_bytes) + len(item_bytes)
        detached[name] = deepcopy(item)
    if payload_bytes > MAX_FILTER_PAYLOAD_BYTES:
        raise ValueError(f"{field_name} payload must be at most {MAX_FILTER_PAYLOAD_BYTES} bytes")
    return MappingProxyType(detached)


def _json_scalar_equal(left: object, right: object) -> bool:
    if not _is_json_scalar(left) or not _is_json_scalar(right):
        return False
    return json.dumps(
        left,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ) == json.dumps(
        right,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    )


def _json_scalar_mapping_equal(
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> bool:
    return left.keys() == right.keys() and all(
        _json_scalar_equal(left[name], right[name]) for name in left
    )


def _scalar_mapping_payload_bytes(value: Mapping[str, JSONScalar]) -> int:
    return sum(
        len(name.encode("utf-8"))
        + len(
            json.dumps(item, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
        for name, item in value.items()
    )


def _json_mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} must be a mapping with string keys")
    detached = deepcopy(dict(value))
    try:
        json.dumps(detached, ensure_ascii=True, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain JSON-compatible values") from exc
    return MappingProxyType(detached)


@dataclass(frozen=True)
class AskFilters:
    """Common exact filters, enforced against the authoritative catalog."""

    bucket: str | None = None
    key_prefix: str = ""
    tier: str | None = None
    mime: str | None = None
    size: int | None = None
    content_sha256: str | None = None
    object_metadata: Mapping[str, JSONScalar] = field(default_factory=dict)
    document_metadata: Mapping[str, JSONScalar] = field(default_factory=dict)

    def __post_init__(self) -> None:
        bucket = _optional_identity_text(self.bucket, field_name="bucket")
        tier = _optional_identity_text(self.tier, field_name="tier")
        mime = _optional_text(self.mime, field_name="mime")
        object.__setattr__(self, "bucket", bucket)
        object.__setattr__(self, "tier", tier)
        object.__setattr__(self, "mime", mime)
        identity_payload_bytes = 0
        for name, value, errors in (
            ("bucket", bucket, "surrogatepass"),
            ("tier", tier, "surrogatepass"),
            ("mime", mime, "strict"),
        ):
            if value is None:
                continue
            try:
                encoded = value.encode("utf-8", errors=errors)
            except UnicodeEncodeError as exc:
                raise ValueError(f"{name} must be valid UTF-8") from exc
            if len(encoded) > MAX_FILTER_VALUE_BYTES:
                raise ValueError(
                    f"{name} must be at most {MAX_FILTER_VALUE_BYTES} UTF-8 bytes"
                )
            identity_payload_bytes += len(encoded)
        if not isinstance(self.key_prefix, str):
            raise ValueError("key_prefix must be a string")
        key_prefix_bytes = self.key_prefix.encode("utf-8", errors="surrogatepass")
        if len(key_prefix_bytes) > MAX_KEY_PREFIX_BYTES:
            raise ValueError(f"key_prefix must be at most {MAX_KEY_PREFIX_BYTES} UTF-8 bytes")
        if self.size is not None:
            _bounded_integer(
                self.size,
                field_name="size",
                minimum=0,
                maximum=2**63 - 1,
            )
        object.__setattr__(
            self,
            "content_sha256",
            _sha256(self.content_sha256, field_name="content_sha256", optional=True),
        )
        safe_object_metadata = _scalar_mapping(
            self.object_metadata,
            field_name="object_metadata",
        )
        internal_filters = sorted(set(safe_object_metadata).intersection(_INTERNAL_METADATA_KEYS))
        if internal_filters:
            raise ValueError(
                "object_metadata cannot filter internal field(s): "
                + ", ".join(internal_filters)
            )
        safe_document_metadata = _scalar_mapping(
            self.document_metadata,
            field_name="document_metadata",
            allowed_names=frozenset(DOCUMENT_METADATA_FIELDS),
        )
        if any(value is None for value in safe_document_metadata.values()):
            raise ValueError("document_metadata filter values must not be null")
        aggregate_payload_bytes = (
            identity_payload_bytes
            + len(key_prefix_bytes)
            + _scalar_mapping_payload_bytes(safe_object_metadata)
            + _scalar_mapping_payload_bytes(safe_document_metadata)
        )
        if aggregate_payload_bytes > MAX_FILTER_PAYLOAD_BYTES:
            raise ValueError(
                f"Ask filter payload must be at most {MAX_FILTER_PAYLOAD_BYTES} bytes"
            )
        object.__setattr__(self, "object_metadata", safe_object_metadata)
        object.__setattr__(self, "document_metadata", safe_document_metadata)


@dataclass(frozen=True)
class AskQuery:
    """One bounded hybrid retrieval request."""

    text: str
    filters: AskFilters = field(default_factory=AskFilters)
    limit: int = 10
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT
    passages_per_result: int = DEFAULT_PASSAGES_PER_RESULT
    synthesize: bool = False
    exact_vector: bool = False
    retrieval_mode: RetrievalMode = RetrievalMode.HYBRID

    def __post_init__(self) -> None:
        query_text = _required_text(self.text, field_name="query text")
        try:
            query_bytes = query_text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("query text must be valid UTF-8") from exc
        if len(query_bytes) > MAX_QUERY_BYTES:
            raise ValueError(f"query text must be at most {MAX_QUERY_BYTES} UTF-8 bytes")
        if not isinstance(self.filters, AskFilters):
            raise ValueError("filters must be AskFilters")
        if not isinstance(self.retrieval_mode, RetrievalMode):
            raise ValueError("retrieval_mode must be RetrievalMode")
        limit = _bounded_integer(
            self.limit,
            field_name="limit",
            minimum=1,
            maximum=MAX_ASK_RESULTS,
        )
        candidate_limit = _bounded_integer(
            self.candidate_limit,
            field_name="candidate_limit",
            minimum=1,
            maximum=MAX_ASK_CANDIDATES,
        )
        if candidate_limit < limit:
            raise ValueError("candidate_limit must be greater than or equal to limit")
        _bounded_integer(
            self.passages_per_result,
            field_name="passages_per_result",
            minimum=1,
            maximum=MAX_PASSAGES_PER_RESULT,
        )
        if not isinstance(self.synthesize, bool):
            raise ValueError("synthesize must be a boolean")
        if not isinstance(self.exact_vector, bool):
            raise ValueError("exact_vector must be a boolean")


@dataclass(frozen=True)
class FusionConfig:
    """Weighted reciprocal-rank fusion settings."""

    rank_constant: float = DEFAULT_RRF_RANK_CONSTANT
    metadata_weight: float = 1.0
    keyword_weight: float = 1.0
    vector_weight: float = 1.0

    def __post_init__(self) -> None:
        rank_constant = _finite_number(self.rank_constant, field_name="rank_constant")
        if rank_constant <= 0:
            raise ValueError("rank_constant must be greater than zero")
        weights = tuple(
            _finite_number(getattr(self, name), field_name=name)
            for name in ("metadata_weight", "keyword_weight", "vector_weight")
        )
        if any(weight < 0 for weight in weights):
            raise ValueError("fusion weights must be non-negative")
        if not any(weight > 0 for weight in weights):
            raise ValueError("at least one fusion weight must be greater than zero")
        total_weight = sum(weights)
        if not math.isfinite(total_weight):
            raise ValueError("the aggregate fusion weight must be finite")
        if not math.isfinite(total_weight / (rank_constant + 1.0)):
            raise ValueError("fusion settings can produce a non-finite result score")

    def weight(self, signal: RetrievalSignal) -> float:
        return {
            RetrievalSignal.METADATA: float(self.metadata_weight),
            RetrievalSignal.KEYWORD: float(self.keyword_weight),
            RetrievalSignal.VECTOR: float(self.vector_weight),
        }[signal]

    def contribution(self, signal: RetrievalSignal, rank: int) -> float:
        _bounded_integer(rank, field_name="rank", minimum=1, maximum=2**63 - 1)
        return self.weight(signal) / (float(self.rank_constant) + rank)


@dataclass(frozen=True)
class MetadataSearchQuery:
    text: str
    filters: AskFilters
    limit: int

    def __post_init__(self) -> None:
        _required_text(self.text, field_name="metadata query text")
        if not isinstance(self.filters, AskFilters):
            raise ValueError("metadata filters must be AskFilters")
        _bounded_integer(
            self.limit,
            field_name="metadata query limit",
            minimum=1,
            maximum=MAX_ASK_CANDIDATES,
        )


@dataclass(frozen=True)
class MetadataSearchHit:
    bucket: str
    key: str
    score: float

    def __post_init__(self) -> None:
        _identity_text(self.bucket, field_name="metadata hit bucket")
        _identity_text(self.key, field_name="metadata hit key")
        score = _finite_number(self.score, field_name="metadata hit score")
        if score < 0:
            raise ValueError("metadata hit score must be non-negative")


class MetadataRetriever(Protocol):
    def search(self, query: MetadataSearchQuery) -> list[MetadataSearchHit]: ...


class KeywordRetriever(Protocol):
    def search(self, query: KeywordSearchQuery) -> list[KeywordSearchHit]: ...


class VectorRetriever(Protocol):
    def search(
        self,
        query: str,
        *,
        filters: SimilaritySearchFilters | None = None,
        limit: int = 10,
        exact: bool = False,
    ) -> list[SimilaritySearchResult]: ...


@dataclass(frozen=True)
class ObjectCitation:
    object_id: str
    bucket: str
    key: str
    tier: str
    size: int
    mime: str | None
    content_sha256: str | None
    object_metadata: Mapping[str, object]
    document_metadata: Mapping[str, object]
    object_metadata_truncated: bool = False
    document_metadata_truncated: bool = False

    def __post_init__(self) -> None:
        _sha256(self.object_id, field_name="object_id")
        _identity_text(self.bucket, field_name="citation bucket")
        _identity_text(self.key, field_name="citation key")
        _identity_text(self.tier, field_name="citation tier")
        if self.object_id != _catalog_object_id(self.bucket, self.key):
            raise ValueError("object_id does not match the citation coordinate")
        _bounded_integer(
            self.size,
            field_name="citation size",
            minimum=0,
            maximum=2**63 - 1,
        )
        _optional_text(self.mime, field_name="citation mime")
        _sha256(self.content_sha256, field_name="citation content_sha256", optional=True)
        object.__setattr__(
            self,
            "object_metadata",
            _json_mapping(self.object_metadata, field_name="citation object_metadata"),
        )
        object.__setattr__(
            self,
            "document_metadata",
            _json_mapping(self.document_metadata, field_name="citation document_metadata"),
        )
        if not isinstance(self.object_metadata_truncated, bool):
            raise ValueError("object_metadata_truncated must be a boolean")
        if not isinstance(self.document_metadata_truncated, bool):
            raise ValueError("document_metadata_truncated must be a boolean")

    @property
    def citation_id(self) -> str:
        return f"object:{self.object_id}"


@dataclass(frozen=True)
class PassageCitation:
    object_id: str
    bucket: str
    key: str
    source: RetrievalSignal
    passage_id: str
    passage_index: int
    start_codepoint: int
    end_codepoint: int
    text_sha256: str
    source_sha256: str
    document_text_sha256: str
    document_id: str | None = None
    space_id: str | None = None

    def __post_init__(self) -> None:
        _sha256(self.object_id, field_name="passage object_id")
        _identity_text(self.bucket, field_name="passage bucket")
        _identity_text(self.key, field_name="passage key")
        if self.object_id != _catalog_object_id(self.bucket, self.key):
            raise ValueError("passage object_id does not match its catalog coordinate")
        if not isinstance(self.source, RetrievalSignal) or self.source not in {
            RetrievalSignal.KEYWORD,
            RetrievalSignal.VECTOR,
        }:
            raise ValueError("passage source must be keyword or vector")
        _required_text(self.passage_id, field_name="passage_id")
        _bounded_integer(
            self.passage_index,
            field_name="passage_index",
            minimum=0,
            maximum=2**63 - 1,
        )
        start = _bounded_integer(
            self.start_codepoint,
            field_name="start_codepoint",
            minimum=0,
            maximum=2**63 - 1,
        )
        end = _bounded_integer(
            self.end_codepoint,
            field_name="end_codepoint",
            minimum=0,
            maximum=2**63 - 1,
        )
        if end < start:
            raise ValueError("end_codepoint must not precede start_codepoint")
        _sha256(self.text_sha256, field_name="passage text_sha256")
        _sha256(self.source_sha256, field_name="passage source_sha256")
        _sha256(
            self.document_text_sha256,
            field_name="passage document_text_sha256",
        )
        _optional_text(self.document_id, field_name="passage document_id")
        _optional_text(self.space_id, field_name="passage space_id")
        if self.source is RetrievalSignal.VECTOR and (
            self.document_id is None or self.space_id is None
        ):
            raise ValueError("vector passage citations require document_id and space_id")

    @property
    def citation_id(self) -> str:
        return f"passage:{self.object_id}:{self.source.value}:{self.passage_id}"


@dataclass(frozen=True)
class PassageMatch:
    signal: RetrievalSignal
    rank: int
    raw_score: float

    def __post_init__(self) -> None:
        if not isinstance(self.signal, RetrievalSignal):
            raise ValueError("passage match signal must be RetrievalSignal")
        if self.signal is RetrievalSignal.METADATA:
            raise ValueError("metadata matches do not identify text passages")
        _bounded_integer(self.rank, field_name="passage rank", minimum=1, maximum=2**63 - 1)
        _finite_number(self.raw_score, field_name="passage raw_score")


@dataclass(frozen=True)
class RetrievedPassage:
    citation: PassageCitation
    text: str
    match: PassageMatch

    def __post_init__(self) -> None:
        if not isinstance(self.citation, PassageCitation):
            raise ValueError("citation must be PassageCitation")
        if not isinstance(self.text, str):
            raise ValueError("passage text must be a string")
        if len(self.text) != self.citation.end_codepoint - self.citation.start_codepoint:
            raise ValueError("passage text length must match citation offsets")
        try:
            digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        except UnicodeEncodeError as exc:
            raise ValueError("passage text must be valid UTF-8") from exc
        if digest != self.citation.text_sha256:
            raise ValueError("passage text does not match its citation digest")
        if not isinstance(self.match, PassageMatch) or self.match.signal is not self.citation.source:
            raise ValueError("passage match signal must match its citation source")


@dataclass(frozen=True)
class ScoreComponent:
    signal: RetrievalSignal
    rank: int
    raw_score: float
    weight: float
    contribution: float

    def __post_init__(self) -> None:
        if not isinstance(self.signal, RetrievalSignal):
            raise ValueError("score signal must be RetrievalSignal")
        _bounded_integer(self.rank, field_name="score rank", minimum=1, maximum=2**63 - 1)
        _finite_number(self.raw_score, field_name="score raw_score")
        weight = _finite_number(self.weight, field_name="score weight")
        contribution = _finite_number(self.contribution, field_name="score contribution")
        if weight < 0 or contribution < 0:
            raise ValueError("score weight and contribution must be non-negative")


@dataclass(frozen=True)
class RetrievalResult:
    citation: ObjectCitation
    score: float
    score_components: tuple[ScoreComponent, ...]
    passages: tuple[RetrievedPassage, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.citation, ObjectCitation):
            raise ValueError("result citation must be ObjectCitation")
        score = _finite_number(self.score, field_name="result score")
        if score < 0:
            raise ValueError("result score must be non-negative")
        components = tuple(self.score_components)
        if not components or any(not isinstance(item, ScoreComponent) for item in components):
            raise ValueError("result must contain score components")
        if len({component.signal for component in components}) != len(components):
            raise ValueError("result score components must have unique signals")
        if not math.isclose(
            score,
            sum(component.contribution for component in components),
            rel_tol=1e-12,
            abs_tol=1e-15,
        ):
            raise ValueError("result score must equal its component contributions")
        passages = tuple(self.passages)
        if any(not isinstance(item, RetrievedPassage) for item in passages):
            raise ValueError("result passages must contain RetrievedPassage values")
        if any(
            (
                item.citation.object_id,
                item.citation.bucket,
                item.citation.key,
            )
            != (
                self.citation.object_id,
                self.citation.bucket,
                self.citation.key,
            )
            for item in passages
        ):
            raise ValueError("every passage must cite the result object")
        if len({item.citation.citation_id for item in passages}) != len(passages):
            raise ValueError("result passage citations must be unique")
        object.__setattr__(self, "score_components", components)
        object.__setattr__(self, "passages", passages)


@dataclass(frozen=True)
class ProviderDiagnostic:
    component: str
    state: ProviderState
    error_type: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.component, field_name="provider component")
        if not isinstance(self.state, ProviderState):
            raise ValueError("provider state must be ProviderState")
        _optional_text(self.error_type, field_name="provider error_type")
        if self.state is ProviderState.UNAVAILABLE and self.error_type is None:
            raise ValueError("unavailable providers must identify their error type")
        if self.state is not ProviderState.UNAVAILABLE and self.error_type is not None:
            raise ValueError("only unavailable providers may identify an error type")


@dataclass(frozen=True)
class GeneratedAnswer:
    text: str
    citations: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip() or "\0" in self.text:
            raise ValueError("generated answer text must be non-empty and NUL-free")
        try:
            answer_bytes = self.text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("generated answer text must be valid UTF-8") from exc
        if len(answer_bytes) > MAX_GENERATED_ANSWER_BYTES:
            raise ValueError(
                f"generated answer text must be at most {MAX_GENERATED_ANSWER_BYTES} bytes"
            )
        if isinstance(self.citations, (str, bytes, bytearray)):
            raise ValueError("generated answer citations must be a finite collection")
        citations = tuple(islice(self.citations, MAX_GENERATED_CITATIONS + 1))
        if not citations or any(not isinstance(item, str) or not item for item in citations):
            raise ValueError("generated answers must contain citation identifiers")
        if len(citations) > MAX_GENERATED_CITATIONS:
            raise ValueError(
                f"generated answers may cite at most {MAX_GENERATED_CITATIONS} identifiers"
            )
        for item in citations:
            try:
                citation_bytes = item.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError(
                    "generated answer citation identifiers must be valid UTF-8"
                ) from exc
            if len(citation_bytes) > MAX_GENERATED_CITATION_BYTES:
                raise ValueError(
                    "generated answer citation identifiers must be at most "
                    f"{MAX_GENERATED_CITATION_BYTES} UTF-8 bytes"
                )
        if len(set(citations)) != len(citations):
            raise ValueError("generated answer citations must be unique")
        object.__setattr__(self, "citations", citations)


@dataclass(frozen=True)
class AnswerGenerationRequest:
    query: str
    mode: RetrievalMode
    results: tuple[RetrievalResult, ...]

    def __post_init__(self) -> None:
        _required_text(self.query, field_name="answer query")
        if not isinstance(self.mode, RetrievalMode):
            raise ValueError("answer mode must be RetrievalMode")
        results = tuple(self.results)
        if not results or any(not isinstance(item, RetrievalResult) for item in results):
            raise ValueError("answer generation requires retrieval results")
        object.__setattr__(self, "results", results)

    @property
    def citation_ids(self) -> tuple[str, ...]:
        identifiers: list[str] = []
        for result in self.results:
            identifiers.append(result.citation.citation_id)
            identifiers.extend(passage.citation.citation_id for passage in result.passages)
        return tuple(identifiers)


class AnswerProvider(Protocol):
    def generate(self, request: AnswerGenerationRequest) -> GeneratedAnswer: ...


@dataclass(frozen=True)
class AskResponse:
    mode: RetrievalMode
    results: tuple[RetrievalResult, ...]
    providers: tuple[ProviderDiagnostic, ...]
    generation_status: GenerationStatus
    answer: GeneratedAnswer | None = None
    schema_version: int = field(init=False, default=ASK_SCHEMA_VERSION)

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RetrievalMode):
            raise ValueError("response mode must be RetrievalMode")
        results = tuple(self.results)
        providers = tuple(self.providers)
        if any(not isinstance(item, RetrievalResult) for item in results):
            raise ValueError("response results must contain RetrievalResult values")
        if any(not isinstance(item, ProviderDiagnostic) for item in providers):
            raise ValueError("response providers must contain ProviderDiagnostic values")
        if len({item.component for item in providers}) != len(providers):
            raise ValueError("response provider diagnostics must have unique components")
        if not isinstance(self.generation_status, GenerationStatus):
            raise ValueError("generation_status must be GenerationStatus")
        if self.answer is not None and not isinstance(self.answer, GeneratedAnswer):
            raise ValueError("response answer must be GeneratedAnswer or None")
        if (self.answer is None) != (self.generation_status is not GenerationStatus.SUCCEEDED):
            raise ValueError("a generated answer is required only for succeeded generation")
        object.__setattr__(self, "results", results)
        object.__setattr__(self, "providers", providers)

    @property
    def active_signals(self) -> tuple[RetrievalSignal, ...]:
        return self.mode.signals


def _catalog_object_id(bucket: str, key: str) -> str:
    encoded = json.dumps(
        ["catalog-object", bucket, key],
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _current_content_sha256(record: ObjectRecord) -> str | None:
    content = record.metadata.get("content_identity")
    if not isinstance(content, Mapping):
        return None
    digest = content.get("sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in _LOWERCASE_HEX for character in digest)
        or record.metadata.get("sha256") != digest
        or content.get("size") != record.size
    ):
        return None
    return digest


def _current_extraction(
    record: ObjectRecord,
) -> tuple[str, Mapping[str, object]] | None:
    if _current_content_sha256(record) is None:
        return None
    extraction = record.metadata.get("document_extraction")
    mime = record.metadata.get("mime")
    parser = extraction.get("parser") if isinstance(extraction, Mapping) else None
    text = extraction.get("text") if isinstance(extraction, Mapping) else None
    schema_version = (
        extraction.get("schema_version") if isinstance(extraction, Mapping) else None
    )
    normalization_version = (
        extraction.get("normalization_version")
        if isinstance(extraction, Mapping)
        else None
    )
    if (
        not isinstance(extraction, Mapping)
        or extraction.get("status") != "succeeded"
        or extraction.get("source_size") != record.size
        or not isinstance(mime, str)
        or extraction.get("source_mime") != mime
        or extraction.get("failure_code") is not None
        or isinstance(schema_version, bool)
        or schema_version != EXTRACTION_SCHEMA_VERSION
        or isinstance(normalization_version, bool)
        or normalization_version != NORMALIZATION_VERSION
        or not isinstance(parser, Mapping)
        or any(
            not isinstance(parser.get(name), str) or not parser.get(name)
            for name in ("name", "implementation_version", "runtime_version")
        )
        or not isinstance(text, str)
    ):
        return None
    try:
        text_bytes = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    stored_text_bytes = extraction.get("text_bytes")
    if isinstance(stored_text_bytes, bool) or stored_text_bytes != text_bytes:
        return None
    raw = extraction.get("document_metadata")
    if not isinstance(raw, Mapping):
        return None
    safe = {
        name: deepcopy(raw[name])
        for name in DOCUMENT_METADATA_FIELDS
        if name in raw and _is_json_scalar(raw[name]) and raw[name] is not None
    }
    return text, MappingProxyType(safe)


def _current_document_metadata(record: ObjectRecord) -> Mapping[str, object]:
    current = _current_extraction(record)
    return MappingProxyType({}) if current is None else current[1]


def _bounded_public_mapping(
    value: Mapping[str, object],
) -> tuple[Mapping[str, object], bool]:
    metadata = deepcopy(dict(value))
    try:
        encoded = json.dumps(
            metadata,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError):
        return MappingProxyType({}), True
    if len(encoded) > MAX_RESULT_METADATA_BYTES:
        return MappingProxyType({}), True
    return MappingProxyType(metadata), False


def _public_object_metadata(record: ObjectRecord) -> tuple[Mapping[str, object], bool]:
    metadata = {
        key: deepcopy(value)
        for key, value in record.metadata.items()
        if key not in _INTERNAL_METADATA_KEYS
    }
    return _bounded_public_mapping(metadata)


def _object_citation(record: ObjectRecord) -> ObjectCitation:
    object_metadata, truncated = _public_object_metadata(record)
    document_metadata, document_truncated = _bounded_public_mapping(
        _current_document_metadata(record)
    )
    mime_value = record.metadata.get("mime")
    mime = mime_value if isinstance(mime_value, str) and mime_value else None
    return ObjectCitation(
        object_id=_catalog_object_id(record.bucket, record.key),
        bucket=record.bucket,
        key=record.key,
        tier=record.tier,
        size=record.size,
        mime=mime,
        content_sha256=_current_content_sha256(record),
        object_metadata=object_metadata,
        document_metadata=document_metadata,
        object_metadata_truncated=truncated,
        document_metadata_truncated=document_truncated,
    )


def _matches_filters(record: ObjectRecord, filters: AskFilters) -> bool:
    mime_value = record.metadata.get("mime")
    mime = mime_value if isinstance(mime_value, str) and mime_value else None
    if filters.bucket is not None and record.bucket != filters.bucket:
        return False
    if filters.key_prefix and not record.key.startswith(filters.key_prefix):
        return False
    if filters.tier is not None and record.tier != filters.tier:
        return False
    if filters.mime is not None and mime != filters.mime:
        return False
    if filters.size is not None and record.size != filters.size:
        return False
    if (
        filters.content_sha256 is not None
        and _current_content_sha256(record) != filters.content_sha256
    ):
        return False
    if any(
        name not in record.metadata
        or not _json_scalar_equal(record.metadata[name], value)
        for name, value in filters.object_metadata.items()
    ):
        return False
    document_metadata = _current_document_metadata(record)
    return not any(
        name not in document_metadata
        or not _json_scalar_equal(document_metadata[name], value)
        for name, value in filters.document_metadata.items()
    )


def _scalar_text(value: object) -> str:
    if not _is_json_scalar(value):
        raise ValueError("metadata search values must be finite JSON scalars")
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _metadata_fields(record: ObjectRecord) -> Iterator[tuple[str, float]]:
    mime = record.metadata.get("mime")
    yield record.bucket, 1.0
    yield record.key, 4.0
    yield record.tier, 1.0
    if isinstance(mime, str):
        yield mime, 1.0
    for name, value in record.metadata.items():
        if name in _INTERNAL_METADATA_KEYS or not _is_json_scalar(value):
            continue
        yield f"{name} {_scalar_text(value)}", 1.0
    for name, value in _current_document_metadata(record).items():
        if _is_json_scalar(value):
            yield f"{name} {_scalar_text(value)}", 2.0 if name == "title" else 1.0


def _metadata_score(record: ObjectRecord, query: str) -> float:
    folded_query = query.casefold()
    query_tokens = frozenset(_TOKEN.findall(folded_query))
    if not query_tokens:
        return 0.0
    score = 0.0
    for raw_value, weight in _metadata_fields(record):
        value = raw_value[:MAX_FILTER_VALUE_BYTES].casefold()
        field_tokens = frozenset(_TOKEN.findall(value))
        overlap = len(query_tokens & field_tokens)
        if overlap:
            score += weight * (overlap / len(query_tokens))
        if folded_query in value:
            score += weight
    return score


@dataclass(frozen=True)
class _MetadataHeapEntry:
    hit: MetadataSearchHit

    def __lt__(self, other: _MetadataHeapEntry) -> bool:
        if self.hit.score != other.hit.score:
            return self.hit.score < other.hit.score
        return (self.hit.bucket, self.hit.key) > (other.hit.bucket, other.hit.key)


class CatalogMetadataRetriever:
    """Bounded-memory metadata ranking over a catalog object iterator."""

    def __init__(self, catalog: CatalogStore, *, batch_size: int = 1_000) -> None:
        self.catalog = catalog
        self.batch_size = _bounded_integer(
            batch_size,
            field_name="metadata batch_size",
            minimum=1,
            maximum=2**31 - 1,
        )

    def search(self, query: MetadataSearchQuery) -> list[MetadataSearchHit]:
        if not isinstance(query, MetadataSearchQuery):
            raise ValueError("query must be MetadataSearchQuery")
        records = self.catalog.iter_objects(batch_size=self.batch_size)
        heap: list[_MetadataHeapEntry] = []
        try:
            for record in records:
                if not _matches_filters(record, query.filters):
                    continue
                score = _metadata_score(record, query.text)
                if score <= 0:
                    continue
                entry = _MetadataHeapEntry(
                    MetadataSearchHit(record.bucket, record.key, score)
                )
                if len(heap) < query.limit:
                    heapq.heappush(heap, entry)
                elif heap[0] < entry:
                    heapq.heapreplace(heap, entry)
        finally:
            close = getattr(records, "close", None)
            if callable(close):
                close()
        return sorted(
            (entry.hit for entry in heap),
            key=lambda hit: (-hit.score, hit.bucket, hit.key),
        )


def _retrieval_mode(*, keyword: bool, vector: bool) -> RetrievalMode:
    if keyword and vector:
        return RetrievalMode.HYBRID
    if keyword:
        return RetrievalMode.METADATA_KEYWORD
    if vector:
        return RetrievalMode.METADATA_VECTOR
    return RetrievalMode.METADATA


def _signal_sort_key(signal: RetrievalSignal) -> int:
    return _SIGNAL_ORDER[signal.value]


def _keyword_passage(
    hit: KeywordSearchHit,
    *,
    record: ObjectRecord,
    rank: int,
    source_sha256: str,
    document_text_sha256: str,
) -> RetrievedPassage | None:
    object_id = _catalog_object_id(record.bucket, record.key)
    if hit.object_id != object_id:
        raise ValueError("keyword hit object_id does not match its catalog coordinate")
    if not hit.text:
        return None
    citation = PassageCitation(
        object_id=object_id,
        bucket=record.bucket,
        key=record.key,
        source=RetrievalSignal.KEYWORD,
        passage_id=hit.passage_id,
        passage_index=hit.passage_ordinal,
        start_codepoint=hit.char_start,
        end_codepoint=hit.char_end,
        text_sha256=hashlib.sha256(hit.text.encode("utf-8")).hexdigest(),
        source_sha256=source_sha256,
        document_text_sha256=document_text_sha256,
    )
    return RetrievedPassage(
        citation=citation,
        text=hit.text,
        match=PassageMatch(RetrievalSignal.KEYWORD, rank, float(hit.score)),
    )


def _vector_passage(
    hit: SimilaritySearchResult,
    *,
    record: ObjectRecord,
    rank: int,
) -> RetrievedPassage:
    object_id = _catalog_object_id(record.bucket, record.key)
    citation = PassageCitation(
        object_id=object_id,
        bucket=record.bucket,
        key=record.key,
        source=RetrievalSignal.VECTOR,
        passage_id=str(hit.passage_id),
        passage_index=hit.passage_index,
        start_codepoint=hit.start_codepoint,
        end_codepoint=hit.end_codepoint,
        text_sha256=hit.text_sha256,
        source_sha256=hit.source_sha256,
        document_text_sha256=hit.document_text_sha256,
        document_id=str(hit.document_id),
        space_id=str(hit.space_id),
    )
    return RetrievedPassage(
        citation=citation,
        text=hit.text,
        match=PassageMatch(RetrievalSignal.VECTOR, rank, float(hit.score)),
    )


def _select_passages(
    by_signal: Mapping[RetrievalSignal, Sequence[RetrievedPassage]],
    *,
    limit: int,
) -> tuple[RetrievedPassage, ...]:
    selected: list[RetrievedPassage] = []
    selected_ids: set[str] = set()
    sources = sorted(
        (signal for signal, passages in by_signal.items() if passages),
        key=_signal_sort_key,
    )
    first_pass = sorted(
        (by_signal[signal][0] for signal in sources),
        key=lambda passage: (
            passage.match.rank,
            _signal_sort_key(passage.match.signal),
            passage.citation.start_codepoint,
            passage.citation.passage_id,
        ),
    )
    remaining = sorted(
        (
            passage
            for signal in sources
            for passage in by_signal[signal][1:]
        ),
        key=lambda passage: (
            passage.match.rank,
            _signal_sort_key(passage.match.signal),
            passage.citation.start_codepoint,
            passage.citation.passage_id,
        ),
    )
    for passage in (*first_pass, *remaining):
        citation_id = passage.citation.citation_id
        if citation_id in selected_ids:
            continue
        selected.append(passage)
        selected_ids.add(citation_id)
        if len(selected) >= limit:
            break
    return tuple(selected)


class AskService:
    """Fuse catalog metadata and optional search providers into grounded results."""

    def __init__(
        self,
        catalog: CatalogStore,
        *,
        metadata: MetadataRetriever | None = None,
        keyword: KeywordRetriever | None = None,
        vector: VectorRetriever | None = None,
        answer_provider: AnswerProvider | None = None,
        fusion: FusionConfig | None = None,
    ) -> None:
        self.catalog = catalog
        self.metadata = (
            CatalogMetadataRetriever(catalog) if metadata is None else metadata
        )
        self.keyword = keyword
        self.vector = vector
        self.answer_provider = answer_provider
        self.fusion = FusionConfig() if fusion is None else fusion

    @staticmethod
    def _keyword_query(query: AskQuery) -> KeywordSearchQuery:
        filters = query.filters
        return KeywordSearchQuery(
            text=query.text,
            filters=KeywordSearchFilters(
                bucket=filters.bucket,
                tier=filters.tier,
                size=filters.size,
                mime=filters.mime,
                content_sha256=filters.content_sha256,
                document_metadata=filters.document_metadata,
            ),
            limit=query.candidate_limit,
        )

    @staticmethod
    def _vector_filters(filters: AskFilters) -> SimilaritySearchFilters:
        return SimilaritySearchFilters(
            buckets=frozenset() if filters.bucket is None else frozenset({filters.bucket}),
            key_prefix=filters.key_prefix,
            tiers=frozenset() if filters.tier is None else frozenset({filters.tier}),
            mime_types=frozenset() if filters.mime is None else frozenset({filters.mime}),
            metadata={
                name: value
                for name, value in filters.object_metadata.items()
                if isinstance(value, str) and "\0" not in value
            },
        )

    def _metadata_hits(self, query: AskQuery) -> list[MetadataSearchHit]:
        hits = self.metadata.search(
            MetadataSearchQuery(query.text, query.filters, query.candidate_limit)
        )
        if not isinstance(hits, list) or any(
            not isinstance(hit, MetadataSearchHit) for hit in hits
        ):
            raise TypeError("metadata retriever must return a list of MetadataSearchHit values")
        return sorted(
            hits[: query.candidate_limit],
            key=lambda hit: (-hit.score, hit.bucket, hit.key),
        )

    def _keyword_hits(
        self,
        query: AskQuery,
    ) -> tuple[list[KeywordSearchHit], ProviderDiagnostic]:
        if RetrievalSignal.KEYWORD not in query.retrieval_mode.signals:
            return [], ProviderDiagnostic("keyword", ProviderState.NOT_REQUESTED)
        if self.keyword is None:
            return [], ProviderDiagnostic("keyword", ProviderState.MISSING)
        try:
            hits = self.keyword.search(self._keyword_query(query))
        except RetrievalProviderUnavailableError as exc:
            return [], ProviderDiagnostic(
                "keyword",
                ProviderState.UNAVAILABLE,
                type(exc).__name__,
            )
        if not isinstance(hits, list) or any(not isinstance(hit, KeywordSearchHit) for hit in hits):
            raise TypeError("keyword retriever must return a list of KeywordSearchHit values")
        return hits[: query.candidate_limit], ProviderDiagnostic(
            "keyword",
            ProviderState.SUCCEEDED,
        )

    def _vector_hits(
        self,
        query: AskQuery,
    ) -> tuple[list[SimilaritySearchResult], ProviderDiagnostic]:
        if RetrievalSignal.VECTOR not in query.retrieval_mode.signals:
            return [], ProviderDiagnostic("vector", ProviderState.NOT_REQUESTED)
        if self.vector is None:
            return [], ProviderDiagnostic("vector", ProviderState.MISSING)
        try:
            hits = self.vector.search(
                query.text,
                filters=self._vector_filters(query.filters),
                limit=query.candidate_limit,
                exact=query.exact_vector,
            )
        except (
            RetrievalProviderUnavailableError,
            EmbeddingBackendUnsupportedError,
            EmbeddingProviderConfigurationError,
            TransientEmbeddingProviderError,
        ) as exc:
            return [], ProviderDiagnostic(
                "vector",
                ProviderState.UNAVAILABLE,
                type(exc).__name__,
            )
        if not isinstance(hits, list) or any(
            not isinstance(hit, SimilaritySearchResult) for hit in hits
        ):
            raise TypeError("vector retriever must return SimilaritySearchResult values")
        return hits[: query.candidate_limit], ProviderDiagnostic(
            "vector",
            ProviderState.SUCCEEDED,
        )

    @staticmethod
    def _records(
        catalog: CatalogStore,
        coordinates: set[tuple[str, str]],
        filters: AskFilters,
    ) -> dict[tuple[str, str], ObjectRecord]:
        records: dict[tuple[str, str], ObjectRecord] = {}
        for bucket, key in sorted(coordinates):
            record = catalog.get(bucket, key)
            if record is not None and _matches_filters(record, filters):
                records[(bucket, key)] = record
        return records

    @staticmethod
    def _valid_keyword_hit(
        hit: KeywordSearchHit,
        record: ObjectRecord,
        current: tuple[str, Mapping[str, object]] | None,
    ) -> bool:
        if hit.object_id != _catalog_object_id(record.bucket, record.key):
            raise ValueError("keyword hit object_id does not match its catalog coordinate")
        mime_value = record.metadata.get("mime")
        current_mime = mime_value if isinstance(mime_value, str) and mime_value else None
        if not all(
            (
                hit.size == record.size,
                hit.mime == current_mime,
                hit.content_sha256 == _current_content_sha256(record),
                _json_scalar_mapping_equal(
                    hit.document_metadata,
                    {} if current is None else current[1],
                ),
            )
        ):
            return False
        if not hit.text:
            if hit.char_start != 0 or hit.char_end != 0:
                raise ValueError("empty keyword passages must use the 0..0 metadata span")
            return True
        if current is None:
            return False
        normalized_text = current[0]
        return (
            0 <= hit.char_start < hit.char_end <= len(normalized_text)
            and normalized_text[hit.char_start : hit.char_end] == hit.text
        )

    def _results(
        self,
        query: AskQuery,
        metadata_hits: Sequence[MetadataSearchHit],
        keyword_hits: Sequence[KeywordSearchHit],
        vector_hits: Sequence[SimilaritySearchResult],
    ) -> tuple[RetrievalResult, ...]:
        coordinates = {
            (hit.bucket, hit.key)
            for hit in metadata_hits
        }
        coordinates.update((hit.bucket, hit.key) for hit in keyword_hits)
        coordinates.update((hit.bucket, hit.key) for hit in vector_hits)
        records = self._records(self.catalog, coordinates, query.filters)
        current_extractions = {
            coordinate: _current_extraction(record)
            for coordinate, record in records.items()
        }
        current_text_sha256 = {
            coordinate: (
                None
                if current is None
                else hashlib.sha256(current[0].encode("utf-8")).hexdigest()
            )
            for coordinate, current in current_extractions.items()
        }

        metadata_by_object: dict[tuple[str, str], MetadataSearchHit] = {}
        for metadata_hit_item in metadata_hits:
            coordinate = (metadata_hit_item.bucket, metadata_hit_item.key)
            record = records.get(coordinate)
            if record is None or coordinate in metadata_by_object:
                continue
            # The retriever owns metadata ranking, but the authoritative
            # snapshot must still contain a current query match. This removes
            # a stale hit whose searchable metadata changed after retrieval
            # without replacing backend scores with a second ranking scheme.
            if _metadata_score(record, query.text) <= 0:
                continue
            metadata_by_object[coordinate] = metadata_hit_item

        keyword_by_object: dict[
            tuple[str, str],
            list[tuple[int, KeywordSearchHit]],
        ] = {}
        retained_keyword_rank = 0
        for keyword_hit_item in keyword_hits:
            coordinate = (keyword_hit_item.bucket, keyword_hit_item.key)
            record = records.get(coordinate)
            if record is None or not self._valid_keyword_hit(
                keyword_hit_item,
                record,
                current_extractions[coordinate],
            ):
                continue
            retained_keyword_rank += 1
            keyword_by_object.setdefault(coordinate, []).append(
                (retained_keyword_rank, keyword_hit_item)
            )

        vector_by_object: dict[
            tuple[str, str],
            list[tuple[int, SimilaritySearchResult]],
        ] = {}
        retained_vector_rank = 0
        for vector_hit_item in vector_hits:
            coordinate = (vector_hit_item.bucket, vector_hit_item.key)
            record = records.get(coordinate)
            if record is None:
                continue
            mime_value = record.metadata.get("mime")
            current_mime = (
                mime_value if isinstance(mime_value, str) and mime_value else None
            )
            if vector_hit_item.mime != current_mime:
                continue
            current = current_extractions[coordinate]
            if current is None:
                continue
            normalized_text = current[0]
            document_text_sha256 = current_text_sha256[coordinate]
            assert document_text_sha256 is not None
            if (
                vector_hit_item.source_sha256 != _current_content_sha256(record)
                or vector_hit_item.document_text_sha256
                != document_text_sha256
                or not (
                    0
                    <= vector_hit_item.start_codepoint
                    < vector_hit_item.end_codepoint
                    <= len(normalized_text)
                )
                or normalized_text[
                    vector_hit_item.start_codepoint : vector_hit_item.end_codepoint
                ]
                != vector_hit_item.text
            ):
                continue
            retained_vector_rank += 1
            vector_by_object.setdefault(coordinate, []).append(
                (retained_vector_rank, vector_hit_item)
            )

        metadata_object_ranks = {
            coordinate: rank
            for rank, coordinate in enumerate(metadata_by_object, start=1)
        }
        keyword_object_ranks = {
            coordinate: rank
            for rank, coordinate in enumerate(keyword_by_object, start=1)
        }
        vector_object_ranks = {
            coordinate: rank
            for rank, coordinate in enumerate(vector_by_object, start=1)
        }

        fused: list[RetrievalResult] = []
        active_coordinates = (
            set(metadata_by_object) | set(keyword_by_object) | set(vector_by_object)
        )
        for coordinate in active_coordinates:
            record = records[coordinate]
            components: list[ScoreComponent] = []
            passages_by_signal: dict[RetrievalSignal, list[RetrievedPassage]] = {}

            metadata_hit = metadata_by_object.get(coordinate)
            if metadata_hit is not None:
                rank = metadata_object_ranks[coordinate]
                signal = RetrievalSignal.METADATA
                components.append(
                    ScoreComponent(
                        signal,
                        rank,
                        float(metadata_hit.score),
                        self.fusion.weight(signal),
                        self.fusion.contribution(signal, rank),
                    )
                )

            object_keyword_hits = keyword_by_object.get(coordinate, [])
            if object_keyword_hits:
                signal = RetrievalSignal.KEYWORD
                object_rank = keyword_object_ranks[coordinate]
                best_keyword_hit = object_keyword_hits[0][1]
                components.append(
                    ScoreComponent(
                        signal,
                        object_rank,
                        float(best_keyword_hit.score),
                        self.fusion.weight(signal),
                        self.fusion.contribution(signal, object_rank),
                    )
                )
                passages: list[RetrievedPassage] = []
                source_sha256 = _current_content_sha256(record)
                document_text_sha256 = current_text_sha256[coordinate]
                for passage_rank, hit in object_keyword_hits:
                    if not hit.text:
                        continue
                    if source_sha256 is None or document_text_sha256 is None:
                        raise RuntimeError(
                            "validated keyword passage lost its current catalog source"
                        )
                    passage = _keyword_passage(
                        hit,
                        record=record,
                        rank=passage_rank,
                        source_sha256=source_sha256,
                        document_text_sha256=document_text_sha256,
                    )
                    assert passage is not None
                    passages.append(passage)
                if passages:
                    passages_by_signal[signal] = passages

            object_vector_hits = vector_by_object.get(coordinate, [])
            if object_vector_hits:
                signal = RetrievalSignal.VECTOR
                object_rank = vector_object_ranks[coordinate]
                best_vector_hit = object_vector_hits[0][1]
                components.append(
                    ScoreComponent(
                        signal,
                        object_rank,
                        float(best_vector_hit.score),
                        self.fusion.weight(signal),
                        self.fusion.contribution(signal, object_rank),
                    )
                )
                passages_by_signal[signal] = [
                    _vector_passage(hit, record=record, rank=passage_rank)
                    for passage_rank, hit in object_vector_hits
                ]

            components.sort(key=lambda item: _signal_sort_key(item.signal))
            score = sum(component.contribution for component in components)
            fused.append(
                RetrievalResult(
                    citation=_object_citation(record),
                    score=score,
                    score_components=tuple(components),
                    passages=_select_passages(
                        passages_by_signal,
                        limit=query.passages_per_result,
                    ),
                )
            )

        fused.sort(
            key=lambda result: (
                -result.score,
                result.citation.bucket,
                result.citation.key,
            )
        )
        return tuple(fused[: query.limit])

    def _generate(
        self,
        query: AskQuery,
        *,
        mode: RetrievalMode,
        results: tuple[RetrievalResult, ...],
    ) -> tuple[GeneratedAnswer | None, GenerationStatus, ProviderDiagnostic]:
        if not query.synthesize:
            return (
                None,
                GenerationStatus.NOT_REQUESTED,
                ProviderDiagnostic("generation", ProviderState.NOT_REQUESTED),
            )
        if self.answer_provider is None:
            return (
                None,
                GenerationStatus.PROVIDER_MISSING,
                ProviderDiagnostic("generation", ProviderState.MISSING),
            )
        if not results:
            return (
                None,
                GenerationStatus.NO_CONTEXT,
                ProviderDiagnostic("generation", ProviderState.NO_CONTEXT),
            )
        request = AnswerGenerationRequest(query.text, mode, results)
        try:
            answer = self.answer_provider.generate(request)
        except AnswerProviderUnavailableError as exc:
            return (
                None,
                GenerationStatus.PROVIDER_UNAVAILABLE,
                ProviderDiagnostic(
                    "generation",
                    ProviderState.UNAVAILABLE,
                    type(exc).__name__,
                ),
            )
        if not isinstance(answer, GeneratedAnswer):
            raise TypeError("answer provider must return GeneratedAnswer")
        allowed_citations = set(request.citation_ids)
        unknown = sorted(set(answer.citations) - allowed_citations)
        if unknown:
            raise AnswerCitationError(
                "answer provider returned unknown citations: " + ", ".join(unknown)
            )
        return (
            answer,
            GenerationStatus.SUCCEEDED,
            ProviderDiagnostic("generation", ProviderState.SUCCEEDED),
        )

    def ask(self, query: AskQuery) -> AskResponse:
        if not isinstance(query, AskQuery):
            raise ValueError("query must be AskQuery")
        metadata_hits = self._metadata_hits(query)
        keyword_hits, keyword_diagnostic = self._keyword_hits(query)
        vector_hits, vector_diagnostic = self._vector_hits(query)
        mode = _retrieval_mode(
            keyword=keyword_diagnostic.state is ProviderState.SUCCEEDED,
            vector=vector_diagnostic.state is ProviderState.SUCCEEDED,
        )
        results = self._results(query, metadata_hits, keyword_hits, vector_hits)
        answer, generation_status, generation_diagnostic = self._generate(
            query,
            mode=mode,
            results=results,
        )
        return AskResponse(
            mode=mode,
            results=results,
            providers=(
                ProviderDiagnostic("metadata", ProviderState.SUCCEEDED),
                keyword_diagnostic,
                vector_diagnostic,
                generation_diagnostic,
            ),
            generation_status=generation_status,
            answer=answer,
        )


__all__ = [
    "ASK_SCHEMA_VERSION",
    "AnswerCitationError",
    "AnswerGenerationRequest",
    "AnswerProvider",
    "AnswerProviderUnavailableError",
    "AskFilters",
    "AskQuery",
    "AskResponse",
    "AskService",
    "CatalogMetadataRetriever",
    "FusionConfig",
    "GeneratedAnswer",
    "GenerationStatus",
    "KeywordRetriever",
    "MetadataRetriever",
    "MetadataSearchHit",
    "MetadataSearchQuery",
    "ObjectCitation",
    "PassageCitation",
    "PassageMatch",
    "ProviderDiagnostic",
    "ProviderState",
    "RetrievalMode",
    "RetrievalProviderUnavailableError",
    "RetrievalResult",
    "RetrievalSignal",
    "RetrievedPassage",
    "ScoreComponent",
    "VectorRetriever",
]
