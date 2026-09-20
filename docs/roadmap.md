# CogniStore Roadmap

This document tracks large-scale next steps and milestones. Use the checkboxes to track progress.

M1–M4 are completed delivery history. Current work is
[M5: release readiness and controlled pilot](#m5-execution-plan), tracked by
[epic #155](https://github.com/melliott18/CogniStore/issues/155). The
[production pilot specification](production_pilot.md) defines the selected
deployment, workload, numeric qualification gates, and operating boundaries.

## Platform foundation

- [x] Persistent control plane
  - [x] Migrate catalog to Postgres + pgvector; add migrations and a small DAL
    ([#30](https://github.com/melliott18/CogniStore/issues/30))
  - [x] Event/audit tables for moves, policy decisions, failures, retries
    ([#31](https://github.com/melliott18/CogniStore/issues/31))
- [x] Queue + scheduler
  - [x] Message bus (NATS JetStream) and background workers
  - [x] Periodic scheduler for scans and policy passes, with a repair-job extension point
  - [x] Fenced recovery for stale scheduled runs after hard worker loss ([#89](https://github.com/melliott18/CogniStore/issues/89))

## Indexing and knowledge layer

- [x] Rich extraction
  - [x] Use libmagic (python-magic) for robust MIME detection
    ([#32](https://github.com/melliott18/CogniStore/issues/32))
  - [x] Document parsing (PDF/DOCX) pipeline
    ([#33](https://github.com/melliott18/CogniStore/issues/33))
  - [x] Chunking, checksums, and CAS identities
    ([#34](https://github.com/melliott18/CogniStore/issues/34))
  - [x] Safe deduplication references and deletion semantics
    ([#35](https://github.com/melliott18/CogniStore/issues/35))
- [x] Embeddings + search
  - [x] Sentence-transformers or API embeddings → pgvector
    ([#36](https://github.com/melliott18/CogniStore/issues/36))
  - [x] Rebuildable keyword index through a Tantivy-based adapter
    ([#37](https://github.com/melliott18/CogniStore/issues/37))
  - [x] "Ask" service that blends metadata + vector + keyword
    ([#38](https://github.com/melliott18/CogniStore/issues/38))

## Policy engine v2

- [x] Signals + guardrails (#43–#45)
  - [x] Recency/frequency from access logs; importance tags; residency timers; hysteresis/cooldowns
- [x] Learning loop (#46–#48)
  - [x] Supervised baseline; log features/labels; offline evaluation
  - [x] LLM-assisted decisions with JSON schema validation, dry-run, and safe fallbacks
- [x] Explainability (#49–#50)
  - [x] Persist structured reasons; render in UI/API; diff before/after placement

The [M3 closeout evidence](evidence/m3/README.md) maps these requirements to
implementation and tests. The learned baseline is an offline execution-success
experiment with production promotion disabled; its movement-volume and gate
sensitivity metrics do not establish real financial savings or tier stability.
Runtime stability controls and hard modeled budgets must be configured.

## Scale-out orchestration

- [x] Streaming, chunked moves
  - [x] Bounded streaming; multipart uploads (S3); integrity checks (hash before/after)
- [x] Idempotency + transactions
  - [x] Idempotent move jobs; two-phase catalog updates; retries with backoff; DLQs
- [x] Throughput controls
  - [x] Concurrency pools per tier, rate limiting, backpressure

> Verification note (updated 2026-08-29): these components meet their written
> delivery criteria. The cleanup races recorded as BUG-2026-001 and
> BUG-2026-002 remain fixed with generation-fenced cleanup. M1 acceptance is
> recorded in the [retained closeout evidence](evidence/m1/README.md); the
> 2026-08-27 verification document remains a historical review.

## Multi-backend storage

- [x] New drivers
  - [x] S3 (MinIO/AWS) with multipart support
  - [x] Azure Blob
  - [x] GCS
- [x] Tier/pool abstractions (#51–#53)
  - [x] Regions, cost/latency/carbon attributes; policy integrates budgets

## Security, compliance, tenancy

- [x] Storage boundary hardening
  - [x] Race-safe POSIX containment under concurrent path mutation ([#91](https://github.com/melliott18/CogniStore/issues/91))
- [x] AuthN/Z and tenancy
  - [x] JWT/OIDC; RBAC; per-tenant isolation
- [x] Secrets + encryption
  - [x] KMS/Vault integration; encryption at rest/in transit
- [x] Governance
  - [x] Hard region/locality filtering for placement candidates (#51)
  - [x] PII detection hooks; legal holds; tamper-evident audit logs

## Observability and ops

- [x] Metrics/tracing/logging
  - [x] Prometheus/Grafana, OpenTelemetry traces, structured logs
- [x] SLOs and budgets
  - [x] Modeled cost/carbon budget guardrails (#53)
  - [x] Move latency/error SLOs, throughput targets, and operational alerts
- [x] Repair/health
  - [x] Consistency checks, auto-repair, orphan cleanup

## API, CLI, and UI

- [x] External APIs
  - [x] Versioned REST/OpenAPI contract
    ([#39](https://github.com/melliott18/CogniStore/issues/39))
  - [x] Typed Python SDK
    ([#40](https://github.com/melliott18/CogniStore/issues/40))
- [x] Content-search UI and sample corpus
  ([#41](https://github.com/melliott18/CogniStore/issues/41))
- [x] Admin UI
  - [x] Placement reasons, dry-run previews, and before/after diffs (#50)
  - [x] General administration of drivers, tiers, policies, actions, and audit history
- [x] CLI polish
  - [x] Global config file, profiles, dry-run, verbose and JSON outputs
  - [x] Close secret-redaction and operator-contract gaps in [#26](https://github.com/melliott18/CogniStore/issues/26)

## Reliability, performance, cost

- [x] Benchmarks and scale tests
  - [x] Configurable mixed-size harness with throughput and tail latencies per backend
  - [x] Execute and retain the canonical one-million-object report ([evidence](evidence/m1/README.md), [#29](https://github.com/melliott18/CogniStore/issues/29))
- [x] Chaos/resilience
  - [x] Reduced CI fault injection verifies mover retries, idempotency, and source retention
  - [x] Demonstrate fenced stale scheduled-run recovery after a hard process kill
  - [x] Demonstrate the full-scale recovery profile
- [x] Cost/carbon modeling (#52–#53)
  - [x] Estimators per tier; what-if simulations for policy changes

## Delivery and DX

- [x] Packaging and deployments
  - [x] Non-root runtime/development Docker images and local Compose stack
  - [x] Helm charts, Terraform references with a provider lock, and production configs (#71–#72)
- [x] CI/CD
  - [x] Ruff/mypy, test matrix, coverage gates, security scans
- [x] Docs
  - [x] Operator runbooks, migration guides, reference architectures, sample datasets

---

## Milestones

- [x] M1: S3 driver + streaming mover + dry‑run + guardrails (completed 2026-08-29)
  - Success: Move 1M small files hot↔warm reliably with idempotent jobs and integrity checks
  - Exit gate: the clean-revision full-profile [report](evidence/m1/README.md) passed and is retained on `main`; #29, #16, and the M1 milestone are closed
- [x] M2: Postgres/pgvector + embeddings + keyword search (completed 2026-09-08)
  - Success: Query objects by content via API/UI; policy uses MIME + embeddings features
  - Exit gate: #30–#42 and verification/tracking follow-ups #100–#102 are
    complete; [retained closeout evidence](evidence/m2/README.md) maps the
    acceptance criteria to the qualified revision and passing CI reports
  - Epic [#13](https://github.com/melliott18/CogniStore/issues/13) and the
    [M2 milestone](https://github.com/melliott18/CogniStore/milestone/1) are closed
  - Completed execution record: [next-ticket roadmap](next_ticket_roadmap_2026-08-27.md)
- [x] M3: Policy engine v2 (signals + hysteresis + cost/carbon budgets; completed 2026-09-12)
  - Success: Automated, explainable actions under budget constraints; no tier flapping
  - Exit gate: #43–#53 are complete; [retained closeout evidence](evidence/m3/README.md)
    records acceptance coverage, local validation, and the unavailable GitHub Actions run
  - Epic [#15](https://github.com/melliott18/CogniStore/issues/15) and the
    [M3 milestone](https://github.com/melliott18/CogniStore/milestone/2) are closed
- [x] M4: Multi-tenant, observable, and deployable (completed 2026-09-19)
  - Success: Helm chart, dashboards, alerts; RBAC; audit-complete; run on k8s with autoscaling
  - Exit gate: #54–#73 and POSIX hardening #91 are complete; the
    [retained closeout evidence](evidence/m4/README.md) maps acceptance criteria
    to merged delivery, fresh local validation, and retained deployment drills
  - Epic [#14](https://github.com/melliott18/CogniStore/issues/14), the
    [M4 milestone](https://github.com/melliott18/CogniStore/milestone/3), and
    [roadmap tracker #12](https://github.com/melliott18/CogniStore/issues/12) are closed
- [ ] M5: Release readiness and controlled pilot
  - Success: resolve release blockers, qualify an immutable candidate for the
    [selected deployment and workload](production_pilot.md), and complete a
    bounded pilot with a measured expand/fix/stop decision
  - Exit gate: the audit, manual acceptance, recovery, and load/alert campaigns
    pass against the identified candidate and environment; recorded pilot entry
    approval and retained pilot outcomes support the final decision
  - [Epic #155](https://github.com/melliott18/CogniStore/issues/155) and the
    [M5 milestone](https://github.com/melliott18/CogniStore/milestone/5) track this
    forward scope; completed M1–M4 acceptance remains closed

M1–M4 delivery is complete. GitHub Actions remains blocked before job startup by
account billing/spending limits; M4 acceptance uses recorded local validation.
The evidence distinguishes emulator, local, and historical Kubernetes checks
from environment-specific production qualification. Deployments still require
the operator security, encryption, credential, and infrastructure prerequisites.

## M5 execution plan

The 2026-09-20 readiness assessment identified two P1 release blockers:
case-insensitive POSIX tenant-namespace aliases bypassing isolation/holds
([#156](https://github.com/melliott18/CogniStore/issues/156)) and an older DELETE
removing a concurrent replacement PUT's catalog record
([#157](https://github.com/melliott18/CogniStore/issues/157)). Both fixes, hosted CI
restoration, and the target specification can proceed independently. Historical
feature acceptance does not establish production qualification for this candidate.

| Work | Ticket | Required before completion |
| --- | --- | --- |
| Fix tenant namespace aliases | [#156](https://github.com/melliott18/CogniStore/issues/156) | None; P1 pilot blocker |
| Fix overlapping DELETE/PUT publication | [#157](https://github.com/melliott18/CogniStore/issues/157) | None; P1 pilot blocker |
| Restore hosted CI and qualify supported runtimes | [#158](https://github.com/melliott18/CogniStore/issues/158) | None |
| Select deployment, workload, owners, and gates | [#159](https://github.com/melliott18/CogniStore/issues/159) | None; [versioned specification](production_pilot.md) |
| Freeze reproducible release candidate | [#160](https://github.com/melliott18/CogniStore/issues/160) | #156, #157, #158, #159 |
| Deploy isolated production-configured staging | [#161](https://github.com/melliott18/CogniStore/issues/161) | #159, #160 |
| Audit safety, security, and data consistency | [#162](https://github.com/melliott18/CogniStore/issues/162) | #159, #160, #161 for final signoff; source/configuration review can start after target selection |
| Run manual CLI/API/SDK/browser acceptance | [#163](https://github.com/melliott18/CogniStore/issues/163) | #159, #161 |
| Qualify recovery, coherent restore, rotation, and upgrade rollback | [#164](https://github.com/melliott18/CogniStore/issues/164) | #159, #161 |
| Qualify realistic load, capacity, and alert delivery | [#165](https://github.com/melliott18/CogniStore/issues/165) | #159, #161 |
| Run the gated pilot and decide expansion | [#166](https://github.com/melliott18/CogniStore/issues/166) | Passing #162, #163, #164, #165 evidence and recorded entry approval |

The child issues' live **Dependencies** sections are authoritative and are retained
in the generated [ticket mirror](tickets.md). Qualification campaigns may run in
parallel only with isolated test scopes, so destructive drills cannot remove
another campaign's dependencies. Retain sanitized evidence with exact source,
image, dependency, configuration, and environment identities; explain skips and
store artifacts in the repository or durable linked storage.

The [pilot specification](production_pilot.md) sets numeric targets before testing
and defines entry, exit, stop, and rollback criteria. Material changes after
qualification require scoped requalification. A failed or stopped pilot requires
an explicit fix/stop decision and does not automatically satisfy M5. The next
roadmap follows measured pilot outcomes and linked, prioritized follow-up tickets.
