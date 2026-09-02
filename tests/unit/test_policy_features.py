from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from cognistore.core.catalog import ObjectRecord
from cognistore.core.embedding_index import (
    EmbeddingIndexer,
    SimilaritySearchFilters,
    SimilaritySearchResult,
)
from cognistore.core.embeddings import EmbeddingSpace
from cognistore.core.policy_features import (
    CatalogPolicyFeatureLoader,
    EmbeddingFeatureRequest,
    EmbeddingPolicyFeature,
    EmbeddingSimilarityFeatureProvider,
    FeatureState,
    MimePolicyFeature,
    PolicyFeatureProvenance,
    PolicyFeatures,
)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _mime_detection(mime: str | None = "application/pdf") -> dict[str, object]:
    return {
        "schema_version": 1,
        "mime": mime,
        "detector": "libmagic" if mime is not None else "none",
        "provenance": "content" if mime is not None else "none",
        "confidence": "high" if mime is not None else "none",
        "content_mime": mime,
        "filename_mime": "application/pdf",
        "filename_encoding": None,
        "disagreement": False,
        "status": "detected" if mime is not None else "unclassified",
        "fallback_reason": None if mime is not None else "libmagic_no_match",
    }


def _record(
    *,
    bucket: str = "documents",
    key: str = "sample.pdf",
    payload: bytes = b"current source",
    mime: str | None = "application/pdf",
    include_detection: bool = True,
    include_identity: bool = True,
) -> ObjectRecord:
    content_sha256 = _digest(payload)
    metadata: dict[str, object] = {"mime": mime}
    if include_identity:
        metadata.update(
            {
                "sha256": content_sha256,
                "content_identity": {
                    "schema_version": 1,
                    "representation": "source-bytes",
                    "digest_algorithm": "sha256",
                    "sha256": content_sha256,
                    "size": len(payload),
                },
            }
        )
    if include_detection:
        metadata["mime_detection"] = _mime_detection(mime)
    return ObjectRecord(
        bucket=bucket,
        key=key,
        size=len(payload),
        tier="warm",
        metadata=metadata,
    )


def _content_sha256(record: ObjectRecord) -> str:
    identity = record.metadata["content_identity"]
    assert isinstance(identity, dict)
    digest = identity["sha256"]
    assert isinstance(digest, str)
    return digest


def _provenance(
    record: ObjectRecord,
    *,
    source: str = "test-provider",
) -> PolicyFeatureProvenance:
    return PolicyFeatureProvenance(
        source=source,
        source_version=1,
        content_sha256=_content_sha256(record),
        details={"z": 2, "a": "first"},
    )


def test_projection_is_immutable_and_serializes_deterministically() -> None:
    record = _record()
    raw_details: dict[str, object] = {"z": 2, "a": "first"}
    provenance = PolicyFeatureProvenance(
        source="test-provider",
        source_version=1,
        content_sha256=_content_sha256(record),
        details=raw_details,  # type: ignore[arg-type]
    )
    projection = PolicyFeatures(
        mime=MimePolicyFeature(
            FeatureState.FRESH,
            provenance=provenance,
            value="application/pdf",
        ),
        embeddings=(
            EmbeddingPolicyFeature(
                "zeta",
                "z query",
                FeatureState.FRESH,
                provenance,
                0.25,
            ),
            EmbeddingPolicyFeature(
                "alpha",
                "a query",
                FeatureState.MISSING,
                provenance,
            ),
        ),
    )

    raw_details["a"] = "mutated"
    first = projection.to_dict()
    assert first == {
        "schema_version": 1,
        "mime": {
            "state": "fresh",
            "value": "application/pdf",
            "provenance": {
                "source": "test-provider",
                "source_version": 1,
                "content_sha256": _content_sha256(record),
                "details": {"a": "first", "z": 2},
            },
        },
        "embeddings": [
            {
                "name": "alpha",
                "query": "a query",
                "state": "missing",
                "similarity": None,
                "provenance": provenance.to_dict(),
            },
            {
                "name": "zeta",
                "query": "z query",
                "state": "fresh",
                "similarity": 0.25,
                "provenance": provenance.to_dict(),
            },
        ],
    }
    first["schema_version"] = 99
    assert projection.to_dict()["schema_version"] == 1
    with pytest.raises(FrozenInstanceError):
        projection.schema_version = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        provenance.details["new"] = "value"  # type: ignore[index]


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_projection_rejects_non_integer_schema_version(schema_version: object) -> None:
    record = _record()

    with pytest.raises(ValueError, match="unsupported policy feature schema_version"):
        PolicyFeatures(
            mime=MimePolicyFeature(
                FeatureState.FRESH,
                provenance=_provenance(record),
                value="application/pdf",
            ),
            schema_version=schema_version,  # type: ignore[arg-type]
        )


