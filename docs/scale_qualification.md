# Move scale and recovery qualification

The M1 qualification harness performs deterministic, integrity-checked object
moves through the real CogniStore drivers and move journal. It measures the
POSIX and S3-compatible paths separately, injects recovery faults, moves every
object forward and back, and writes a machine-readable evidence report.

> **Evidence status (verified 2026-08-29):** canonical run
> `full-20260827-205845` passed from clean revision `7961c82` with exactly one
> million objects on each required path, all eight fault scenarios recovered,
> and zero silent loss or corruption. The complete report and SHA-256 checksum
> are retained in the [M1 closeout evidence](evidence/m1/README.md). Repeated
> CI artifacts demonstrate reduced-scale repeatability; this retained artifact
> records the successful manual full-profile execution.

Run the harness from the repository root with this exact entry point:

```bash
python tests/perf/perf_async_put_get.py --help
```

The detailed JSON report is written to `--output`. Standard output contains
only a compact JSON summary, while progress is written to standard error. The
process exits `0` when the campaign passes, `1` when a started campaign fails,
and `2` when its configuration cannot be constructed.

## What one campaign does

For each configured `--path NAME:SOURCE_TIER:DESTINATION_TIER`, the harness:

1. requires an empty run-specific namespace on both tiers;
2. creates deterministic payloads on the source tier;
3. moves every object to the destination, injecting the selected faults;
4. verifies full SHA-256 payloads, namespace counts, catalog placements, and
   completed move journals;
5. replays a bounded sample of completed idempotency keys and verifies that no
   destination generation or transition history changes;
6. moves every object back to the original source and repeats the integrity
   and idempotency audits; and
7. atomically writes the JSON evidence file, including failure evidence when a
   campaign-level assertion fails.

Object keys are scoped below
`qualification/<run-id>/<path-name>/` and use zero-padded numeric names such as
`000000000003.bin`. A successful round trip leaves the verified objects on the
original source tier. It does not delete those objects, the SQLite catalog, the
JSON report, or an auto-created S3 bucket.

`--object-count` is per path. A full run with the two required example paths
therefore seeds two million distinct objects and performs four million logical
moves: one million forward and one million reverse on each path.

## Profiles

| Profile | Purpose and enforced constraints | Success value |
| --- | --- | --- |
| `reduced` | Fast regression evidence suitable for repeated execution. Defaults to 16 objects and may use any valid path set. A dirty checkout is recorded but is not rejected. | `reduced_scale_only` |
| `full` | Acceptance evidence. Requires exactly 1,000,000 objects per path, the canonical mixed-size workload, a clean identifiable Git revision, at least one POSIX-to-POSIX path, and at least one path containing an S3 driver. | `full_scale_passed` |

Both profiles perform forward and reverse moves and the same full-payload
audits. `--faults standard` is the default and is required to collect the four
recovery cases described below. `--faults none` is useful for a throughput-only
baseline, but it does not qualify failure recovery.

### Reduced local POSIX run

The repository `drivers.yaml` defines host-local `hot` and `warm` POSIX tiers.
Use a new run ID, catalog, output file, and empty qualification namespace for
every campaign:

```bash
set -euo pipefail

mkdir -p test-results
qualification_run_id="reduced-$(date -u +%Y%m%d-%H%M%S)"
qualification_catalog="test-results/${qualification_run_id}.sqlite3"
qualification_output="test-results/${qualification_run_id}.json"
test ! -e "${qualification_catalog}"
test ! -e "${qualification_output}"

python tests/perf/perf_async_put_get.py \
  --drivers drivers.yaml \
  --catalog-db "${qualification_catalog}" \
  --output "${qualification_output}" \
  --run-id "${qualification_run_id}" \
  --profile reduced \
  --object-count 16 \
  --path posix:hot:warm \
  --workers 4 \
  --faults standard
```

A passing report is evidence only for that reduced execution. Running the same
configuration again provides another comparable sample; one report alone does
not establish repeatability. The reduced profile deliberately produces
`acceptance_status: reduced_scale_only`.

### Full one-million-object command

Run the full campaign in an isolated Compose project so the paths and MinIO
hostname in `docker/drivers.yaml` resolve exactly as documented. The
development image does not need Git installed: the host first verifies the
checkout and passes its provenance into the container. Do not edit the
checkout while the campaign is running.

