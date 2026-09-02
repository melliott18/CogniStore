from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import replace
from uuid import uuid4

import pytest

from cognistore.core.embedding_index import (
    MAX_SIMILARITY_FILTER_PAYLOAD_BYTES,
    MAX_SIMILARITY_FILTER_TEXT_BYTES,
    MAX_SIMILARITY_FILTER_VALUES,
    MAX_SIMILARITY_KEY_PREFIX_BYTES,
    MAX_SIMILARITY_METADATA_FILTERS,
    MAX_SIMILARITY_METADATA_KEY_BYTES,
    MAX_SIMILARITY_METADATA_VALUE_BYTES,
    EmbeddingDocument,
    EmbeddingDocumentSource,
    EmbeddingIndexer,
    EmbeddingIndexIncompleteError,
    EmbeddingSourceChangedError,
    HnswIndexConfig,
    SimilaritySearchFilters,
)
from cognistore.core.embeddings import (
    EmbeddingRetryPolicy,
    EmbeddingSpace,
    TransientEmbeddingProviderError,
)
from cognistore.core.passages import (
    DeterministicPassageChunker,
    NormalizedDocumentIdentity,
    Passage,
    PassageChunkerConfig,
)


def _space(revision: str = "model-commit-a") -> EmbeddingSpace:
    return EmbeddingSpace(
        provider_implementation="test-provider",
        provider_implementation_version="1",
        model="test/model",
        model_revision=revision,
        dimensions=2,
        preprocessing="test-input",
    )


def _source(text: str = "alpha beta") -> EmbeddingDocumentSource:
    source_sha256 = hashlib.sha256(b"canonical source").hexdigest()
    return EmbeddingDocumentSource(
        bucket="documents",
        key="sample.pdf",
        extraction_schema_version=1,
        source_mime="application/pdf",
        document=NormalizedDocumentIdentity(
            manifest_id=uuid4(),
            source_sha256=source_sha256,
            extraction_schema_version=1,
            source_mime="application/pdf",
            parser_name="test-parser",
            parser_implementation_version="1",
            parser_runtime_version="2.0",
        ),
        text=text,
    )


def test_embedding_source_requires_matching_identity_provenance() -> None:
    source = _source()

    with pytest.raises(ValueError, match="extraction_schema_version"):
        replace(source, extraction_schema_version=2)
    with pytest.raises(ValueError, match="source_mime"):
        replace(source, source_mime="application/octet-stream")


class _Provider:
    def __init__(self, space: EmbeddingSpace | None = None) -> None:
        self.space = space or _space()
        self.document_calls: list[tuple[str, ...]] = []
        self.query_calls: list[str] = []
        self.fail_document_call: int | None = None

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        detached = tuple(texts)
        self.document_calls.append(detached)
        if self.fail_document_call == len(self.document_calls):
            raise TransientEmbeddingProviderError("temporary provider outage")
        return tuple((float(len(text)), 1.0) for text in detached)

    def embed_query(self, text: str) -> tuple[float, ...]:
        self.query_calls.append(text)
        return (float(len(text)), 1.0)


