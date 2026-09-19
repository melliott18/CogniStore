# CogniStore CLI reference

The installed `cognistore` command and `python -m cognistore.cli` are
equivalent. The general form is:

```text
cognistore [GLOBAL_OPTIONS] COMMAND [COMMAND_OPTIONS]
```

Global options may appear before or after `COMMAND`; command-specific options
belong after it. This permits both `cognistore --profile local ls ...` and
`cognistore ls ... --profile local`.

## Configuration and profiles

### Precedence

CogniStore resolves every configurable global value independently, from
highest to lowest precedence:

1. an explicit CLI option;
2. its `COGNISTORE_*` environment variable;
3. the selected named profile;
4. the configuration file's `defaults` mapping;
5. the CLI's built-in default.

An explicit `--nats-url` replaces the complete configured URL list on its
first occurrence; repeat it to provide a CLI-selected cluster. Boolean output
settings can be explicitly disabled with `--no-json` or `--no-dry-run`, which
is useful when a lower-precedence layer enables them. `--no-verbose` likewise
disables diagnostics enabled by an environment value, selected profile, or
file default; a later `-v` or `--verbose` on the same command enables them.

### Configuration file selection

The CLI selects at most one YAML file, in this order:

1. `--config PATH`;
2. `COGNISTORE_CONFIG`;
3. `$XDG_CONFIG_HOME/cognistore/config.yaml`, when `XDG_CONFIG_HOME` is set;
4. `~/.config/cognistore/config.yaml`.

A missing file selected by `--config` or `COGNISTORE_CONFIG` is an error. A
missing implicit file in the XDG or home location is ignored. `--no-config`
skips all file loading, including `COGNISTORE_CONFIG`; it cannot be combined
with `--config` or `--profile`. Ordinary value variables such as
`COGNISTORE_DRIVERS` still apply with `--no-config`.

Home markers in a selected configuration path are expanded. Other relative
paths in the YAML are passed through and therefore resolve from the process's
current working directory, not from the YAML file's directory.

### Profile selection

After loading the file, the CLI selects a profile in this order:

1. `--profile NAME`;
2. `COGNISTORE_PROFILE`;
3. the file's `default_profile`.

Selecting a profile requires a loaded configuration file, and the name must
exist in `profiles`. Profile names may contain letters, digits, dots,
underscores, and hyphens, and must begin with a letter or digit. A selected
profile overlays `defaults`; environment variables and CLI options can then
overlay individual profile values.

### YAML v1 example

```yaml
version: 1
default_profile: local

defaults:
  job_stream: COGNISTORE_JOBS
  job_subject: cognistore.jobs
  job_consumer: cognistore-workers
  ack_wait: 30
  stream_max_messages: 10000
  stream_max_bytes: 1073741824
  dead_letter_max_age: 2592000
  audit_retention_max_age: 2592000
  json: false
  dry_run: false
  verbose: false

profiles:
  local:
    drivers: ./drivers.yaml
    catalog_db: ./catalog.db
    schedule_db: ./schedule.db
    nats_url:
      - nats://127.0.0.1:4222

  production:
    drivers: /etc/cognistore/drivers.yaml
    schedule_db: /var/lib/cognistore/schedule.sqlite3
    nats_url:
      - nats://nats-a.internal:4222
      - nats://nats-b.internal:4222
    dead_letter_stream: COGNISTORE_JOBS_DLQ
    dead_letter_subject: cognistore.jobs.dead
```

For the production profile, supply the PostgreSQL catalog DSN through
`COGNISTORE_CATALOG_DB`. Supply its password from the deployment secret store
through a libpq credential source such as `PGPASSWORD` or a password file; the
DSN can then remain passwordless. YAML does not interpolate environment
placeholders, so do not put a password-bearing DSN in this file.

The v1 loader is strict: `version` must be the integer `1`; unknown or
duplicate keys, unsafe YAML tags, invalid types, an undefined default profile,
and malformed YAML are rejected. The only top-level keys are `version`,
`default_profile`, `defaults`, and `profiles`.

For example, with the file in the implicit location:

```bash
cognistore --profile local ls-tier hot demo-bucket --prefix reports/
```

## Global options and environment variables

Configuration selectors are not profile values:

