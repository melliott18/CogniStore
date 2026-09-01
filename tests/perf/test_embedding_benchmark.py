from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.perf.embedding_benchmark import (
    BenchmarkConfig,
    CorpusQuery,
    DatabaseVersions,
    EmbeddingBenchmarkError,
    HnswIndexConfiguration,
    SearchHit,
    canonical_report_json,
    checksum_from_manifest,
    latency_summary,
    load_corpus,
    report_sha256,
    run_similarity_benchmark,
)
from tests.perf.pgvector_embedding_benchmark import (
    DeterministicBenchmarkEmbeddingProvider,
)

_FIXTURE_DIRECTORY = Path(__file__).resolve().parents[1] / "fixtures" / "embeddings"
_CORPUS_PATH = _FIXTURE_DIRECTORY / "corpus.json"
_CHECKSUM_PATH = _FIXTURE_DIRECTORY / "SHA256SUMS"


class _NanosecondClock:
    def __init__(self, durations_ms: list[int]) -> None:
        values: list[int] = []
        cursor = 0
        for duration_ms in durations_ms:
            values.extend((cursor, cursor + duration_ms * 1_000_000))
            cursor += (duration_ms + 1) * 1_000_000
        self._values = iter(values)

    def __call__(self) -> int:
        return next(self._values)


class _PreparedAdapter:
    def __init__(self, passage_ids: list[str]) -> None:
        self._passage_ids = passage_ids
        self.approximate_calls = 0

    @property
    def indexed_rows(self) -> int:
        return len(self._passage_ids)

    @property
    def dimensions(self) -> int:
        return 3

    def exact_search(self, query: CorpusQuery, *, limit: int) -> list[SearchHit]:
        del query
        return [
            SearchHit(passage_id=passage_id, distance=index / 10)
            for index, passage_id in enumerate(self._passage_ids[:limit])
        ]

    def approximate_search(
        self,
        query: CorpusQuery,
        *,
        limit: int,
    ) -> list[SearchHit]:
        self.approximate_calls += 1
        passage_ids = list(self._passage_ids[:limit])
        if query.query_id == "legal-hold" and limit > 1:
            passage_ids[-1] = self._passage_ids[limit]
        return [
            SearchHit(passage_id=passage_id, distance=index / 10)
            for index, passage_id in enumerate(passage_ids)
        ]

    def database_versions(self) -> DatabaseVersions:
        return DatabaseVersions(postgres="16.10", pgvector="0.8.6")

    def hnsw_index_configuration(self) -> HnswIndexConfiguration:
        return HnswIndexConfiguration(
            index_name="passage_embeddings_space_fixture_hnsw",
            ddl=(
                "CREATE INDEX passage_embeddings_space_fixture_hnsw ON "
                "passage_embeddings USING hnsw ((embedding::vector(3)) "
                "vector_cosine_ops) WHERE space_id = 'fixture'"
            ),
            dimensions=3,
            m=16,
            ef_construction=64,
            index_size_bytes=65_536,
        )

    def search_space_metadata(self) -> dict[str, object]:
        return {
            "space_id": "fixture-space-v1",
            "fingerprint": "f" * 64,
            "provider_implementation": "fixture",
            "provider_implementation_version": "1",
            "model": "hash-embedding",
            "model_revision": "1",
            "dimensions": 3,
            "distance_metric": "cosine",
            "normalization": "l2",
        }

    def query_settings(self) -> dict[str, object]:
        return {
            "distance": "cosine",
            "hnsw.ef_search": 100,
            "hnsw.iterative_scan": "strict_order",
        }

    def explain_approximate(self, query: CorpusQuery, *, limit: int) -> list[object]:
        return [
            {
                "Plan": {
                    "Node Type": "Index Scan",
                    "Index Name": "passage_embeddings_space_fixture_hnsw",
                    "Plan Rows": limit,
                    "Filter": query.query_id,
                }
            }
        ]


def _checked_corpus():
    expected = checksum_from_manifest(_CHECKSUM_PATH, filename=_CORPUS_PATH.name)
    return load_corpus(_CORPUS_PATH, expected_sha256=expected)


def test_embedding_fixture_checksum_and_references_are_valid() -> None:
    corpus = _checked_corpus()

    assert corpus.name == "cognistore-semantic-search-v1"
    assert corpus.license == "MIT"
    assert len(corpus.passages) == 16
    assert len(corpus.queries) == 8
    assert {query.query_id for query in corpus.queries} == {
        "credential-containment",
        "duplicate-bill",
        "evening-solar",
        "legal-hold",
        "model-isolation",
        "offline-navigation",
        "restore-test",
        "starter-feeding",
    }