The following example uses fresh named volumes, a fresh SQLite path, a fresh
container output path, a fresh host output path, exactly 1,000,000 objects,
and the required paths `posix:hot:warm` and `s3:hot:object`:

```bash
set -euo pipefail

test -z "$(git status --porcelain)"
qualification_revision="$(git rev-parse HEAD)"
qualification_run_id="full-$(date -u +%Y%m%d-%H%M%S)"
qualification_catalog="/var/lib/cognistore/${qualification_run_id}.sqlite3"
qualification_container_output="/test-results/${qualification_run_id}.json"
qualification_host_output="test-results/${qualification_run_id}.json"
export COMPOSE_PROJECT_NAME="cognistore-${qualification_run_id}"

mkdir -p test-results
test ! -e "${qualification_host_output}"
docker compose up --build --wait
docker compose --profile development build development

docker compose --profile development run --rm --no-deps development \
  sh -c 'test ! -e "$1" && test ! -e "$2"' \
  qualification-preflight \
  "${qualification_catalog}" \
  "${qualification_container_output}"

qualification_status=0
COGNISTORE_QUALIFICATION_GIT_REVISION="${qualification_revision}" \
COGNISTORE_QUALIFICATION_GIT_DIRTY=false \
docker compose --profile development run --rm \
  -e COGNISTORE_QUALIFICATION_GIT_REVISION \
  -e COGNISTORE_QUALIFICATION_GIT_DIRTY \
  development \
  python tests/perf/perf_async_put_get.py \
    --drivers docker/drivers.yaml \
    --catalog-db "${qualification_catalog}" \
    --output "${qualification_container_output}" \
    --run-id "${qualification_run_id}" \
    --bucket cognistore-qualification \
    --profile full \
    --object-count 1000000 \
    --path posix:hot:warm \
    --path s3:hot:object \
    --workers 4 \
    --faults standard \
    --max-attempts 3 \
    --retry-base-delay 0.1 \
    --retry-max-delay 0.2 \
    --lease-seconds 0.05 \
    --worker-termination-timeout 30 \
  || qualification_status=$?

qualification_export_status=0
qualification_host_temporary=""
cleanup_qualification_temporary() {
  if [[ -n "${qualification_host_temporary}" ]]; then
    rm -f -- "${qualification_host_temporary}" || true
  fi
}
trap cleanup_qualification_temporary EXIT

set +e
qualification_host_temporary="$(mktemp \
  "test-results/.${qualification_run_id}.XXXXXX")"
qualification_export_status=$?
if (( qualification_export_status == 0 )); then
  docker compose --profile development run --rm --no-deps \
    --entrypoint cat development "${qualification_container_output}" \
    > "${qualification_host_temporary}"
  qualification_export_status=$?
fi
if (( qualification_export_status == 0 )); then
  if [[ -s "${qualification_host_temporary}" ]]; then
    mv -- "${qualification_host_temporary}" "${qualification_host_output}"
    qualification_export_status=$?
  else
    qualification_export_status=1
  fi
fi
if [[ -n "${qualification_host_temporary}" ]]; then
  rm -f -- "${qualification_host_temporary}"
  qualification_cleanup_status=$?
  if (( qualification_export_status == 0 && qualification_cleanup_status != 0 )); then
    qualification_export_status=${qualification_cleanup_status}
  fi
  if (( qualification_cleanup_status == 0 )); then
    qualification_host_temporary=""
    trap - EXIT
  fi
fi
set -e
if (( qualification_status != 0 )); then
  exit "${qualification_status}"
fi
if (( qualification_export_status != 0 )); then
  echo "qualification passed but its evidence could not be exported" >&2
  exit "${qualification_export_status}"
fi
```

The report records the supplied revision under
`environment.git.source: environment`. If `git` is available in the execution
environment, the harness instead obtains the revision and dirty state directly
and records `source: git`. The full profile fails before seeding unless the
recorded revision is non-empty and the dirty value is `false`.

The command is a procedure, not a checked-in result. Preserve the exported
JSON and review it before destroying the isolated volumes. The wrapper captures
the harness exit status, attempts the atomic export even when the campaign
fails, and then returns the original nonzero status.

## Workload configuration

