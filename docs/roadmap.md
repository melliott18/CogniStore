# CogniStore Roadmap

This document tracks large-scale next steps and milestones. Use the checkboxes to track progress.

## Platform foundation

- [ ] Persistent control plane
  - [ ] Migrate catalog to Postgres + pgvector; add migrations and a small DAL
  - [ ] Event/audit tables for moves, policy decisions, failures, retries
- [ ] Queue + scheduler
  - [x] Message bus (NATS JetStream) and background workers
  - [x] Periodic scheduler for scans and policy passes, with a repair-job extension point
  - [ ] Fenced recovery for stale scheduled runs after hard worker loss ([#89](https://github.com/melliott18/CogniStore/issues/89))

## Indexing and knowledge layer

- [ ] Rich extraction
  - [ ] Use libmagic (python-magic) for robust MIME detection
  - [ ] Document parsing (PDF/DOCX) pipeline (textract or Apache Tika)
  - [ ] Chunking and checksum/dedup pipeline (CAS keys by sha256)
- [ ] Embeddings + search
  - [ ] Sentence-transformers or API embeddings → pgvector
  - [ ] Keyword index (OpenSearch/Elasticsearch or Tantivy-based library)
  - [ ] "Ask" service that blends metadata + vector + keyword

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

> Verification note (2026-08-27): these components meet their written delivery
> criteria. The cleanup races recorded as BUG-2026-001 and BUG-2026-002 remain
> fixed with generation-fenced cleanup. Current M1 blockers and validation
> evidence are tracked in `docs/m1_verification_2026-08-27.md`.

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

- [ ] External APIs
  - [ ] REST (FastAPI) and/or gRPC; Python SDK
- [ ] Admin UI
  - [ ] Drivers, tiers, policies, actions, audit trail; dry-run previews and diffs
- [ ] CLI polish
  - [x] Global config file, profiles, dry-run, verbose and JSON outputs
  - [ ] Close secret-redaction and operator-contract gaps in [#26](https://github.com/melliott18/CogniStore/issues/26)

## Reliability, performance, cost

- [ ] Benchmarks and scale tests
  - [x] Configurable mixed-size harness with throughput and tail latencies per backend
  - [ ] Execute and retain the canonical one-million-object report ([#29](https://github.com/melliott18/CogniStore/issues/29))
- [ ] Chaos/resilience
  - [x] Reduced CI fault injection verifies mover retries, idempotency, and source retention
  - [ ] Demonstrate the full-scale recovery profile and stale scheduled-run recovery
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

- [ ] M1: S3 driver + streaming mover + dry‑run + guardrails
  - Success: Move 1M small files hot↔warm reliably with idempotent jobs and integrity checks
  - Exit gate: close #26, #89, and #90; retain a passing full-profile report and close #29; then close #16
- [ ] M2: Postgres/pgvector + embeddings + keyword search
  - Success: Query objects by content via API/UI; policy uses MIME + embeddings features
  - Execution order: `docs/next_ticket_roadmap_2026-08-27.md`
- [ ] M3: Policy engine v2 (signals + hysteresis + cost/carbon budgets)
  - Success: Automated, explainable actions under budget constraints; no tier flapping
- [ ] M4: Multi-tenant, observable, and deployable
  - Success: Helm chart, dashboards, alerts; RBAC; audit-complete; run on k8s with autoscaling