| Option | Environment equivalent | Purpose |
| --- | --- | --- |
| `--config PATH` | `COGNISTORE_CONFIG` | Select the v1 YAML file. |
| `--no-config` | none | Disable YAML file and profile loading. |
| `--profile NAME` | `COGNISTORE_PROFILE` | Select a named profile. |

The following settings are valid in `defaults` and every profile. Their CLI
forms are global options:

| YAML key | CLI option | Environment variable | Built-in |
| --- | --- | --- | --- |
| `base` | `--base PATH` | `COGNISTORE_BASE` | none |
| `drivers` | `--drivers PATH` | `COGNISTORE_DRIVERS` | none |
| `catalog_db` | `--catalog-db LOCATOR` / `--catalog-url LOCATOR` | `COGNISTORE_CATALOG_DB` | in-memory when the command permits it |
| `schedule_db` | `--schedule-db PATH` | `COGNISTORE_SCHEDULE_DB` | the SQLite catalog path when compatible; otherwise none |
| `nats_url` | `--nats-url URL` (repeatable) | `COGNISTORE_NATS_URL` (comma-separated) | `nats://127.0.0.1:4222` |
| `job_stream` | `--job-stream NAME` | `COGNISTORE_JOB_STREAM` | `COGNISTORE_JOBS` |
| `job_subject` | `--job-subject SUBJECT` | `COGNISTORE_JOB_SUBJECT` | `cognistore.jobs` |
| `job_consumer` | `--job-consumer NAME` | `COGNISTORE_JOB_CONSUMER` | `cognistore-workers` |
| `ack_wait` | `--ack-wait SECONDS` | `COGNISTORE_ACK_WAIT` | `30` |
| `stream_max_messages` | `--stream-max-messages COUNT` | `COGNISTORE_STREAM_MAX_MESSAGES` | `10000` |
| `stream_max_bytes` | `--stream-max-bytes BYTES` | `COGNISTORE_STREAM_MAX_BYTES` | `1073741824` |
| `dead_letter_stream` | `--dead-letter-stream NAME` | `COGNISTORE_DEAD_LETTER_STREAM` | `<job-stream>_DLQ` |
| `dead_letter_subject` | `--dead-letter-subject SUBJECT` | `COGNISTORE_DEAD_LETTER_SUBJECT` | `<job-subject>.dead` |
| `dead_letter_max_age` | `--dead-letter-max-age SECONDS` | `COGNISTORE_DEAD_LETTER_MAX_AGE` | `2592000` (30 days) |
| `audit_retention_max_age` | `--audit-retention-max-age SECONDS` | `COGNISTORE_AUDIT_RETENTION_MAX_AGE` | `2592000` (30 days) |
| `json` | `--json` / `--no-json` | `COGNISTORE_JSON` | `false` |
| `dry_run` | `--dry-run` / `--no-dry-run` | `COGNISTORE_DRY_RUN` | `false` |
| `verbose` | `-v` / `--verbose` / `--no-verbose` | `COGNISTORE_VERBOSE` | `false` |

In YAML, `nats_url` is a non-empty list of strings. Environment booleans
accept `1`, `true`, `yes`, or `on`, and `0`, `false`, `no`, or `off`, without
regard to case. Numeric values must be finite and use the type shown by the
example. Repeated `-v` forms such as `-vv` are accepted; currently any positive
verbosity count enables the same diagnostic stream.

`catalog_db` accepts a filesystem path, a `sqlite://` URL, or a
`postgresql://`/`postgres://` DSN. `--catalog-url` is only an alias for the CLI
option; the YAML key remains `catalog_db` and the environment variable remains
`COGNISTORE_CATALOG_DB`. Writable SQLite and PostgreSQL catalogs apply the
packaged Alembic migrations automatically. A read-only command refuses a
catalog that has not already reached the current migration head. The
[PostgreSQL catalog operations guide](postgres_catalog.md) covers schema
ownership and SQLite cutover.