| Flag | Default | Meaning and validation |
| --- | --- | --- |
| `--drivers PATH` | required | Driver YAML loaded by CogniStore. Every path tier must exist and each source/destination pair must represent different backends. |
| `--catalog-db PATH` | required | Persistent SQLite move catalog. `:memory:` is rejected and the path must not already exist. |
| `--output PATH` | required | Atomic JSON evidence destination. It must differ from the catalog path. |
| `--run-id ID` | UTC timestamp | Namespace and idempotency scope. Accepts 1-64 letters, digits, dots, underscores, or hyphens, starting with a letter or digit. |
| `--bucket NAME` | `cognistore-qualification` | Bucket used on every configured tier. |
| `--object-count N` | `16` | Positive object count per path. `full` requires exactly `1000000`; standard faults require at least four. |
| `--size BYTES[:WEIGHT]` | default mix below | Repeatable non-negative byte size with an optional positive integer weight. Supplying any `--size` replaces the complete default mix. The `full` profile requires the unchanged default mix. |
| `--path NAME:SOURCE:DESTINATION` | required | Repeatable path. Names must be unique; all three fields must be non-empty and cannot contain `/` or `:`. |
| `--workers N` | `4` | Positive thread count. The harness keeps no more than twice this number of ordinary tasks submitted at once. |
| `--seed N` | `29` | Integer used to generate deterministic SHA-256-derived payload bytes. It does not randomize the size cycle. |
| `--profile reduced\|full` | `reduced` | Selects the enforced evidence level described above. |
| `--faults none\|standard` | `standard` | Disables faults or runs all four standard fault scenarios per path. |
| `--max-attempts N` | `3` | Bounded attempts in the initial retry phase. Standard faults need at least two; backend unavailability intentionally reaches this limit before a separate same-key recovery phase. |
| `--retry-base-delay SECONDS` | `0.1` | Positive finite initial retry delay. Retries use deterministic exponential backoff with no jitter. |
| `--retry-max-delay SECONDS` | `0.2` | Positive finite cap for retry delay; it must be at least the base delay. |
| `--lease-seconds SECONDS` | `0.05` | Positive move-ownership lease. It must be shorter than the base retry delay so a retry can claim the interrupted job. |
| `--worker-termination-timeout SECONDS` | `30` | Positive time allowed for the child worker to reach its durable transfer checkpoint before the run fails. |

### Deterministic default size mix

When no `--size` flag is supplied, sizes repeat in a deterministic 100-object
cycle. The distribution does not depend on worker scheduling:

| Object size | Weight | Objects in a 1,000,000-object path | Payload bytes |
| ---: | ---: | ---: | ---: |
| 256 B | 50 | 500,000 | 128,000,000 |
| 1,024 B | 30 | 300,000 | 307,200,000 |
| 4,096 B | 15 | 150,000 | 614,400,000 |
| 16,384 B | 5 | 50,000 | 819,200,000 |
| **Total** | **100** | **1,000,000** | **1,868,800,000** |

Each path moves those logical bytes twice. The two-path full example therefore
reports 4,000,000 logical moves and 7,475,200,000 logical payload bytes moved,
before considering transfer retries and verification reads.

Payloads are generated from `SHA-256("<seed>:<path-name>:<index>")`, repeated
and truncated to the selected size. This makes every expected byte
reconstructable without storing a separate source manifest.

## Standard fault mechanisms

With `--faults standard`, object indices 0-3 on the forward leg of every path
exercise one fault scenario each. A scenario can inject more than one failure
event. The reverse leg is fault-free so it provides a clean return-path
measurement.

| Fault | Mechanism | Required evidence |
| --- | --- | --- |
| `timeout` | The destination proxy publishes the object, then raises a one-shot `TimeoutError`, creating an ambiguous write outcome. | Category `timeout`, visible publication recovered under the same idempotency key, intact source until verification, bounded recovery. |
| `throttling` | The destination proxy raises a one-shot error carrying HTTP status 429. | Category `throttled`, retryable attempt, intact source, same idempotency key, bounded recovery. |
| `backend_unavailable` | The destination proxy raises `ConnectionError` before every write in the initial bounded phase. After exactly `max_attempts` failures, the proxy becomes healthy and the harness performs a separate same-key resume. | Category `unavailable`, exact retry-limit enforcement, intact source on every failed attempt, and verified post-exhaustion recovery. |
| `worker_termination` | A spawned worker pauses only after persisting `TRANSFERRED`; the parent requests `multiprocessing.Process.kill()` and falls back to `terminate()` only if the process remains alive. The report records every requested action plus the observed exit code and signal name when the platform exposes one. | Persisted checkpoint, forced-termination exit evidence, intact source, lease expiry, resume by a new owner with the same idempotency key, verified completion. |

