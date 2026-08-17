# Background workers

CogniStore submits writable `catalog-scan` and `policy-run` commands to a NATS
JetStream queue. Dry-runs remain synchronous previews. `--sync` is an explicit
development escape hatch for running writable work inline.

## Prerequisites

Install the Python requirements and run NATS Server 2.10 or newer with
JetStream plus persistent storage. A local standalone server can be started
with:

```bash
nats-server -js -sd /var/lib/cognistore/nats
```

The repository-managed container environment belongs to ticket #28. Do not use
an in-memory JetStream store for durable jobs.

## Start a worker

Workers load storage credentials and driver paths from their own trusted
configuration. Jobs carry operation inputs, never driver configuration or
credentials. All workers attached to one consumer must therefore use the same
drivers and catalog database.

```bash
python -m cognistore.cli \
  --drivers drivers.yaml \
  --catalog-db /var/lib/cognistore/catalog.db \
  --nats-url nats://127.0.0.1:4222 \
  worker
```

The URL can instead be set with `COGNISTORE_NATS_URL`. The default topology is
stream `COGNISTORE_JOBS`, subject `cognistore.jobs`, and durable consumer
`cognistore-workers`; deployments may override all three global options.
The first worker creates the consumer atomically. Later workers validate the
existing topology and refuse to start if settings such as `--ack-wait` differ;
they never rewrite a live shared consumer.

The worker handles SIGINT and SIGTERM gracefully. Tune `--ack-wait`,
`--heartbeat-interval`, `--shutdown-grace`, and `--settlement-timeout` together;
the heartbeat interval must be lower than the acknowledgement deadline.
Storage operations already running in a Python thread cannot be killed safely.
If one exceeds both shutdown deadlines, the worker remains draining, keeps its
lease heartbeat alive, and waits for the operation's side-effect boundary
before it settles the job and closes NATS.

Application retries are configured with `--max-attempts` (default 7),
`--retry-base-delay` (1 second), `--retry-max-delay` (30 seconds), and
`--retry-jitter` (0.2, or 20%). The default retry window is long enough for a
job delivered to another worker to outlive the default 30-second move lease.

## Submit work

```bash
python -m cognistore.cli --drivers drivers.yaml \
  catalog-scan hot demo-bucket --prefix reports/ --json

python -m cognistore.cli --drivers drivers.yaml \
  policy-run demo-bucket --prefix reports/ --threshold 1048576 --json
```

Submission returns `job_id`, `correlation_id`, stream sequence, and whether the
publish was deduplicated. Pass `--job-id UUID` when retrying an uncertain
submission inside the server deduplication window, and `--correlation-id VALUE`
to carry an existing request or trace identifier.

`--catalog-db` is intentionally rejected on background submission because the
worker owns the persistent catalog configuration. This prevents a CLI from
appearing to target one database while a remote worker uses another.

## At-least-once warning for consumers

**A handler can receive the same logical job more than once.** A worker can
finish side effects and lose its ACK, or die before ACK, causing redelivery.
Publish deduplication does not prevent delivery duplicates.

Handlers must use the stable `job_id` as an idempotency key, propagate the
`correlation_id`, and tolerate `JobContext.attempt > 1`. The runtime does not
silently suppress duplicates. `JobContext.cumulative_attempt` includes attempts
from earlier operator redrives, while `attempt` is the current JetStream
delivery cycle.

## Retry and terminal-error policy

Timeouts, connection failures, unavailable/5xx backends, throttling/429
responses, SQLite contention, and live move leases are retryable. Malformed
envelopes or payloads, unknown job types, invalid storage requests, permission
failures, destination collisions, and proven integrity mismatches are terminal.
Unclassified third-party backend exceptions receive bounded retries rather than
looping forever.

After failed delivery attempt `n`, the nominal delay is
`min(max_delay, base_delay * 2 ** (n - 1))`. Proportional jitter is applied and
the final delay remains capped. The worker sends a delayed NAK, so it does not
sleep or hold an acknowledgement lease during backoff. The message envelope,
job ID, correlation ID, and policy move idempotency namespace stay unchanged.