Some commands impose stronger requirements than the global default. Workers,
move inspection, move recovery, `content-reference-report`, and
`policy-dataset-export` require a persistent SQL catalog. The reference report
and dataset export require an existing catalog and open it read-only, without
`--drivers` or `--base`. `policy-dataset-validate` reads a local JSON file or stdin
and requires no catalog. `policy-baseline-train` and `policy-baseline-evaluate`
read local JSON artifacts and require no catalog, drivers, or queue service.
`consistency-scan`, `consistency-export`, and `consistency-repair` require a persistent catalog locator,
`--drivers`, and a trusted `--scope-config` tenant binding. The scan opens the
existing catalog read-only; export verifies the binding without contacting it.
`orphan-cleanup` requires those same inputs and an existing tenant catalog
partition. It defaults to a read-only report; `--quarantine` and explicit
`--execute --candidate-id` open that partition writable without migrations.
Repair plans open the catalog read-only; `--enable-repair` allows safe move
resumption using the existing catalog schema, without installing migrations.
Scheduler state is always SQLite: a PostgreSQL worker requires an
explicit persistent `schedule_db`, while a SQLite worker may reuse its catalog
file when `schedule_db` is omitted. A non-preview scheduler requires a
persistent `schedule_db` or a SQLite `catalog_db` fallback. Tier operations
require `--drivers`; only `put`, `get`, and `ls` can instead use the single POSIX
`--base` driver.

## JSON v1 and stream contracts

`--json` reserves stdout for machine-readable output. Every object uses this
common envelope:

```json
{
  "schema": "cognistore.cli",
  "schema_version": 1,
  "command": "move",
  "status": "completed"
}
```

Command-specific fields are added to that envelope. Consumers should select a
decoder using `schema`, `schema_version`, and `command`, branch on `status`,
and tolerate additional fields. Typical successful statuses are `success`,
`planned`, `queued`, `completed`, `redriven`, and `ready`.

`--help` is human-readable by default. With `--json`, it returns one v1 success
object whose `help_format` is `text` and whose `help` field contains the same
usage text.

Finite commands write exactly one JSON object followed by one newline. A
successfully started `worker` or `scheduler` writes one `status: "ready"`
object, including in `--once` mode. If the daemon later exits with an
operational failure, it may append a v1 error object; treat daemon stdout as a
JSON Lines event stream. Periodic `auto-refresh --interval SECONDS` writes one
`status: "completed"` v1 object per completed cycle, so its stdout is JSON
Lines. The one-cycle `auto-refresh` form writes one object.

Human explanations, warnings, progress, verbose diagnostics, and logging go to
stderr in JSON mode. They never precede or follow a finite command's JSON
object on stdout. In human mode, successful results use stdout and diagnostics
or errors use stderr.

Failures use `status: "error"` and include this stable v1 core:

```json
{
  "schema": "cognistore.cli",
  "schema_version": 1,
  "command": "move-resume",
  "status": "error",
  "error_type": "MoveJobNotFound",
  "error": "move job not found: manual:reports-2026-08",
  "exit_code": 1,
  "retryable": false
}
```

`command` can be `null` when failure happens before a command is identified.
The version 1 exit contract guarantees `0` for a successful operation or
preview, `130` for interruption, and a non-zero status for every other
failure, even when a valid error object was written. Values `1` and `2` do not
carry stable category semantics in schema version 1 across parser,
configuration, handler-validation, and operational-preflight paths.
Automation must branch on the JSON `status` and `error_type`, not infer a
category from `1` versus `2`.

### Verbose output and redaction

`-v` enables configuration provenance and failure diagnostics on stderr. JSON
mode does not disable verbose output; it keeps those diagnostics off stdout.

Before CogniStore renders JSON, errors, or verbose messages, it recursively
replaces recognized credentials with `[REDACTED]`. Redaction covers sensitive
mapping keys (including passwords, API/access/secret keys, tokens, cookies,
credentials, and private keys), URL user information and sensitive query
parameters, authorization headers, cookies, bearer/basic credentials, PEM
private-key blocks, and values attached to or following sensitive-looking CLI
options. Operational identifiers such as profile names, correlation IDs, and
move idempotency keys remain visible. Redaction is defense in depth rather than
secret storage: keep literal credentials out of command lines and
configuration files and use the storage driver's documented credential chain.

## Dry-run contract

`--dry-run` performs validation and the reads needed to build a useful plan,
but it does not perform CogniStore-managed storage, catalog, queue, cache, or
local-output writes. A plan can therefore fail when a source is absent, a
destination collides, a profile input is invalid, or another precondition is
not satisfied. `--json` represents a preview with `status: "planned"` and,
where applicable, `dry_run: true`.

