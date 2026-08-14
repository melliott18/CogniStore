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
silently suppress duplicates. Job-specific retry, dead-letter, and redrive
policy is deferred to ticket #24; transactional movement idempotency is ticket
#23.

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
redeliveries, and the last error.

## Integration tests

Tests use unique streams and expect an isolated JetStream-enabled server:

```bash
export COGNISTORE_NATS_URL=nats://127.0.0.1:4222
python -m pytest -q -m integration tests/integration/test_nats_worker.py
```

They cover publish/claim/ACK, explicit NAK redelivery, connection-loss restart
redelivery with the same job and correlation IDs, graceful in-flight shutdown,
and sequential/concurrent rejection of conflicting consumer lease settings.
