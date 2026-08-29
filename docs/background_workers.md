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

The [repository-managed container environment](setup_guide.md) runs this
topology with file-backed JetStream on a named volume. Do not use an in-memory
JetStream store for durable jobs.

## Start a worker

Workers load storage credentials and driver paths from their own trusted
configuration. Jobs carry operation inputs, never driver configuration or
credentials. All workers attached to one consumer must therefore use the same
drivers and catalog database. Catalog consumers use the backend-neutral
`CatalogStore` contract; the durable `SQLCatalog` implementation accepts
SQLite or PostgreSQL and owns a short transaction for each operation.

```bash
export COGNISTORE_CATALOG_DB='postgresql://cognistore@db.example/cognistore'

python -m cognistore.cli \
  --drivers drivers.yaml \
  --schedule-db /var/lib/cognistore/schedule.sqlite3 \
  --nats-url nats://127.0.0.1:4222 \
  worker
```

`--catalog-url` is an alias for `--catalog-db`; configuration files and the
environment use `catalog_db` and `COGNISTORE_CATALOG_DB`. A writable catalog
open applies the packaged Alembic migrations. Keep DSN credentials in the
deployment secret store rather than in commands or checked-in YAML. A
passwordless DSN can use libpq's `PGPASSWORD` or password-file support; Compose
maps `COGNISTORE_POSTGRES_PASSWORD` to `PGPASSWORD` for its clients. For local
single-host operation, a SQLite path such as
`--catalog-db /var/lib/cognistore/catalog.sqlite3` remains supported.

The scheduler coordination store is a separate concern. It is always a
persistent SQLite file. A PostgreSQL worker therefore requires
`--schedule-db`; a SQLite worker can omit it to reuse the SQLite catalog file
for backward compatibility. Every scheduler and worker that handles scheduled
jobs must use the same scheduler-state file. That topology is suitable for a
single host; do not place SQLite on an unsafe network filesystem to simulate a
multi-host coordinator.

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

## Schedule recurring work

The scheduler is a separate publisher process. Its YAML file has one required
`jobs` mapping, whose keys are stable schedule IDs:

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

Each entry requires `type`, boolean `enabled`, a positive
`interval_seconds`, and a `payload` mapping. Intervals must be representable at
microsecond precision and cannot exceed 100 years. A `catalog.scan` payload requires
`tier` and `bucket`; `prefix` is optional and defaults to the empty prefix. A
`policy.run` payload requires `bucket`, `policy` (`simple`, `llm`, or
`content`), a non-negative `threshold`, and a non-empty list of unique
`allowed_tiers`.
It also accepts `prefix`, `llm_threshold`, and the content-policy lists
`hot_name_patterns`, `warm_name_patterns`, `cold_name_patterns`,
`hot_mime_prefixes`, `warm_mime_prefixes`, and `cold_mime_prefixes`.

Start it with the same NATS topology, drivers, and SQLite scheduler-state file
used by its workers:

```bash
cognistore --drivers drivers.yaml \
  --schedule-db /var/lib/cognistore/schedule.sqlite3 \
  scheduler --schedule-config schedules.yaml
```

The shared persistent scheduler database is mandatory: it holds schedule
timing, the full pending job envelope, active-scope state, worker execution
leases, and fenced-recovery audits. It is outside the PostgreSQL catalog DAL
and outside the SQLite-to-PostgreSQL catalog import. For compatibility, a
SQLite deployment may pass only `--catalog-db catalog.sqlite3`; the scheduler
then uses that file. The SQLite `:memory:` database is rejected for normal
schedulers and workers because separate connections and processes cannot share
it. A new schedule is due immediately. Setting `enabled: false` prevents new
reservations; after changing it back to `true` and restarting the scheduler,
the schedule is immediately due again. Entries removed from the file are also
disabled in durable state.

Later due times use a fixed interval measured from reservation, not from job
completion. Only one occurrence may be active for an exact scope. If downtime
or a long-running occurrence spans several intervals, the scheduler reserves
one overdue occurrence when the scope is available and calculates its next due
time from that new reservation; it does not publish a catch-up burst. An
interval change stays anchored to the last reservation, so the new duration
takes effect when the scheduler reloads its configuration. Scope identity is
exact rather than hierarchical:

- `catalog.scan`: type, tier, bucket, and prefix
- `policy.run`: type, bucket, and prefix

Thus `reports/` and `reports/2026/` are distinct scopes. Policy tuning fields
do not change scope identity, and two configured entries for the same scope
are rejected. This single-flight lock applies to scheduler-created occurrences,
identified by their durable schedule metadata. A manually submitted scan or
policy job is an independent operator action and does not join that lock, so
operators must not target the same scope manually while its schedule is active.

Before publishing, the scheduler durably stores the complete envelope and a
fresh UUID job ID. A crash or uncertain publish leaves a pending reservation;
on restart, a scheduler reclaims it and publishes the same envelope with the
same message and job ID. A different occurrence always gets a different job
ID. Publication retries and worker delivery retries reuse an ID only for that
one occurrence. A permanently invalid or oversized envelope is recorded as a
terminal publication failure and releases its scope; a failed publication is
isolated so the scheduler still attempts other due scopes in that cycle.

Workers coordinate scheduled deliveries through a lease controlled by
`worker --schedule-lock-ttl` (60 seconds by default). The worker renews that
lease every one-third of the TTL. A second delivery cannot execute while the
run has a durable owner: TTL expiry is a liveness signal, not proof that a
thread-backed operation stopped, and never authorizes automatic takeover of a
`running` occurrence. A hard worker crash after that transition therefore
leaves the occurrence quarantined until an operator proves the former worker
has stopped and uses the fenced recovery workflow below. Lease expiry alone
never clears ownership.

### Recover a stale scheduled run

Use this workflow only after a worker process was lost while a scheduled run
was `running`:

1. Fence the former worker outside CogniStore. Stop its service, scale its
   deployment to zero, or isolate the host, and obtain durable evidence such as
   the exited PID, supervisor event, terminated instance ID, or deployment
   generation. Stop any automatic restart policy until recovery is complete.
2. Preserve the SQLite scheduler store and JetStream diagnostics. Wait at
   least the
   configured `--schedule-lock-ttl`, then list stale runs and inspect the exact
   occurrence. These commands open the scheduler store read-only.

   ```bash
   cognistore --schedule-db schedule.sqlite3 schedule-run-list --stale --json
   cognistore --schedule-db schedule.sqlite3 schedule-run-status JOB_ID --json
   ```

3. Copy the current `execution_owner` from the status response and choose a
   stable recovery UUID. Preview the request first. The preview checks the
   running state, expected owner, expired lease, and durable scope lock but
   does not reserve that state; the writable command rechecks all conditions
   in one transaction.

   ```bash
   recovery_id="$(python -c 'import uuid; print(uuid.uuid4())')"
   cognistore --schedule-db schedule.sqlite3 schedule-run-recover JOB_ID \
     --recovery-id "${recovery_id}" \
     --expected-owner OWNER_ID \
     --operator OPERATOR_ID \
     --reason "worker host terminated during scheduled scan" \
     --fence-evidence "instance i-0123456789 stopped; event 2026-08-27T20:15Z" \
     --confirm-former-worker-fenced --dry-run --json

   cognistore --schedule-db schedule.sqlite3 schedule-run-recover JOB_ID \
     --recovery-id "${recovery_id}" \
     --expected-owner OWNER_ID \
     --operator OPERATOR_ID \
     --reason "worker host terminated during scheduled scan" \
     --fence-evidence "instance i-0123456789 stopped; event 2026-08-27T20:15Z" \
     --confirm-former-worker-fenced --json
   ```

4. Restart a replacement worker on the same catalog and durable consumer. The
   original JetStream delivery resumes with the same job ID, schedule scope,
   and redrive generation. After it succeeds, verify `schedule-run-status` and
   allow the scheduler to publish the next interval.

Recovery atomically changes only the same occurrence from `running` to
`retry_wait`; it does not create a successor occurrence or advance the
dead-letter redrive generation. The immutable audit record retains the
recovery UUID, prior owner and lease, operator, reason, fence evidence, and
timestamp. Repeating an uncertain request with the same UUID and identical
fields returns the original record. Reusing that UUID with different fields,
recovering before lease expiry, or using an owner that changed since inspection
fails closed. Re-inspect rather than editing scheduler tables directly.