Read-only commands accept `--dry-run` for uniform scripting; it does not change
their query. The complete command matrix is:

| Command | Dry-run behavior |
| --- | --- |
| `put` | Validates the local input and reports its size and whether the destination would be overwritten, without writing the object. |
| `get` | Stats the source and reports the local output path, size, and whether it would overwrite a file; does not create directories or write the file. |
| `ls` | Read-only. Runs the normal listing and makes no mutations. |
| `move` | Validates the complete source/destination plan and reports a planned move without storage or catalog writes. With `--idempotency-key`, it checks the persistent journal: an incomplete exact match reports `would_resume`, a completed match reports `already_completed`, and a failed job returns a non-zero error. |
| `move-status` | Read-only. Returns the same stored job and transition history. |
| `move-list` | Read-only. Returns the same deterministic, optionally filtered job list. |
| `move-resume` | Reads the durable job and reports `would_resume` or `already_completed`; it does not advance the job, transfer data, clean up a source, or write the catalog. `resume_preconditions` records the checked journal state and driver pair, reports that no ownership claim was attempted, and names every phase-specific storage condition left unchecked. A non-terminal preview therefore reports `readiness: "not_confirmed"`; the writable resume may still fail. |
| `content-reference-report` | Read-only in every mode. Returns the same deterministic comparison of stored and topology-derived reference counts plus grace-period reclamation eligibility. It never repairs catalog state or deletes content. |
| `consistency-scan` | Always read-only toward source catalog and storage. Dry-run scans into a disposable temporary report; with `--resume`, previews a snapshot of the checkpoint. No report, checkpoint, or audit changes are retained. |
| `consistency-export` | Validates the report's tenant/source binding and new output target, returning the saved summary without creating an export or audit event. |
| `orphan-cleanup` | Reports current protections without writes by default. `--dry-run` previews either quarantine or execution; `--no-dry-run` alone does not authorize deletion. |
| `consistency-repair` | Returns current repair decisions without changing the report, catalog, or storage; overrides `--enable-repair`. Without either flag, plans are audited only in the report. |
| `schedule-run-list` | Read-only. Lists durable scheduled occurrences, with optional state, schedule-ID, and expired-running-lease filters. |
| `schedule-run-status` | Read-only. Returns one occurrence plus its immutable fenced-recovery audit records. |
| `schedule-run-recover` | Requires `--confirm-former-worker-fenced` in both modes. Dry-run opens the SQLite scheduler store read-only and checks the exact run, expected owner, expired lease, and scope lock. It does not clear ownership or write an audit record; the writable command atomically rechecks every condition. |
| `ls-tier` | Read-only. Runs the normal tier listing and makes no mutations. |
| `catalog-scan` | Scans the selected storage scope synchronously and returns the objects that would be indexed. It neither updates the catalog nor enqueues a background job; `--sync` is unnecessary. |
| `tier-profile` | Validates and lists supported tier paths and any `--metrics-out` target. It does not run storage benchmarks or write metrics. This command profiles storage tiers; it is unrelated to selecting a CLI configuration profile. |
| `devices-scan` | Performs read-only OS device discovery, but does not write `--hardware-out`. |
| `auto-refresh` | Reports the cache paths, interval, and tiers without creating the cache directory, discovering devices, profiling storage, writing cache files, or entering the periodic loop. |
| `policy-run` | Runs policy selection synchronously and returns planned actions without moves, catalog updates, or queue publication. It may read an existing catalog and fresh discovery caches, but never refreshes a missing/stale cache during a preview. |
| `policy-dataset-export` | Reads an existing catalog and computes the same privacy-filtered dataset, but reports the planned output without writing a file or creating directories. |
| `policy-dataset-validate` | Read-only. Validates the input JSON and returns the same issue report and exit status. |
| `policy-baseline-train`, `policy-baseline-evaluate` | Compute and validate the same model or evaluation report, but do not write the output file or create directories. JSON includes the computed artifact. |
| `worker` | **Unsupported.** Consuming a delivery necessarily owns and settles queue state and may run writable catalog or storage handlers, so there is no faithful side-effect-free worker preview. The command exits with usage status `2`; preview the originating `catalog-scan` or `policy-run` command instead. If a profile enables `dry_run`, pass `--no-dry-run` when starting a worker. |
| `scheduler` | Loads and validates the drivers and schedule file, then reports every declared schedule. It does not require or open the scheduler catalog, inspect durable due state, connect to NATS, reserve occurrences, or publish jobs; JSON includes `due_state_checked: false`. |
| `dead-letter-redrive` (`job-redrive`, `dlq-redrive`) | Validates and canonicalizes the dead-letter UUID, then reports a planned redrive. It does not connect to NATS, check whether the entry exists, or republish it; JSON includes `existence_checked: false`. |