def test_similarity_report_records_latency_index_explain_and_recall() -> None:
    corpus = _checked_corpus()
    adapter = _PreparedAdapter([passage.passage_id for passage in corpus.passages])

    report = run_similarity_benchmark(
        corpus,
        adapter,
        config=BenchmarkConfig(warmups=1, runs=1, limit=2),
        clock_ns=_NanosecondClock(list(range(1, 9))),
        generated_at=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
    )

    assert adapter.approximate_calls == 16
    assert report["generated_at"] == "2026-09-01T12:00:00Z"
    assert report["corpus"] == {
        "name": corpus.name,
        "fixture_path": "tests/fixtures/embeddings/corpus.json",
        "sha256": corpus.sha256,
        "license": "MIT",
        "rows": 16,
        "query_count": 8,
    }
    assert report["workload"] == {
        "indexed_rows": 16,
        "dimensions": 3,
        "warmups_per_query": 1,
        "runs_per_query": 1,
        "limit": 2,
        "measured_query_count": 8,
    }
    assert report["database"] == {
        "postgres_version": "16.10",
        "pgvector_version": "0.8.6",
    }
    assert report["latency_ms"] == {
        "sample_count": 8,
        "min": 1.0,
        "p50": 4.0,
        "p95": 8.0,
        "p99": 8.0,
        "max": 8.0,
        "definition": (
            "prepared approximate pgvector query through complete result "
            "materialization; provider inference, fixture loading, exact baseline "
            "queries, and EXPLAIN are excluded"
        ),
    }
    assert report["recall_vs_exact"]["min"] == 0.5  # type: ignore[index]
    assert report["recall_vs_exact"]["mean"] == pytest.approx(0.9375)  # type: ignore[index]
    assert report["hnsw_index"]["m"] == 16  # type: ignore[index]
    assert report["hnsw_index"]["ef_construction"] == 64  # type: ignore[index]
    assert report["search_space"] == {
        "space_id": "fixture-space-v1",
        "fingerprint": "f" * 64,
        "provider_implementation": "fixture",
        "provider_implementation_version": "1",
        "model": "hash-embedding",
        "model_revision": "1",
        "dimensions": 3,
        "distance_metric": "cosine",
        "normalization": "l2",
    }
    assert report["explain"]["query_id"] == "legal-hold"  # type: ignore[index]
    assert report["explain"]["plan"][0]["Plan"]["Node Type"] == "Index Scan"  # type: ignore[index]

    serialized = canonical_report_json(report)
    assert json.loads(serialized) == report
    assert len(report_sha256(report)) == 64


def test_latency_summary_uses_nearest_rank_percentiles() -> None:
    assert latency_summary([4, 1, 3, 2]) == {
        "sample_count": 4,
        "min": 1.0,
        "p50": 2.0,
        "p95": 4.0,
        "p99": 4.0,
        "max": 4.0,
    }


@pytest.mark.parametrize("values", [[], [-1], [float("nan")], [float("inf")]])
def test_latency_summary_rejects_invalid_samples(values: list[float]) -> None:
    with pytest.raises(EmbeddingBenchmarkError):
        latency_summary(values)


def test_corpus_checksum_mismatch_fails_closed() -> None:
    with pytest.raises(EmbeddingBenchmarkError, match="checksum mismatch"):
        load_corpus(_CORPUS_PATH, expected_sha256="0" * 64)


def test_benchmark_refuses_an_incomplete_prepared_search_space() -> None:
    corpus = _checked_corpus()
    adapter = _PreparedAdapter([passage.passage_id for passage in corpus.passages[:-1]])

    with pytest.raises(EmbeddingBenchmarkError, match="row count does not match"):
        run_similarity_benchmark(corpus, adapter)


def test_live_benchmark_provider_is_deterministic_normalized_and_nfc_stable() -> None:
    provider = DeterministicBenchmarkEmbeddingProvider(dimensions=32)

    documents = provider.embed_documents(("Cafe\u0301 backup", "unrelated text"))
    query = provider.embed_query("Caf\u00e9 backup")

    assert documents[0] == query
    assert documents == provider.embed_documents(("Cafe\u0301 backup", "unrelated text"))
    assert len(query) == 32
    assert math.sqrt(sum(component * component for component in query)) == pytest.approx(1.0)
    assert provider.space.dimensions == 32
    assert provider.space.normalization == "l2"
