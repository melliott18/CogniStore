# CogniStore
AI-Powered Data Lifecycle Manager

## Quickstart

CogniStore supports CPython 3.10 through 3.14. CI exercises every supported
minor version.

### Docker stack

Start the non-root CogniStore worker, file-backed NATS JetStream, and MinIO
from a clean checkout:

```bash
docker compose up --build --wait
```

Run every integration test inside the same isolated stack with one command:

```bash
docker compose --profile integration up --build \
  --abort-on-container-exit \
  --exit-code-from integration-tests
```

Named volumes retain the worker catalog, storage tiers, JetStream state,
MinIO data, and test report across ordinary stops. See the
[Docker development and integration guide](docs/setup_guide.md) for service
URLs, credentials, an editable development shell, safe shutdown diagnostics,
and the explicit data-reset command.

### Local Python environment

- Create a virtual environment and install the package with its development
  tools:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

- Run the same quality gates used by pull requests:

```bash
python -m ruff check .
python -m mypy cognistore
python -m pytest tests/unit tests/conformance
python -m pytest tests/integration
python -m pytest --import-mode=importlib \
  --cov=cognistore --cov-report=term-missing --cov-report=xml
python -m bandit -c pyproject.toml -r cognistore -ll -ii
python -m pip_audit .
python -m build
python -m twine check dist/*
```

The coverage command enforces the repository's 80% minimum. Integration tests
that require NATS or MinIO skip unless their documented environment variables
point to isolated test services; the filesystem/catalog integration suite runs
without external services. The explicit import mode is a temporary workaround
for the duplicate qualification-test basenames tracked in
[#90](https://github.com/melliott18/CogniStore/issues/90); grouped test commands
do not collide. Install Gitleaks separately and run
`gitleaks git --redact .` to perform the same secret scan used in CI.

For deterministic POSIX/S3 throughput, tail-latency, integrity, and injected
failure-recovery evidence, use the
[move scale and recovery qualification guide](docs/scale_qualification.md).
The CI campaign is deliberately reduced scale; no one-million-object result is
claimed by this branch.

For a runtime-only install, use `python -m pip install .`. The generated
`cognistore` command and `python -m cognistore.cli` invoke the same CLI.

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
the command-by-command dry-run matrix, and durable manual-move recovery.

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

Then in Python:

```python
from cognistore.drivers.driver_loader import load_drivers
drivers = load_drivers("drivers.yaml")
hot = drivers["hot"]
hot.put_object("bucket", "key.txt", b"hello")
```

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
source cleanup happens only after that atomic catalog commit. SQLite catalogs
persist phase history, terminal reasons, ownership, and expiring leases so an
incomplete move can be claimed and resumed after a worker failure.

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
symbolic-link components are rejected.

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
uses version IDs and an atomic ETag precondition.

### Catalog and policy runner via CLI

You can build a catalog from an existing tier and then run a simple policy pass to move objects automatically.

```bash
# Optionally use a persistent SQLite catalog
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
```

Notes:
- The worker must use a persistent `--catalog-db`; background submissions intentionally do not select a database.
- Inline commands use an in-memory catalog when `--catalog-db` is omitted.
- Writable `catalog-scan` and `policy-run` commands enqueue durable background jobs by default; run a worker with a persistent `--catalog-db`. Dry-runs stay synchronous, and `--sync` is available for explicit development-only inline execution.
- `catalog-scan` captures metadata including sha256, mime, and a small sample length.
- `policy-run` supports:
	- `--policy simple|llm|content` (default: simple)
	- `--allowed-tiers hot,warm` to constrain decisions
	- `--dry-run` to validate and report planned moves without writes
	- `--json` for one machine-readable result object
	- `--threshold` (and `--llm-threshold` for the LLM path)
	- `--metrics-in` to use measured tier metrics (see tier profiling below)
	- `--hardware-in` to use OS-reported device types with default profiles
	- `--auto-discover` to scan devices and profile tiers automatically when no inputs are supplied; dry-runs consume only fresh existing caches and never refresh them
	- `--cache-dir` and `--cache-ttl` to control where/when auto caches are refreshed
	- Content-aware flags:
		- `--hot-name PATTERN` (repeatable) → glob patterns that should be placed in hot (e.g., `*.hot.txt`)
		- `--warm-name PATTERN` (repeatable) → glob patterns for warm (e.g., `*.zip`)
		- `--hot-mime PREFIX` (repeatable) → MIME prefix for hot (e.g., `text/`, `image/`)
		- `--warm-mime PREFIX` (repeatable) → MIME prefix for warm (e.g., `application/zip`)
	The LLM mode currently uses a threshold-based mock provider; you can swap in a real provider later.

### Durable background workers

CogniStore uses file-backed NATS JetStream and a durable pull consumer for
at-least-once execution. Start NATS Server 2.10 or newer with JetStream
enabled, then run:

```bash
python -m cognistore.cli --drivers drivers.yaml \
  --catalog-db /tmp/cognistore/catalog.db \
  --nats-url nats://127.0.0.1:4222 worker
```

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

Run the scheduler as a separate process, using the same catalog database as
every worker that consumes its jobs:

```bash
cognistore --drivers drivers.yaml --catalog-db catalog.db scheduler --schedule-config schedules.yaml
```

This must be a persistent SQLite file; worker and scheduler commands reject
`:memory:` because their control-plane state must be shared across connections.

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
