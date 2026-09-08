# CogniStore Roadmap

This document tracks large-scale next steps and milestones. Use the checkboxes to track progress.

## Platform foundation

- [x] Persistent control plane
  - [x] Migrate catalog to Postgres + pgvector; add migrations and a small DAL
    ([#30](https://github.com/melliott18/CogniStore/issues/30))
  - [x] Event/audit tables for moves, policy decisions, failures, retries
    ([#31](https://github.com/melliott18/CogniStore/issues/31))
- [ ] Queue + scheduler
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

- [ ] Signals + guardrails
  - [ ] Recency/frequency from access logs; importance tags; residency timers; hysteresis/cooldowns
- [ ] Learning loop
  - [ ] Supervised baseline; log features/labels; offline evaluation
  - [ ] LLM-assisted decisions with JSON schema validation, dry-run, and safe fallbacks
- [ ] Explainability
  - [ ] Persist structured reasons; render in UI/API; diff before/after placement

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

- [ ] New drivers
  - [x] S3 (MinIO/AWS) with multipart support
  - [ ] Azure Blob
  - [ ] GCS
- [ ] Tier/pool abstractions
  - [ ] Regions, cost/latency/carbon attributes; policy integrates budgets

## Security, compliance, tenancy

- [ ] Storage boundary hardening
  - [ ] Race-safe POSIX containment under concurrent path mutation ([#91](https://github.com/melliott18/CogniStore/issues/91))
- [ ] AuthN/Z and tenancy
  - [ ] JWT/OIDC; RBAC; per-tenant isolation
- [ ] Secrets + encryption
  - [ ] KMS/Vault integration; encryption at rest/in transit
- [ ] Governance
  - [ ] PII detection hooks; legal holds; immutable audit logs; data locality constraints

## Observability and ops

- [ ] Metrics/tracing/logging
  - [ ] Prometheus/Grafana, OpenTelemetry traces, structured logs
- [ ] SLOs and budgets
  - [ ] Move latency/error SLOs, throughput targets; cost/carbon guardrails and alerts
- [ ] Repair/health
  - [ ] Consistency checks, auto-repair, orphan cleanup

## API, CLI, and UI

- [x] External APIs
  - [x] Versioned REST/OpenAPI contract
    ([#39](https://github.com/melliott18/CogniStore/issues/39))
  - [x] Typed Python SDK
    ([#40](https://github.com/melliott18/CogniStore/issues/40))
- [x] Content-search UI and sample corpus
  ([#41](https://github.com/melliott18/CogniStore/issues/41))
- [ ] Admin UI
  - [ ] Drivers, tiers, policies, actions, audit trail; dry-run previews and diffs
- [ ] CLI polish
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
- [ ] Cost/carbon modeling
  - [ ] Estimators per tier; what-if simulations for policy changes

## Delivery and DX

- [ ] Packaging and deployments
  - [x] Non-root runtime/development Docker images and local Compose stack
  - [ ] Helm charts, Terraform samples, locked dependencies, and production configs
- [x] CI/CD
  - [x] Ruff/mypy, test matrix, coverage gates, security scans
- [ ] Docs
  - [ ] Operator runbooks, migration guides, reference architectures, sample datasets

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
- [ ] M3: Policy engine v2 (signals + hysteresis + cost/carbon budgets)
  - Success: Automated, explainable actions under budget constraints; no tier flapping
- [ ] M4: Multi-tenant, observable, and deployable
  - Success: Helm chart, dashboards, alerts; RBAC; audit-complete; run on k8s with autoscaling