A retryable failure explicitly clears the delivery owner but keeps the logical
scope active, so delayed NAK and redelivery cannot overlap a later occurrence.
Success marks the occurrence complete and releases its scope before ACK; an
ACK-uncertain duplicate observes that terminal state and skips the handler.
Terminal or exhausted work releases the scope only after its dead-letter record
is durably published, before the source message is ACKed.

If delivery or lease renewal fails, the worker fails closed: it cancels the
handler and leaves the source message unsettled. Thread-backed storage work is
allowed to reach its safe side-effect boundary while its execution lease keeps
renewing. Once that boundary is reached, the worker attempts an owner-fenced
transition back to retryable state; if the SQLite scheduler store remains
unavailable, the run stays quarantined as `running` instead of risking overlap.
Missing or mismatched scheduler state likewise fails the worker closed instead
of dead-lettering and permanently wedging the real scope. A dead-letter redrive
reuses the occurrence ID and advances a durable redrive generation; the same
generation cannot execute twice, and an old occurrence cannot be redriven
after a newer occurrence for that scope has been created.

Additional scheduled job families, such as future repair operations, can be
added without branching in the scheduler: define a `ScheduledJobDefinition`
with payload normalization and scope construction, register it with
`ScheduleRegistry.register()`, and pass that registry to
`load_schedule_config()`. The corresponding worker handler remains a separate
required integration.

## Bounded concurrency, rates, and backpressure

Each worker owns at most `--max-in-flight` deliveries (default 8). The shared
durable consumer permits `--max-ack-pending` unsettled deliveries across all
workers (default 64), and that shared value must be at least the local worker
limit. Configure the same consumer value on every process.

The main work stream is finite and rejects new publications instead of evicting
older work. Its defaults are 10,000 stored messages and 1 GiB; set
`--stream-max-messages` and `--stream-max-bytes` before the subcommand on every
producer and worker that uses the stream. When a publication would exceed
either limit, submission raises a retryable queue-saturation error.
Acknowledging work frees capacity.

Per-tier controls use a separate YAML document:

```yaml
max_queue_depth: 32
defaults:
  source_concurrency: 2
  destination_concurrency: 2
  bytes_per_second: null
  operations_per_second: null
tiers:
  hot:
    source_concurrency: 4
    destination_concurrency: 2
    bytes_per_second: 268435456
    operations_per_second: 200
  warm:
    source_concurrency: 2
    destination_concurrency: 2
    bytes_per_second: 67108864
    operations_per_second: 50
```

Start the worker with `worker --tier-limits limits.yaml`. `null` byte or
operation rates mean unlimited. An operation token is charged to both the
source and destination tier when an object move is admitted. Source reads and
destination writes consume their tier's byte budget; destination verification
reads consume the destination budget again. Source and destination slots are
independent, and a move acquires both roles atomically. Policy batches are
interleaved by tier pair, and the bounded scheduler preserves FIFO order within
a pair while skipping a blocked pair when an unrelated pair can run.

After replacing the file with a fully validated configuration, send the worker
`SIGHUP`. Reload is atomic: active moves keep their permits, lower concurrency
limits delay only new admissions until existing work drains, and future stream
chunks use the new rates. An invalid reload leaves the last valid limits in
place. Tier limits may change live, but the tier names and `max_queue_depth`
are process-lifetime settings and require a worker restart. Storage driver
instances and credentials are never reloaded. If another `SIGHUP` arrives
during a reload, the worker performs another reload afterward so the newest
file version is not missed.

These pools are process-local. Running multiple worker processes multiplies
their possible backend load, so deployments must divide desired aggregate
limits between workers. Fairness applies to the bounded local admission window;
the single durable NATS subject remains FIFO for work that has not yet been
claimed.

### Upgrading an existing JetStream deployment

Ticket #25 intentionally tightens the durable topology. A stream created by an
older CogniStore release was unbounded and used `DiscardOld`, and its consumer
defaulted to one outstanding acknowledgement. New processes reject that
topology instead of silently changing a live queue or evicting retained jobs.

