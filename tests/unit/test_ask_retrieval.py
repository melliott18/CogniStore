from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from cognistore.core.catalog import Catalog, ObjectRecord
from cognistore.core.embedding_index import (
    EmbeddingBackendUnsupportedError,
    SimilaritySearchFilters,
    SimilaritySearchResult,
)
from cognistore.core.embeddings import (
    EmbeddingProviderConfigurationError,
    TransientEmbeddingProviderError,
)
from cognistore.search.ask import (
    AnswerCitationError,
    AnswerGenerationRequest,
    AnswerProviderUnavailableError,
    AskFilters,
    AskQuery,
    AskService,
    CatalogMetadataRetriever,
    FusionConfig,
    GeneratedAnswer,
    GenerationStatus,
    MetadataSearchHit,
    MetadataSearchQuery,
    ProviderDiagnostic,
    ProviderState,
    RetrievalMode,
    RetrievalProviderUnavailableError,
    RetrievalSignal,
)
from cognistore.search.keyword import KeywordSearchHit, KeywordSearchQuery

_FIXTURE_DIRECTORY = Path(__file__).parents[1] / "fixtures" / "ask"
_GOLDEN_PATH = _FIXTURE_DIRECTORY / "golden.json"
_CHECKSUM_PATH = _FIXTURE_DIRECTORY / "SHA256SUMS"


