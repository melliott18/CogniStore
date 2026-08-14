# CogniStore
AI-Powered Data Lifecycle Manager

## Quickstart

- Create a virtual environment and install deps

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

- Run tests

```bash
pytest -q
```

- Try the POSIX driver via CLI

```bash
# Put and get a file using the filesystem as storage
python -m cognistore.cli --base /tmp/cognistore put demo-bucket path/to/key.txt README.md
python -m cognistore.cli --base /tmp/cognistore get demo-bucket path/to/key.txt /tmp/out.txt
python -m cognistore.cli --base /tmp/cognistore ls demo-bucket --prefix path/
```

### Driver configuration (optional)

You can instantiate drivers from a YAML file using `cognistore.drivers.driver_loader.load_drivers`.

Example `drivers.yaml`:

```yaml
tiers:
	hot:
		driver: posix
		path: /tmp/cognistore/hot
	warm:
		driver: posix
		path: /tmp/cognistore/warm
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
    list_page_size: 1000
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

MinIO integration tests are opt-in. With a separately managed test instance
running, set its API endpoint and disposable credentials, then run the marked
test module:

```bash
export COGNISTORE_MINIO_ENDPOINT_URL=http://127.0.0.1:9000
export COGNISTORE_MINIO_ACCESS_KEY='<MinIO access key>'
export COGNISTORE_MINIO_SECRET_KEY='<MinIO secret key>'
pytest -q -m integration tests/integration/test_s3_minio.py
```

See [`docs/s3_driver.md`](docs/s3_driver.md) for every configuration option,
credential guidance, AWS/MinIO examples, and the opt-in MinIO integration-test
command. The repository-managed Docker Compose environment is intentionally
tracked separately by [ticket #28](https://github.com/melliott18/CogniStore/issues/28).

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

Submit scans and policy passes with the commands above. Their output contains a
stable job ID and correlation ID. A job may be delivered more than once, so
handlers must use the job ID as an idempotency key; publish deduplication does
not make consumer effects exactly once.

Worker liveness is served at `http://127.0.0.1:8081/healthz`, and readiness
(including a live JetStream stream/consumer probe) at `/readyz`. See
[`docs/background_workers.md`](docs/background_workers.md) for setup,
configuration, shutdown semantics, the complete consumer contract, and
integration-test instructions. The stack choice is recorded in
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