class _Repository:
    def __init__(self, source: EmbeddingDocumentSource) -> None:
        self.source = source
        self.spaces: dict[object, HnswIndexConfig] = {}
        self.documents: dict[object, EmbeddingDocument] = {}
        self.vectors: dict[tuple[object, object], tuple[float, ...]] = {}
        self.completed: set[tuple[object, object]] = set()
        self.write_calls = 0
        self.raise_after_write_call: int | None = None
        self.changed = False
        self.search_args: tuple[object, ...] | None = None

    def load_source(self, bucket: str, key: str) -> EmbeddingDocumentSource:
        assert (bucket, key) == (self.source.bucket, self.source.key)
        return self.source

    def ensure_space(self, space: EmbeddingSpace, config: HnswIndexConfig) -> None:
        self.spaces[space.space_id] = config

    def prepare_document(self, document: EmbeddingDocument) -> None:
        existing = self.documents.setdefault(document.document_id, document)
        assert existing == document

    def existing_passage_ids(
        self,
        document_id: object,
        space_id: object,
    ) -> frozenset[object]:
        document = self.documents[document_id]
        return frozenset(
            passage.passage_id
            for passage in document.passages
            if (space_id, passage.passage_id) in self.vectors
        )

    def reset_document_space(
        self,
        document: EmbeddingDocument,
        space: EmbeddingSpace,
    ) -> None:
        self.completed.discard((document.document_id, space.space_id))
        passage_ids = {passage.passage_id for passage in document.passages}
        self.vectors = {
            key: value
            for key, value in self.vectors.items()
            if not (key[0] == space.space_id and key[1] in passage_ids)
        }

    def write_embeddings(
        self,
        document: EmbeddingDocument,
        space: EmbeddingSpace,
        values: Sequence[tuple[Passage, Sequence[float]]],
    ) -> None:
        if self.changed:
            raise EmbeddingSourceChangedError("source changed")
        self.write_calls += 1
        for passage, vector in values:
            self.vectors[(space.space_id, passage.passage_id)] = tuple(vector)
        if self.raise_after_write_call == self.write_calls:
            raise RuntimeError("ambiguous response after commit")

    def complete_document_space(
        self,
        document: EmbeddingDocument,
        space: EmbeddingSpace,
    ) -> None:
        if self.changed:
            raise EmbeddingSourceChangedError("source changed")
        persisted = {
            passage.passage_id
            for passage in document.passages
            if (space.space_id, passage.passage_id) in self.vectors
        }
        if len(persisted) != len(document.passages):
            raise EmbeddingIndexIncompleteError("incomplete")
        self.completed.add((document.document_id, space.space_id))

    def search(
        self,
        space: EmbeddingSpace,
        query_vector: Sequence[float],
        *,
        filters: SimilaritySearchFilters,
        limit: int,
        exact: bool = False,
    ) -> list[object]:
        self.search_args = (space, tuple(query_vector), filters, limit, exact)
        return []


def _indexer(
    repository: _Repository,
    provider: _Provider,
    *,
    batch_size: int = 1,
    max_attempts: int = 1,
) -> EmbeddingIndexer:
    return EmbeddingIndexer(
        repository,
        provider,
        chunker=DeterministicPassageChunker(
            PassageChunkerConfig(max_codepoints=5, overlap_codepoints=0)
        ),
        batch_size=batch_size,
        retry_policy=EmbeddingRetryPolicy(max_attempts=max_attempts),
        sleep=lambda _delay: None,
    )


def test_provider_failure_resumes_missing_batches_without_duplicate_vectors() -> None:
    source = _source("abcde fghij")
    repository = _Repository(source)
    provider = _Provider()
    provider.fail_document_call = 2
    indexer = _indexer(repository, provider)

    with pytest.raises(TransientEmbeddingProviderError):
        indexer.index_object(source.bucket, source.key)

    assert len(repository.vectors) == 1
    assert repository.completed == set()

    provider.fail_document_call = None
    report = indexer.index_object(source.bucket, source.key)

    assert report.total_passages == 3
    assert report.embedded_passages == 2
    assert report.reused_passages == 1
    assert len(repository.vectors) == 3
    assert repository.completed == {(report.document_id, report.space_id)}


def test_ambiguous_failure_after_commit_replay_skips_the_durable_batch() -> None:
    source = _source("abcdefghij")
    repository = _Repository(source)
    provider = _Provider()
    repository.raise_after_write_call = 1
    indexer = _indexer(repository, provider)

    with pytest.raises(RuntimeError, match="ambiguous"):
        indexer.index_object(source.bucket, source.key)
    first_calls = tuple(provider.document_calls)
    assert len(repository.vectors) == 1

    repository.raise_after_write_call = None
    report = indexer.index_object(source.bucket, source.key)

    assert report.reused_passages == 1
    assert len(repository.vectors) == 2
    assert provider.document_calls[: len(first_calls)] == list(first_calls)
    assert provider.document_calls[-1] == ("fghij",)


