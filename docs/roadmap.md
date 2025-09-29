# CogniStore Roadmap

This document tracks large-scale next steps and milestones. Use the checkboxes to track progress.

## Platform foundation

- [ ] Persistent control plane
  - [ ] Migrate catalog to Postgres + pgvector; add migrations and a small DAL
  - [ ] Event/audit tables for moves, policy decisions, failures, retries
- [ ] Queue + scheduler
  - [ ] Message bus (NATS/Kafka) and background workers (Celery/Dramatiq/RQ)
  - [ ] Periodic scheduler for scans, policy passes, and repair jobs

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

- [ ] Streaming, chunked moves
  - [ ] Zero-copy/streaming where possible; multipart uploads (S3); integrity checks (hash before/after)
- [ ] Idempotency + transactions
  - [ ] Idempotent move jobs; two-phase catalog updates; retries with backoff; DLQs
- [ ] Throughput controls
  - [ ] Concurrency pools per tier, rate limiting, backpressure

## Multi-backend storage

- [ ] New drivers
  - [ ] S3 (MinIO/AWS) with multipart support
  - [ ] Azure Blob
  - [ ] GCS
- [ ] Tier/pool abstractions
  - [ ] Regions, cost/latency/carbon attributes; policy integrates budgets

## Security, compliance, tenancy

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
  - [ ] Global config file, profiles, dry-run, verbose and JSON outputs

## Reliability, performance, cost

- [ ] Benchmarks and scale tests
  - [ ] N objects, mixed sizes; throughput and tail latencies per backend
- [ ] Chaos/resilience
  - [ ] Inject failures/timeouts; verify retries/idempotency; fault budgets
- [ ] Cost/carbon modeling
  - [ ] Estimators per tier; what-if simulations for policy changes

## Delivery and DX

- [ ] Packaging and deployments
  - [ ] Docker images, Helm charts, Terraform samples; production configs
- [ ] CI/CD
  - [ ] Ruff/mypy, test matrix, coverage gates, security scans
- [ ] Docs
  - [ ] Operator runbooks, migration guides, reference architectures, sample datasets

---

## Milestones

- [ ] M1: S3 driver + streaming mover + dry‑run + guardrails
  - Success: Move 1M small files hot↔warm reliably with idempotent jobs and integrity checks
- [ ] M2: Postgres/pgvector + embeddings + keyword search
  - Success: Query objects by content via API/UI; policy uses MIME + embeddings features
- [ ] M3: Policy engine v2 (signals + hysteresis + cost/carbon budgets)
  - Success: Automated, explainable actions under budget constraints; no tier flapping
- [ ] M4: Multi-tenant, observable, and deployable
  - Success: Helm chart, dashboards, alerts; RBAC; audit-complete; run on k8s with autoscaling