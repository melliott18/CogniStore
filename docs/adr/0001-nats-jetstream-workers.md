# ADR 0001: NATS JetStream for durable background workers

- Status: Accepted
- Date: 2026-08-13
- Ticket: [#18](https://github.com/melliott18/CogniStore/issues/18)
- Amended: 2026-08-17 by [#24](https://github.com/melliott18/CogniStore/issues/24)
- Amended: 2026-08-18 by [#25](https://github.com/melliott18/CogniStore/issues/25)
- Amended: 2026-08-23 by [#19](https://github.com/melliott18/CogniStore/issues/19)

## Context

Catalog scans and placement passes are long-running control-plane work. Running
them inside the submitting CLI process means a lost terminal or process can
silently interrupt the operation. CogniStore needs a durable job boundary with
enqueue, claim, positive acknowledgement, negative acknowledgement,
redelivery, correlation metadata, and graceful worker shutdown.

The roadmap named NATS or Kafka for messaging and Celery, Dramatiq, or RQ for
workers. Ticket #18 established one coherent foundation and deliberately
deferred periodic scheduling, job-specific retry/backoff, dead-letter redrive,
and capacity controls. The #24 amendment added retry and redrive on that same
delivery identity. The #25 amendment adds bounded worker concurrency, queue
capacity, and per-tier movement admission and rates.

## Decision

Use NATS JetStream and the official asynchronous `nats-py` client. CogniStore's
small `AsyncWorker` runtime is the worker stack. Workers require NATS Server
2.10 or newer so durable consumers can be created with the atomic
create-only action.

The queue topology is:

- a finite file-backed JetStream stream using `WorkQueuePolicy` and
  `DiscardNew`, with no age or per-subject eviction limit;
- one named, durable pull consumer shared by all worker instances;
- explicit per-message acknowledgements;
- a configurable shared `max_ack_pending` limit (worker CLI default 64), paired
  with a smaller per-process in-flight limit (worker CLI default 8; the
  programmatic compatibility defaults remain 1 and 1);
- an acknowledgement deadline extended with `in_progress` heartbeats;
- unlimited broker redelivery as a safety net, with application-owned bounded
  delayed retries and a separate immutable dead-letter stream.

Each worker also owns a bounded, process-local movement controller. It acquires
source and destination concurrency slots atomically, paces operation admission
and streamed bytes per tier, and schedules runnable tier-pair lanes fairly
within the claimed window. Policy batches are interleaved across tier pairs.
The controller can atomically reload tier limits without revoking active work;
tier membership and admission-queue capacity remain restart-time settings.

Worker startup uses atomic create-only semantics, then reads and validates the
effective durable-consumer configuration. It never updates an existing
consumer. Concurrent first starts therefore select one configuration, and any
worker with a conflicting acknowledgement deadline or topology fails readiness
without shortening another worker's live lease.

The same exact-validation rule applies to the work stream's capacity and
non-eviction settings. Upgrades from the original unbounded `DiscardOld`
stream are an explicit operator migration performed only after active
deliveries drain; CogniStore never deletes, recreates, or silently mutates a
stream containing queued work.

Publishers wait for JetStream's publish acknowledgement. Successful handlers
use `ack_sync`, so completion is reported only after the server confirms the
ACK. Retryable handler failures use delayed NAK with bounded exponential
backoff and jitter. Terminal or exhausted deliveries are published to the
file-backed limits-retention DLQ before the source is acknowledged. A worker
process that disappears before settlement leaves the delivery pending;
JetStream offers it again after `ack_wait`.

Job envelopes are versioned JSON and carry a stable UUID `job_id`, a
`correlation_id`, creation time, job type, payload, and string metadata. The ID
and correlation value are also NATS headers. `Nats-Msg-Id` reduces duplicate
publishes inside JetStream's finite deduplication window, but does not change
the consumer delivery guarantee.

Periodic scans and policy passes are produced by a separate scheduler process.
The scheduler and workers share SQLite control-plane state in the worker's
catalog database. A due occurrence is transactionally reserved with its exact
envelope before publication; an uncertain publication is retried with the same
occurrence UUID, while every later occurrence receives a new UUID. Durable
scope state coalesces missed intervals and prevents a later occurrence from
starting while an earlier one is queued or retrying. Worker attempts acquire an
owner-fenced scope lease and renew its liveness timestamp while handling the
delivery. Expiry alone never transfers a durable `running` owner, because it
cannot prove that thread-backed side effects stopped; retry, success, or
confirmed dead-letter publication performs the explicit owner-fenced state
transition. The scheduler registry owns payload validation and scope derivation
so later repair job types can extend scheduling without changing its timing
engine.

Malformed envelopes and unregistered job types are terminal and move to the
DLQ with their exact raw bytes, headers, parse/validation traceback, source
publication identity, and sequence. Logical records use content-addressed,
payload-bounded chunks and a compact per-source manifest, so diagnostics larger
than the server's single-message limit remain publishable. Confirmed
publish-before-ACK order makes quarantine recoverable across an unknown
source-ACK outcome. Operators can redrive valid envelopes with the original
logical job ID; a distinct NATS transport message ID bypasses main-stream
publish deduplication. An immutable intent precedes that publish and a
completion record follows its PubAck, preserving the audit chain across the
redrive crash window. Queue submissions reserve a small amount of the server's
payload limit for that audit metadata using exact header-plus-body preflight,
and a self-contained pending intent can finish even after its older diagnostic
entry expires. Later redrives are admitted dynamically as their chain grows.

## Delivery contract

Delivery is **at least once**, never exactly once. Duplicate delivery is a
normal outcome when a worker dies, an ACK is lost, an acknowledgement deadline
expires, or a publisher retries outside the deduplication window.

Every consumer must therefore:

1. use `job_id` as the logical operation/idempotency key;
2. preserve `correlation_id` in logs and downstream work;
3. tolerate `context.attempt > 1` and `context.redelivered == True`;
   use `context.cumulative_attempt` when attempts across redrives matter;
4. perform side effects before returning, because return triggers ACK;
5. propagate failures so the runtime can NAK rather than silently lose work.

The current scan upserts are naturally repeatable. Policy movement uses the
stable job ID as the namespace for catalog-backed two-phase move idempotency;
duplicate delivery remains visible rather than being hidden by this runtime.

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
explicit flow control. Finite stream capacity, bounded local ownership, and
bounded movement admission keep broker and worker memory growth measurable
under overload.

Running the system requires a JetStream-enabled NATS server and persistent
server storage. This ticket does not add repository-managed containers; that is
ticket #28. Operations are at least once, so job implementations must be made
idempotent rather than relying on the broker to provide exactly-once effects.
Per-tier controls are process-local, so an operator running multiple worker
processes must divide aggregate backend limits between them. A single FIFO
subject can provide fairness only among work already claimed into a bounded
local window; global tier-aware broker scheduling would require partitioned
subjects or a distributed coordinator.
An interruption during a multi-message diagnostic write can leave unreferenced
content-addressed chunks until DLQ retention expires, but no source delivery is
ACKed without a complete, reassembled, checksum-verified manifest.

## Alternatives considered

- Kafka provides durable logs but requires more consumer-group and deployment
  machinery for this small work-queue foundation.
- Celery, Dramatiq, and RQ add useful task APIs but introduce another worker
  abstraction, and their most common broker combinations do not match the
  roadmap's NATS/Kafka direction as directly.
- An in-process or SQLite queue would simplify local setup but would not provide
  the selected scale-out message-bus foundation.
