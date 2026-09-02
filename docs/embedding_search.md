# Versioned embedding search and qualification

CogniStore indexes normalized text passages as versioned embeddings in
PostgreSQL with pgvector. Source-byte chunks are content identity and
deduplication records; they are not text passages and must never be sent to an
embedding provider.

Every similarity query selects one explicit search space. A search-space
identity includes the provider implementation and version, model and immutable
model version, endpoint namespace for API-compatible providers, vector
dimensions, preprocessing, normalization, and distance metric. Whether an API
request explicitly asks for reduced dimensions is also part of that identity.
Re-embedding with another incompatible identity creates a different space;
queries do not infer a latest model or combine spaces.
Each object keeps an independent active passage layout per model space, so a
new model can adopt a different chunker without making the prior model's
citations disappear.

Passages have a separate identity from source-byte chunks. It includes the
source manifest and digest, extraction schema and MIME, parser implementation
and runtime, normalization contract, text digest, and codepoint chunker
configuration. This makes parser, extraction, and passage-layout upgrades
rebuildable without overwriting earlier provenance.

Embedding storage requires pgvector 0.8.0 or newer. Migration `0006_embeddings`
fails closed for an older platform-provided extension because filtered HNSW
search relies on iterative scans. Dense vectors are limited to 16,000
dimensions; HNSW spaces are limited to 2,000 dimensions by pgvector. Larger
dense spaces must use a different representation, while spaces between 2,001
and 16,000 dimensions can be registered only with HNSW disabled for exact
search.

## Index and search API

Install `cognistore[embeddings]` to use the local sentence-transformers
adapter. Its default loader accepts a Hub model ID and requires a full
40-character commit SHA; mutable local paths, including a same-named path that
otherwise looks like a Hub ID, are rejected at configuration and lazy-load
time. The default space identity also versions sentence-transformers,
Transformers, Tokenizers, PyTorch, and NumPy. A custom model factory may load a
local artifact only when `provider_implementation_version` is a
`sha256:<64 lowercase hex>` digest covering the adapter, model artifacts, and
inference runtime.
`OpenAICompatibleEmbeddingProvider` calls `/v1/embeddings`, requires an
immutable deployment version, isolates spaces by a credential-free endpoint
hash, and accepts unencrypted HTTP only for loopback development endpoints.
Set `request_dimensions=True` only when the remote API should receive the
dimension parameter; that wire contract has a distinct space identity.

```python
import os

from cognistore.core.embedding_index import (
    EmbeddingIndexer,
    SimilaritySearchFilters,
)
from cognistore.core.embeddings import SentenceTransformersEmbeddingProvider
from cognistore.db import PgVectorEmbeddingStore, SQLCatalog

provider = SentenceTransformersEmbeddingProvider(
    model=os.environ["COGNISTORE_EMBEDDING_MODEL"],
    revision=os.environ["COGNISTORE_EMBEDDING_MODEL_COMMIT"],
    dimensions=384,
)
with SQLCatalog(os.environ["COGNISTORE_CATALOG_DB"]) as catalog:
    indexer = EmbeddingIndexer(
        PgVectorEmbeddingStore(catalog),
        provider,
        batch_size=32,
    )
    indexed = indexer.index_object("documents", "policies/retention.pdf")
    hits = indexer.search(
        "Which records are under legal hold?",
        filters=SimilaritySearchFilters(
            buckets=frozenset({"documents"}),
            key_prefix="policies/",
            tiers=frozenset({"warm", "cold"}),
            mime_types=frozenset({"application/pdf"}),
            metadata={"department": "legal"},
        ),
        limit=10,
    )
```

Provider calls occur outside catalog transactions. Each completed batch is
upserted by `(space_id, passage_id)`, so a retry resumes only missing passage
IDs and cannot create duplicate vectors. `force=True` first removes the
object's active mapping plus the target completion gate and vectors
transactionally; a failed forced pass is hidden from search and a normal retry
resumes its durable new batches. A forced refresh is rejected with
`EmbeddingForceConflictError` when another object currently shares that exact
document/model space, so one alias cannot invalidate another alias's search
results. Reset, batch write, and completion transactions take the same
exclusive shared-document lock, so a force refresh cannot race a completion
gate.

