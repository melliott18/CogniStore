"""Provider- and DAL-neutral similarity benchmark and report utilities.

The production embedding DAL is intentionally injected through
``SimilarityBenchmarkAdapter``.  Keeping orchestration here independent of a
particular provider lets the same fixture compare exact pgvector results with
the configured HNSW search space without including model or network latency in
the measured query samples.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TypeAlias

CORPUS_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1

JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


class EmbeddingBenchmarkError(ValueError):
    """The fixture, adapter, or measured result violated the benchmark contract."""


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EmbeddingBenchmarkError(f"{field} must be a non-empty string")
    return value


def _positive_integer(value: object, field: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise EmbeddingBenchmarkError(f"{field} must be a {qualifier} integer")
    return value


def _json_value(value: object, field: str) -> JSONValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EmbeddingBenchmarkError(f"{field} contains a non-finite number")
        return value
    if isinstance(value, list):
        return [_json_value(item, f"{field}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        output: dict[str, JSONValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise EmbeddingBenchmarkError(f"{field} contains a non-string key")
            output[key] = _json_value(item, f"{field}.{key}")
        return output
    raise EmbeddingBenchmarkError(f"{field} contains a non-JSON value")


def _strict_object(
    value: object,
    *,
    field: str,
    expected: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EmbeddingBenchmarkError(f"{field} must be a JSON object")
    missing = sorted(expected.difference(value))
    extra = sorted(set(value).difference(expected))
    if missing:
        raise EmbeddingBenchmarkError(f"{field} is missing fields: {', '.join(missing)}")
    if extra:
        raise EmbeddingBenchmarkError(f"{field} has unknown fields: {', '.join(extra)}")
    return value


@dataclass(frozen=True)
class CorpusPassage:
    passage_id: str
    document_id: str
    bucket: str
    object_key: str
    tier: str
    mime: str
    metadata: Mapping[str, JSONValue]
    text: str

    def __post_init__(self) -> None:
        for field in (
            "passage_id",
            "document_id",
            "bucket",
            "object_key",
            "tier",
            "mime",
            "text",
        ):
            _required_string(getattr(self, field), f"passage.{field}")
        normalized = _json_value(self.metadata, "passage.metadata")
        if not isinstance(normalized, dict):
            raise EmbeddingBenchmarkError("passage.metadata must be a JSON object")
        object.__setattr__(self, "metadata", normalized)


@dataclass(frozen=True)
class CorpusQuery:
    query_id: str
    text: str
    relevant_passage_ids: tuple[str, ...]
    filters: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        _required_string(self.query_id, "query.query_id")
        _required_string(self.text, "query.text")
        if not self.relevant_passage_ids:
            raise EmbeddingBenchmarkError(
                "query.relevant_passage_ids must contain at least one passage"
            )
        for passage_id in self.relevant_passage_ids:
            _required_string(passage_id, "query.relevant_passage_ids[]")
        if len(set(self.relevant_passage_ids)) != len(self.relevant_passage_ids):
            raise EmbeddingBenchmarkError("query.relevant_passage_ids must not contain duplicates")
        normalized = _json_value(self.filters, "query.filters")
        if not isinstance(normalized, dict):
            raise EmbeddingBenchmarkError("query.filters must be a JSON object")
        object.__setattr__(self, "filters", normalized)


@dataclass(frozen=True)
class EmbeddingBenchmarkCorpus:
    source_path: Path
    sha256: str
    name: str
    license: str
    description: str
    passages: tuple[CorpusPassage, ...]
    queries: tuple[CorpusQuery, ...]

    def __post_init__(self) -> None:
        _required_string(self.name, "corpus.name")
        _required_string(self.license, "corpus.license")
        _required_string(self.description, "corpus.description")
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise EmbeddingBenchmarkError("corpus SHA-256 must be canonical lowercase hex")
        if not self.passages:
            raise EmbeddingBenchmarkError("corpus must contain at least one passage")
        if not self.queries:
            raise EmbeddingBenchmarkError("corpus must contain at least one query")

        passage_ids = [passage.passage_id for passage in self.passages]
        if len(set(passage_ids)) != len(passage_ids):
            raise EmbeddingBenchmarkError("corpus passage IDs must be unique")
        query_ids = [query.query_id for query in self.queries]
        if len(set(query_ids)) != len(query_ids):
            raise EmbeddingBenchmarkError("corpus query IDs must be unique")

        known_passages = set(passage_ids)
        for query in self.queries:
            missing = sorted(set(query.relevant_passage_ids).difference(known_passages))
            if missing:
                raise EmbeddingBenchmarkError(
                    f"query {query.query_id!r} references unknown passages: {', '.join(missing)}"
                )


_CORPUS_FIELDS = frozenset(
    {"schema_version", "name", "license", "description", "passages", "queries"}
)
_PASSAGE_FIELDS = frozenset(
    {
        "passage_id",
        "document_id",
        "bucket",
        "object_key",
        "tier",
        "mime",
        "metadata",
        "text",
    }
)
_QUERY_FIELDS = frozenset({"query_id", "text", "relevant_passage_ids", "filters"})


def load_corpus(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
) -> EmbeddingBenchmarkCorpus:
    """Load and strictly validate one project-authored semantic corpus."""

    source_path = Path(path)
    payload = source_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise EmbeddingBenchmarkError(
            f"corpus checksum mismatch: expected {expected_sha256}, observed {digest}"
        )
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EmbeddingBenchmarkError("corpus must be UTF-8 JSON") from exc
    corpus = _strict_object(document, field="corpus", expected=_CORPUS_FIELDS)
    if corpus["schema_version"] != CORPUS_SCHEMA_VERSION:
        raise EmbeddingBenchmarkError(
            f"unsupported corpus schema_version {corpus['schema_version']!r}"
        )
    if not isinstance(corpus["passages"], list):
        raise EmbeddingBenchmarkError("corpus.passages must be a list")
    if not isinstance(corpus["queries"], list):
        raise EmbeddingBenchmarkError("corpus.queries must be a list")

    passages: list[CorpusPassage] = []
    for index, raw_passage in enumerate(corpus["passages"]):
        passage = _strict_object(
            raw_passage,
            field=f"corpus.passages[{index}]",
            expected=_PASSAGE_FIELDS,
        )
        passages.append(
            CorpusPassage(
                passage_id=passage["passage_id"],
                document_id=passage["document_id"],
                bucket=passage["bucket"],
                object_key=passage["object_key"],
                tier=passage["tier"],
                mime=passage["mime"],
                metadata=passage["metadata"],
                text=passage["text"],
            )
        )

    queries: list[CorpusQuery] = []
    for index, raw_query in enumerate(corpus["queries"]):
        query = _strict_object(
            raw_query,
            field=f"corpus.queries[{index}]",
            expected=_QUERY_FIELDS,
        )
        raw_relevant = query["relevant_passage_ids"]
        if not isinstance(raw_relevant, list) or any(
            not isinstance(passage_id, str) for passage_id in raw_relevant
        ):
            raise EmbeddingBenchmarkError(
                f"corpus.queries[{index}].relevant_passage_ids must be a list of strings"
            )
        queries.append(
            CorpusQuery(
                query_id=query["query_id"],
                text=query["text"],
                relevant_passage_ids=tuple(raw_relevant),
                filters=query["filters"],
            )
        )

    return EmbeddingBenchmarkCorpus(
        source_path=source_path,
        sha256=digest,
        name=corpus["name"],
        license=corpus["license"],
        description=corpus["description"],
        passages=tuple(passages),
        queries=tuple(queries),
    )


def checksum_from_manifest(path: str | Path, *, filename: str) -> str:
    """Read one SHA256SUMS entry without accepting ambiguous duplicates."""

    matches: list[str] = []
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 2:
            raise EmbeddingBenchmarkError("checksum manifest contains a malformed entry")
        digest, entry_name = fields
        if entry_name.lstrip("*") == filename:
            matches.append(digest)
    if len(matches) != 1:
        raise EmbeddingBenchmarkError(
            f"checksum manifest must contain exactly one entry for {filename!r}"
        )
    digest = matches[0]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise EmbeddingBenchmarkError("checksum manifest contains an invalid SHA-256")
    return digest


@dataclass(frozen=True)
class SearchHit:
    passage_id: str
    distance: float

    def __post_init__(self) -> None:
        _required_string(self.passage_id, "search hit passage_id")
        if (
            isinstance(self.distance, bool)
            or not isinstance(self.distance, (int, float))
            or not math.isfinite(self.distance)
            or self.distance < 0
        ):
            raise EmbeddingBenchmarkError(
                "search hit distance must be a non-negative finite number"
            )


@dataclass(frozen=True)
class DatabaseVersions:
    postgres: str
    pgvector: str

    def __post_init__(self) -> None:
        _required_string(self.postgres, "PostgreSQL version")
        _required_string(self.pgvector, "pgvector version")


@dataclass(frozen=True)
class HnswIndexConfiguration:
    index_name: str
    ddl: str
    dimensions: int
    m: int
    ef_construction: int
    index_size_bytes: int
    operator_class: str = "vector_cosine_ops"

    def __post_init__(self) -> None:
        _required_string(self.index_name, "HNSW index name")
        _required_string(self.ddl, "HNSW index DDL")
        _required_string(self.operator_class, "HNSW operator class")
        _positive_integer(self.dimensions, "HNSW dimensions")
        _positive_integer(self.m, "HNSW m")
        _positive_integer(self.ef_construction, "HNSW ef_construction")
        _positive_integer(
            self.index_size_bytes,
            "HNSW index_size_bytes",
            allow_zero=True,
        )


class SimilarityBenchmarkAdapter(Protocol):
    """Prepared pgvector search space consumed by the benchmark harness.

    Implementations must pre-embed the fixture queries.  ``exact_search`` and
    ``approximate_search`` must measure database retrieval only, not provider
    inference or network time.
    """

    @property
    def indexed_rows(self) -> int: ...

    @property
    def dimensions(self) -> int: ...

    def exact_search(self, query: CorpusQuery, *, limit: int) -> Sequence[SearchHit]: ...

    def approximate_search(
        self,
        query: CorpusQuery,
        *,
        limit: int,
    ) -> Sequence[SearchHit]: ...

    def database_versions(self) -> DatabaseVersions: ...

    def hnsw_index_configuration(self) -> HnswIndexConfiguration: ...

    def search_space_metadata(self) -> Mapping[str, JSONValue]: ...

    def query_settings(self) -> Mapping[str, JSONValue]: ...

    def explain_approximate(self, query: CorpusQuery, *, limit: int) -> JSONValue: ...


@dataclass(frozen=True)
class BenchmarkConfig:
    warmups: int = 5
    runs: int = 20
    limit: int = 10

    def __post_init__(self) -> None:
        _positive_integer(self.warmups, "warmups", allow_zero=True)
        _positive_integer(self.runs, "runs")
        _positive_integer(self.limit, "limit")


def _percentile(values: Sequence[float], percentile: int) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil((percentile / 100) * len(ordered)))
    return ordered[rank - 1]


def latency_summary(values: Sequence[float]) -> dict[str, float | int]:
    """Return nearest-rank latency percentiles used by CogniStore reports."""

    if not values:
        raise EmbeddingBenchmarkError("at least one latency sample is required")
    normalized: list[float] = []
    for value in values:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise EmbeddingBenchmarkError("latency samples must be non-negative finite numbers")
        normalized.append(float(value))
    return {
        "sample_count": len(normalized),
        "min": min(normalized),
        "p50": _percentile(normalized, 50),
        "p95": _percentile(normalized, 95),
        "p99": _percentile(normalized, 99),
        "max": max(normalized),
    }


def _validated_hits(
    hits: Sequence[SearchHit],
    *,
    known_passages: frozenset[str],
    limit: int,
    operation: str,
) -> tuple[SearchHit, ...]:
    output = tuple(hits)
    if len(output) > limit:
        raise EmbeddingBenchmarkError(f"{operation} returned more than limit={limit} hits")
    if any(not isinstance(hit, SearchHit) for hit in output):
        raise EmbeddingBenchmarkError(f"{operation} returned a non-SearchHit value")
    passage_ids = [hit.passage_id for hit in output]
    if len(set(passage_ids)) != len(passage_ids):
        raise EmbeddingBenchmarkError(f"{operation} returned duplicate passage IDs")
    unknown = sorted(set(passage_ids).difference(known_passages))
    if unknown:
        raise EmbeddingBenchmarkError(
            f"{operation} returned unknown passages: {', '.join(unknown)}"
        )
    return output


def _recall(exact: Sequence[SearchHit], approximate: Sequence[SearchHit]) -> float:
    exact_ids = {hit.passage_id for hit in exact}
    if not exact_ids:
        raise EmbeddingBenchmarkError("exact search returned no rows; recall is undefined")
    approximate_ids = {hit.passage_id for hit in approximate}
    return len(exact_ids.intersection(approximate_ids)) / len(exact_ids)


def _utc_timestamp(value: datetime | None) -> str:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise EmbeddingBenchmarkError("generated_at must include a timezone")
    return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _portable_fixture_path(path: Path) -> str:
    """Avoid retaining machine-specific checkout prefixes in evidence."""

    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return path.name


def run_similarity_benchmark(
    corpus: EmbeddingBenchmarkCorpus,
    adapter: SimilarityBenchmarkAdapter,
    *,
    config: BenchmarkConfig | None = None,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
    generated_at: datetime | None = None,
) -> dict[str, JSONValue]:
    """Measure prepared HNSW searches and compare them with exact pgvector results."""

    active_config = config or BenchmarkConfig()
    indexed_rows = _positive_integer(adapter.indexed_rows, "adapter indexed_rows")
    dimensions = _positive_integer(adapter.dimensions, "adapter dimensions")
    if indexed_rows != len(corpus.passages):
        raise EmbeddingBenchmarkError(
            "prepared search space row count does not match the corpus: "
            f"{indexed_rows} != {len(corpus.passages)}"
        )

    versions = adapter.database_versions()
    if not isinstance(versions, DatabaseVersions):
        raise EmbeddingBenchmarkError("adapter returned invalid database versions")
    index = adapter.hnsw_index_configuration()
    if not isinstance(index, HnswIndexConfiguration):
        raise EmbeddingBenchmarkError("adapter returned invalid HNSW configuration")
    if index.dimensions != dimensions:
        raise EmbeddingBenchmarkError(
            "HNSW index dimensions do not match the prepared search space"
        )
    search_space = _json_value(adapter.search_space_metadata(), "search space")
    if not isinstance(search_space, dict):
        raise EmbeddingBenchmarkError("search space metadata must be a JSON object")
    required_space_fields = {
        "space_id",
        "fingerprint",
        "provider_implementation",
        "provider_implementation_version",
        "model",
        "model_revision",
        "dimensions",
        "distance_metric",
        "normalization",
    }
    missing_space_fields = sorted(required_space_fields.difference(search_space))
    if missing_space_fields:
        raise EmbeddingBenchmarkError(
            f"search space metadata is missing fields: {', '.join(missing_space_fields)}"
        )
    for field in required_space_fields.difference({"dimensions"}):
        _required_string(search_space[field], f"search space {field}")
    if search_space["dimensions"] != dimensions:
        raise EmbeddingBenchmarkError(
            "search space dimensions do not match the prepared search space"
        )
    query_settings = _json_value(adapter.query_settings(), "query settings")
    if not isinstance(query_settings, dict):
        raise EmbeddingBenchmarkError("query settings must be a JSON object")

    known_passages = frozenset(passage.passage_id for passage in corpus.passages)
    exact_by_query: dict[str, tuple[SearchHit, ...]] = {}
    for query in corpus.queries:
        exact_by_query[query.query_id] = _validated_hits(
            adapter.exact_search(query, limit=active_config.limit),
            known_passages=known_passages,
            limit=active_config.limit,
            operation=f"exact search for {query.query_id!r}",
        )

    for _ in range(active_config.warmups):
        for query in corpus.queries:
            _validated_hits(
                adapter.approximate_search(query, limit=active_config.limit),
                known_passages=known_passages,
                limit=active_config.limit,
                operation=f"HNSW warmup for {query.query_id!r}",
            )

    latency_samples_ms: list[float] = []
    recall_by_query: dict[str, list[float]] = {query.query_id: [] for query in corpus.queries}
    last_approximate: dict[str, tuple[SearchHit, ...]] = {}
    for _ in range(active_config.runs):
        for query in corpus.queries:
            started = clock_ns()
            approximate = _validated_hits(
                adapter.approximate_search(query, limit=active_config.limit),
                known_passages=known_passages,
                limit=active_config.limit,
                operation=f"HNSW search for {query.query_id!r}",
            )
            finished = clock_ns()
            if finished < started:
                raise EmbeddingBenchmarkError("benchmark clock moved backwards")
            latency_samples_ms.append((finished - started) / 1_000_000)
            exact = exact_by_query[query.query_id]
            recall_by_query[query.query_id].append(_recall(exact, approximate))
            last_approximate[query.query_id] = approximate

    per_query_recall: list[JSONValue] = []
    all_recall_samples: list[float] = []
    for query in corpus.queries:
        samples = recall_by_query[query.query_id]
        all_recall_samples.extend(samples)
        exact = exact_by_query[query.query_id]
        approximate = last_approximate[query.query_id]
        per_query_recall.append(
            {
                "query_id": query.query_id,
                "samples": len(samples),
                "min": min(samples),
                "mean": sum(samples) / len(samples),
                "max": max(samples),
                "exact_passage_ids": [hit.passage_id for hit in exact],
                "last_approximate_passage_ids": [hit.passage_id for hit in approximate],
            }
        )

    representative_query = corpus.queries[0]
    explain = _json_value(
        adapter.explain_approximate(
            representative_query,
            limit=active_config.limit,
        ),
        "EXPLAIN plan",
    )

    report: dict[str, JSONValue] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "benchmark": "pgvector-filtered-similarity",
        "generated_at": _utc_timestamp(generated_at),
        "corpus": {
            "name": corpus.name,
            "fixture_path": _portable_fixture_path(corpus.source_path),
            "sha256": corpus.sha256,
            "license": corpus.license,
            "rows": len(corpus.passages),
            "query_count": len(corpus.queries),
        },
        "workload": {
            "indexed_rows": indexed_rows,
            "dimensions": dimensions,
            "warmups_per_query": active_config.warmups,
            "runs_per_query": active_config.runs,
            "limit": active_config.limit,
            "measured_query_count": len(latency_samples_ms),
        },
        "database": {
            "postgres_version": versions.postgres,
            "pgvector_version": versions.pgvector,
        },
        "hnsw_index": {
            "index_name": index.index_name,
            "ddl": index.ddl,
            "dimensions": index.dimensions,
            "operator_class": index.operator_class,
            "m": index.m,
            "ef_construction": index.ef_construction,
            "index_size_bytes": index.index_size_bytes,
        },
        "search_space": search_space,
        "query_settings": query_settings,
        "latency_ms": {
            **latency_summary(latency_samples_ms),
            "definition": (
                "prepared approximate pgvector query through complete result materialization; "
                "provider inference, fixture loading, exact baseline queries, and EXPLAIN are "
                "excluded"
            ),
        },
        "recall_vs_exact": {
            "metric": f"recall@{active_config.limit}",
            "sample_count": len(all_recall_samples),
            "min": min(all_recall_samples),
            "mean": sum(all_recall_samples) / len(all_recall_samples),
            "max": max(all_recall_samples),
            "per_query": per_query_recall,
        },
        "explain": {
            "query_id": representative_query.query_id,
            "format": "json",
            "analyze": True,
            "buffers": True,
            "plan": explain,
        },
    }
    # Fail here instead of producing a partial or non-portable report.
    json.dumps(report, allow_nan=False, sort_keys=True)
    return report


def canonical_report_json(report: Mapping[str, JSONValue]) -> str:
    """Serialize a report deterministically for evidence checksums."""

    normalized = _json_value(report, "report")
    if not isinstance(normalized, dict):
        raise EmbeddingBenchmarkError("report must be a JSON object")
    return (
        json.dumps(
            normalized,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def report_sha256(report: Mapping[str, JSONValue]) -> str:
    return hashlib.sha256(canonical_report_json(report).encode("utf-8")).hexdigest()