def test_every_feature_state_requires_provenance_and_similarity_is_bounded() -> None:
    with pytest.raises(ValueError, match="provenance"):
        MimePolicyFeature(FeatureState.MISSING, provenance=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="provenance"):
        EmbeddingPolicyFeature(
            "topic",
            "query",
            FeatureState.UNAVAILABLE,
            None,  # type: ignore[arg-type]
        )
    record = _record()
    with pytest.raises(ValueError, match="between -1 and 1"):
        EmbeddingPolicyFeature(
            "topic",
            "query",
            FeatureState.FRESH,
            _provenance(record),
            1.01,
        )


def test_loader_projects_fresh_mime_detection_with_current_content_provenance() -> None:
    record = _record()

    projection = CatalogPolicyFeatureLoader().load([record])[(record.bucket, record.key)]

    assert projection.mime.state is FeatureState.FRESH
    assert projection.mime.value == "application/pdf"
    assert projection.mime.provenance.source == "mime_detection"
    assert projection.mime.provenance.source_version == 1
    assert projection.mime.provenance.content_sha256 == _content_sha256(record)
    assert projection.mime.provenance.details["detector"] == "libmagic"


def test_loader_marks_unbound_catalog_mime_stale() -> None:
    record = _record(include_detection=False, include_identity=False)

    feature = CatalogPolicyFeatureLoader().load([record])[(record.bucket, record.key)].mime

    assert feature.state is FeatureState.STALE
    assert feature.value is None
    assert feature.provenance.source == "catalog_metadata"
    assert feature.provenance.content_sha256 is None
    assert feature.provenance.details["reason"] == "current_content_identity_missing"


def test_loader_marks_identity_bound_mime_without_detection_envelope_stale() -> None:
    record = _record(include_detection=False)

    feature = CatalogPolicyFeatureLoader().load([record])[(record.bucket, record.key)].mime

    assert feature.state is FeatureState.STALE
    assert feature.value is None
    assert feature.provenance.content_sha256 == _content_sha256(record)
    assert feature.provenance.details["reason"] == "mime_detection_missing"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda record: record.metadata.update(mime="text/plain"),
        lambda record: record.metadata.update(mime_detection={"schema_version": 1}),
        lambda record: record.metadata["content_identity"].update(sha256="bad"),  # type: ignore[union-attr]
    ],
)
def test_loader_marks_malformed_or_mismatched_detection_stale(
    mutation: Callable[[ObjectRecord], object],
) -> None:
    record = _record()
    mutation(record)

    feature = CatalogPolicyFeatureLoader().load([record])[(record.bucket, record.key)].mime

    assert feature.state is FeatureState.STALE
    assert feature.value is None
    assert feature.provenance is not None


def test_loader_rejects_semantically_impossible_filename_fallback() -> None:
    record = _record()
    record.metadata["mime_detection"] = {
        "schema_version": 1,
        "mime": "application/pdf",
        "detector": "filename",
        "provenance": "filename",
        "confidence": "high",
        "content_mime": "text/plain",
        "filename_mime": "application/pdf",
        "filename_encoding": None,
        "disagreement": False,
        "status": "fallback",
        "fallback_reason": "libmagic_unavailable",
    }

    feature = CatalogPolicyFeatureLoader().load([record])[(
        record.bucket,
        record.key,
    )].mime

    assert feature.state is FeatureState.STALE
    assert feature.value is None
    assert feature.provenance.details["reason"] == "metadata_inconsistent"


def test_loader_marks_absent_mime_missing_with_provenance() -> None:
    record = _record(mime=None, include_detection=False)

    feature = CatalogPolicyFeatureLoader().load([record])[(record.bucket, record.key)].mime

    assert feature.state is FeatureState.MISSING
    assert feature.provenance.details["reason"] == "mime_missing"


def test_missing_embedding_provider_emits_missing_with_attempt_provenance() -> None:
    record = _record()
    request = EmbeddingFeatureRequest("retention", "records retention")

    projection = CatalogPolicyFeatureLoader().load([record], [request])[
        (record.bucket, record.key)
    ]

    feature = projection.embeddings[0]
    assert feature.state is FeatureState.MISSING
    assert feature.similarity is None
    assert feature.provenance.source == "embedding_similarity"
    assert feature.provenance.content_sha256 == _content_sha256(record)
    assert feature.provenance.details["reason"] == "provider_missing"