def test_force_reembedding_updates_same_keys_without_creating_duplicates() -> None:
    source = _source("abcdefghij")
    repository = _Repository(source)
    provider = _Provider()
    indexer = _indexer(repository, provider, batch_size=2)

    first = indexer.index_object(source.bucket, source.key)
    second = indexer.index_object(source.bucket, source.key)
    forced = indexer.index_object(source.bucket, source.key, force=True)

    assert first.embedded_passages == 2
    assert second.embedded_passages == 0
    assert forced.embedded_passages == 2
    assert len(repository.vectors) == 2
    assert first.document_id == second.document_id == forced.document_id


def test_failed_forced_reembedding_is_hidden_and_resumes_missing_batches() -> None:
    source = _source("abcdefghij")
    repository = _Repository(source)
    provider = _Provider()
    indexer = _indexer(repository, provider, batch_size=1)
    initial = indexer.index_object(source.bucket, source.key)
    provider.fail_document_call = len(provider.document_calls) + 2

    with pytest.raises(TransientEmbeddingProviderError):
        indexer.index_object(source.bucket, source.key, force=True)

    assert (initial.document_id, initial.space_id) not in repository.completed
    assert len(repository.vectors) == 1

    provider.fail_document_call = None
    resumed = indexer.index_object(source.bucket, source.key)

    assert resumed.reused_passages == 1
    assert resumed.embedded_passages == 1
    assert len(repository.vectors) == 2
    assert (resumed.document_id, resumed.space_id) in repository.completed


def test_model_revisions_use_disjoint_vector_keys_and_search_spaces() -> None:
    source = _source("abcdefghij")
    repository = _Repository(source)
    first_provider = _Provider(_space("model-commit-a"))
    second_provider = _Provider(_space("model-commit-b"))

    first = _indexer(repository, first_provider, batch_size=2).index_object(
        source.bucket, source.key
    )
    second = _indexer(repository, second_provider, batch_size=2).index_object(
        source.bucket, source.key
    )

    assert first.document_id == second.document_id
    assert first.space_id != second.space_id
    assert len(repository.vectors) == 4
    assert {space_id for space_id, _passage_id in repository.vectors} == {
        first.space_id,
        second.space_id,
    }


def test_transient_query_is_retried_and_filters_are_forwarded() -> None:
    source = _source()
    repository = _Repository(source)
    provider = _Provider()
    calls = 0

    def flaky_query(text: str) -> tuple[float, ...]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TransientEmbeddingProviderError("retry")
        return (1.0, 2.0)

    provider.embed_query = flaky_query  # type: ignore[method-assign]
    filters = SimilaritySearchFilters(
        buckets=frozenset({"documents"}),
        tiers=frozenset({"warm"}),
        mime_types=frozenset({"application/pdf"}),
        metadata={"department": "legal"},
    )
    indexer = _indexer(repository, provider, max_attempts=2)

    assert (
        indexer.search(
            "retention rules",
            filters=filters,
            limit=7,
            exact=True,
        )
        == []
    )

    assert calls == 2
    assert repository.search_args == (
        provider.space,
        (1.0, 2.0),
        filters,
        7,
        True,
    )


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("buckets", "documents"),
        ("object_keys", "sample.pdf"),
        ("tiers", b"warm"),
        ("mime_types", 1),
        ("buckets", {"documents": True}),
        ("mime_types", frozenset({"application/pdf\0other"})),
        ("mime_types", frozenset({1})),
    ],
)
def test_similarity_filters_reject_ambiguous_or_malformed_collections(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=field_name):
        SimilaritySearchFilters(**{field_name: value})  # type: ignore[arg-type]


def test_similarity_filters_bound_collection_counts_and_text_lengths() -> None:
    with pytest.raises(ValueError, match="at most"):
        SimilaritySearchFilters(
            buckets=frozenset(
                f"bucket-{index}" for index in range(MAX_SIMILARITY_FILTER_VALUES + 1)
            )
        )
    with pytest.raises(ValueError, match="bytes"):
        SimilaritySearchFilters(tiers=frozenset({"x" * (MAX_SIMILARITY_FILTER_TEXT_BYTES + 1)}))
    with pytest.raises(ValueError, match="key_prefix"):
        SimilaritySearchFilters(key_prefix="x" * (MAX_SIMILARITY_KEY_PREFIX_BYTES + 1))