Normal searches use the space-specific HNSW index. Set `exact=True` on
`EmbeddingIndexer.search` for a sequential exact baseline with globally
deterministic distance ties. HNSW results are deterministically ordered within
the returned overfetched candidate set, but candidate membership at a boundary
with more than four times the requested limit of exactly equal distances is an
approximate-index choice; use exact mode when that edge case matters.
Spaces registered with HNSW disabled automatically use that same global exact
ordering even when callers leave `exact` at its default.

Search filters are intentionally resource bounded: each bucket, tier, or MIME
collection accepts at most 256 values; metadata accepts at most 64 pairs;
individual values are capped at 4 KiB (metadata keys at 256 bytes); and the
combined filter payload is capped at 64 KiB. Bucket, tier, and key-prefix
filters preserve the catalog's NUL-safe identity semantics, while MIME and
JSON metadata filters require valid NUL-free UTF-8.

Result `object_metadata` is a citation-oriented projection, not a full catalog
record. PostgreSQL limits the raw serialized metadata envelope to 16 KiB before
transfer; larger records return `{}` with `object_metadata_truncated` set to
true. For bounded records, the internal `document_extraction` and
`content_identity` envelopes are removed before the result is exposed. This
two-stage projection preserves catalog JSON containing escaped NUL values
without allowing multi-megabyte extraction text to be duplicated across hits.
Each result also carries the source-content SHA-256 and full normalized-text
SHA-256 so higher-level retrieval can reject a hit if catalog content or its
current extraction changes between similarity search and citation hydration.
Metadata predicates still evaluate against the full catalog JSON. Use
`SQLCatalog.get()` when the complete catalog metadata record is required.
PostgreSQL cannot materialize fields from a legacy JSON object containing an
escaped NUL or lone Unicode surrogate as `TEXT`; such an object remains
searchable by identity, tier, MIME, and vector distance but is conservatively
excluded whenever a metadata predicate is present, rather than aborting the
whole query. Escape matching accounts for JSON backslash parity and removes
valid surrogate pairs first, so literal `\u0000`/`\uD800` text and valid
non-BMP characters such as emoji remain metadata-filterable.

## Semantic fixture corpus

The project-authored fixture at
`tests/fixtures/embeddings/corpus.json` contains 16 normalized passages from
eight fictional documents, eight semantic queries, metadata filters, and
relevance labels. It is intentionally small and deterministic. The fixture is
MIT licensed alongside its provenance notes and checksum manifest.

Verify it before loading:

```shell
cd tests/fixtures/embeddings
shasum -a 256 -c SHA256SUMS
```

The checksum identifies the exact corpus used by a report. A report for an
older checksum remains historical evidence and must not be presented as a
measurement of a modified fixture. Relevance labels describe expected semantic
matches; they are inputs, not measured results.

## Benchmark contract

`tests.perf.embedding_benchmark` is provider- and DAL-neutral. Its
`SimilarityBenchmarkAdapter` receives a prepared, single-model pgvector search
space. The adapter must cache query embeddings before measurement so provider
inference and remote API latency are excluded from database query latency.

An adapter supplies:

- indexed row count and vector dimensions;
- exact and HNSW search functions that apply each fixture query's filters;
- the non-secret search-space identity;
- PostgreSQL and pgvector versions;
- HNSW DDL, dimensions, operator class, `m`, `ef_construction`, and index size;
- query settings such as `hnsw.ef_search` and `hnsw.iterative_scan`; and
- `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` for a representative query.

The exact path must bypass approximate indexes while preserving the same
distance function, selected model space, filters, ordering, and result limit.
The HNSW path must use the production query shape. Isolate any session settings
used to force an exact baseline so they cannot leak into the approximate
samples.

The neutral callable harness is:

