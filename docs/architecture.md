# CogniStore architecture

This document describes the currently implemented architecture, including the
M4 security, observability, repair, and deployment components. The
[reference architectures](reference_architectures.md) distinguish supported
deployment shapes and their qualification boundaries; the
[operator handbook](operator_handbook.md) provides operational procedures.

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
                              POSIX, S3-compatible, GCS, and Azure Blob tiers

CatalogStore -- bounded projection --> keyword search service --> Tantivy generations

CatalogStore -- authoritative metadata --+
pgvector similarity search --------------+--> Ask retrieval service --> ranked citations
Tantivy keyword search ------------------+             |
                                                      +--> optional answer provider
```

The CLI can execute selected development operations synchronously, but normal
catalog scans and policy passes are submitted as durable jobs. The scheduler
also publishes durable envelopes; it never runs scan or policy logic inline.
External Python applications use the typed SDK, which communicates only through
the REST API v1 boundary.

## Components

| Area | Implementation | Responsibility |
| --- | --- | --- |
| CLI | `cognistore/cli/` | Strict configuration/profile resolution, human or JSON output, previews, storage commands, worker/scheduler lifecycle, move recovery, and DLQ redrive |
| REST API | `cognistore/api/` | Versioned FastAPI transport, explicit public schemas, bounded cursor pages, stable errors, service injection, and deterministic OpenAPI generation |
| Python SDK | `cognistore/sdk/` | Server-independent typed REST v1 client, response and error models, cursor iteration, and asynchronous job polling |
| Storage | `cognistore/drivers/` | Common object contract plus POSIX, S3-compatible, GCS, and optional Azure Blob drivers, streaming I/O, capability flags, generations, conditional deletion, and durability hooks |
| Catalog | `cognistore/core/catalog.py`, `cognistore/db/` | Backend-neutral `CatalogStore` contract plus the in-memory `Catalog` and transactional `SQLCatalog`; normalized objects, placements, tiers, pools, scan fences, durable move journals, leases, and transition history on SQLite or PostgreSQL |
| Movement | `cognistore/core/mover.py`, `move_jobs.py` | Bounded transfer, full SHA-256 verification, generation fencing, resumable phases, and catalog placement commit |
| Queue | `cognistore/jobs/nats_queue.py` | Bounded JetStream setup, publish/claim/ACK/NAK, health, dead-letter records, and redrive |
| Worker | `cognistore/jobs/runtime.py`, `handlers.py` | Bounded concurrent delivery, heartbeats, retry classification, graceful shutdown, and job dispatch |
| Scheduler | `cognistore/jobs/scheduler.py` | Strict interval configuration, durable reservations, envelopes with retry-stable identity, scoped single-flight execution, and restart recovery before execution begins |
| Throughput | `cognistore/core/throughput.py` | Per-tier source/destination concurrency, bounded admission, operation/byte rates, fairness, live reconfiguration, and metrics snapshots |
| Discovery/policy | `cognistore/core/scanner.py`, `policy*.py`, `placement*.py`, `estimation.py`, `utils/` | Storage observations, behavioral/content signals, guarded rule and LLM decisions, offline baseline evaluation, structured explanations, estimated placement objectives, budget admission, device discovery, and tier profiling |
| Keyword search | `cognistore/search/` | Versioned normalized-passage projection, BM25 ranking, exact metadata filters, synchronous replace/delete visibility, and atomic full rebuild from the catalog |
| Ask | `cognistore/search/` | Hybrid retrieval and grounded citations |
| Security | `cognistore/auth/`, `cognistore/encryption.py` | JWT identity, role authorization, tenant partitions and storage namespaces, verified transport and deployment encryption evidence |
| Operations | `cognistore/core/`, `cognistore/cli/`, `helm/cognistore/`, `examples/terraform/aws/` | Legal holds, audit integrity, consistency reports and constrained repair, orphan quarantine/cleanup, Helm workloads and AWS infrastructure reference |

## State and ownership

CogniStore currently has four authoritative durable state planes plus one
reconstructable derived plane:

1. JetStream owns job delivery, redelivery, and source-message settlement.
2. The catalog DAL owns object placement, move state and transitions, move
   leases, access history, policy decision snapshots, cost/carbon budget
   definitions and reservations, legal holds, and audit-integrity evidence in
   tenant-owned PostgreSQL or SQLite partitions.
3. `SQLiteScheduleStore` separately owns scheduled occurrence/reservation
   state, execution leases, and fenced-recovery audits.
4. Storage backends own object bytes and backend-specific generation tokens.

The Tantivy keyword directory is derived from catalog extraction records. Its
versioned generations are persistent for query availability but are not source
data; a complete generation can be reconstructed through the catalog's bounded
full-object iterator.

Ask adds no durable state plane. It reads detached authoritative catalog
snapshots, vector hits from one explicit embedding space, and keyword hits from
the selected Tantivy generation. Because vector and keyword passage layouts are
distinct, it combines ranks at the catalog-object coordinate and preserves each
passage as source-qualified citation evidence. Common filters are applied again
to current catalog state before any candidate is returned. Optional answer
providers receive only the selected citations and do not gain catalog, index,
or storage access through the Ask contract.

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
extension, records whether CogniStore created it, and stores versioned
normalized-text passages in model-specific embedding spaces. Each compatible
space can own a partial expression HNSW index without mixing dimensions or
model revisions. Scheduler state intentionally remains outside the catalog DAL
in a persistent SQLite file. See the
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

## Policy decisions and admission

Policy runners project versioned MIME, embedding, and observed access features
through the catalog contract. Simple/content rules and schema-validated LLM
adapters use shared importance, minimum-residency, and cooldown controls.
Numerical hysteresis applies to simple/content rule boundaries; the validated
LLM adapter uses cooldown because it exposes no numerical boundary. Topology
eligibility filters hard locality requirements before estimate-based scoring.
Stability controls default to zero and take effect when configured. Provider
failure selects a safe stay; provider output cannot grant an override.
Execution rechecks authoritative constraints before movement.

Writable evaluations retain immutable feature snapshots and structured reasons
for moves, stays, and suppressions. REST, SDK, and the Placement UI expose frozen
before/after placement alongside separately derived job outcomes. A selection
or preview does not establish that a move completed.

Tier/pool observations carry units, sources, timestamps, and freshness.
Versioned estimators expose missing inputs rather than treating them as zero.
Configured cost/carbon allowances reserve modeled charges atomically with move
admission, including concurrent workers and conservative retry accounting.
Numeric-limit overrides require attributable audit evidence. What-if simulation
compares detached catalog snapshots without catalog or storage mutations.

The supervised baseline trains and evaluates offline against observed move
success labels. It gates historical rule decisions, does not choose new tiers,
and remains ineligible for production promotion. Its proxy comparisons do not
measure real financial savings or object-level flapping. See the
[M3 closeout evidence](evidence/m3/README.md), [placement controls](placement_controls.md),
[policy baseline](policy_baseline.md), and [budget guide](policy_budgets.md).

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

The opt-in `sample` Compose profile adds a finite loader followed by a
same-origin API/UI process. It composes the production extraction, Tantivy,
pgvector, REST, and citation paths with checked project-authored documents and
deterministic offline sample providers.

The [Helm chart](kubernetes.md) deploys the production API/UI and workers with
operator-managed PostgreSQL, JetStream, storage, identity, TLS, and runtime
Secrets. Normal independent worker scaling leaves the SQLite scheduler
disabled; enabling schedules constrains the scheduler and its workers to one
node with the same persistent file. The [AWS Terraform reference](terraform.md)
provisions private EKS, RDS, S3, and supporting infrastructure; application and
dependency releases remain separate operator steps. Neither deployment
creates a distributed Tantivy writer or automatically restores durable state.

CI exercises Python 3.10–3.14, live NATS, MinIO, and PostgreSQL/pgvector
integration, package and security gates, the Compose shutdown probe, and a reduced movement
qualification. The manual full profile completed on 2026-08-29 with one million
objects on each POSIX and S3-compatible path; its
[report and checksum](evidence/m1/README.md) are retained as the M1 acceptance
artifact.

## Current boundaries

- PostgreSQL catalog persistence, pgvector-backed versioned embeddings,
  metadata-filtered similarity search, bounded PDF/DOCX extraction, and
  embedded keyword indexing are present. The versioned Ask Python service blends
  bounded catalog metadata with optional vector and keyword providers, using
  object-level weighted reciprocal-rank fusion and authoritative catalog
  post-filtering. A versioned REST API exposes object, catalog, Ask, policy,
  and asynchronous action contracts. The typed Python SDK consumes that REST
  boundary and adds catalog iteration and durable-job polling conveniences.
  The packaged browser UI uses that same contract for keyword, vector, and
  hybrid Ask views, with exact filters, provider diagnostics, ranked evidence,
  metadata, and cited-object downloads.
- Catalog scans persist a full-source SHA-256 plus transactional, versioned
  source-byte chunk/CAS mappings. Active logical mappings maintain shared
  full-object and chunk-edge counts transactionally; logical deletion leaves
  global content intact, and read-only reconciliation reports conservative
  grace-period reclamation eligibility. Physical CAS deletion is not a current
  component. Deterministic normalized-text passage identities feed embedding
  providers without treating source-byte CAS chunks as text; keyword passages
  are a separately versioned normalized-text projection.
- The Tantivy adapter is single-writer and host-local. Catalog-to-index writes
  do not yet have a durable outbox, so failed updates are repaired by retry or
  full rebuild and rebuilds require a quiesced/replayed mutation window.
- Ask requires the authoritative catalog but treats keyword, vector, and answer
  providers as optional capabilities. Its response reports the active retrieval
  mode and provider statuses. Requests can skip unselected providers; missing
  requested providers degrade to the supported remaining signals, and missing
  answer generation does not suppress ranked citations. The REST API exposes
  that same service response without bypassing its authoritative catalog
  checks; production provider composition remains an injected runtime concern.
- POSIX containment uses descriptor-relative no-follow operations and verifies
  service-user ownership and directory permissions. Privileged processes and
  other processes using the service account remain trusted not to relocate
  open directories outside the tier, insert hard links, or change mount
  topology. See the [supported mutation model](posix_containment.md).
- JWT authentication, current role and tenant revalidation, partitioned
  catalogs and storage namespaces, legal holds, integrity-protected audit
  history, metrics/traces, SLO rules, consistency repair, and explicit orphan
  cleanup are implemented. Operator-owned TLS, encrypted storage, secret
  delivery, trusted broker access, and recovery evidence are still required.
  Tenant API processes do not expose aggregate `/metrics` to tenants.
- Automated repair resumes only eligible existing durable moves using the
  recorded identities and current fences. It is not a general reconciliation
  engine and does not infer permission to overwrite collisions or delete
  unknown objects. Physical CAS reclamation remains outside the shipped
  content-reference contract.

See the [design contracts](design.md), [M1 closeout evidence](evidence/m1/README.md),
[M2 closeout evidence](evidence/m2/README.md), [M3 closeout evidence](evidence/m3/README.md),
[historical M1 verification](m1_verification_2026-08-27.md), and
[next-ticket roadmap](next_ticket_roadmap_2026-08-27.md) for invariants, evidence,
open findings, and implementation order.
