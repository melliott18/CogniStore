# ADR 0001: NATS JetStream for durable background workers

- Status: Accepted
- Date: 2026-08-13
- Ticket: [#18](https://github.com/melliott18/CogniStore/issues/18)

## Context

Catalog scans and placement passes are long-running control-plane work. Running
them inside the submitting CLI process means a lost terminal or process can
silently interrupt the operation. CogniStore needs a durable job boundary with
enqueue, claim, positive acknowledgement, negative acknowledgement,
redelivery, correlation metadata, and graceful worker shutdown.

The roadmap named NATS or Kafka for messaging and Celery, Dramatiq, or RQ for
workers. The first milestone needs one coherent foundation, not periodic
scheduling, job-specific retry/backoff, or a dead-letter/redrive system.

## Decision

Use NATS JetStream and the official asynchronous `nats-py` client. CogniStore's
small `AsyncWorker` runtime is the worker stack. Workers require NATS Server
2.10 or newer so durable consumers can be created with the atomic
create-only action.

The queue topology is:

- a file-backed JetStream stream using `WorkQueuePolicy`;
- one named, durable pull consumer shared by all worker instances;
- explicit per-message acknowledgements;
- `max_ack_pending=1` for the initial single-in-flight runtime;
- an acknowledgement deadline extended with `in_progress` heartbeats;
- unlimited broker redelivery attempts, with no backoff or DLQ policy yet.

Worker startup uses atomic create-only semantics, then reads and validates the
effective durable-consumer configuration. It never updates an existing
consumer. Concurrent first starts therefore select one configuration, and any
worker with a conflicting acknowledgement deadline or topology fails readiness
without shortening another worker's live lease.

Publishers wait for JetStream's publish acknowledgement. Successful handlers
use `ack_sync`, so completion is reported only after the server confirms the
ACK. Failed handlers NAK the delivery for immediate generic redelivery. A
worker process that disappears before settlement leaves the delivery pending;
JetStream offers it again after `ack_wait`.

Job envelopes are versioned JSON and carry a stable UUID `job_id`, a
`correlation_id`, creation time, job type, payload, and string metadata. The ID
and correlation value are also NATS headers. `Nats-Msg-Id` reduces duplicate
publishes inside JetStream's finite deduplication window, but does not change
the consumer delivery guarantee.

Malformed envelopes and unregistered job types fail the worker closed and stay
unacknowledged in JetStream. This preserves the only durable copy for operator
diagnosis. Quarantine, classification, retry limits, DLQ transfer, and redrive
remain deferred to ticket #24.

## Delivery contract

Delivery is **at least once**, never exactly once. Duplicate delivery is a
normal outcome when a worker dies, an ACK is lost, an acknowledgement deadline
expires, or a publisher retries outside the deduplication window.

Every consumer must therefore:

1. use `job_id` as the logical operation/idempotency key;
2. preserve `correlation_id` in logs and downstream work;
3. tolerate `context.attempt > 1` and `context.redelivered == True`;
4. perform side effects before returning, because return triggers ACK;
5. propagate failures so the runtime can NAK rather than silently lose work.

The current scan upserts are naturally repeatable. Fully idempotent movement
transactions are tracked separately by ticket #23; duplicate delivery remains
visible rather than being hidden by this runtime.

## Shutdown and health

On SIGINT or SIGTERM, readiness becomes false and the worker stops issuing new
pulls. It waits for in-flight handlers through the configured grace period,
ACKs completed work, and cancels cooperative handlers after the grace period so
they can NAK before the NATS connection drains. Thread-backed storage work
defers repeated cancellation until its side effects reach a safe boundary. If
it exceeds the normal shutdown deadlines, the worker keeps its connection and
heartbeat alive and continues waiting rather than allowing an overlapping
redelivery.

The worker exposes:

- `GET /healthz`: liveness of the worker supervisor, including while draining;
- `GET /readyz`: a fresh NATS and JetStream stream/consumer probe plus worker
  claim-acceptance state.

Readiness is false during startup, reconnection, shutdown, after a failed
JetStream probe, or when the durable topology is unavailable.

## Consequences

NATS JetStream supplies the required durability and acknowledgement primitives
without a second task-framework abstraction. Pull consumers give CogniStore
explicit flow control and a direct path to later worker scaling.

Running the system requires a JetStream-enabled NATS server and persistent
server storage. This ticket does not add repository-managed containers; that is
ticket #28. Operations are at least once, so job implementations must be made
idempotent rather than relying on the broker to provide exactly-once effects.

## Alternatives considered

- Kafka provides durable logs but requires more consumer-group and deployment
  machinery for this small work-queue foundation.
- Celery, Dramatiq, and RQ add useful task APIs but introduce another worker
  abstraction, and their most common broker combinations do not match the
  roadmap's NATS/Kafka direction as directly.
- An in-process or SQLite queue would simplify local setup but would not provide
  the selected scale-out message-bus foundation.