```python
from tests.perf.embedding_benchmark import (
    BenchmarkConfig,
    checksum_from_manifest,
    load_corpus,
    run_similarity_benchmark,
)

fixture = "tests/fixtures/embeddings/corpus.json"
checksum = checksum_from_manifest(
    "tests/fixtures/embeddings/SHA256SUMS",
    filename="corpus.json",
)
corpus = load_corpus(fixture, expected_sha256=checksum)

# Construct and prepare a SimilarityBenchmarkAdapter through the embedding DAL.
# Preparation embeds passages and queries before the timed calls begin.
adapter = prepared_embedding_benchmark_adapter

report = run_similarity_benchmark(
    corpus,
    adapter,
    config=BenchmarkConfig(warmups=5, runs=20, limit=10),
)
```

`tests.perf.pgvector_embedding_benchmark.PgVectorBenchmarkAdapter` is the live
implementation. It publishes each fixture passage as one unique source object,
uses the production indexing and filtered-search DAL, caches deterministic
L2-normalized query vectors, and maps production citations back to fixture
passage IDs. Its exact path uses the production transaction-local exact-search
switch; its approximate path uses the normal HNSW settings and query shape.

Run the retained Compose qualification from the repository root. Supply the
local database password through the PostgreSQL client environment; do not put
it in the DSN or retained report:

```shell
COGNISTORE_POSTGRES_PORT=55433 docker compose up -d postgres
export PGPASSWORD="${COGNISTORE_POSTGRES_PASSWORD:?set the local Compose password}"
export COGNISTORE_BENCHMARK_POSTGRES_DSN="postgresql://cognistore@127.0.0.1:55433/postgres"
.venv/bin/python -m tests.perf.pgvector_embedding_benchmark \
  --warmups 5 --runs 20 --limit 10 \
  --output tests/perf/results/pgvector-compose-report.json
(cd tests/perf/results && shasum -a 256 -c pgvector-compose-report.sha256)
```

The runner creates a randomly named sibling database from the administrator
DSN, migrates it, performs the run, and forcibly drops it in a `finally` block.
Neither the DSN nor credentials are copied into evidence.

## Report contents

The harness returns canonical JSON-safe data containing:

- corpus path, SHA-256, license, passage rows, and query count;
- indexed rows, dimensions, warmups, runs, limit, and measured query count;
- PostgreSQL and pgvector versions;
- provider/model/search-space identity without credentials;
- HNSW definition, build options, dimensions, and byte size;
- relevant pgvector query settings and a JSON execution plan;
- minimum, p50, p95, p99, and maximum materialized-query latency; and
- per-query and aggregate HNSW recall against exact pgvector results.

Percentiles use the same nearest-rank method as CogniStore's move qualification.
Latency begins immediately before the prepared approximate database call and
ends after its results are materialized. Fixture loading, embedding generation,
warmups, exact baseline queries, and `EXPLAIN` are excluded.

This report is descriptive rather than a machine-independent pass threshold. A
small corpus may lead PostgreSQL to choose a sequential scan even when the HNSW
index is valid; the retained execution plan makes that choice visible instead
of forcing a favorable result. Recall against the exact path remains required.

## Live-run integration checklist

The production adapter uses an isolated migrated test database and:

1. verify the fixture checksum;
2. register one explicit, immutable model space and index all 16 passages;
3. pre-embed all eight queries with that same provider identity;
4. verify the prepared row count and dimension before timing;
5. obtain exact top-k results for each filtered query;
6. perform configured warmups and timed HNSW runs;
7. collect versions, index definition and size, query settings, and the actual
   JSON execution plan; and
8. serialize the report canonically and retain its SHA-256 with any accepted
   evidence.

Use `SELECT version()` for PostgreSQL, `pg_extension.extversion` for pgvector,
`pg_get_indexdef` for canonical index DDL, and `pg_relation_size` for index
bytes. Redact database locators, authorization headers, API keys, and internal
endpoint credentials before retaining a report.

The canonical live report and its SHA-256 are retained under
`tests/perf/results/`. Regenerate both together whenever the fixture, production
query shape, HNSW configuration, PostgreSQL/pgvector version, or deterministic
benchmark provider changes. Report values must always come from a live isolated
database run; never fill fields with samples or estimates.