def _object_id(bucket: str, key: str) -> str:
    payload = json.dumps(
        ["catalog-object", bucket, key],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record(
    key: str,
    *,
    bucket: str = "documents",
    tier: str = "warm",
    size: int = 128,
    mime: str = "application/pdf",
    digest: str = "a" * 64,
    object_metadata: Mapping[str, object] | None = None,
    document_metadata: Mapping[str, object] | None = None,
    text: str | None = None,
) -> ObjectRecord:
    normalized_text = text or f"Normalized source text for {key}."
    metadata: dict[str, object] = dict(object_metadata or {})
    metadata.update(
        {
            "mime": mime,
            "sha256": digest,
            "content_identity": {
                "sha256": digest,
                "size": size,
            },
            "document_extraction": {
                "schema_version": 1,
                "status": "succeeded",
                "source_mime": mime,
                "source_size": size,
                "parser": {
                    "name": "cognistore-ask-fixture",
                    "implementation_version": "1",
                    "runtime_version": "1",
                },
                "normalization_version": 1,
                "text": normalized_text,
                "text_bytes": len(normalized_text.encode("utf-8")),
                "document_metadata": dict(document_metadata or {}),
                "failure_code": None,
            },
        }
    )
    return ObjectRecord(bucket=bucket, key=key, size=size, tier=tier, metadata=metadata)


class _ReadCatalog(Catalog):
    """Minimal detached-snapshot catalog preserving fixture content identity."""

    def __init__(self, records: Sequence[ObjectRecord]) -> None:
        super().__init__()
        self._ask_records = {
            (record.bucket, record.key): deepcopy(record) for record in records
        }

    def get(self, bucket: str, key: str) -> ObjectRecord | None:
        record = self._ask_records.get((bucket, key))
        return None if record is None else deepcopy(record)

    def iter_objects(self, *, batch_size: int = 1000) -> Iterator[ObjectRecord]:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        for coordinate in sorted(self._ask_records):
            yield deepcopy(self._ask_records[coordinate])


def _catalog(*records: ObjectRecord) -> _ReadCatalog:
    return _ReadCatalog(records)


def _digest(record: ObjectRecord) -> str:
    value = record.metadata["sha256"]
    assert isinstance(value, str)
    return value


def _document_text(record: ObjectRecord) -> str:
    extraction = record.metadata["document_extraction"]
    assert isinstance(extraction, dict)
    text = extraction["text"]
    assert isinstance(text, str)
    return text


def _document_metadata(record: ObjectRecord) -> dict[str, object]:
    extraction = record.metadata["document_extraction"]
    assert isinstance(extraction, dict)
    metadata = extraction["document_metadata"]
    assert isinstance(metadata, dict)
    return dict(metadata)


def _keyword_hit(
    record: ObjectRecord,
    *,
    passage_id: str,
    text: str | None = None,
    score: float = 1.0,
    passage_index: int = 0,
    start_codepoint: int = 0,
) -> KeywordSearchHit:
    passage_text = _document_text(record) if text is None else text
    return KeywordSearchHit(
        score=score,
        object_id=_object_id(record.bucket, record.key),
        passage_id=passage_id,
        bucket=record.bucket,
        key=record.key,
        tier=record.tier,
        size=record.size,
        mime=str(record.metadata["mime"]),
        content_sha256=_digest(record),
        passage_ordinal=passage_index,
        char_start=start_codepoint,
        char_end=start_codepoint + len(passage_text),
        text=passage_text,
        document_metadata=_document_metadata(record),
    )


def _vector_hit(
    record: ObjectRecord,
    *,
    passage_id: str,
    text: str | None = None,
    cosine_distance: float = 0.1,
    passage_index: int = 0,
    start_codepoint: int = 0,
    document_id: str = "10000000-0000-0000-0000-000000000001",
    space_id: str = "20000000-0000-0000-0000-000000000001",
) -> SimilaritySearchResult:
    passage_text = _document_text(record) if text is None else text
    return SimilaritySearchResult(
        space_id=UUID(space_id),
        passage_id=UUID(passage_id),
        document_id=UUID(document_id),
        source_sha256=_digest(record),
        document_text_sha256=_sha256(_document_text(record)),
        bucket=record.bucket,
        key=record.key,
        tier=record.tier,
        mime=str(record.metadata["mime"]),
        passage_index=passage_index,
        start_codepoint=start_codepoint,
        end_codepoint=start_codepoint + len(passage_text),
        text=passage_text,
        text_sha256=_sha256(passage_text),
        cosine_distance=cosine_distance,
        object_metadata={},
    )


class _MetadataRetriever:
    def __init__(self, hits: Sequence[MetadataSearchHit]) -> None:
        self.hits = list(hits)
        self.calls: list[MetadataSearchQuery] = []

    def search(self, query: MetadataSearchQuery) -> list[MetadataSearchHit]:
        self.calls.append(query)
        return list(self.hits)


class _KeywordRetriever:
    def __init__(
        self,
        hits: Sequence[KeywordSearchHit] = (),
        *,
        error: Exception | None = None,
    ) -> None:
        self.hits = list(hits)
        self.error = error
        self.calls: list[KeywordSearchQuery] = []

    def search(self, query: KeywordSearchQuery) -> list[KeywordSearchHit]:
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        return list(self.hits)


class _VectorRetriever:
    def __init__(
        self,
        hits: Sequence[SimilaritySearchResult] = (),
        *,
        error: Exception | None = None,
    ) -> None:
        self.hits = list(hits)
        self.error = error
        self.calls: list[tuple[str, SimilaritySearchFilters | None, int, bool]] = []

    def search(
        self,
        query: str,
        *,
        filters: SimilaritySearchFilters | None = None,
        limit: int = 10,
        exact: bool = False,
    ) -> list[SimilaritySearchResult]:
        self.calls.append((query, filters, limit, exact))
        if self.error is not None:
            raise self.error
        return list(self.hits)


class _AnswerProvider:
    def __init__(
        self,
        generate: Callable[[AnswerGenerationRequest], GeneratedAnswer],
    ) -> None:
        self._generate = generate
        self.calls: list[AnswerGenerationRequest] = []

    def generate(self, request: AnswerGenerationRequest) -> GeneratedAnswer:
        self.calls.append(request)
        return self._generate(request)


def _diagnostics(response_providers: Sequence[ProviderDiagnostic]) -> dict[str, ProviderDiagnostic]:
    return {diagnostic.component: diagnostic for diagnostic in response_providers}


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: AskQuery(""), "non-empty"),
        (lambda: AskQuery(" padded "), "outer whitespace"),
        (lambda: AskQuery("nul\0query"), "NUL"),
        (lambda: AskQuery("q" * (16 * 1_024 + 1)), "query text must be at most"),
        (lambda: AskQuery("query", limit=0), "limit"),
        (lambda: AskQuery("query", limit=2, candidate_limit=1), "candidate_limit"),
        (lambda: AskQuery("query", passages_per_result=0), "passages_per_result"),
        (lambda: AskQuery("query", synthesize=cast(Any, 1)), "synthesize"),
        (lambda: AskQuery("query", exact_vector=cast(Any, 1)), "exact_vector"),
        (lambda: AskQuery("query", filters=cast(Any, {})), "filters"),
        (lambda: AskFilters(bucket=""), "bucket"),
        (lambda: AskFilters(bucket="b" * (4 * 1_024 + 1)), "bucket must be at most"),
        (
            lambda: AskFilters(key_prefix="k" * (4 * 1_024 + 1)),
            "key_prefix must be at most",
        ),
        (lambda: AskFilters(size=-1), "size"),
        (lambda: AskFilters(content_sha256="not-a-digest"), "content_sha256"),
        (
            lambda: AskFilters(
                object_metadata=cast(Any, {"nested": {"no": "objects"}})
            ),
            "JSON scalars",
        ),
        (
            lambda: AskFilters(document_metadata={"subject": None}),
            "must not be null",
        ),
        (lambda: AskFilters(document_metadata={"department": "legal"}), "unsupported"),
        (lambda: FusionConfig(rank_constant=float("nan")), "finite"),
        (lambda: FusionConfig(vector_weight=float("inf")), "finite"),
    ],
)
def test_ask_query_and_filters_reject_invalid_or_ambiguous_inputs(
    factory: Callable[[], object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_native_filter_translation_preserves_supported_provider_filters() -> None:
    metadata = _MetadataRetriever([])
    keyword = _KeywordRetriever()
    vector = _VectorRetriever()
    filters = AskFilters(
        bucket="documents",
        key_prefix="policies/",
        tier="warm",
        mime="application/pdf",
        size=128,
        content_sha256="a" * 64,
        object_metadata={"department": "legal", "retention_years": 7},
        document_metadata={"language": "en"},
    )

    response = AskService(
        _catalog(),
        metadata=metadata,
        keyword=keyword,
        vector=vector,
    ).ask(
        AskQuery(
            "legal hold",
            filters=filters,
            limit=4,
            candidate_limit=9,
            exact_vector=True,
        )
    )

    assert response.mode is RetrievalMode.HYBRID
    assert metadata.calls == [MetadataSearchQuery("legal hold", filters, 9)]
    assert len(keyword.calls) == 1
    keyword_query = keyword.calls[0]
    assert keyword_query.text == "legal hold"
    assert keyword_query.limit == 9
    assert keyword_query.offset == 0
    assert keyword_query.filters.bucket == "documents"
    assert keyword_query.filters.tier == "warm"
    assert keyword_query.filters.size == 128
    assert keyword_query.filters.mime == "application/pdf"
    assert keyword_query.filters.content_sha256 == "a" * 64
    assert keyword_query.filters.document_metadata == {"language": "en"}

    assert len(vector.calls) == 1
    vector_text, vector_filters, vector_limit, exact = vector.calls[0]
    assert vector_text == "legal hold"
    assert vector_limit == 9
    assert exact is True
    assert vector_filters == SimilaritySearchFilters(
        buckets=frozenset({"documents"}),
        key_prefix="policies/",
        tiers=frozenset({"warm"}),
        mime_types=frozenset({"application/pdf"}),
        metadata={"department": "legal"},
    )


def test_null_object_metadata_filter_requires_the_key_to_be_present() -> None:
    missing = _record(
        "missing.pdf",
        object_metadata={"tag": "needle"},
        digest="1" * 64,
    )
    explicit_null = _record(
        "null.pdf",
        object_metadata={"tag": "needle", "nullable": None},
        digest="2" * 64,
    )
    retriever = CatalogMetadataRetriever(_catalog(missing, explicit_null), batch_size=1)

    hits = retriever.search(
        MetadataSearchQuery(
            "needle",
            AskFilters(object_metadata={"nullable": None}),
            10,
        )
    )

    assert [(hit.bucket, hit.key) for hit in hits] == [("documents", "null.pdf")]


def test_exact_metadata_filters_do_not_conflate_booleans_and_integers() -> None:
    integer = _record(
        "integer.pdf",
        object_metadata={"tag": "needle", "flag": 1},
        document_metadata={"title": "needle", "page_count": 1},
        digest="1" * 64,
    )
    boolean = _record(
        "boolean.pdf",
        object_metadata={"tag": "needle", "flag": True},
        document_metadata={"title": "needle", "page_count": True},
        digest="2" * 64,
    )
    retriever = CatalogMetadataRetriever(_catalog(integer, boolean), batch_size=1)

    object_hits = retriever.search(
        MetadataSearchQuery(
            "needle",
            AskFilters(object_metadata={"flag": True}),
            10,
        )
    )
    document_hits = retriever.search(
        MetadataSearchQuery(
            "needle",
            AskFilters(document_metadata={"page_count": True}),
            10,
        )
    )

    assert [(hit.bucket, hit.key) for hit in object_hits] == [
        ("documents", "boolean.pdf")
    ]
    assert [(hit.bucket, hit.key) for hit in document_hits] == [
        ("documents", "boolean.pdf")
    ]


def test_catalog_metadata_retriever_is_bounded_and_deterministic_across_ties() -> None:
    records = (
        _record("z.pdf", digest="1" * 64, object_metadata={"tag": "needle"}),
        _record("a.pdf", digest="2" * 64, object_metadata={"tag": "needle"}),
        _record("m.pdf", digest="3" * 64, object_metadata={"tag": "needle"}),
    )
    retriever = CatalogMetadataRetriever(_catalog(*records), batch_size=1)
    query = MetadataSearchQuery("needle", AskFilters(), 2)

    first = retriever.search(query)
    second = retriever.search(query)

    assert first == second
    assert [(hit.bucket, hit.key) for hit in first] == [
        ("documents", "a.pdf"),
        ("documents", "m.pdf"),
    ]
    assert [hit.score for hit in first] == pytest.approx([2.0, 2.0])


def test_object_level_rrf_uses_one_rank_per_signal_despite_duplicate_passages() -> None:
    first = _record(
        "a.pdf",
        digest="1" * 64,
        object_metadata={"tag": "evidence"},
        document_metadata={"language": "en"},
    )
    second = _record(
        "b.pdf",
        digest="2" * 64,
        object_metadata={"tag": "evidence"},
        document_metadata={"language": "en"},
    )
    keyword = _KeywordRetriever(
        [
            _keyword_hit(first, passage_id="1" * 64, score=10.0),
            _keyword_hit(first, passage_id="2" * 64, score=9.0, passage_index=1),
            _keyword_hit(second, passage_id="3" * 64, score=8.0),
        ]
    )
    vector = _VectorRetriever(
        [
            _vector_hit(
                second,
                passage_id="00000000-0000-0000-0000-000000000301",
                cosine_distance=0.01,
            ),
            _vector_hit(
                first,
                passage_id="00000000-0000-0000-0000-000000000302",
                cosine_distance=0.02,
            ),
        ]
    )
    fusion = FusionConfig(rank_constant=10.0)

    response = AskService(
        _catalog(first, second),
        metadata=_MetadataRetriever(
            [
                MetadataSearchHit(first.bucket, first.key, 2.0),
                MetadataSearchHit(second.bucket, second.key, 1.0),
            ]
        ),
        keyword=keyword,
        vector=vector,
        fusion=fusion,
    ).ask(AskQuery("evidence", limit=2, candidate_limit=5, passages_per_result=3))

    assert [result.citation.key for result in response.results] == ["a.pdf", "b.pdf"]
    first_result, second_result = response.results
    assert {
        component.signal: component.rank for component in first_result.score_components
    } == {
        RetrievalSignal.METADATA: 1,
        RetrievalSignal.KEYWORD: 1,
        RetrievalSignal.VECTOR: 2,
    }
    assert first_result.score == pytest.approx(2.0 / 11.0 + 1.0 / 12.0)
    assert len(first_result.passages) == 3
    assert {
        component.signal: component.rank for component in second_result.score_components
    } == {
        RetrievalSignal.METADATA: 2,
        RetrievalSignal.KEYWORD: 2,
        RetrievalSignal.VECTOR: 1,
    }
    assert second_result.score == pytest.approx(2.0 / 12.0 + 1.0 / 11.0)


def test_keyword_and_vector_chunk_identities_remain_source_qualified() -> None:
    record = _record(
        "source.pdf",
        digest="4" * 64,
        document_metadata={"language": "en"},
        text="keyword layout | vector layout is different",
    )
    keyword_id = "a" * 64
    vector_id = "00000000-0000-0000-0000-000000000201"
    response = AskService(
        _catalog(record),
        metadata=_MetadataRetriever([]),
        keyword=_KeywordRetriever(
            [_keyword_hit(record, passage_id=keyword_id, text="keyword layout")]
        ),
        vector=_VectorRetriever(
            [
                _vector_hit(
                    record,
                    passage_id=vector_id,
                    text="vector layout is different",
                    start_codepoint=len("keyword layout | "),
                )
            ]
        ),
    ).ask(AskQuery("layout", limit=1, candidate_limit=5, passages_per_result=2))

    result = response.results[0]
    assert [(passage.citation.source, passage.citation.passage_id) for passage in result.passages] == [
        (RetrievalSignal.KEYWORD, keyword_id),
        (RetrievalSignal.VECTOR, vector_id),
    ]
    assert [passage.citation.citation_id for passage in result.passages] == [
        f"passage:{result.citation.object_id}:keyword:{keyword_id}",
        f"passage:{result.citation.object_id}:vector:{vector_id}",
    ]
    assert result.passages[0].citation.document_id is None
    assert result.passages[0].citation.space_id is None
    assert result.passages[1].citation.document_id is not None
    assert result.passages[1].citation.space_id is not None
    assert {passage.citation.source_sha256 for passage in result.passages} == {
        _digest(record)
    }
    assert {passage.citation.document_text_sha256 for passage in result.passages} == {
        _sha256(_document_text(record))
    }
    with pytest.raises(ValueError, match="object_id does not match"):
        replace(result.passages[0].citation, key="different.pdf")

    other_key = "other.pdf"
    other_citation = replace(
        result.passages[0].citation,
        object_id=_object_id(result.citation.bucket, other_key),
        key=other_key,
    )
    other_passage = replace(result.passages[0], citation=other_citation)
    with pytest.raises(ValueError, match="every passage must cite the result object"):
        replace(result, passages=(other_passage,))


def test_every_candidate_is_post_filtered_against_current_catalog_state() -> None:
    common: dict[str, str | int] = {"department": "legal", "retention_years": 7}
    good = _record(
        "allowed/good.pdf",
        digest="1" * 64,
        object_metadata=common,
        document_metadata={"language": "en"},
    )
    wrong_prefix = _record(
        "outside/prefix.pdf",
        digest="2" * 64,
        object_metadata=common,
        document_metadata={"language": "en"},
    )
    wrong_metadata = _record(
        "allowed/wrong-metadata.pdf",
        digest="3" * 64,
        object_metadata={"department": "finance", "retention_years": 7},
        document_metadata={"language": "en"},
    )
    wrong_document = _record(
        "allowed/wrong-language.pdf",
        digest="4" * 64,
        object_metadata=common,
        document_metadata={"language": "fr"},
    )
    records = (wrong_prefix, wrong_metadata, wrong_document, good)
    filters = AskFilters(
        key_prefix="allowed/",
        object_metadata=common,
        document_metadata={"language": "en"},
    )

    response = AskService(
        _catalog(*records),
        metadata=_MetadataRetriever(
            [MetadataSearchHit(record.bucket, record.key, 4.0) for record in records]
        ),
        keyword=_KeywordRetriever(
            [
                _keyword_hit(record, passage_id=f"{index:x}" * 64)
                for index, record in enumerate(records, start=1)
            ]
        ),
        vector=_VectorRetriever(
            [
                _vector_hit(
                    record,
                    passage_id=f"00000000-0000-0000-0000-{index:012d}",
                )
                for index, record in enumerate(records, start=1)
            ]
        ),
    ).ask(AskQuery("evidence", filters=filters, limit=4, candidate_limit=10))

    assert response.mode is RetrievalMode.HYBRID
    assert [result.citation.key for result in response.results] == ["allowed/good.pdf"]


def test_deleted_stale_and_digest_mismatched_keyword_hits_are_dropped() -> None:
    valid = _record("valid.pdf", digest="1" * 64, document_metadata={"language": "en"})
    deleted = _record("deleted.pdf", digest="2" * 64, document_metadata={"language": "en"})
    digest_changed = _record(
        "digest-changed.pdf",
        digest="3" * 64,
        document_metadata={"language": "en"},
    )
    stale = _record("stale.pdf", digest="4" * 64, document_metadata={"language": "en"})
    stale_hit = replace(_keyword_hit(stale, passage_id="4" * 64), size=stale.size + 1)
    digest_hit = replace(
        _keyword_hit(digest_changed, passage_id="3" * 64),
        content_sha256="f" * 64,
    )
    keyword = _KeywordRetriever(
        [
            stale_hit,
            _keyword_hit(deleted, passage_id="2" * 64),
            digest_hit,
            _keyword_hit(valid, passage_id="1" * 64),
        ]
    )

    response = AskService(
        _catalog(valid, digest_changed, stale),
        metadata=_MetadataRetriever([]),
        keyword=keyword,
    ).ask(AskQuery("evidence", limit=4, candidate_limit=10))

    assert [result.citation.key for result in response.results] == ["valid.pdf"]
    assert response.results[0].score_components[0].rank == 1


def test_stale_vector_source_text_digest_and_span_hits_are_dropped() -> None:
    valid = _record("valid.pdf", digest="1" * 64, text="prefix evidence")
    source_changed = _record("source-changed.pdf", digest="2" * 64, text="prefix evidence")
    text_changed = _record("text-changed.pdf", digest="3" * 64, text="prefix evidence")
    stale_span = _record("stale-span.pdf", digest="4" * 64, text="prefix evidence")
    valid_hit = _vector_hit(
        valid,
        passage_id="00000000-0000-0000-0000-000000000401",
        text="evidence",
        start_codepoint=7,
    )
    source_hit = replace(
        _vector_hit(
            source_changed,
            passage_id="00000000-0000-0000-0000-000000000402",
            text="evidence",
            start_codepoint=7,
        ),
        source_sha256="f" * 64,
    )
    text_hit = replace(
        _vector_hit(
            text_changed,
            passage_id="00000000-0000-0000-0000-000000000403",
            text="evidence",
            start_codepoint=7,
        ),
        document_text_sha256="f" * 64,
    )
    span_hit = replace(
        _vector_hit(
            stale_span,
            passage_id="00000000-0000-0000-0000-000000000404",
            text="evidence",
            start_codepoint=7,
        ),
        start_codepoint=6,
        end_codepoint=14,
    )

    response = AskService(
        _catalog(valid, source_changed, text_changed, stale_span),
        metadata=_MetadataRetriever([]),
        vector=_VectorRetriever([source_hit, text_hit, span_hit, valid_hit]),
    ).ask(AskQuery("evidence", limit=4, candidate_limit=10))

    assert [result.citation.key for result in response.results] == ["valid.pdf"]
    assert response.results[0].score_components[0].rank == 1


def test_incompatible_current_extraction_cannot_ground_any_retrieval_lane() -> None:
    record = _record(
        "invalid-extraction.pdf",
        digest="5" * 64,
        object_metadata={"tag": "evidence"},
        document_metadata={"language": "en"},
        text="current evidence",
    )
    keyword_hit = _keyword_hit(record, passage_id="5" * 64)
    vector_hit = _vector_hit(
        record,
        passage_id="00000000-0000-0000-0000-000000000405",
    )
    extraction = record.metadata["document_extraction"]
    assert isinstance(extraction, dict)
    extraction["normalization_version"] = 999

    response = AskService(
        _catalog(record),
        metadata=_MetadataRetriever([MetadataSearchHit(record.bucket, record.key, 1.0)]),
        keyword=_KeywordRetriever([keyword_hit]),
        vector=_VectorRetriever([vector_hit]),
    ).ask(
        AskQuery(
            "evidence",
            filters=AskFilters(document_metadata={"language": "en"}),
        )
    )

    assert response.results == ()


def test_falsey_injected_retriever_and_fusion_config_are_preserved() -> None:
    class FalseyMetadataRetriever(_MetadataRetriever):
        def __bool__(self) -> bool:
            return False

    class FalseyFusionConfig(FusionConfig):
        def __bool__(self) -> bool:
            return False

    record = _record("falsey.pdf", object_metadata={"tag": "needle"})
    metadata = FalseyMetadataRetriever(
        [MetadataSearchHit(record.bucket, record.key, 1.0)]
    )
    fusion = FalseyFusionConfig(rank_constant=7.0)
    service = AskService(_catalog(record), metadata=metadata, fusion=fusion)

    response = service.ask(AskQuery("needle"))

    assert service.metadata is metadata
    assert service.fusion is fusion
    assert metadata.calls
    assert [result.citation.key for result in response.results] == ["falsey.pdf"]


@pytest.mark.parametrize(
    ("keyword", "vector", "expected_mode", "keyword_state", "vector_state"),
    [
        (None, None, RetrievalMode.METADATA, ProviderState.MISSING, ProviderState.MISSING),
        (
            _KeywordRetriever(),
            None,
            RetrievalMode.METADATA_KEYWORD,
            ProviderState.SUCCEEDED,
            ProviderState.MISSING,
        ),
        (
            None,
            _VectorRetriever(),
            RetrievalMode.METADATA_VECTOR,
            ProviderState.MISSING,
            ProviderState.SUCCEEDED,
        ),
        (
            _KeywordRetriever(),
            _VectorRetriever(),
            RetrievalMode.HYBRID,
            ProviderState.SUCCEEDED,
            ProviderState.SUCCEEDED,
        ),
    ],
)
def test_retrieval_mode_and_status_report_configured_capabilities(
    keyword: _KeywordRetriever | None,
    vector: _VectorRetriever | None,
    expected_mode: RetrievalMode,
    keyword_state: ProviderState,
    vector_state: ProviderState,
) -> None:
    record = _record("metadata.pdf", object_metadata={"tag": "needle"})
    response = AskService(
        _catalog(record),
        metadata=_MetadataRetriever([MetadataSearchHit(record.bucket, record.key, 1.0)]),
        keyword=keyword,
        vector=vector,
    ).ask(AskQuery("needle"))
    diagnostics = _diagnostics(response.providers)

    assert response.mode is expected_mode
    assert diagnostics["metadata"].state is ProviderState.SUCCEEDED
    assert diagnostics["keyword"].state is keyword_state
    assert diagnostics["vector"].state is vector_state
    assert diagnostics["generation"].state is ProviderState.NOT_REQUESTED
    assert response.generation_status is GenerationStatus.NOT_REQUESTED
    assert response.answer is None


def test_missing_generation_provider_degrades_without_losing_retrieval() -> None:
    record = _record("metadata.pdf")
    response = AskService(
        _catalog(record),
        metadata=_MetadataRetriever([MetadataSearchHit(record.bucket, record.key, 1.0)]),
    ).ask(AskQuery("metadata", synthesize=True))

    assert [result.citation.key for result in response.results] == ["metadata.pdf"]
    assert response.generation_status is GenerationStatus.PROVIDER_MISSING
    assert _diagnostics(response.providers)["generation"].state is ProviderState.MISSING
    assert response.answer is None


def test_generation_reports_no_context_without_calling_the_provider() -> None:
    provider = _AnswerProvider(lambda _request: pytest.fail("provider must not be called"))
    response = AskService(
        _catalog(),
        metadata=_MetadataRetriever([]),
        answer_provider=provider,
    ).ask(AskQuery("nothing", synthesize=True))

    assert response.results == ()
    assert response.generation_status is GenerationStatus.NO_CONTEXT
    assert _diagnostics(response.providers)["generation"].state is ProviderState.NO_CONTEXT
    assert provider.calls == []


@pytest.mark.parametrize(
    "error",
    [
        RetrievalProviderUnavailableError("offline"),
        EmbeddingBackendUnsupportedError("no pgvector"),
        EmbeddingProviderConfigurationError("not configured"),
        TransientEmbeddingProviderError("temporarily offline"),
    ],
)
def test_classified_vector_provider_failures_degrade_to_unavailable(error: Exception) -> None:
    response = AskService(
        _catalog(),
        metadata=_MetadataRetriever([]),
        vector=_VectorRetriever(error=error),
    ).ask(AskQuery("query"))
    diagnostic = _diagnostics(response.providers)["vector"]

    assert response.mode is RetrievalMode.METADATA
    assert diagnostic.state is ProviderState.UNAVAILABLE
    assert diagnostic.error_type == type(error).__name__


def test_classified_keyword_provider_failure_degrades_to_unavailable() -> None:
    response = AskService(
        _catalog(),
        metadata=_MetadataRetriever([]),
        keyword=_KeywordRetriever(error=RetrievalProviderUnavailableError("offline")),
    ).ask(AskQuery("query"))
    diagnostic = _diagnostics(response.providers)["keyword"]

    assert response.mode is RetrievalMode.METADATA
    assert diagnostic.state is ProviderState.UNAVAILABLE
    assert diagnostic.error_type == "RetrievalProviderUnavailableError"


def test_classified_answer_provider_failure_preserves_retrieval() -> None:
    record = _record("metadata.pdf")

    def unavailable(_request: AnswerGenerationRequest) -> GeneratedAnswer:
        raise AnswerProviderUnavailableError("offline")

    response = AskService(
        _catalog(record),
        metadata=_MetadataRetriever([MetadataSearchHit(record.bucket, record.key, 1.0)]),
        answer_provider=_AnswerProvider(unavailable),
    ).ask(AskQuery("metadata", synthesize=True))

    assert [result.citation.key for result in response.results] == ["metadata.pdf"]
    assert response.generation_status is GenerationStatus.PROVIDER_UNAVAILABLE
    diagnostic = _diagnostics(response.providers)["generation"]
    assert diagnostic.state is ProviderState.UNAVAILABLE
    assert diagnostic.error_type == "AnswerProviderUnavailableError"
    assert response.answer is None


def test_generation_receives_only_selected_grounded_citations() -> None:
    record = _record("grounded.pdf", document_metadata={"language": "en"})

    def answer(request: AnswerGenerationRequest) -> GeneratedAnswer:
        return GeneratedAnswer(
            "The selected evidence supports the answer.",
            (request.citation_ids[0], request.citation_ids[-1]),
        )

    provider = _AnswerProvider(answer)
    response = AskService(
        _catalog(record),
        metadata=_MetadataRetriever([MetadataSearchHit(record.bucket, record.key, 2.0)]),
        keyword=_KeywordRetriever(
            [
                _keyword_hit(record, passage_id="1" * 64, passage_index=0),
                _keyword_hit(record, passage_id="2" * 64, passage_index=1),
            ]
        ),
        answer_provider=provider,
    ).ask(
        AskQuery(
            "grounded",
            limit=1,
            candidate_limit=5,
            passages_per_result=1,
            synthesize=True,
        )
    )

    assert response.generation_status is GenerationStatus.SUCCEEDED
    assert response.answer is not None
    assert len(provider.calls) == 1
    request = provider.calls[0]
    assert request.results == response.results
    assert request.citation_ids == (
        response.results[0].citation.citation_id,
        response.results[0].passages[0].citation.citation_id,
    )
    assert response.answer.citations == request.citation_ids


def test_generation_rejects_unknown_citation_identifiers() -> None:
    record = _record("grounded.pdf")
    provider = _AnswerProvider(
        lambda _request: GeneratedAnswer("Unsupported citation.", ("passage:invented",))
    )
    service = AskService(
        _catalog(record),
        metadata=_MetadataRetriever([MetadataSearchHit(record.bucket, record.key, 1.0)]),
        answer_provider=provider,
    )

    with pytest.raises(AnswerCitationError, match="unknown citations"):
        service.ask(AskQuery("grounded", synthesize=True))


def test_generated_answer_bounds_an_untrusted_citation_iterable() -> None:
    def citation_ids() -> Iterator[str]:
        index = 0
        while True:
            yield f"citation:{index}"
            index += 1

    untrusted_citations: Any = citation_ids()

    with pytest.raises(ValueError, match="may cite at most"):
        GeneratedAnswer("Bounded answer.", untrusted_citations)


def test_equal_rrf_scores_use_catalog_coordinates_as_deterministic_ties() -> None:
    first = _record("a.pdf", digest="1" * 64)
    second = _record("b.pdf", digest="2" * 64, object_metadata={"tag": "tie"})
    service = AskService(
        _catalog(first, second),
        metadata=_MetadataRetriever([MetadataSearchHit(second.bucket, second.key, 1.0)]),
        keyword=_KeywordRetriever([_keyword_hit(first, passage_id="1" * 64)]),
    )

    orders = [
        [result.citation.key for result in service.ask(AskQuery("tie")).results]
        for _ in range(3)
    ]

    assert orders == [["a.pdf", "b.pdf"]] * 3
    assert service.ask(AskQuery("tie")).results[0].score == pytest.approx(
        service.ask(AskQuery("tie")).results[1].score
    )


def _load_golden() -> dict[str, Any]:
    payload = _GOLDEN_PATH.read_bytes()
    checksum_line = _CHECKSUM_PATH.read_text(encoding="ascii").strip()
    expected_checksum, filename = checksum_line.split(maxsplit=1)
    assert filename == "golden.json"
    assert hashlib.sha256(payload).hexdigest() == expected_checksum
    document = json.loads(payload.decode("utf-8"))
    assert isinstance(document, dict)
    assert document["schema_version"] == 1
    return document


def _fixture_record(raw: Mapping[str, Any]) -> ObjectRecord:
    return _record(
        raw["key"],
        bucket=raw["bucket"],
        tier=raw["tier"],
        size=raw["size"],
        mime=raw["mime"],
        digest=raw["content_sha256"],
        object_metadata=raw["object_metadata"],
        document_metadata=raw["document_metadata"],
        text=raw["source_text"],
    )


def test_golden_queries_match_expected_object_order_and_passage_citations() -> None:
    golden = _load_golden()
    records_by_id = {
        raw["id"]: _fixture_record(raw)
        for raw in golden["objects"]
    }
    object_id_by_coordinate = {
        (record.bucket, record.key): object_id
        for object_id, record in records_by_id.items()
    }
    catalog = _catalog(*records_by_id.values())

    for raw_query in golden["queries"]:
        metadata_hits = [
            MetadataSearchHit(
                records_by_id[item["object"]].bucket,
                records_by_id[item["object"]].key,
                item["score"],
            )
            for item in raw_query["metadata_hits"]
        ]
        keyword_hits = [
            _keyword_hit(
                records_by_id[item["object"]],
                passage_id=item["passage_id"],
                text=item["text"],
                score=item["score"],
                passage_index=item["passage_index"],
                start_codepoint=item["start_codepoint"],
            )
            for item in raw_query["keyword_hits"]
        ]
        vector_hits = [
            _vector_hit(
                records_by_id[item["object"]],
                passage_id=item["passage_id"],
                document_id=item["document_id"],
                space_id=item["space_id"],
                text=item["text"],
                cosine_distance=item["cosine_distance"],
                passage_index=item["passage_index"],
                start_codepoint=item["start_codepoint"],
            )
            for item in raw_query["vector_hits"]
        ]
        query = AskQuery(
            raw_query["text"],
            filters=AskFilters(**raw_query["filters"]),
            limit=raw_query["limit"],
            candidate_limit=raw_query["candidate_limit"],
            passages_per_result=raw_query["passages_per_result"],
        )

        response = AskService(
            catalog,
            metadata=_MetadataRetriever(metadata_hits),
            keyword=_KeywordRetriever(keyword_hits),
            vector=_VectorRetriever(vector_hits),
        ).ask(query)

        expected = raw_query["expected"]
        assert response.mode.value == expected["mode"], raw_query["id"]
        actual_order = [
            object_id_by_coordinate[(result.citation.bucket, result.citation.key)]
            for result in response.results
        ]
        assert actual_order == expected["order"], raw_query["id"]
        for object_id, result in zip(actual_order, response.results):
            actual_passages = [
                {
                    "source": passage.citation.source.value,
                    "passage_id": passage.citation.passage_id,
                }
                for passage in result.passages
            ]
            assert actual_passages == expected["passages"][object_id], raw_query["id"]