If a retryable failure reaches `--max-attempts`, or if a failure is terminal,
the worker publishes an immutable record to a separate file-backed limits
stream before acknowledging the source delivery. The default stream is
`<job-stream>_DLQ`, with subject prefix `<job-subject>.dead`; override them with
`--dead-letter-stream` and `--dead-letter-subject`. Records are retained for 30
days by default (`--dead-letter-max-age`), which must be at least the main
stream's deduplication window. Each logical record is written as checksummed,
payload-bounded immutable chunks followed by a compact manifest; the source is
ACKed only after every chunk and the manifest receive PubAcks. This allows even
a near-limit or malformed source message to be quarantined without exceeding
the NATS server's payload ceiling. Reassembly retains the exact original bytes
and headers, parsed envelope when valid, job and correlation identifiers,
delivery and cumulative attempts, source publication and stream/consumer
sequences, classification and reason, exception type/message, failure time,
and traceback.

Normal queue submission preflights the exact NATS header-plus-body wire size of
the original publication and the first redrive's intent, main-stream job, and
completion audit. It includes JetStream's expected-stream header and any lower
per-stream message limit. An envelope without enough headroom is rejected
before publication, rather than accepting a job that could be quarantined but
never republished. Every later redrive is rechecked before its intent is
written because the audit chain grows; newly submitted jobs are guaranteed one
redrive, not an unlimited number. Producers that bypass CogniStore and publish
raw NATS messages do not receive this headroom guarantee.

The worker logs the `dead_letter_id`. Redrive a valid entry with:

```bash
python -m cognistore.cli \
  --nats-url nats://127.0.0.1:4222 \
  dead-letter-redrive DEAD_LETTER_ID --json
```

Redrive keeps the original logical `job_id`, correlation ID, creation time, and
payload, but uses a distinct deterministic NATS transport message ID so it is
not suppressed by the main stream's publish-deduplication window. Immutable
dead-letter and redrive audit records link every cycle. An immutable intent is
persisted before the main-stream publish and a completion record follows its
PubAck, so a process loss cannot make a redrive unaudited; retrying a pending
intent remains at least once with the same logical job ID. The intent contains
the complete republish envelope and audit context, so it can finish even if the
older diagnostic entry expires during a prolonged main-stream outage. Repeated
invocations for a completed entry return the existing audit result. A malformed
envelope cannot be automatically redriven because repairing it is intentionally
out of scope.
The command makes only bounded connection attempts; with `--json`, operational
failures are emitted as a machine-readable error object and return nonzero.

Policy handlers derive a distinct move idempotency key for each object from the
stable policy-job ID. Each move persists these checkpoints in the worker's
SQLite catalog:

`prepared -> transferred -> verified -> committed -> cleanup -> completed`

`failed` is terminal and records the verification reason. Catalog placement is
committed atomically with the `verified -> committed` transition; cleanup then
rechecks the destination and performs the driver's idempotent source delete.
Move rows carry an owner and expiring lease. The same owner may immediately
resume its work, while another worker must wait for lease expiry after a
process failure.

Operators and diagnostics can query `SQLiteCatalog.get_move_job()`,
`list_move_jobs()`, and `list_move_job_transitions()`. `Mover.recover_incomplete()`
claims available non-terminal jobs, and policy handlers invoke it for the
current delivery before making new placement decisions.

## Health and readiness

By default the worker listens only on `127.0.0.1:8081`:

```bash
curl --fail http://127.0.0.1:8081/healthz
curl --fail http://127.0.0.1:8081/readyz
```

`/healthz` reports whether the worker supervisor is live. `/readyz` performs a
fresh connection, account, stream, and consumer probe and returns 503 unless
the worker is accepting claims. Both JSON responses include worker state,
active job IDs, completion counts, bus state, pending jobs, outstanding ACKs,
redeliveries, retry/dead-letter counts, and the last error.

## Integration tests

Tests use unique streams and expect an isolated JetStream-enabled server:

```bash
export COGNISTORE_NATS_URL=nats://127.0.0.1:4222
python -m pytest -q -m integration tests/integration/test_nats_worker.py
```

They cover publish/claim/ACK, explicit and delayed NAK redelivery,
connection-loss restart redelivery with the same job and correlation IDs,
graceful in-flight shutdown, DLQ/redrive behavior, and sequential/concurrent
rejection of conflicting consumer lease settings. Unit coverage includes
timeouts, throttling, unavailable backends, malformed requests, and retry
exhaustion.

If a process dies between individual diagnostic chunk PubAcks and the manifest
PubAck, unreferenced content-addressed chunks can remain in the DLQ until
`--dead-letter-max-age`. They never authorize a source ACK and cannot be
mistaken for an operator-visible entry; a later delivery writes and verifies a
complete manifest before settlement.
