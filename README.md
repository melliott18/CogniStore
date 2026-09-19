# CogniStore
AI-Powered Data Lifecycle Manager

## Quickstart

CogniStore supports CPython 3.10 through 3.14. CI exercises every supported
minor version.

Production is the default security profile: data connections require verified
TLS and persistent volumes require current encryption attestations. Follow the
[encryption deployment and recovery guide](docs/encryption.md) before deploying.
The local Docker stack explicitly selects the isolated `development` profile.

### Docker stack

Start the non-root CogniStore worker, PostgreSQL with pgvector, file-backed
NATS JetStream, and MinIO from a clean checkout:

```bash
docker compose up --build --wait
```

Run every integration test inside the same isolated stack with one command:

```bash
docker compose --profile integration up --build \
  --abort-on-container-exit \
  --exit-code-from integration-tests
```

Named volumes retain the PostgreSQL catalog, SQLite scheduler state, storage
tiers, JetStream state, MinIO data, and test report across ordinary stops. See
the [Docker development and integration guide](docs/setup_guide.md) for service
URLs, credentials, an editable development shell, safe shutdown diagnostics,
and the explicit data-reset command.

Add the optional [local observability stack](docs/observability.md) for
Prometheus metrics, provisioned Grafana dashboards, redacted JSON logs, and
OpenTelemetry traces from API requests through queued work and storage:

```bash
COGNISTORE_OTEL_ENABLED=true docker compose --profile observability up --build --wait
```

The [operational SLOs and runbooks](docs/operational_slos.md) cover rolling error
budgets, throughput, capacity, and policy cost/carbon alerts. The provisioned SLO
dashboard and [M1 evidence evaluator](docs/slo_model.md) use the versioned model.

### Kubernetes

The [Helm deployment guide](docs/kubernetes.md) covers production API/UI and
worker deployments, external persistent dependencies, migrations, upgrades,
rollback, and API/queue autoscaling. It also documents the single-node storage
constraint for the optional recurring scheduler and the automated cluster
qualification workflow.

### Local Python environment

- Create a virtual environment and install the package with its development
  tools:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,azure]"
# Isolated local tutorials and emulators use explicit development settings.
export COGNISTORE_SECURITY_PROFILE=development
```

Content-aware MIME detection uses the native libmagic library. The Docker
images include it; for host development install `libmagic1` on Debian/Ubuntu
or `file-libs` on Fedora/RHEL, or install `libmagic` with Homebrew. Windows
host installs need a compatible libmagic DLL visible to `python-magic`;
without one, and whenever libmagic cannot be loaded on any platform, scans
continue with filename-based inference and record that fallback in catalog
provenance.

- Run the same quality gates used by pull requests:

```bash
python -m ruff check .
python -m mypy cognistore
python -m pytest tests/unit tests/conformance
python -m pytest tests/integration
python -m pytest \
  --cov=cognistore --cov-report=term-missing --cov-report=xml