The default writable forms of `catalog-scan` and `policy-run` publish a durable
job. `--sync` instead performs their writes inline for development. Dry-run
always remains synchronous and does neither kind of write. Background
submissions ignore a configuration-file or environment `catalog_db`; the
worker uses its own persistent catalog. Passing `--catalog-db` or
`--catalog-url` explicitly to a background submission is a usage error so an
operator cannot accidentally target the wrong journal. Other commands that do
not consume a catalog likewise do not open or create the configured database.

## Policy dataset export and validation

```bash
cognistore --catalog-db catalog.db policy-dataset-export \
  --output policy-dataset.json --as-of 2026-09-09T00:00:00Z \
  --after 2026-09-01T00:00:00Z --before 2026-09-08T00:00:00Z \
  --sample-rate 0.25 --seed september --observation-seconds 86400 \
  --exclude-field snapshot.object.pool_id --json
cognistore policy-dataset-validate --input policy-dataset.json --json
```

`policy-dataset-export` requires `--output`; all other command options are
optional. `--as-of` defaults to now, the decision time bounds are unbounded,
`--sample-rate` defaults to `1`, `--seed` to `0`, and `--observation-seconds` to
`86400`. Timestamps must include a timezone. Repeat `--exclude-field` for
additional row-relative dotted paths, using `*` for list elements. Default
sensitive-field exclusions always apply. The parent output directory must
exist; the complete JSON file is published atomically.

`policy-dataset-validate` accepts a local `--input` path or reads stdin when
omitted or set to `-`. It exits `0` if valid and `1` for validation or input
errors. `--allow-missing-labels` permits incomplete supervised labels while
retaining other checks. JSON reports contain `valid`, `count`, and `issues`
with a code, path, and message. See [Policy datasets](policy_datasets.md) for
versioning, privacy, sampling, replay, and label semantics.

`policy-baseline-train --input DATASET --training-config CONFIG --output MODEL`
trains an offline baseline from an exported snapshot.
`policy-baseline-evaluate --input DATASET --model MODEL --output REPORT`
compares it with the recorded rules on time-based holdouts. Both support
`--json` and `--dry-run`; output directories must already exist for publication.
See the [supervised baseline guide](policy_baseline.md) for reproducible examples,
model provenance, leakage checks, metrics, and promotion requirements.

## Tenant-scoped consistency checks

`consistency-scan` compares catalog placements, backend listings/stats,
generation-bound checksums, and durable move state. It records discrepancies in
a separate SQLite report and supports bounded work and resumable checkpoints:

```sh
cognistore --drivers drivers.yaml --catalog-db catalog.db consistency-scan \
  --tenant acme --scope-config tenants.yaml --report reports/acme.sqlite3 \
  --max-items 1000 --json

cognistore --drivers drivers.yaml --catalog-db catalog.db consistency-scan \
  --tenant acme --scope-config tenants.yaml --report reports/acme.sqlite3 \
  --resume --max-items 1000 --json

cognistore --drivers drivers.yaml --catalog-db catalog.db consistency-export \
  --tenant acme --scope-config tenants.yaml --report reports/acme.sqlite3 \
  --output reports/acme.jsonl --json

cognistore --drivers drivers.yaml --catalog-db catalog.db consistency-repair \
  --tenant acme --scope-config tenants.yaml --report reports/acme.sqlite3 --json

cognistore --drivers drivers.yaml --catalog-db catalog.db consistency-repair \
  --tenant acme --scope-config tenants.yaml --report reports/acme.sqlite3 \
  --enable-repair --json
```

