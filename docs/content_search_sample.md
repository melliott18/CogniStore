# Content-search UI and sample corpus

CogniStore includes a same-origin browser UI and a small, project-authored
corpus that exercise the M2 discovery path from object ingestion through
citations. The sample runs without model credentials or network model downloads.
It uses PostgreSQL/pgvector for vector persistence, Tantivy for keyword search,
and the production PDF/DOCX extraction and Ask orchestration code.

## Start from a fresh checkout

Build the runtime image, load the checked corpus, and start its API/UI:

```shell
docker compose --profile sample up --build --wait sample-api
```

Open [http://127.0.0.1:8080/ui/](http://127.0.0.1:8080/ui/). Set
`COGNISTORE_SAMPLE_API_PORT` before the command to use a different host port.
The `sample-loader` dependency completes before `sample-api` starts, so the
first query never races corpus indexing.

The loader is idempotent. It writes the four manifest objects, performs stable
catalog scans and document extraction, atomically rebuilds the keyword index,
and indexes normalized passages into one versioned pgvector space. An ordinary
stop retains the catalog, objects, and indexes:

```shell
docker compose --profile sample down
```

To intentionally remove **all** data in the repository-managed Compose volumes,
including non-sample development data, use `docker compose down --volumes`.

## Try each query mode

The Search view has two provider-selectable modes:

- **Keyword** requests `metadata+keyword` retrieval. Try
  `quarterly restore drill` to find both coordinates that reference the same
  DOCX bytes.
- **Vector** requests `metadata+vector` retrieval. Try
  `How does an isolated restore prove backup recovery?` with tier `hot`.
- **Ask** requests the full `metadata+keyword+vector` mode and answer
  synthesis. Try `What prevents contract deletion during a legal hold?` with
  MIME type `application/pdf`.

Every result exposes its provider ranks and raw scores, object citation,
passage citations and offsets, current object/document metadata, and content
SHA-256. **Download cited object** resolves the citation through the versioned
object API rather than reading a storage path directly; active content is
forced to an attachment instead of executing in the UI origin.

Filters are exact and share the Ask v1 contract: bucket, key prefix, tier, MIME
type, size, content SHA-256, scalar object metadata, and allowlisted document
metadata. The UI displays metadata truncation explicitly.

The **Placement** view also supports [policy explanations](placement_explanations.md):
preview a policy against a catalog object, inspect its retained decisions, or
follow a policy job's decisions through execution. Proposed placement changes
are labeled separately from completed moves. Previews work without a worker;
execution history requires retained policy decisions from actual policy runs.

## Corpus provenance and duplicate content

The packaged manifest lives in
`cognistore/samples/content_search/assets/manifest.json`. It describes two PDFs,
one DOCX, and four stored coordinates. The backup DOCX is intentionally loaded
under both `runbooks/database-backups.docx` and
`z-archive/database-backups-copy.docx`; both coordinates have identical bytes,
content hashes, canonical content identity, and embedding document identity.

All visible text and document metadata were written for CogniStore and are
licensed under MIT. `SHA256SUMS` covers the manifest and every unique binary.
Verify the installed assets without a database:

```shell
cognistore-sample verify
```

The offline sample embedding provider uses deterministic, normalized token and
bigram feature hashing. It exists solely to make the complete pgvector workflow
repeatable without credentials; it is not represented as a production semantic
model. Production deployments should inject a configured embedding and answer
provider into `AskService`.

## Run the loader and server directly

The Compose profile is the shortest clean-environment path. The installed CLI
can also target a PostgreSQL/pgvector catalog and a three-tier driver file:

```shell
export COGNISTORE_DRIVERS=/path/to/sample-drivers.yaml
export COGNISTORE_CATALOG_DB='postgresql://cognistore@127.0.0.1:55432/cognistore'
export COGNISTORE_KEYWORD_INDEX=/tmp/cognistore-sample-keyword
export PGPASSWORD='<local database password>'

cognistore-sample load
cognistore-sample serve --host 127.0.0.1 --port 8080
```

The driver file must define the `hot`, `warm`, and `cold` tiers named by the
manifest. `docker/sample-drivers.yaml` is the container example. Use
`cognistore-sample serve --load` to refresh the corpus in the same process
before serving.

## Provider and failure states

The UI distinguishes four outcomes:

- a pending request shows a loading state and prevents duplicate submission;
- a successful response with no matching provider-backed results shows an
  empty state;
- missing or unavailable keyword, vector, or generation providers produce a
  degraded warning while any remaining ranked citations stay visible; and
- validation, API, and transport failures produce an error state. API failures
  include the safe request ID when one is available.

Search requests use the optional `retrieval_mode` field on `POST /v1/ask`.
Omitting it preserves the original hybrid default. Providers outside the
requested mode report `not_requested`; a requested provider that is missing or
unavailable degrades the response to the signals that actually succeeded.

## Automated reduced-scale workflow

The integration workflow creates a new isolated PostgreSQL database, loads all
four objects, verifies exact-byte deduplication and embedding reuse, runs
keyword, exact-vector, and Ask requests through FastAPI, validates generated
citations, and downloads the top cited object through `/v1/objects`. It then
reloads the corpus to prove idempotent index repair and vector reuse.

Run it against an administrative test database:

```shell
export COGNISTORE_TEST_POSTGRES_DSN='postgresql://cognistore@127.0.0.1:55432/postgres'
export PGPASSWORD='<local database password>'
python -m pytest tests/integration/test_content_search_sample.py
```

CI supplies an isolated PostgreSQL/pgvector service and runs this reduced-scale
workflow automatically. Without `COGNISTORE_TEST_POSTGRES_DSN`, the integration
test skips without weakening the default local unit suite.