Before upgrading production, stop producers and workers, wait for the durable
consumer's acknowledgement-pending count to reach zero, and take a JetStream
snapshot or backup. Use your NATS administration tooling to edit the existing
stream in place: retain its work-queue policy, file storage, and subject; set
finite message and byte limits; select `DiscardNew`; disable `MaxAge`; and leave
the per-subject message limit unlimited. Choose initial finite limits at or
above the stream's current stored message and byte counts. Then update the
existing durable consumer's `MaxAckPending` to the value passed as
`--max-ack-pending` (64 by default). Editing in place preserves queued work; do
not delete or recreate a stream that still contains jobs. Restart every
producer and worker with the same stream limits and verify `/readyz` before
resuming submissions.

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

Explicit `--catalog-db` and `--catalog-url` options are intentionally rejected
on background submission because the worker owns the persistent catalog
configuration. This prevents a CLI from appearing to target one database while
a remote worker uses another.

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
responses, catalog or scheduler database contention, and live move leases are
retryable. Malformed envelopes or payloads, unknown job types, invalid storage
requests, permission
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
SQL catalog:

`prepared -> transferred -> verified -> committed -> cleanup -> completed`

`failed` is terminal and records the verification reason. Catalog placement is
committed atomically with the `verified -> committed` transition; cleanup then
rechecks the destination and performs the driver's idempotent source delete.
Move rows carry an owner and expiring lease. The same owner may immediately
resume its work, while another worker must wait for lease expiry after a
process failure.

Operators and diagnostics can use the CLI's `move-status` and `move-list`
commands or query `SQLCatalog.get_move_job()`, `list_move_jobs()`, and
`list_move_job_transitions()`. `Mover.recover_incomplete()` claims available
non-terminal jobs for synchronous callers. Policy handlers perform the
equivalent recovery through the tier admission controller before making new
placement decisions; a move with another live owner defers the whole delivery
instead of planning around an in-progress destination.

## Health and readiness

By default the worker listens only on `127.0.0.1:8081`:

```bash
curl --fail http://127.0.0.1:8081/healthz
curl --fail http://127.0.0.1:8081/readyz
```

`/healthz` reports whether the worker supervisor is live and returns the most
recent cached bus snapshot. `/readyz` performs a fresh connection, account,
stream, and consumer probe and returns 503 unless the worker is accepting
claims. Both JSON responses include worker state, active job IDs, local
capacity and saturation, completion counts, bus state, pending jobs,
outstanding ACKs, stored message/byte utilization, redeliveries,
retry/dead-letter counts, and the last error. A publication can still be
rejected below 100% byte utilization when the next message is larger than the
remaining space; callers should count the retryable queue-saturation error as
the authoritative rejection signal. When tier limits are active, the worker
payload also includes per-tier source/destination active and waiting counts,
admission queue depth, byte/operation totals, current throttling, and cumulative
throttle/saturation counters.

## Integration tests

Tests use unique streams and expect an isolated JetStream-enabled server:

```bash
export COGNISTORE_NATS_URL=nats://127.0.0.1:4222
python -m pytest -q -m integration \
  tests/integration/test_nats_worker.py \
  tests/integration/test_scheduled_run_recovery.py
```

They cover publish/claim/ACK, explicit and delayed NAK redelivery,
connection-loss restart redelivery with the same job and correlation IDs,
graceful in-flight shutdown, a live worker SIGKILL followed by fenced
same-occurrence recovery and later-interval progress, message and byte capacity
without eviction, DLQ/redrive behavior, and sequential/concurrent rejection of
conflicting consumer lease settings. Unit and conformance coverage additionally exercises
bounded concurrent claiming, per-tier source/destination fairness, byte and
operation pacing, atomic live reload, cancellation cleanup, and a 10,000-attempt
`tracemalloc` saturation stress case.

If a process dies between individual diagnostic chunk PubAcks and the manifest
PubAck, unreferenced content-addressed chunks can remain in the DLQ until
`--dead-letter-max-age`. They never authorize a source ACK and cannot be
mistaken for an operator-visible entry; a later delivery writes and verifies a
complete manifest before settlement.
