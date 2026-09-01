"""Live PostgreSQL/pgvector adapter for the embedding qualification corpus.

The adapter intentionally uses a small, deterministic feature-hashing provider.
It exercises the production passage/index/search path without adding model download
or network latency to the database benchmark.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import math
import os
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import sqlalchemy as sa

from cognistore.core.content_identity import ContentIdentityBuilder
from cognistore.core.embedding_index import (
    EmbeddingIndexer,
    HnswIndexConfig,
    SimilaritySearchFilters,
    SimilaritySearchResult,
)
from cognistore.core.embeddings import EmbeddingSpace, EmbeddingVector
from cognistore.core.passages import DeterministicPassageChunker, PassageChunkerConfig
from cognistore.db import PgVectorEmbeddingStore, SQLCatalog
from cognistore.db.engine import normalize_database_url

from .embedding_benchmark import (
    BenchmarkConfig,
    CorpusPassage,
    CorpusQuery,
    DatabaseVersions,
    EmbeddingBenchmarkCorpus,
    EmbeddingBenchmarkError,
    HnswIndexConfiguration,
    JSONValue,
    SearchHit,
    _json_value,
    canonical_report_json,
    checksum_from_manifest,
    load_corpus,
    report_sha256,
    run_similarity_benchmark,
)

DEFAULT_DIMENSIONS = 96
DEFAULT_RESULTS_PATH = Path("tests/perf/results/pgvector-compose-report.json")
_INTERNAL_PASSAGE_METADATA_KEY = "cognistore_benchmark_passage_id"
_TOKEN_PATTERN = re.compile(r"[\w]+", re.UNICODE)
_SEARCH_OVERFETCH_FACTOR = 4


class DeterministicBenchmarkEmbeddingProvider:
    """Non-network, L2-normalized feature hashing for repeatable qualification.

    This is deliberately not a production semantic model. Its only purpose is to
    prepare stable vectors so exact and HNSW database behavior can be compared.
    """

    def __init__(self, *, dimensions: int = DEFAULT_DIMENSIONS) -> None:
        self.space = EmbeddingSpace(
            provider_implementation="cognistore-benchmark-feature-hashing",
            provider_implementation_version="1",
            model="cognistore/fixture-token-bigram-hash",
            model_revision="2026-09-01-fixture-v1",
            dimensions=dimensions,
            preprocessing="unicode-nfc-casefold-token-bigram-sha256",
            preprocessing_version=1,
        )

    @staticmethod
    def _features(text: str) -> tuple[str, ...]:
        normalized = unicodedata.normalize("NFC", text).casefold()
        tokens = tuple(_TOKEN_PATTERN.findall(normalized))
        bigrams = tuple(f"{left}\x1f{right}" for left, right in zip(tokens, tokens[1:]))
        return ("__bias__", *tokens, *bigrams)

    def _vector(self, text: str) -> EmbeddingVector:
        values = [0.0] * self.space.dimensions
        for feature in self._features(text):
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:8], "big") % self.space.dimensions
            sign = 1.0 if digest[8] & 1 else -1.0
            values[bucket] += sign
        norm = math.sqrt(sum(component * component for component in values))
        if norm == 0:
            raise EmbeddingBenchmarkError("benchmark feature hashing produced a zero vector")
        return tuple(component / norm for component in values)

    def embed_documents(self, texts: Sequence[str]) -> tuple[EmbeddingVector, ...]:
        return tuple(self._vector(text) for text in texts)

    def embed_query(self, text: str) -> EmbeddingVector:
        return self._vector(text)


def _publish_passage(
    catalog: SQLCatalog,
    passage: CorpusPassage,
) -> tuple[str, str]:
    """Publish one fixture passage as one unique normalized source object."""

    bucket = passage.bucket
    key = f"{passage.object_key}/__passage__/{passage.passage_id}"
    payload = passage.text.encode("utf-8")
    content = ContentIdentityBuilder(chunk_size=64).build(
        BytesIO(payload),
        expected_size=len(payload),
    )
    metadata: dict[str, object] = dict(passage.metadata)
    metadata.update(
        {
            _INTERNAL_PASSAGE_METADATA_KEY: passage.passage_id,
            "mime": passage.mime,
            "document_extraction": {
                "schema_version": 1,
                "status": "succeeded",
                "source_mime": passage.mime,
                "source_size": len(payload),
                "parser": {
                    "name": "cognistore-benchmark-fixture-parser",
                    "implementation_version": "1",
                    "runtime_version": "1",
                },
                "normalization_version": 1,
                "text": passage.text,
                "text_bytes": len(payload),
                "output_bytes": len(payload),
                "document_metadata": {
                    "fixture_document_id": passage.document_id,
                },
                "failure_code": None,
            },
        }
    )
    fence = catalog.capture_scan_fence(bucket, key)
    published = catalog.upsert_scan_observation(
        bucket,
        key,
        size=len(payload),
        tier=passage.tier,
        generation=f"benchmark:{content.sha256}",
        metadata=metadata,
        fence=fence,
        content=content,
    )
    if not published:
        raise EmbeddingBenchmarkError(
            f"benchmark fixture passage was rejected by its scan fence: {passage.passage_id}"
        )
    return bucket, key


def _query_filters(query: CorpusQuery) -> SimilaritySearchFilters:
    metadata: dict[str, str] = {}
    for key, value in query.filters.items():
        if not isinstance(value, str):
            raise EmbeddingBenchmarkError(
                f"query {query.query_id!r} has a non-string production metadata filter"
            )
        metadata[key] = value
    return SimilaritySearchFilters(metadata=metadata)


class PgVectorBenchmarkAdapter:
    """Prepared production-DAL adapter consumed by the neutral harness."""

    def __init__(
        self,
        corpus: EmbeddingBenchmarkCorpus,
        catalog: SQLCatalog,
        *,
        provider: DeterministicBenchmarkEmbeddingProvider | None = None,
        hnsw: HnswIndexConfig | None = None,
    ) -> None:
        self.corpus = corpus
        self.catalog = catalog
        self.provider = provider or DeterministicBenchmarkEmbeddingProvider()
        self.hnsw = hnsw or HnswIndexConfig()
        self.store = PgVectorEmbeddingStore(catalog)
        self._query_vectors = {
            query.query_id: self.provider.embed_query(query.text) for query in corpus.queries
        }
        self._prepare_passages()
        self._metadata = self.store.index_metadata(self.provider.space)

    def _prepare_passages(self) -> None:
        chunker = DeterministicPassageChunker(
            PassageChunkerConfig(max_codepoints=4_096, overlap_codepoints=0)
        )
        indexer = EmbeddingIndexer(
            self.store,
            self.provider,
            chunker=chunker,
            batch_size=16,
            hnsw=self.hnsw,
        )
        indexed_rows = 0
        for passage in self.corpus.passages:
            bucket, key = _publish_passage(self.catalog, passage)
            result = indexer.index_object(bucket, key)
            if result.total_passages != 1:
                raise EmbeddingBenchmarkError(
                    f"fixture passage {passage.passage_id!r} produced "
                    f"{result.total_passages} indexed passages"
                )
            indexed_rows += result.total_passages
        if indexed_rows != len(self.corpus.passages):
            raise EmbeddingBenchmarkError("not every fixture passage was indexed")

    @property
    def indexed_rows(self) -> int:
        value = self._metadata["vector_count"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EmbeddingBenchmarkError("production vector count is malformed")
        return value

    @property
    def dimensions(self) -> int:
        return self.provider.space.dimensions

    @staticmethod
    def _hit(result: SimilaritySearchResult) -> SearchHit:
        passage_id = result.object_metadata.get(_INTERNAL_PASSAGE_METADATA_KEY)
        if not isinstance(passage_id, str) or not passage_id:
            raise EmbeddingBenchmarkError(
                "production similarity result lost its fixture passage identity"
            )
        return SearchHit(
            passage_id=passage_id,
            distance=result.cosine_distance,
        )

    def _search(
        self,
        query: CorpusQuery,
        *,
        limit: int,
        exact: bool,
    ) -> list[SearchHit]:
        results = self.store.search(
            self.provider.space,
            self._query_vectors[query.query_id],
            filters=_query_filters(query),
            limit=limit,
            exact=exact,
        )
        return [self._hit(result) for result in results]

    def exact_search(self, query: CorpusQuery, *, limit: int) -> list[SearchHit]:
        return self._search(query, limit=limit, exact=True)

    def approximate_search(
        self,
        query: CorpusQuery,
        *,
        limit: int,
    ) -> list[SearchHit]:
        return self._search(query, limit=limit, exact=False)

    def database_versions(self) -> DatabaseVersions:
        return DatabaseVersions(
            postgres=str(self._metadata["postgres_version"]),
            pgvector=str(self._metadata["pgvector_version"]),
        )

    def hnsw_index_configuration(self) -> HnswIndexConfiguration:
        hnsw = self._metadata["hnsw"]
        if not isinstance(hnsw, Mapping):
            raise EmbeddingBenchmarkError("production HNSW metadata is malformed")
        index_name = hnsw.get("index_name")
        index_definition = hnsw.get("index_definition")
        if not isinstance(index_name, str) or not isinstance(index_definition, str):
            raise EmbeddingBenchmarkError("the benchmark search space has no HNSW index")
        return HnswIndexConfiguration(
            index_name=index_name,
            ddl=index_definition,
            dimensions=self.dimensions,
            m=int(hnsw["m"]),
            ef_construction=int(hnsw["ef_construction"]),
            index_size_bytes=int(hnsw["index_size_bytes"]),
        )

    def search_space_metadata(self) -> Mapping[str, JSONValue]:
        metadata = _json_value(
            self.provider.space.to_metadata(),
            "production embedding space metadata",
        )
        if not isinstance(metadata, dict):  # pragma: no cover - fixed provider contract
            raise EmbeddingBenchmarkError("production embedding space metadata is malformed")
        return metadata

    def query_settings(self) -> Mapping[str, JSONValue]:
        hnsw = self._metadata["hnsw"]
        if not isinstance(hnsw, Mapping):
            raise EmbeddingBenchmarkError("production HNSW metadata is malformed")
        return {
            "distance": self.provider.space.distance_metric,
            "hnsw.ef_search": int(hnsw["ef_search"]),
            "hnsw.iterative_scan": str(hnsw["iterative_scan"]),
            "candidate_overfetch_factor": _SEARCH_OVERFETCH_FACTOR,
        }

    def explain_approximate(self, query: CorpusQuery, *, limit: int) -> JSONValue:
        """Capture and explain the production approximate-search statement."""

        captured: list[tuple[str, dict[str, Any]]] = []

        def capture_search_statement(
            _connection: object,
            _cursor: object,
            statement: str,
            parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            normalized = " ".join(statement.upper().split())
            if not normalized.startswith("SELECT EMBEDDING_CANDIDATES.SPACE_ID"):
                return
            if not isinstance(parameters, Mapping):
                raise EmbeddingBenchmarkError(
                    "production search did not use named bounded parameters"
                )
            captured.append((statement, dict(parameters)))

        sa.event.listen(
            self.catalog.engine,
            "before_cursor_execute",
            capture_search_statement,
        )
        try:
            self.store.search(
                self.provider.space,
                self._query_vectors[query.query_id],
                filters=_query_filters(query),
                limit=limit,
                exact=False,
            )
        finally:
            sa.event.remove(
                self.catalog.engine,
                "before_cursor_execute",
                capture_search_statement,
            )
        if len(captured) != 1:
            raise EmbeddingBenchmarkError(
                "could not capture exactly one production similarity query"
            )
        statement, parameters = captured[0]
        with self.catalog.engine.begin() as connection:
            connection.exec_driver_sql(f"SET LOCAL hnsw.ef_search = {self.hnsw.ef_search}")
            connection.exec_driver_sql(
                f"SET LOCAL hnsw.iterative_scan = '{self.hnsw.iterative_scan}'"
            )
            plan = connection.exec_driver_sql(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement,
                parameters,
            ).scalar_one()
        return _json_value(plan, "production EXPLAIN plan")


@contextlib.contextmanager
def isolated_postgres_database(admin_dsn: str) -> Iterator[str]:
    """Yield a uniquely named sibling database and remove it after the run."""

    admin_url = sa.engine.make_url(normalize_database_url(admin_dsn))
    if admin_url.get_backend_name() != "postgresql":
        raise EmbeddingBenchmarkError("benchmark administrator DSN must use PostgreSQL")
    database_name = f"cognistore_embedding_benchmark_{uuid4().hex}"
    benchmark_url = admin_url.set(database=database_name)
    admin_engine = sa.create_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    quoted_name = admin_engine.dialect.identifier_preparer.quote(database_name)
    created = False
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f"CREATE DATABASE {quoted_name} TEMPLATE template0")
        created = True
        yield benchmark_url.render_as_string(hide_password=False)
    finally:
        if created:
            with admin_engine.connect() as connection:
                connection.exec_driver_sql(f"DROP DATABASE {quoted_name} WITH (FORCE)")
        admin_engine.dispose()


def run_live_benchmark(
    corpus: EmbeddingBenchmarkCorpus,
    *,
    admin_dsn: str,
    config: BenchmarkConfig | None = None,
) -> dict[str, JSONValue]:
    """Run a live benchmark inside a disposable, migrated database."""

    with isolated_postgres_database(admin_dsn) as benchmark_dsn:
        with SQLCatalog(benchmark_dsn) as catalog:
            adapter = PgVectorBenchmarkAdapter(corpus, catalog)
            return run_similarity_benchmark(corpus, adapter, config=config)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the retained CogniStore pgvector embedding benchmark",
    )
    parser.add_argument(
        "--admin-dsn",
        default=os.environ.get("COGNISTORE_BENCHMARK_POSTGRES_DSN"),
        help=("PostgreSQL administrator DSN (or set COGNISTORE_BENCHMARK_POSTGRES_DSN)"),
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("tests/fixtures/embeddings/corpus.json"),
    )
    parser.add_argument(
        "--checksum-manifest",
        type=Path,
        default=Path("tests/fixtures/embeddings/SHA256SUMS"),
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_RESULTS_PATH)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--limit", type=int, default=10)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.admin_dsn:
        raise SystemExit("--admin-dsn or COGNISTORE_BENCHMARK_POSTGRES_DSN is required")
    expected_checksum = checksum_from_manifest(
        args.checksum_manifest,
        filename=args.corpus.name,
    )
    corpus = load_corpus(args.corpus, expected_sha256=expected_checksum)
    report = run_live_benchmark(
        corpus,
        admin_dsn=args.admin_dsn,
        config=BenchmarkConfig(
            warmups=args.warmups,
            runs=args.runs,
            limit=args.limit,
        ),
    )
    serialized = canonical_report_json(report)
    digest = report_sha256(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialized, encoding="utf-8")
    checksum_path = args.output.with_suffix(".sha256")
    checksum_path.write_text(f"{digest}  {args.output.name}\n", encoding="ascii")
    print(f"wrote {args.output} ({digest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