class _UnavailableFeatureProvider:
    def load(
        self,
        records: Sequence[ObjectRecord],
        requests: Sequence[EmbeddingFeatureRequest],
    ) -> Mapping[tuple[str, str], Sequence[EmbeddingPolicyFeature]]:
        raise RuntimeError("backend is offline")


def test_provider_exception_emits_unavailable_with_error_type() -> None:
    record = _record()
    request = EmbeddingFeatureRequest("retention", "records retention")
    loader = CatalogPolicyFeatureLoader(_UnavailableFeatureProvider())

    feature = loader.load([record], [request])[(record.bucket, record.key)].embeddings[0]

    assert feature.state is FeatureState.UNAVAILABLE
    assert feature.provenance.details == {
        "error_type": "RuntimeError",
        "reason": "provider_unavailable",
    }


class _StaticFeatureProvider:
    def __init__(self, feature: EmbeddingPolicyFeature) -> None:
        self.feature = feature

    def load(
        self,
        records: Sequence[ObjectRecord],
        requests: Sequence[EmbeddingFeatureRequest],
    ) -> Mapping[tuple[str, str], Sequence[EmbeddingPolicyFeature]]:
        record = records[0]
        return {(record.bucket, record.key): (self.feature,)}


def test_loader_rejects_embedding_feature_for_stale_content() -> None:
    record = _record()
    request = EmbeddingFeatureRequest("retention", "records retention")
    stale_provenance = PolicyFeatureProvenance(
        source="test-provider",
        source_version=1,
        content_sha256="0" * 64,
        details={"model_revision": "model-commit-a"},
    )
    supplied = EmbeddingPolicyFeature(
        request.name,
        request.query,
        FeatureState.FRESH,
        stale_provenance,
        0.8,
    )

    feature = CatalogPolicyFeatureLoader(_StaticFeatureProvider(supplied)).load(
        [record], [request]
    )[(record.bucket, record.key)].embeddings[0]

    assert feature.state is FeatureState.STALE
    assert feature.similarity is None
    assert feature.provenance.content_sha256 == _content_sha256(record)
    assert feature.provenance.details["reason"] == "content_sha256_mismatch"
    assert feature.provenance.details["evidence_content_sha256"] == "0" * 64
    assert feature.provenance.details["model_revision"] == "model-commit-a"


def _space() -> EmbeddingSpace:
    return EmbeddingSpace(
        provider_implementation="test-provider",
        provider_implementation_version="1",
        model="test/model",
        model_revision="model-commit-a",
        dimensions=2,
        preprocessing="test-input",
    )


class _QueryProvider:
    def __init__(self) -> None:
        self.space = _space()
        self.query_calls: list[str] = []

    def embed_query(self, text: str) -> tuple[float, ...]:
        self.query_calls.append(text)
        return (1.0, 0.0)

    def embed_documents(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[float, ...], ...]:
        return tuple((1.0, 0.0) for _text in texts)


class _SearchRepository:
    def __init__(self, hits: Mapping[tuple[str, str], SimilaritySearchResult]) -> None:
        self.hits = dict(hits)
        self.search_calls: list[
            tuple[EmbeddingSpace, tuple[float, ...], SimilaritySearchFilters, int, bool]
        ] = []

    def search(
        self,
        space: EmbeddingSpace,
        query_vector: Sequence[float],
        *,
        filters: SimilaritySearchFilters,
        limit: int,
        exact: bool = False,
    ) -> list[SimilaritySearchResult]:
        self.search_calls.append((space, tuple(query_vector), filters, limit, exact))
        if len(filters.buckets) != 1 or len(filters.object_keys) != 1:
            return []
        coordinate = (next(iter(filters.buckets)), next(iter(filters.object_keys)))
        hit = self.hits.get(coordinate)
        return [] if hit is None else [hit]


def _hit(record: ObjectRecord, *, source_sha256: str | None = None) -> SimilaritySearchResult:
    return SimilaritySearchResult(
        space_id=_space().space_id,
        passage_id=uuid4(),
        document_id=uuid4(),
        source_sha256=source_sha256 or _content_sha256(record),
        document_text_sha256="a" * 64,
        bucket=record.bucket,
        key=record.key,
        tier=record.tier,
        mime="application/pdf",
        passage_index=3,
        start_codepoint=0,
        end_codepoint=10,
        text="retention policy",
        text_sha256="b" * 64,
        cosine_distance=0.2,
        object_metadata={},
        indexed_at="2026-09-01T12:00:00Z",
    )