Timeout and throttling are one-shot. Backend unavailability persists through
the configured retry limit, then recovers in a separately counted resume phase.
Consequently, a scenario's total `attempts` may exceed `max_attempts`, while
`bounded_retry_attempts` may not; `recovery_attempts` records the later phase.
The standard backend-unavailable scenario must set `retry_limit_reached: true`.

Storage scenarios call `Mover` directly and use the production
`classify_job_error` function and `RetryPolicy`. They do not exercise
JetStream delivery, DLQ publication, or operator redrive; those production
worker contracts remain covered by the NATS integration suite. This report's
`fault_execution_scope` states that they are not evaluated here. The report records
every failed attempt, classification, source-retention result, configured
limit, and recovery time. The termination case also records the durable
checkpoint and child exit code. Any missing injection, incorrect
classification, unplanned exhaustion, source loss, checksum mismatch, catalog
mismatch, non-idempotent replay, or extra object causes the campaign to fail.

## Evidence JSON and metric definitions

Reports use schema `cognistore.move-qualification`, version `1`. The principal
top-level structure is:

```json
{
  "schema": "cognistore.move-qualification",
  "schema_version": 1,
  "profile": "reduced",
  "acceptance_status": "reduced_scale_only",
  "status": "passed",
  "started_at": "2026-08-26T12:00:00.000000Z",
  "finished_at": "2026-08-26T12:01:00.000000Z",
  "environment": {},
  "configuration": {},
  "drivers": {},
  "paths": [],
  "summary": {}
}
```

A failed started campaign sets `status: failed`, uses
`reduced_scale_failed` or `full_scale_failed`, and adds an `error` object with
the exception type and message. The current path is appended before work
begins and updated phase by phase, so completed setup, metrics, fault records,
audits, and bounded ambient-failure samples survive later failures.
Configuration errors that occur before a campaign report exists exit with
status 2 and may not produce an output file.

The report sections have these contracts:

| Section | Contents |
| --- | --- |
| `environment` | Hostname, OS/platform, machine, Python implementation/version, CogniStore version, CPU count, physical memory when available, Git revision/dirty/source, and selected CI environment identifiers. |
| `configuration` | Run ID, bucket, object count, weighted size classes, exact per-size object/byte distribution, paths, workers, seed, profile, fault scope/mode, every retry/lease/termination setting, resolved driver path, non-secret effective-driver fingerprint, and catalog path. |
| `drivers` | Used tier names with driver type, capabilities, and non-secret effective settings such as base path, sanitized endpoint, region, chunk size, multipart threshold, list page size, and addressing style. Endpoint userinfo, query, and fragment components are omitted. Credentials are never included or hashed. |
| `paths[]` | Path identity/backend types, namespace prefix, current phase/status, setup timing, forward and reverse metrics, injected fault evidence, bounded aggregate/sample evidence for ambient failures, replay counts, and forward/reverse integrity audits. |
| `summary` | Started/completed paths, objects per path, completed logical moves/bytes, fault-scenario and individual injection/failed-attempt counts, ambient-failure count, silent-loss/corruption status, acceptance criteria, and whole-campaign elapsed time. |

Forward and reverse phase metrics are defined as follows:

- `elapsed_seconds` is wall-clock time from starting that direction until every
  logical move completes. Audit and replay time is reported separately and is
  not part of the direction throughput.
- `objects_per_second` is successfully completed logical objects divided by
  direction wall-clock seconds.
- `mib_per_second` is logical payload bytes divided by 1,048,576 and direction
  wall-clock seconds. It excludes retry retransfers and verification reads, so
  it is not physical backend bandwidth.
- `attempts` counts all move attempts and `retries` is `attempts - objects`.
- `latency_ms` contains `min`, `p50`, `p95`, `p99`, and `max`. A sample begins
  when its logical move invocation begins and ends after durable verified
  completion. It includes that move's retries and backoff, but excludes time
  waiting in the thread-executor queue.
