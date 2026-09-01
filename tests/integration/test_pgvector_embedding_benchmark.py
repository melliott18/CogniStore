from __future__ import annotations

import json
from pathlib import Path

import pytest

from cognistore.db import SQLCatalog
from tests.perf.embedding_benchmark import (
    BenchmarkConfig,
    checksum_from_manifest,
    load_corpus,
    run_similarity_benchmark,
)
from tests.perf.pgvector_embedding_benchmark import PgVectorBenchmarkAdapter

pytestmark = pytest.mark.integration

_FIXTURE_DIRECTORY = Path(__file__).resolve().parents[1] / "fixtures" / "embeddings"
_CORPUS_PATH = _FIXTURE_DIRECTORY / "corpus.json"
_CHECKSUM_PATH = _FIXTURE_DIRECTORY / "SHA256SUMS"


def test_live_pgvector_benchmark_adapter_uses_production_search_shape(
    postgres_dsn: str,
) -> None:
    corpus = load_corpus(
        _CORPUS_PATH,
        expected_sha256=checksum_from_manifest(
            _CHECKSUM_PATH,
            filename=_CORPUS_PATH.name,
        ),
    )

    with SQLCatalog(postgres_dsn) as catalog:
        adapter = PgVectorBenchmarkAdapter(corpus, catalog)
        report = run_similarity_benchmark(
            corpus,
            adapter,
            config=BenchmarkConfig(warmups=0, runs=1, limit=10),
        )

    assert report["workload"]["indexed_rows"] == 16  # type: ignore[index]
    assert report["workload"]["dimensions"] == 96  # type: ignore[index]
    assert report["database"]["pgvector_version"] == "0.8.6"  # type: ignore[index]
    assert report["hnsw_index"]["operator_class"] == "vector_cosine_ops"  # type: ignore[index]
    assert report["recall_vs_exact"]["min"] == 1.0  # type: ignore[index]
    plan = report["explain"]["plan"]  # type: ignore[index]
    assert isinstance(plan, list)
    assert plan and isinstance(plan[0], dict) and "Plan" in plan[0]
    index_name = report["hnsw_index"]["index_name"]  # type: ignore[index]
    assert isinstance(index_name, str)
    assert index_name in json.dumps(plan)