python -m bandit -c pyproject.toml -r cognistore -ll -ii
python -m pip_audit .
python -m build
python -m twine check dist/*
```

The repository configures collision-safe import identities, so plain
`python -m pytest` collects and runs the complete default suite. The coverage
command enforces the repository's 80% minimum. Integration tests that require
NATS, MinIO, Azurite/Azure Blob, GCS, or PostgreSQL skip unless their documented
environment variables point to isolated test services; filesystem and SQLite
catalog coverage runs without external services. Install Gitleaks separately and run
`gitleaks git --redact .` to perform the same secret scan used in CI.

For deterministic POSIX/S3 throughput, tail-latency, integrity, and injected
failure-recovery evidence, use the
[move scale and recovery qualification guide](docs/scale_qualification.md).
The canonical M1 campaign passed with one million objects on each required path;
the complete report and checksum are retained in the
[M1 closeout evidence](docs/evidence/m1/README.md). CI continues to exercise the
same harness at reduced scale.

For a runtime-only install, use `python -m pip install .`. The generated
`cognistore` command and `python -m cognistore.cli` invoke the same CLI.
Azure Blob storage additionally requires `python -m pip install '.[azure]'`;
use `'.[dev,azure]'` for its development and conformance tests.

- Try the POSIX driver via CLI

```bash
# Put and get a file using the filesystem as storage
python -m cognistore.cli --base /tmp/cognistore put demo-bucket path/to/key.txt README.md
python -m cognistore.cli --base /tmp/cognistore get demo-bucket path/to/key.txt /tmp/out.txt
python -m cognistore.cli --base /tmp/cognistore ls demo-bucket --prefix path/
```

Global YAML configuration and named profiles can replace repeated options. For
example, after creating the v1 `local` profile shown in the
[CLI reference](docs/cli.md):

```bash
cognistore --profile local move hot warm demo-bucket path/to/key.txt \
  --idempotency-key manual:path-to-key --dry-run --json
```

The reference also defines configuration precedence, the JSON/stdout contract,
the command-by-command dry-run matrix, durable manual-move recovery, and the
read-only shared-content reference report.

### REST API

Start the version 1 FastAPI service against the same driver and catalog
configuration used by workers:

```bash
COGNISTORE_SECURITY_PROFILE=development \
COGNISTORE_DRIVERS=./drivers.yaml \
COGNISTORE_CATALOG_DB=./catalog.sqlite3 \
cognistore-api --host 127.0.0.1 --port 8080
```

The service exposes physical objects, bounded catalog pages, grounded Ask,
policy evaluation, queued scans and policy runs, and durable job polling under
`/v1`. See the [REST API reference](docs/rest_api.md) and the checked
[OpenAPI 3.1 contract](docs/openapi/v1.json).

The local development example permits anonymous requests. Set `COGNISTORE_AUTH_ISSUER`
and `COGNISTORE_AUTH_AUDIENCE` to require JWT access tokens on every `/v1`
request, and set `COGNISTORE_AUTHORIZATION_POLICY` to a JSON role-binding file
to grant protected operations. Authenticated deployments without a policy deny
access. The [authorization guide](docs/authorization.md) documents roles, the
complete permission matrix, and worker revalidation.
[Authentication setup](docs/authentication.md) covers OIDC discovery,
JWKS rotation, service clients, and token-free job and audit attribution.

For multiple tenants, set `COGNISTORE_TENANT_POLICY` on the API and workers.
The [tenant isolation guide](docs/tenancy.md) covers server-owned membership,
separate catalog/index partitions, storage namespaces, and worker revalidation.
Roles grant access only within the assigned tenant.

Optional [PII detection and policy hooks](docs/pii_detection.md) classify extracted
PDF/DOCX text with bounded, replaceable detectors. Tenant-specific scan settings
retain redacted classifications, and PII-aware policies hold placement when
detection is unknown.

### Python SDK

The typed synchronous SDK covers every REST API v1 operation plus automatic
catalog pagination and asynchronous job polling. It ships in the default
`cognistore` installation:

```python
from cognistore.sdk import AskRequest, CogniStoreClient

with CogniStoreClient("http://127.0.0.1:8080") as client:
    answer = client.ask(AskRequest(text="Which documents explain tiering?"))

print(answer.model_dump_json(indent=2))
```

See the [Python SDK guide](docs/python_sdk.md) for installation, configuration,
complete method coverage, pagination, typed errors, action polling, and the API
v1 compatibility and deprecation policy. A minimal runnable query is in
[`examples/python_sdk_query.py`](examples/python_sdk_query.py).

### Content-search UI and sample corpus

Run the complete offline discovery sample from a fresh checkout:

```bash
docker compose --profile sample up --build --wait sample-api
```

Then open [http://127.0.0.1:8080/ui/](http://127.0.0.1:8080/ui/). The profile
loads a checked, MIT-licensed PDF/DOCX corpus (including exact duplicate
content), extracts and indexes it with Tantivy and pgvector, and serves Search
and Ask views with filters, ranked evidence, provider diagnostics, citations,
metadata, and cited-object links. The included offline embedding and answer
providers are deterministic sample implementations, not production models.

See the [content-search sample guide](docs/content_search_sample.md) for the
three query modes, direct CLI workflow, corpus provenance, state behavior, and
reduced-scale end-to-end test. Verify packaged assets alone with
`cognistore-sample verify`.

### Driver configuration (optional)

You can instantiate drivers from a YAML file using `cognistore.drivers.driver_loader.load_drivers`.

Example `drivers.yaml`:

```yaml
tiers:
  hot:
    driver: posix
    path: /tmp/cognistore/hot
    chunk_size: 8388608
  warm:
    driver: posix
    path: /tmp/cognistore/warm
    chunk_size: 8388608
```

The same file can opt policy evaluation into the PostgreSQL/pgvector embedding
index. Without this block, embedding rules remain safely `missing` and cannot
trigger a move:

```yaml
embedding:
  provider: openai-compatible
  base_url: https://embeddings.example.com
  api_key_env: COGNISTORE_EMBEDDING_API_KEY
  model: text-embedding-model
  deployment_version: release-2026-09-01
  dimensions: 1536
  request_dimensions: false
```

For locally executed sentence-transformers inference, install
`cognistore[embeddings]` and use an immutable Hub commit:

```yaml
embedding:
  provider: sentence-transformers
  model: sentence-transformers/all-MiniLM-L6-v2
  revision: 0123456789abcdef0123456789abcdef01234567
  dimensions: 384
```

Configured embedding policy features require a PostgreSQL catalog with the
supported pgvector extension. The API, synchronous `policy-run`, and workers
all compose the same provider from this block. OpenAI-compatible API keys are
read only through `api_key_env`; they should not be stored in YAML.

Then in Python:

```python
from cognistore.drivers.driver_loader import load_drivers
drivers = load_drivers("drivers.yaml")
hot = drivers["hot"]
hot.put_object("bucket", "key.txt", b"hello")
```

### Runtime secrets and keys

Production tiers can resolve credential references from Vault KV v2 or AWS
Secrets Manager at operation time. Bounded caches refresh after expiry, while
active streams retain their credential version for the transfer. Keep old
credentials valid through the documented transfer grace window when rotating.
AWS KMS and Vault Transit key providers also unwrap encrypted key material
through the Python API.

Prefer native cloud workload identities when available. See the
[runtime secrets guide](docs/secrets.md) for configuration, least privilege,
rotation, redaction, and provider outage behavior, and
[examples/drivers.production.yaml](examples/drivers.production.yaml) for a
configuration containing references only.

### S3-compatible tiers

The `s3` driver works with AWS S3 and S3-compatible services such as MinIO.
Keep credentials out of `drivers.yaml`: prefer the standard boto3 credential
chain or an AWS profile, or use `access_key_env`, `secret_key_env`, and
`session_token_env` to name environment variables that hold the values.
Literal `access_key`, `secret_key`, and `session_token` fields are supported
for controlled use but must not be committed.

MinIO example:

```yaml
tiers:
  object:
    driver: s3
    endpoint_url: http://127.0.0.1:9000
    region_name: us-east-1
    access_key_env: COGNISTORE_MINIO_ACCESS_KEY
    secret_key_env: COGNISTORE_MINIO_SECRET_KEY
    addressing_style: path
    auto_create_bucket: true
    chunk_size: 8388608
    list_page_size: 1000
    multipart_threshold: 8388608
```

For AWS, omit `endpoint`/`endpoint_url` and normally omit credential fields so
boto3 can use a workload identity or its standard credential chain. Local
development can instead select `profile`/`profile_name`. Both
`region`/`region_name` spellings are accepted:

```yaml
tiers:
  object:
    driver: s3
    region: us-west-2
    profile: cognistore-dev
    addressing_style: auto
    auto_create_bucket: false
```

The driver supports complete and atomic no-overwrite puts, ranged reads such as
`bytes=0-99`, idempotent deletes, metadata stat, and transparent
`ListObjectsV2` pagination. Ranged writes are rejected. Missing objects raise
`FileNotFoundError` on get and stat, while deleting a missing object succeeds.
`list_page_size` controls the API page size rather than limiting total results.
Bucket auto-creation is off by default; when enabled, a missing bucket is
created for a write, not for read-only operations. Keep it off for
pre-provisioned production buckets.

Moves read and write sequentially with bounded buffers instead of loading an
entire object into memory. `chunk_size` and `multipart_threshold` are raw byte
counts and both default to 8388608 bytes (8 MiB). S3 requires `chunk_size` to be
at least 5242880 bytes (5 MiB); the final multipart part may be smaller. Objects
whose size is equal to or greater than `multipart_threshold` use multipart
upload, while smaller objects use a single conditional put staged with
chunk-bounded memory and temporary-file spillover. Objects above the 5 GiB
single-put limit always use multipart upload. Temporary disk usage for a
below-threshold single put can approach the object's size.

Multipart moves upload one part at a time and retain atomic no-overwrite
behavior by applying `If-None-Match: *` when the upload is completed. A
catchable interruption aborts the incomplete upload. The S3 identity therefore
needs `s3:AbortMultipartUpload`. Because a process crash or forced termination
cannot run application cleanup, production buckets should also use an
`AbortIncompleteMultipartUpload` lifecycle rule as a backstop.

Each executed move is also a catalog-backed state machine. An explicit
idempotency key can safely be replayed across worker delivery attempts. The
destination is transferred and verified before the catalog placement changes;
source cleanup happens only after that atomic catalog commit. Durable SQL
catalogs on SQLite or PostgreSQL persist phase history, terminal reasons,
ownership, and expiring leases so an incomplete move can be claimed and
resumed after a worker failure.

MinIO integration tests are opt-in. With a separately managed test instance
running, set its API endpoint and disposable credentials, then run the marked
test module:

```bash
export COGNISTORE_MINIO_ENDPOINT_URL=http://127.0.0.1:9000
export COGNISTORE_MINIO_ACCESS_KEY='<MinIO access key>'
export COGNISTORE_MINIO_SECRET_KEY='<MinIO secret key>'
python -m pytest -q -m integration tests/integration/test_s3_minio.py
```

See [`docs/s3_driver.md`](docs/s3_driver.md) for every configuration option,
credential guidance, AWS/MinIO examples, and the opt-in MinIO integration-test
command. For a repository-managed MinIO instance, use the
[Docker integration stack](docs/setup_guide.md#run-the-integration-suite).

### Azure Blob tiers

The optional `azure_blob` driver maps buckets to containers and keys to block
blobs. Install `'.[azure]'`, then select an account endpoint with Azure identity:

```yaml
tiers:
  object:
    driver: azure_blob
    account_url: https://example.blob.core.windows.net
    auto_create_container: false
    chunk_size: 8388608
    list_page_size: 1000
```

Without an explicit credential, the driver uses `DefaultAzureCredential`.
Alternatively, set `connection_string_env` to the name of an environment
variable containing a connection string. Moves use bounded block uploads,
atomic no-overwrite commits, and ETag conditions for source reads and cleanup.
Failed uploads preserve committed content and leave uncommitted blocks for
Azure's garbage collection.

See the [Azure Blob driver guide](docs/azure_blob_driver.md) for secret-safe
configuration, ranged reads, metadata and error behavior, Azurite tests, and
the explicit opt-in live-cloud validation path.

### Google Cloud Storage tiers

The `gcs` driver supports Google Cloud Storage with Application Default
Credentials (ADC), ranged streaming reads, resumable uploads, paginated
listing, metadata, atomic no-overwrite writes, and generation-fenced cleanup.

```yaml
tiers:
  cloud:
    driver: gcs
    project: example-project
    auto_create_bucket: false
    chunk_size: 8388608
    list_page_size: 1000
```

Omit credential fields to use ADC, including `GOOGLE_APPLICATION_CREDENTIALS`
or an attached workload identity. An explicit `credentials_file` or
`credentials_file_env` selects a trusted authentication file by path; never
store credential JSON in YAML. An explicit `emulator_endpoint` enables
anonymous local emulator access.

Uploads stage and validate the source in a temporary file that spills to disk
above `chunk_size`. Memory stays bounded by transfer buffers, while temporary
disk usage can approach the object's size. Interrupted uploads query the
server's acknowledged offset and resume within a bounded retry budget;
catchable failures attempt to cancel the session. Sessions are not persisted
for cross-process resumption.

See the [GCS driver guide](docs/gcs_driver.md) for every configuration option,
credential setup, error semantics, emulator tests, and opt-in live-cloud
validation against a disposable bucket.

### CLI with tiers

```bash
# Put into default (hot) tier via drivers.yaml
python -m cognistore.cli --drivers drivers.yaml put demo-bucket path/key.txt README.md

# List keys in a specific tier
python -m cognistore.cli --drivers drivers.yaml ls-tier hot demo-bucket --prefix path/

# Move between tiers
python -m cognistore.cli --drivers drivers.yaml move hot warm demo-bucket path/key.txt

# Validate the same move without storage or catalog writes
python -m cognistore.cli --drivers drivers.yaml move hot warm demo-bucket path/key.txt --dry-run

# Emit a machine-readable plan
python -m cognistore.cli --drivers drivers.yaml move hot warm demo-bucket path/key.txt --dry-run --json

# Verify in warm tier
python -m cognistore.cli --drivers drivers.yaml ls-tier warm demo-bucket --prefix path/
```

Moves fail closed: source and destination tiers must be known and distinct,
different tier names may not resolve to the same backend, and an existing
destination object is never overwritten. Remove or relocate a destination
collision explicitly before retrying a move. POSIX bucket and key paths must
be relative, unambiguous paths beneath the tier root; parent traversal and
symbolic-link components are rejected. Filesystem operations use retained
directory descriptors to prevent concurrent symlink swaps from redirecting
object access. POSIX tiers require directories owned by the service account,
protected from other writers, and a trusted namespace whose open directories
stay inside the tier. Missing containment primitives fail closed. See
[POSIX containment and deployment permissions](docs/posix_containment.md) for
the enforced checks, supported mutation model, and platform limitations.

After a destination write commits, the mover independently streams the stored
object to verify its byte count and SHA-256 against the source bytes observed
during transfer. The source and catalog remain unchanged when verification is
incomplete or fails. A definite missing object, size mismatch, or checksum
mismatch raises `MoveVerificationError`; transient destination observation
errors propagate so the durable move remains `transferred` and can resume
verification without retransferring the object. Successful moves persist the
verified size and SHA-256 in the catalog and return a `MoveVerificationResult`.

Move cleanup is generation-fenced. Durable jobs record the source and verified
destination generations; cleanup deletes only the exact source generation that
was transferred and rechecks the destination generation immediately before the
conditional delete. If either key changes, the source is retained, the catalog
is reconciled to it, and the job fails with `MoveGenerationMismatchError`.
POSIX driver mutations coordinate through per-object locks, while S3 deletion
uses version IDs and an atomic ETag precondition. GCS uses atomic generation
preconditions for source reads and deletion.

### Catalog and policy runner via CLI

You can build a catalog from an existing tier and then run a simple policy pass to move objects automatically.

```bash
# Optionally use a persistent SQLite catalog for local inline work
CAT_DB=/tmp/cognistore/catalog.db

# Scan a tier (e.g., hot); the worker owns the persistent catalog
python -m cognistore.cli --drivers drivers.yaml \
	catalog-scan hot demo-bucket --prefix path/

# Run a policy pass: files <= threshold go to hot; larger go to warm
python -m cognistore.cli --drivers drivers.yaml \
	policy-run demo-bucket --prefix path/ --threshold 1048576

# Preview planned actions as JSON without storage or catalog writes
python -m cognistore.cli --drivers drivers.yaml --catalog-db "$CAT_DB" \
	policy-run demo-bucket --prefix path/ --threshold 1048576 --dry-run --json

# Inspect tiers after moves
python -m cognistore.cli --drivers drivers.yaml ls-tier hot demo-bucket --prefix path/
python -m cognistore.cli --drivers drivers.yaml ls-tier warm demo-bucket --prefix path/

# Reconcile shared CAS references without loading drivers or changing state
python -m cognistore.cli --catalog-db "$CAT_DB" \
	content-reference-report --grace-period-seconds 604800 --json
```

Notes:
- The worker must use a persistent `--catalog-db`; background submissions intentionally do not select a database.
- Inline commands use an in-memory catalog when `--catalog-db` is omitted.
- `--catalog-db` accepts a SQLite path/URL or PostgreSQL DSN;
  `--catalog-url` is an equivalent CLI spelling. Writable SQL catalogs apply
  the packaged migrations automatically.
- Writable `catalog-scan` and `policy-run` commands enqueue durable background jobs by default; run a worker with a persistent `--catalog-db`. Dry-runs stay synchronous, and `--sync` is available for explicit development-only inline execution.
- `catalog-scan` streams the complete object into a canonical SHA-256 and
  versioned source-byte chunk manifest while retaining bounded MIME-sample and
  document-extraction metadata. See the
  [content identity contract](docs/content_identity.md).
- `content-reference-report` opens an existing persistent catalog read-only,
  compares materialized and topology-derived full-object/chunk edge counts, and
  reports conservative reclamation eligibility after a configurable grace
  period. It never repairs rows or deletes content; the default grace period is
  seven days.
- `consistency-scan` compares a tenant-bound catalog scope against storage
  metadata, full checksums, and move jobs without modifying source data. Scans
  are rate-limited and resumable; `consistency-export` streams their audited
  findings as JSONL. See [consistency checks](docs/consistency_checks.md) for
  trusted namespace bindings, report paths, and dry-run behavior.
- `importance-set BUCKET KEY LEVEL` records an attributed importance change and
  reevaluates placement; `LEVEL` accepts `low`, `normal`, `high`, `critical`, or
  `clear`. Supply `--actor` and `--provenance`. See
  [importance and minimum residency](docs/placement_controls.md).
- `policy-run` supports:
	- `--policy simple|llm|content` (default: simple)
	- `--allowed-tiers hot,warm` to constrain decisions
	- `--minimum-residency TIER SECONDS` (repeatable) to delay leaving a tier
	- `--importance-tier LEVEL TIER` (repeatable) to configure importance destinations
	- `--cooldown-seconds`, `--size-hysteresis-bytes`, and `--similarity-hysteresis`
	  to reduce tier flapping; these default to zero and must be configured for
	  stability protection (see [placement controls](docs/placement_controls.md))
	- `--dry-run` to validate and report planned moves without writes
	- `--json` for one machine-readable result object
	- `--threshold` for simple/content size rules (`--llm-threshold` is accepted for compatibility and ignored)
	- `--metrics-in` to use measured tier metrics (see tier profiling below)
	- `--hardware-in` to use OS-reported device types with default profiles
	- `--auto-discover` to scan devices and profile tiers automatically when no inputs are supplied; dry-runs consume only fresh existing caches and never refresh them
	- `--cache-dir` and `--cache-ttl` to control where/when auto caches are refreshed
	- Content-aware flags:
		- `--hot-name PATTERN` (repeatable) → glob patterns that should be placed in hot (e.g., `*.hot.txt`)
		- `--warm-name PATTERN` (repeatable) → glob patterns for warm (e.g., `*.zip`)
		- `--hot-mime PREFIX` (repeatable) → MIME prefix for hot (e.g., `text/`, `image/`)
		- `--warm-mime PREFIX` (repeatable) → MIME prefix for warm (e.g., `application/zip`)
		- `--embedding-rule NAME QUERY MIN_SIMILARITY DESTINATION_TIER` (repeatable) → named max-passage semantic classification; missing/stale providers fail closed
	  Feature states, provenance, rule ordering, and reindex behavior are defined
	  in [the policy feature projection contract](docs/policy_features.md).
	The LLM mode uses a configured model service with strict JSON validation and
	a safe stay on invalid, late, or unavailable responses. See
	[LLM-assisted placement](docs/llm_placement.md) for configuration, the HTTP
	contract, Python adapters, and audit behavior.

Observed object reads, writes, and metadata touches also feed configurable
recency/frequency windows in policy projections. See [access history](docs/access_history.md)
for capture coverage, retry identities, sampling, and retention.

Writable policy runs retain versioned feature and decision snapshots for offline
replay. Use `policy-dataset-export` to export privacy-filtered rows with observed
move outcomes and `policy-dataset-validate` to check schema, labels, and time
ordering. See [policy datasets](docs/policy_datasets.md) for sampling, exclusions,
retention, and the limits of execution-success labels.

Use `policy-baseline-train` and `policy-baseline-evaluate` for reproducible
offline experiments on those exports. The
[supervised baseline guide](docs/policy_baseline.md) covers chronological splits,
leakage and imbalance checks, comparison with recorded rules, and promotion
criteria. The baseline predicts move execution success and leaves runtime
policies unchanged.

Every persisted placement decision also includes [structured policy reasons](docs/policy_reasons.md):
versioned reason codes, decisive signals, constraint evidence, and policy/model
versions for moves, stays, and suppressed decisions, linked to the resulting
move history. Dataset exports retain these reasons alongside the feature snapshot.
The **Placement** view at `/ui/` renders [placement explanations and diffs](docs/placement_explanations.md)
for object or job history and side-effect-free previews. Versioned API resources
share the same decision schema, with execution and job status shown separately.

M3 is complete. The [closeout evidence](docs/evidence/m3/README.md) maps all
eleven delivery issues to implementation and validation, including configured
stability guardrails, modeled budgets, and the offline baseline's limits.

### Durable background workers

CogniStore uses file-backed NATS JetStream and a durable pull consumer for
at-least-once execution. Start NATS Server 2.10 or newer with JetStream
enabled, then run:

```bash
python -m cognistore.cli --drivers drivers.yaml \
  --catalog-db /tmp/cognistore/catalog.db \
  --nats-url nats://127.0.0.1:4222 worker
```

For authenticated API jobs, configure the worker with the same
`COGNISTORE_AUTHORIZATION_POLICY` as the API. Every delivery rechecks current
permissions; an authenticated job is denied if the worker has no policy.
Tenant-enabled workers also need the API's `COGNISTORE_TENANT_POLICY` to
validate each job's immutable owner before execution.
The CLI and recurring-schedule examples below describe trusted local workloads
without a principal, so their jobs are rejected by workers with a policy. Use
authenticated REST actions for protected manual workloads, or configure a
dedicated scheduler service identity for
[protected recurring schedules](docs/background_workers.md#protected-schedules).

Recurring scans and policy passes are declared in a YAML `jobs` mapping keyed
by stable schedule IDs:

```yaml
jobs:
  scan-reports:
    type: catalog.scan
    enabled: true
    interval_seconds: 300
    payload:
      tier: hot
      bucket: demo-bucket
      prefix: reports/
  place-reports:
    type: policy.run
    enabled: true
    interval_seconds: 900
    payload:
      bucket: demo-bucket
      prefix: reports/
      policy: simple
      threshold: 1048576
      allowed_tiers: [hot, warm]
```

Run the scheduler as a separate process. With SQLite, the scheduler can keep
using the catalog file as its scheduler-state file for compatibility:

```bash
cognistore --drivers drivers.yaml --catalog-db catalog.db scheduler --schedule-config schedules.yaml
```

With a PostgreSQL catalog, scheduler state remains a separate persistent SQLite
store, and both worker and scheduler must receive its path:

```bash
export COGNISTORE_CATALOG_DB='postgresql://cognistore@db.example/cognistore'
export COGNISTORE_SCHEDULE_DB=/var/lib/cognistore/schedule.sqlite3

cognistore --drivers drivers.yaml worker
cognistore --drivers drivers.yaml scheduler --schedule-config schedules.yaml
```

Keep database credentials in deployment secrets rather than the command line.
The DSN may omit its password when libpq obtains it from `PGPASSWORD`, a
password file, or the deployment's equivalent secret injection; the Compose
stack maps `COGNISTORE_POSTGRES_PASSWORD` to `PGPASSWORD` for its clients.
The PostgreSQL catalog contains objects, placements, move journals, and a
versioned, tamper-evident operational history correlating policy decisions, jobs,
manual actions, retries, failures, and terminal moves. Tenant-scoped auditors can
query, export, and verify evidence through `/v1/audit`; retain checkpoints outside
the catalog to detect replacement of the database and its local integrity head. See the
[audit-event operations guide](docs/audit_events.md). `--schedule-db` contains
schedule timing, reservations, execution leases, and recovery audits. See the
[PostgreSQL catalog operations guide](docs/postgres_catalog.md) for schema
migrations and the supported offline SQLite import.
The [tier and pool topology guide](docs/tier_pools.md) describes multi-pool tiers,
region and locality constraints, attribute freshness, and backend-neutral
placement assignment, with JSON and YAML configuration examples.
The [data locality guide](docs/data_locality.md) describes server-owned tenant
and object rules, expiring region evidence, execution and recovery checks, and
explicit authorized exceptions. Set `COGNISTORE_LOCALITY_CONFIG` on every
process that plans or executes moves to enable these controls.
The [storage estimation guide](docs/storage_estimation.md) covers versioned
cost and operational carbon estimates, explicit workload forecasts, evidence
freshness, uncertainty, deterministic replay, and policy feature injection.
The [policy budgets and what-if guide](docs/policy_budgets.md) covers atomic cost
and carbon allowances, weighted placement objectives, and catalog-only scenario
comparisons with explicit forecast assumptions.

The scheduler-state file must be persistent; worker and scheduler commands
reject `:memory:` because their coordination state must be shared across
connections.

New and re-enabled schedules run immediately. Later runs use a fixed interval
from reservation time, and missed intervals coalesce while the exact target
scope already has an active occurrence. Reservations and pending publications
survive scheduler restarts. Every occurrence receives a new job ID; retries of
that occurrence retain it.

Submit scans and policy passes with the commands above. Their output contains a
stable job ID and correlation ID. A job may be delivered more than once, so
handlers must use the job ID as an idempotency key; publish deduplication does
not make consumer effects exactly once. Policy moves derive per-object keys
from that stable job ID and recover incomplete phases before evaluating the
next placement pass.

Transient timeouts, throttling, and unavailable backends receive bounded
exponential-backoff retries. Terminal or exhausted deliveries are durably
written to a separate immutable dead-letter stream before the source is ACKed;
large diagnostics are split into checksummed, payload-bounded chunks. Operators
can republish a valid entry without changing its logical job ID or audit chain:

```bash
python -m cognistore.cli --nats-url nats://127.0.0.1:4222 \
  dead-letter-redrive DEAD_LETTER_ID --json
```

Worker liveness is served at `http://127.0.0.1:8081/healthz`, and readiness
(including a live JetStream stream/consumer probe) at `/readyz`. See
[`docs/background_workers.md`](docs/background_workers.md) for setup,
configuration, bounded per-tier concurrency/rate controls, live `SIGHUP`
reloads, backpressure metrics, shutdown semantics, the complete consumer
contract, and integration-test instructions. The stack choice is recorded in
[`ADR 0001`](docs/adr/0001-nats-jetstream-workers.md).

Example content-aware pass (ensure you ran `catalog-scan` first so MIME metadata exists):

```bash
python -m cognistore.cli --drivers drivers.yaml --catalog-db "$CAT_DB" \
  policy-run demo-bucket --policy content --sync \
  --hot-name "*.txt" --hot-mime text/ \
  --warm-name "*.zip" --warm-mime application/zip \
  --threshold 1048576
```

### Hardware discovery and tier profiling

CogniStore can automatically discover the hardware type of each tier and profile its performance to inform placement decisions.

Commands:

```bash
# Discover OS-reported hardware for each tier, write to JSON
python -m cognistore.cli --drivers drivers.yaml devices-scan --hardware-out .cognistore/hardware.json

# Profile tiers (first-byte latency, seq read/write MB/s, random IOPS, capacity)
python -m cognistore.cli --drivers drivers.yaml tier-profile --metrics-out .cognistore/tier_metrics.json

# Use metrics in policy-run (preferred when available)
python -m cognistore.cli --drivers drivers.yaml --catalog-db "$CAT_DB" \
	policy-run demo-bucket --policy content \
	--metrics-in .cognistore/tier_metrics.json --dry-run

# Fallback: use hardware classification (nvme/ssd/hdd) with default profiles
python -m cognistore.cli --drivers drivers.yaml --catalog-db "$CAT_DB" \
	policy-run demo-bucket --policy content \
	--hardware-in .cognistore/hardware.json --dry-run
```

Auto modes:

```bash
# Auto-discover on demand (no daemon); caches to .cognistore/
python -m cognistore.cli --drivers drivers.yaml --catalog-db "$CAT_DB" \
	policy-run demo-bucket --policy content \
	--auto-discover --cache-dir .cognistore --cache-ttl 3600 --dry-run

# Background refresher to keep caches up to date (run once)
python -m cognistore.cli --drivers drivers.yaml auto-refresh --cache-dir .cognistore

# Background refresher every 15 minutes
python -m cognistore.cli --drivers drivers.yaml auto-refresh --cache-dir .cognistore --interval 900
```

Details live in `docs/tier_profiling.md`.

## Contributing

Interested in contributing? Please read `CONTRIBUTING.md` and see:
- `docs/git_workflows.md` for branching, PR, and release guidance
- `docs/bug_tracker.md` for the bug tracker format
- `docs/roadmap.md` for upcoming milestones

### Architecture and OS support

Device discovery is modular by OS:
- macOS via `diskutil`
- Linux via `lsblk`
- Windows via PowerShell `Get-PhysicalDisk` (WMIC fallback)

The orchestrator in `cognistore/utils/device_info.py` dispatches to per-OS modules. To support a new OS, add a `device_info_<os>.py` with an `inspect_device_<os>()` function and hook it into the orchestrator.