- Percentiles use the nearest-rank definition: sort `N` samples and select rank
  `ceil(percentile / 100 * N)`, with ranks starting at one.
- `recovery_seconds` begins at the first injected storage failure and ends at
  successful recovery. For worker termination it begins immediately after the
  child is killed and ends after the resumed move verifies.
- `observed_retries` aggregates non-injected backend failures by category,
  recovery and source-retention outcome, and maximum recovery time while
  retaining at most 20 detailed samples per path. This bounds report memory at
  full scale without hiding environmental instability; any unverifiable source
  retention fails the campaign.

The integrity audit reads and compares every complete deterministic payload,
requires the opposite tier to contain zero run objects, checks the exact
catalog placement, and requires a completed move journal. Consequently,
`silent_loss: 0` and `corruption: 0` are emitted only after those assertions
pass.

## CI reduced-scale evidence

CI runs one reduced qualification in the Compose container job after the live
integration and shutdown checks. The workflow forwards `GITHUB_SHA` as the
qualification revision with `dirty=false`, along with `CI`, `GITHUB_ACTIONS`,
`GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `GITHUB_SHA`, `RUNNER_OS`, and
`RUNNER_ARCH`. The report can therefore tie the container run to its clean CI
checkout and retain the GitHub runner identifiers. The campaign uses:

- `docker/drivers.yaml`;
- 12 objects per path;
- equal-weight sizes 0, 257, 4,097, and 16,385 bytes;
- paths `posix:hot:warm` and `s3:hot:object`;
- four workers, standard faults, three maximum attempts, 0.05-second fixed
  retry delay, a 0.02-second lease, and a 30-second worker checkpoint timeout.

A passing artifact is expected to report two completed paths, 48 logical
moves, eight recovered fault scenarios, 12 injected failure events, at least
12 failed attempts, zero silent loss, zero corruption, `status: passed`, and
`acceptance_status: reduced_scale_only`. The four injected scenarios per path
contribute exactly 12 failed attempts; ambient transient retries, if any, are
also included in the total.

The container writes `/test-results/m1-move-qualification.json`. CI exports it
through a same-directory temporary host file, requires that file to be
non-empty, and atomically renames it to
`test-results/m1-move-qualification.json`. A failed or empty container read
therefore leaves no final file that could be uploaded as evidence. CI uploads
the final file as the `m1-move-qualification` artifact for 30 days. Export and
upload steps run even after a preceding failure, so a failure report should
remain available when the harness reached report creation. A missing artifact
is a diagnostic gap, not evidence of success.

Each artifact represents one reduced-scale execution. CI automation makes the
same campaign available on subsequent runs, enabling repeatability to be
assessed across multiple artifacts. No single reduced artifact may be
presented as proof of repeatability or as the one-million-object result.

## Prerequisites, capacity, and cleanup

Before any run:

- install CogniStore and its development dependencies on CPython 3.10-3.14;
- use isolated writable tiers and a persistent writable SQLite location;
- start the configured S3-compatible service and provide credentials through
  the environment variables named in the driver YAML;
- choose a unique run ID and confirm its namespace is empty on both sides of
  every path;
- choose a nonexistent catalog path and a fresh output path; and
- provision enough capacity and inodes for source objects, temporary
  destination copies, the move journal and transition history, verification
  I/O, and the exported report.

For a full run, also capture the clean revision as shown above and retain
machine details, storage topology, container image versions, runner settings,
and the complete JSON report with the result. Accepted repository evidence is
stored under `docs/evidence/<milestone>/` with a human-readable index and a
`SHA256SUMS` manifest; the canonical M1 package is
[`docs/evidence/m1/`](evidence/m1/README.md).

After a run, copy the JSON evidence out before cleanup. `docker compose down`
preserves the named volumes. If the full example used its dedicated
`COMPOSE_PROJECT_NAME` and no other data was placed in that project, remove its
containers and all qualification data with:

```bash
docker compose --profile integration --profile development down \
  --volumes --remove-orphans
```

`--volumes` permanently deletes that Compose project's POSIX objects, MinIO
objects, catalog, NATS state, and container-side report. Do not use it against a
shared development project. For shared tiers, delete only the exact bucket and
`qualification/<run-id>/` prefixes represented by the report, then remove the
matching fresh catalog after preserving any required diagnostics.
