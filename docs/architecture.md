# CogniStore architecture

This document describes the architecture implemented on `main` after the M1
work. It is a current-state reference, not a promise that the later M2–M4
components already exist.

## System view

```text
                         ┌─────────────────────┐
                         │ CLI / operator      │
                         └──────┬───────┬──────┘
                                │       │ enqueue
                                │       v
                    ┌───────────v──┐  ┌──────────────┐
                    │ Catalog/DAL  │  │ NATS         │
                    │ memory/SQLite│  │ JetStream    │
                    └──────┬───────┘  └──────┬───────┘
                           │                 │ at-least-once
                 ┌─────────v────────┐  ┌─────v────────────┐
                 │ Scheduler state │  │ Async workers    │
                 │ SQLite          │  │ retry / DLQ      │
                 └─────────┬────────┘  └─────┬────────────┘
                           │ enqueue          │ handlers
                           └──────────────────┤
                                              v
                               ┌────────────────────────┐
                               │ scan / policy / mover  │
                               └───────────┬────────────┘
                                           │ driver contract
                              ┌────────────v─────────────┐
                              │ POSIX and S3-compatible │
                              │ storage tiers           │
                              └──────────────────────────┘
```

The CLI can execute selected development operations synchronously, but normal
catalog scans and policy passes are submitted as durable jobs. The scheduler
also publishes durable envelopes; it never runs scan or policy logic inline.

## Components

| Area | Implementation | Responsibility |
| --- | --- | --- |
| CLI | `cognistore/cli/` | Strict configuration/profile resolution, human or JSON output, previews, storage commands, worker/scheduler lifecycle, move recovery, and DLQ redrive |
| Storage | `cognistore/drivers/` | Common object contract plus POSIX and S3-compatible drivers, streaming I/O, capability flags, generations, conditional deletion, and durability hooks |
| Catalog | `cognistore/core/catalog.py`, `sqlite_catalog.py` | Object placement/metadata, scan fences, durable move journals, leases, and transition history; in-memory and SQLite implementations |
| Movement | `cognistore/core/mover.py`, `move_jobs.py` | Bounded transfer, full SHA-256 verification, generation fencing, resumable phases, and catalog placement commit |
| Queue | `cognistore/jobs/nats_queue.py` | Bounded JetStream setup, publish/claim/ACK/NAK, health, dead-letter records, and redrive |
| Worker | `cognistore/jobs/runtime.py`, `handlers.py` | Bounded concurrent delivery, heartbeats, retry classification, graceful shutdown, and job dispatch |
| Scheduler | `cognistore/jobs/scheduler.py` | Strict interval configuration, durable reservations, envelopes with retry-stable identity, scoped single-flight execution, and restart recovery before execution begins |
| Throughput | `cognistore/core/throughput.py` | Per-tier source/destination concurrency, bounded admission, operation/byte rates, fairness, live reconfiguration, and metrics snapshots |
| Discovery/policy | `cognistore/core/scanner.py`, `policy*.py`, `utils/` | Storage observations, scan/move coordination, prototype placement rules, device discovery, and tier profiling |

## State and ownership

CogniStore currently has three durable state planes:

1. JetStream owns job delivery, redelivery, and source-message settlement.
2. SQLite owns object placement, move state and transitions, leases, and
   scheduled occurrence/reservation state.
3. Storage backends own object bytes and backend-specific generation tokens.

No one plane is sufficient evidence that an operation completed. A move is
complete only after storage verification, a durable catalog transition, safe
source cleanup, and terminal job state. A worker ACK happens only after its
handler and coordination transitions succeed.

The in-memory catalog is useful for isolated synchronous operation and tests.
Workers, schedulers, durable move inspection, and recovery require SQLite. M2
ticket #30 will introduce Postgres/pgvector behind a transactional DAL and a
documented SQLite migration path.

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
provides health-gated CogniStore, file-backed NATS JetStream, MinIO, integration
tests, and an editable development shell. Named volumes retain catalogs,
objects, queue state, and test evidence across ordinary stops.

CI exercises Python 3.10–3.14, live NATS and MinIO integration, package and
security gates, the Compose shutdown probe, and a reduced movement
qualification. The manual full profile completed on 2026-08-29 with one million
objects on each POSIX and S3-compatible path; its
[report and checksum](evidence/m1/README.md) are retained as the M1 acceptance
artifact.

## Current boundaries

- Postgres/pgvector, extraction, embeddings, keyword search, Ask, REST, SDK,
  and UI are M2 work, not current components.
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