def test_similarity_filters_preserve_existing_positional_constructor_order() -> None:
    filters = SimilaritySearchFilters(
        frozenset({"documents"}),
        "reports/",
        frozenset({"warm"}),
        frozenset({"application/pdf"}),
        {"department": "legal"},
    )

    assert filters.buckets == frozenset({"documents"})
    assert filters.key_prefix == "reports/"
    assert filters.tiers == frozenset({"warm"})
    assert filters.mime_types == frozenset({"application/pdf"})
    assert filters.metadata == {"department": "legal"}
    assert filters.object_keys == frozenset()


def test_similarity_filters_bound_lazy_iterables_without_hanging() -> None:
    def values() -> Iterator[str]:
        while True:
            yield "duplicate"

    with pytest.raises(ValueError, match="at most"):
        SimilaritySearchFilters(buckets=values())  # type: ignore[arg-type]


def test_similarity_filters_preserve_nul_safe_catalog_identities() -> None:
    filters = SimilaritySearchFilters(
        buckets=(value for value in ("bucket\0name",)),
        object_keys=(value for value in ("object\0key",)),
        key_prefix="prefix\0",
        tiers=("hot\0tier",),
    )

    assert filters.buckets == frozenset({"bucket\0name"})
    assert filters.object_keys == frozenset({"object\0key"})
    assert filters.key_prefix == "prefix\0"
    assert filters.tiers == frozenset({"hot\0tier"})


@pytest.mark.parametrize(
    "metadata",
    [
        [],
        {"bad\0key": "value"},
        {"key": "bad\0value"},
        {"x" * (MAX_SIMILARITY_METADATA_KEY_BYTES + 1): "value"},
        {"key": "x" * (MAX_SIMILARITY_METADATA_VALUE_BYTES + 1)},
        {"key": 1},
    ],
)
def test_similarity_filters_reject_malformed_or_oversized_metadata(
    metadata: object,
) -> None:
    with pytest.raises(ValueError, match="metadata"):
        SimilaritySearchFilters(metadata=metadata)  # type: ignore[arg-type]


def test_similarity_filters_bound_metadata_filter_count() -> None:
    with pytest.raises(ValueError, match="at most"):
        SimilaritySearchFilters(
            metadata={
                f"key-{index}": "value" for index in range(MAX_SIMILARITY_METADATA_FILTERS + 1)
            }
        )


def test_similarity_filters_bound_aggregate_payload() -> None:
    value = "x" * MAX_SIMILARITY_METADATA_VALUE_BYTES
    pairs = MAX_SIMILARITY_FILTER_PAYLOAD_BYTES // len(value) + 1

    with pytest.raises(ValueError, match="payload"):
        SimilaritySearchFilters(metadata={f"key-{index}": value for index in range(pairs)})


@pytest.mark.parametrize("filters", [{}, [], False])
def test_indexer_rejects_wrong_filter_type_before_provider_call(filters: object) -> None:
    repository = _Repository(_source())
    provider = _Provider()
    indexer = _indexer(repository, provider)

    with pytest.raises(ValueError, match="SimilaritySearchFilters"):
        indexer.search("query", filters=filters)  # type: ignore[arg-type]

    assert provider.query_calls == []


def test_source_change_prevents_vector_publication_and_completion() -> None:
    source = _source("abcdefghij")
    repository = _Repository(source)
    provider = _Provider()
    repository.changed = True

    with pytest.raises(EmbeddingSourceChangedError):
        _indexer(repository, provider, batch_size=2).index_object(source.bucket, source.key)

    assert repository.completed == set()


def test_empty_normalized_document_completes_without_provider_calls() -> None:
    source = _source("")
    repository = _Repository(source)
    provider = _Provider()

    report = _indexer(repository, provider).index_object(source.bucket, source.key)

    assert report.total_passages == 0
    assert report.embedded_passages == 0
    assert provider.document_calls == []
    assert repository.completed == {(report.document_id, report.space_id)}