The scope configuration binds each tenant to one bucket, key prefix, and set of
tiers. Scan `--prefix` and repeated `--tier` options can only narrow that binding.
`--page-size`, `--requests-per-second`, and `--bytes-per-second` bound inventory
pages and throttle backend reads. Inspect `summary.complete` before treating a
report as finished, and `summary.consistent` to distinguish completion from a
clean result. Scan and export never modify catalog or backend state.

Repair requires a completed report and its exact original scope. It defaults
to a plan audited in the report, with `status: "planned"` and
`summary.plan_only: true`. `--enable-repair` permits resuming eligible existing
moves using their original idempotency keys after current generation and
checksum validation. Unsafe or ambiguous findings receive a quarantine
decision for operator review without moving or deleting their data. Inspect
`summary.actions` and `summary.counts`, and run a new scan after repairs.
See the [consistency checks guide](consistency_checks.md) for the configuration
schema, trust boundary, report/export contracts, and scan limitations.

## Confirmed orphan cleanup

`orphan-cleanup` inspects one logical key and tier in an existing tenant catalog
and storage namespace. Default inspection is read-only. Quarantine persists
the object's generation and protection evidence, then explicit execution
rechecks references, jobs, holds, retention, and the elapsed grace period:

```sh
cognistore --drivers drivers.yaml --catalog-db catalog.db orphan-cleanup \
  --tenant acme --scope-config tenants.yaml --key acme/old.bin --tier hot --json

cognistore --drivers drivers.yaml --catalog-db catalog.db orphan-cleanup \
  --tenant acme --scope-config tenants.yaml --key acme/old.bin --tier hot \
  --quarantine --json

# After the saved candidate's grace period has elapsed:
cognistore --drivers drivers.yaml --catalog-db catalog.db orphan-cleanup \
  --tenant acme --scope-config tenants.yaml --key acme/old.bin --tier hot \
  --execute --candidate-id CANDIDATE_UUID --json
```

`--grace-period-seconds` and `--retention-seconds` each default to seven days
and require finite, strictly positive values. `--dry-run` previews any stage;
`--no-dry-run` alone remains a report. The JSON `summary.conditions` and
`summary.blockers` explain eligibility. Backend deletion failures return exit
code `1` with the durable candidate and retryable failure details; blocked
reports return `0`. See [orphan cleanup](orphan_cleanup.md) for tenant binding,
condition meanings, audit evidence, and retrying interrupted deletion.

## Shared-content reference report

An active [legal hold](legal_holds.md) conservatively disables reclamation for
every content blob in the tenant catalog. Each report entry exposes `legal_hold`;
held entries always have `reclamation_eligible: false`. This protects apparent
orphans when their ownership cannot be established.

Local `put` operations honor holds when supplied the same `--catalog-db` used by
the API and workers. Upload and move dry runs check holds without appending audit
events. Direct local storage access without that catalog is a trusted operator
interface and cannot discover holds in another catalog.

`content-reference-report` inspects an existing SQLite or PostgreSQL catalog
without loading storage drivers:

```bash
cognistore --catalog-db catalog.db content-reference-report --json

cognistore --catalog-db catalog.db content-reference-report \
  --grace-period-seconds 86400 --json
```

The default grace period is `604800` seconds (seven days). The option accepts a
finite, non-negative number; zero makes an otherwise consistent zero-reference
blob eligible immediately. `--dry-run` is accepted for scripting consistency
but does not change the query or output status because the command is always
read-only.

Reference counts preserve edge multiplicity. Each active logical object
contributes one full-object edge and one edge per chunk position. A digest used
as both the full object and its only chunk therefore contributes two edges, and
repeated equal chunks each contribute an edge. JSON reports the two derived
role counts, their total, the stored materialized count, `unreferenced_at`,
stable issue codes, and `reclamation_eligible` for every digest.

The top-level result includes `consistent`, `total_blobs`, `referenced_blobs`,
`unreferenced_blobs`, `eligible_blobs`, `generated_at`,
`grace_period_seconds`, and the ordered `entries` array. Finding an
inconsistency is a successful report: the command returns exit code `0` and
`status: "success"` with `consistent: false`. Every inconsistent entry is
ineligible. The stable initial issue codes are `reference_count_mismatch`,
`referenced_blob_marked_unreferenced`,
`unreferenced_blob_missing_timestamp`, and `invalid_unreferenced_timestamp`.

This command does not establish that physical bytes exist and does not delete
anything. Automated orphan reclamation remains a separate, later workflow.