def test_similarity_provider_embeds_duplicate_query_once_and_searches_each_object_exactly() -> None:
    first = _record(key="first.pdf", payload=b"first")
    second = _record(key="second.pdf", payload=b"second")
    repository = _SearchRepository(
        {
            (first.bucket, first.key): _hit(first),
            (second.bucket, second.key): _hit(second),
        }
    )
    query_provider = _QueryProvider()
    indexer = EmbeddingIndexer(repository, query_provider)  # type: ignore[arg-type]
    loader = CatalogPolicyFeatureLoader(EmbeddingSimilarityFeatureProvider(indexer))
    requests = (
        EmbeddingFeatureRequest("legal", "retention policy"),
        EmbeddingFeatureRequest("records", "retention policy"),
    )

    projections = loader.load([second, first], requests)

    assert query_provider.query_calls == ["retention policy"]
    assert len(repository.search_calls) == 4
    assert all(limit == 1 and exact for _space_, _vector, _filters, limit, exact in repository.search_calls)
    assert {
        (next(iter(filters.buckets)), next(iter(filters.object_keys)))
        for _space_, _vector, filters, _limit, _exact in repository.search_calls
    } == {(first.bucket, first.key), (second.bucket, second.key)}
    feature = projections[(first.bucket, first.key)].embeddings[0]
    assert feature.state is FeatureState.FRESH
    assert feature.similarity == pytest.approx(0.8)
    assert feature.provenance.details["aggregation"] == "max_passage_similarity"
    assert feature.provenance.details["space_id"] == str(query_provider.space.space_id)
    assert feature.provenance.details["document_id"]
    assert feature.provenance.details["passage_id"]
    assert feature.provenance.details["indexed_at"] == "2026-09-01T12:00:00Z"


def test_similarity_provider_reports_missing_and_stale_hits_without_similarity() -> None:
    missing = _record(key="missing.pdf", payload=b"missing")
    stale = _record(key="stale.pdf", payload=b"stale")
    repository = _SearchRepository(
        {(stale.bucket, stale.key): _hit(stale, source_sha256="0" * 64)}
    )
    provider = EmbeddingSimilarityFeatureProvider(
        EmbeddingIndexer(repository, _QueryProvider())  # type: ignore[arg-type]
    )
    request = EmbeddingFeatureRequest("retention", "retention policy")

    features = provider.load([missing, stale], [request])

    missing_feature = features[(missing.bucket, missing.key)][0]
    assert missing_feature.state is FeatureState.MISSING
    assert missing_feature.similarity is None
    assert missing_feature.provenance.details["reason"] == "embedding_not_indexed"
    stale_feature = features[(stale.bucket, stale.key)][0]
    assert stale_feature.state is FeatureState.STALE
    assert stale_feature.similarity is None
    assert stale_feature.provenance.details["reason"] == "hit_content_sha256_mismatch"


def test_similarity_provider_rejects_out_of_range_cosine_score() -> None:
    record = _record()
    hit = replace(_hit(record), cosine_distance=-1.0)
    repository = _SearchRepository({(record.bucket, record.key): hit})
    provider = EmbeddingSimilarityFeatureProvider(
        EmbeddingIndexer(repository, _QueryProvider())  # type: ignore[arg-type]
    )

    feature = provider.load(
        [record],
        [EmbeddingFeatureRequest("retention", "retention policy")],
    )[(record.bucket, record.key)][0]

    assert feature.state is FeatureState.STALE
    assert feature.provenance.details["reason"] == "malformed_similarity"


def test_similarity_provider_keeps_malformed_timestamp_as_stale_evidence() -> None:
    record = _record()
    hit = replace(
        _hit(record),
        indexed_at=datetime(2026, 9, 1, tzinfo=timezone.utc),  # type: ignore[arg-type]
    )
    repository = _SearchRepository({(record.bucket, record.key): hit})
    loader = CatalogPolicyFeatureLoader(
        EmbeddingSimilarityFeatureProvider(
            EmbeddingIndexer(repository, _QueryProvider())  # type: ignore[arg-type]
        )
    )

    feature = loader.load(
        [record],
        [EmbeddingFeatureRequest("retention", "retention policy")],
    )[(record.bucket, record.key)].embeddings[0]

    assert feature.state is FeatureState.STALE
    assert feature.provenance.details["reason"] == "malformed_similarity"
    assert "indexed_at" not in feature.provenance.details
