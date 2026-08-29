# CogniStore architecture

This document describes the currently implemented architecture. It is a
current-state reference, not a promise that every later M2–M4 component already
exists.

## System view

```text
CLI / operator -- catalog calls --> CatalogStore --> memory / SQLite / PostgreSQL
      |
      +-- enqueue --> NATS JetStream -- at least once --> async workers
                          ^                                  |       |
                          |                                  |       +-- catalog --> CatalogStore
                    scheduler                               |
                          ^                                  +-- coordinate --+
                          |                                                   |
                          +---------- SQLite scheduler state <----------------+

async workers -- handlers --> scan / policy / mover -- driver contract -->
                              POSIX and S3-compatible storage tiers
```

The CLI can execute selected development operations synchronously, but normal
catalog scans and policy passes are submitted as durable jobs. The scheduler
also publishes durable envelopes; it never runs scan or policy logic inline.

## Components

| Area | Implementation | Responsibility |
| --- | --- | --- |
| CLI | `cognistore/cli/` | Strict configuration/profile resolution, human or JSON output, previews, storage commands, worker/scheduler lifecycle, move recovery, and DLQ redrive |
| Storage | `cognistore/drivers/` | Common object contract plus POSIX and S3-compatible drivers, streaming I/O, capability flags, generations, conditional deletion, and durability hooks |
| Catalog | `cognistore/core/catalog.py`, `cognistore/db/` | Backend-neutral `CatalogStore` contract plus the in-memory `Catalog` and transactional `SQLCatalog`; normalized objects, placements, tiers, pools, scan fences, durable move journals, leases, and transition history on SQLite or PostgreSQL |
| Movement | `cognistore/core/mover.py`, `move_jobs.py` | Bounded transfer, full SHA-256 verification, generation fencing, resumable phases, and catalog placement commit |
| Queue | `cognistore/jobs/nats_queue.py` | Bounded JetStream setup, publish/claim/ACK/NAK, health, dead-letter records, and redrive |
| Worker | `cognistore/jobs/runtime.py`, `handlers.py` | Bounded concurrent delivery, heartbeats, retry classification, graceful shutdown, and job dispatch |
| Scheduler | `cognistore/jobs/scheduler.py` | Strict interval configuration, durable reservations, envelopes with retry-stable identity, scoped single-flight execution, and restart recovery before execution begins |
| Throughput | `cognistore/core/throughput.py` | Per-tier source/destination concurrency, bounded admission, operation/byte rates, fairness, live reconfiguration, and metrics snapshots |
| Discovery/policy | `cognistore/core/scanner.py`, `policy*.py`, `utils/` | Storage observations, scan/move coordination, prototype placement rules, device discovery, and tier profiling |

## State and ownership

CogniStore currently has four durable state planes:

1. JetStream owns job delivery, redelivery, and source-message settlement.
2. The catalog DAL owns object placement, move state and transitions, and move
   leases in either PostgreSQL or SQLite.
3. `SQLiteScheduleStore` separately owns scheduled occurrence/reservation
   state, execution leases, and fenced-recovery audits.
4. Storage backends own object bytes and backend-specific generation tokens.

No one plane is sufficient evidence that an operation completed. A move is
complete only after storage verification, a durable catalog transition, safe
source cleanup, and terminal job state. A worker ACK happens only after its
handler and coordination transitions succeed.

Core movement, scanning, policy, and worker code depend on the `CatalogStore`
interface rather than database-specific SQL. The in-memory `Catalog` remains
useful for isolated synchronous operation and tests. `SQLCatalog` is the
durable implementation: it owns one short transaction per operation and
supports both SQLite and PostgreSQL through the same contract. Writable opens
apply the packaged Alembic migrations; PostgreSQL migration startup is
serialized, and read-only opens reject a schema that is not at the current
head.

The normalized catalog schema separates objects, their single current
placement, tiers, and pools. Durable object and move-claim fence rows serialize
concurrent mutations. PostgreSQL additionally provisions the `vector`
extension and records whether CogniStore created it, but embedding and search
indexes are not part of this milestone. Scheduler state intentionally remains
outside the catalog DAL in a persistent SQLite file. See the
[PostgreSQL catalog operations guide](postgres_catalog.md) for migrations,
cutover, and that compatibility boundary.

## Movement flow

The durable move state graph is:

```text
PREPARED -> TRANSFERRED -> VERIFIED -> COMMITTED -> CLEANUP -> COMPLETED
     |           |                                  |
     └----------> FAILED <---------------------------┘
```

- `PREPARED`: the source generation and move identity are durable.
- `TRANSFERRED`: destination bytes have been streamed without publishing a
  partial object.
- `VERIFIED`: size and full SHA-256 match the transferred source.
- `COMMITTED`: the verified destination placement is durable in the catalog.
- `CLEANUP`: destination durability/generation is reconfirmed and the exact
  source generation is conditionally deleted.
- `COMPLETED` or `FAILED`: terminal evidence is retained for inspection and
  idempotent replay.

The job is leased and keyed by a caller- or coordinator-owned idempotency key.
Replaying that key resumes its recorded phase; reusing it for different move
coordinates is rejected.

## Scheduling and delivery

Schedules are interval-based YAML definitions normalized by a registry. A
durable reservation contains the complete envelope, scheduled time, logical
scope, and fresh job ID before publication. Uncertain publication is retried
with the same identity. A later occurrence receives a different identity.

JetStream delivery is at least once. Worker handlers must therefore be
idempotent, and settlement order is deliberate:

- success is recorded before ACK;
- retry state is recorded before delayed NAK;
- terminal diagnostics are published to the DLQ before the source is ACKed;
  and
- unknown settlement outcomes do not trigger a contradictory second action.

A hard crash after scheduled execution becomes `running` leaves the occurrence
fail-closed because TTL expiry alone cannot prove prior side effects stopped.
The CLI provides read-only stale-run inspection and an explicit recovery that
requires former-worker fencing evidence, records an immutable operator audit,
and atomically releases the same occurrence for JetStream redelivery without
releasing its logical scope.

## Deployment topology

`Dockerfile` builds non-root runtime and development targets. `docker-compose.yml`
provides health-gated CogniStore, PostgreSQL with pgvector, file-backed NATS
JetStream, MinIO, integration tests, and an editable development shell. The
default worker uses PostgreSQL for its catalog and a separate SQLite file for
scheduler coordination. Named volumes retain catalog data, scheduler state,
objects, queue state, and test evidence across ordinary stops.

CI exercises Python 3.10–3.14, live NATS, MinIO, and PostgreSQL/pgvector
integration, package and security gates, the Compose shutdown probe, and a reduced movement
qualification. The manual full profile completed on 2026-08-29 with one million
objects on each POSIX and S3-compatible path; its
[report and checksum](evidence/m1/README.md) are retained as the M1 acceptance
artifact.

## Current boundaries

- PostgreSQL catalog persistence and pgvector extension setup are present;
  extraction, embeddings, vector/keyword indexing, Ask, REST, SDK, and UI are
  not current components.
- The catalog scanner's canonical checksum/metadata model is incomplete and
  tracked by BUG-2026-004 / #34.
- POSIX path containment rejects static symlinks but is not yet race-safe
  against a concurrent component swap; #91 owns production hardening.
- Authentication, authorization, tenancy, production observability, repair,
  Helm, and Terraform belong to M4.

See the [design contracts](design.md), [M1 closeout evidence](evidence/m1/README.md),
[historical M1 verification](m1_verification_2026-08-27.md), and
[next-ticket roadmap](next_ticket_roadmap_2026-08-27.md) for invariants, evidence,
open findings, and implementation order.