## Durable manual-move recovery

Manual moves use the same durable journal as policy-driven moves. Supply a
stable caller-generated key and a persistent SQLite or PostgreSQL catalog when
you need to recover an interrupted CLI process:

```bash
MOVE_ID='manual:reports-2026-08'

cognistore --drivers drivers.yaml --catalog-db catalog.db \
  move hot warm demo-bucket reports/august.csv \
  --idempotency-key "$MOVE_ID"
```

The key is permanently bound to the exact source tier, destination tier,
bucket, and object key. Reusing it for different coordinates is rejected.
Replaying the same `move ... --idempotency-key` command resumes the recorded
non-terminal phase, even if destination publication already happened; a
completed replay is an idempotent success.

If the original process is interrupted, inspect the existing journal. These
read-only commands require an existing persistent `--catalog-db`, but they do
not require `--drivers`:

```bash
cognistore --catalog-db catalog.db move-status "$MOVE_ID" --json

cognistore --catalog-db catalog.db move-list \
  --state prepared --state transferred --state verified \
  --state committed --state cleanup \
  --idempotency-prefix 'manual:' --json
```

`move-status` returns the job and its ordered transition history. `move-list`
accepts repeatable `--state` filters for `prepared`, `transferred`, `verified`,
`committed`, `cleanup`, `completed`, and `failed`, plus an optional
`--idempotency-prefix`. Results are ordered by creation time and then
idempotency key.

Preview and resume from the stored coordinates with:

```bash
cognistore --drivers drivers.yaml --catalog-db catalog.db \
  move-resume "$MOVE_ID" --dry-run --json

cognistore --drivers drivers.yaml --catalog-db catalog.db \
  move-resume "$MOVE_ID" --json
```

`move-resume` requires an existing persistent catalog and the drivers for the
job's recorded tiers. It resumes only non-terminal jobs. A completed job
returns success with `outcome: "already_completed"`; a failed job returns a
non-zero `MoveJobFailedError` and its recorded terminal reason. This explicit
workflow is preferable to relying on an automatically generated key, because
an abrupt process loss can occur before that generated key is printed.

A non-terminal dry-run is intentionally a journal preview rather than a
readiness promise. Its `resume_preconditions` object marks the journal and
driver-pair checks as complete, marks the writable ownership claim as
unchecked, and lists the unchecked storage checks required by the recorded
phase. Only the writable command can atomically claim ownership and evaluate
those conditions against live storage.

## Scheduled-run inspection and recovery

`schedule-run-list` and `schedule-run-status` require an existing persistent
SQLite scheduler store but do not require `--drivers` or `--base`. Select it
with `--schedule-db`; for compatibility, `--catalog-db` is used as the fallback
when it names a SQLite file or SQLite URL. These commands reject non-SQLite
database URLs and open the scheduler file read-only. Use
`schedule-run-list --stale` to select only `running`
occurrences whose execution owner is present and whose lease expired, then use
`schedule-run-status JOB_ID` to copy the exact owner and review earlier
recovery records.

`schedule-run-recover` requires a stable recovery UUID, the expected owner,
operator identity, reason, fence evidence, and
`--confirm-former-worker-fenced`. A successful write preserves the occurrence
job ID, scope, and redrive generation, changes it to `retry_wait`, clears only
its stale execution ownership, and appends an immutable audit record. Repeating
an uncertain request with the same UUID and identical fields returns the same
record; changed fields fail closed. Use `--dry-run` first to receive the
`preconditions` object. The preview is a point-in-time inspection, so the write
still performs an atomic recheck.

The required process-fencing sequence, example commands, and failure handling
are in the [background-worker recovery runbook](background_workers.md#recover-a-stale-scheduled-run).

## Importance and residency controls

`importance-set BUCKET KEY LEVEL --actor ACTOR --provenance REASON` sets or clears
an attributed importance tag and returns an audited policy reevaluation. `LEVEL`
is `low`, `normal`, `high`, `critical`, or `clear`. Both this command and
`policy-run` accept repeatable `--minimum-residency TIER SECONDS` and
`--importance-tier LEVEL TIER` options. See
[importance and minimum residency](placement_controls.md) for examples, default
importance destinations, timer boundaries, and background-job compatibility.
