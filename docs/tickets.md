# CogniStore Ticket Mirror

> Snapshot synchronized from GitHub Issues on 2026-08-27. GitHub is the source of truth; this file is a local, read-only reference and may become stale after issue updates.

- **Repository:** [melliott18/CogniStore](https://github.com/melliott18/CogniStore)
- **Master tracker:** [#12](https://github.com/melliott18/CogniStore/issues/12)
- **Source roadmap:** [roadmap.md](./roadmap.md)
- **Original proposal:** [proposal.md](./proposal.md)
- **Snapshot:** 65 issues; 54 open, 11 closed

## How to use this mirror

- Start with the master tracker and milestone epic for sequencing.
- Child tickets are listed in issue-number order; their **Dependencies** sections are authoritative.
- Acceptance checkboxes reflect the GitHub issue body at the snapshot date, not live status.
- Make ticket changes in GitHub, then regenerate this file so the remote tracker remains canonical.

## Milestone index

| Milestone | Epic | Delivery tickets | GitHub |
| --- | --- | ---: | --- |
| M1 – Reliable multi-backend movement | [#16](https://github.com/melliott18/CogniStore/issues/16) | 13 | [Open milestone](https://github.com/melliott18/CogniStore/milestone/4) |
| M2 – Knowledge layer and search | [#13](https://github.com/melliott18/CogniStore/issues/13) | 13 | [Open milestone](https://github.com/melliott18/CogniStore/milestone/1) |
| M3 – Explainable policy engine | [#15](https://github.com/melliott18/CogniStore/issues/15) | 11 | [Open milestone](https://github.com/melliott18/CogniStore/milestone/2) |
| M4 – Production platform | [#14](https://github.com/melliott18/CogniStore/issues/14) | 20 | [Open milestone](https://github.com/melliott18/CogniStore/milestone/3) |

## Label taxonomy

- `roadmap` — Approved work from the CogniStore roadmap
- `type:epic` — Tracking issue that groups a milestone or major initiative
- `type:feature` — User-facing or platform capability
- `type:chore` — Engineering, delivery, documentation, or maintenance work
- `area:control-plane` — Catalog, queues, scheduling, and control-plane state
- `area:indexing` — Extraction, embeddings, search, and knowledge services
- `area:policy` — Placement policy signals, models, guardrails, and explanations
- `area:orchestration` — Movement workflows, reliability, and throughput controls
- `area:storage` — Storage drivers, tiers, pools, and backend integrations
- `area:security` — Authentication, authorization, encryption, and governance
- `area:observability` — Metrics, tracing, logging, SLOs, and repair
- `area:api` — External APIs and SDKs
- `area:ui` — Administrative and user interfaces
- `area:delivery` — Packaging, CI/CD, deployment, and developer experience
- `area:docs` — Operator, migration, architecture, and reference documentation
- `bug` — Something isn't working
- `documentation` — Improvements or additions to documentation

## Master tracker

### [#12 — Roadmap: CogniStore delivery plan](https://github.com/melliott18/CogniStore/issues/12)

- **Kind:** Master tracker
- **Status:** Open
- **Milestone:** None
- **Labels:** `type:epic`, `roadmap`
- **Last updated:** 2026-08-22

#### Purpose

This is the top-level tracker for converting the original CogniStore roadmap into an executable GitHub Issues backlog.

Each formal milestone has one epic. Delivery work lives in separately scoped child issues with explicit acceptance criteria, exclusions, and dependency links.

#### Tracking conventions

- **Milestones** communicate delivery sequence: M1 through M4.
- **Epic issues** group work; they are not implementation tickets.
- **Child issues** carry implementation scope and are ordered approximately by dependency inside each epic.
- An issue is complete only when its acceptance criteria, tests, and relevant documentation are satisfied.
- Cross-milestone work remains assigned to the earliest milestone that needs it.

#### Milestone epics

- [ ] [#16](https://github.com/melliott18/CogniStore/issues/16) — [M1 milestone](https://github.com/melliott18/CogniStore/milestone/4): reliable multi-backend movement
- [ ] [#13](https://github.com/melliott18/CogniStore/issues/13) — [M2 milestone](https://github.com/melliott18/CogniStore/milestone/1): knowledge layer and search
- [ ] [#15](https://github.com/melliott18/CogniStore/issues/15) — [M3 milestone](https://github.com/melliott18/CogniStore/milestone/2): explainable policy engine
- [ ] [#14](https://github.com/melliott18/CogniStore/issues/14) — [M4 milestone](https://github.com/melliott18/CogniStore/milestone/3): production platform

#### Backlog inventory

- M1: 13 delivery issues
- M2: 13 delivery issues
- M3: 11 delivery issues
- M4: 20 delivery issues

#### Source of truth

Backlog is maintained against the default `main` branch; GitHub Issues is the canonical tracker.

## M1 – Reliable multi-backend movement

- **GitHub milestone:** [M1 – Reliable multi-backend movement](https://github.com/melliott18/CogniStore/milestone/4)
- **Delivery tickets:** 13
- **Verification follow-ups:** 2
- **Open:** 5 including the epic and verification follow-ups

### [#16 — [Epic] M1 – Reliable multi-backend movement](https://github.com/melliott18/CogniStore/issues/16)

- **Kind:** Milestone epic
- **Status:** Open
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `type:epic`, `roadmap`, `area:orchestration`
- **Last updated:** 2026-08-27

Parent roadmap: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Outcome

CogniStore can move data between POSIX and S3-compatible tiers using a streaming, integrity-checked, idempotent workflow that is safe to preview and operate under load.

#### Milestone success criteria

- Move 1 million small objects between hot and warm tiers without silent loss or corruption.
- Every move is resumable or safely retryable.
- Operators can preview decisions and inspect machine-readable results.
- CI exercises the supported runtime and backend matrix.

#### Child issues

- [x] [#17](https://github.com/melliott18/CogniStore/issues/17) — Implement an S3-compatible storage driver and conformance suite
- [x] [#18](https://github.com/melliott18/CogniStore/issues/18) — Add a message bus and background worker runtime
- [x] [#19](https://github.com/melliott18/CogniStore/issues/19) — Schedule catalog scans and policy passes
- [x] [#20](https://github.com/melliott18/CogniStore/issues/20) — Enforce movement and CLI safety guardrails
- [x] [#21](https://github.com/melliott18/CogniStore/issues/21) — Stream object moves with bounded memory and S3 multipart upload
- [x] [#22](https://github.com/melliott18/CogniStore/issues/22) — Verify object integrity before deleting the source
- [x] [#23](https://github.com/melliott18/CogniStore/issues/23) — Make move jobs idempotent with two-phase catalog updates
- [x] [#24](https://github.com/melliott18/CogniStore/issues/24) — Add retry, backoff, dead-letter, and redrive handling
- [x] [#25](https://github.com/melliott18/CogniStore/issues/25) — Add per-tier concurrency, rate limiting, and backpressure
- [ ] [#26](https://github.com/melliott18/CogniStore/issues/26) — Standardize CLI configuration, profiles, dry-run, verbose, and JSON output
- [x] [#27](https://github.com/melliott18/CogniStore/issues/27) — Establish Python packaging and CI quality gates
- [x] [#28](https://github.com/melliott18/CogniStore/issues/28) — Provide a Docker-based development and integration environment
- [ ] [#29](https://github.com/melliott18/CogniStore/issues/29) — Qualify one-million-object moves and failure recovery

Dependencies listed inside each child issue are authoritative; checklist order is the suggested implementation sequence.

#### Source

`docs/roadmap.md`: M1, platform foundation, scale-out orchestration, S3 backend, CLI polish, benchmarks, packaging, and CI/CD.

#### Verification follow-ups

- [ ] [#89](https://github.com/melliott18/CogniStore/issues/89) — Recover stale scheduled runs safely after worker loss
- [ ] [#90](https://github.com/melliott18/CogniStore/issues/90) — Make the default pytest command collect the full test suite
- [#91](https://github.com/melliott18/CogniStore/issues/91) — Make POSIX path containment race-safe against symlink swaps (scheduled for M4 hardening)

Issue #26 remains unchecked because the 2026-08-27 verification reproduced a
secret-redaction failure. Issue #29 remains unchecked until a retained
full-profile report proves the one-million-object criterion and manual
repeatability.

### Delivery tickets

### [#17 — [M1] Implement an S3-compatible storage driver and conformance suite](https://github.com/melliott18/CogniStore/issues/17)

- **Kind:** Delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `type:feature`, `roadmap`, `area:storage`
- **Last updated:** 2026-08-12

Parent epic: [#16](https://github.com/melliott18/CogniStore/issues/16)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Provide a production-oriented S3 backend that obeys the same storage contract as the POSIX driver.

#### Scope

- Define driver capability flags and a reusable backend conformance suite.
- Implement put, get, ranged get, delete, paginated list, and stat for MinIO and AWS S3.
- Load endpoint, region, bucket behavior, and credentials through configuration without logging secrets.

#### Out of scope

- Multipart move orchestration; that belongs to the streaming mover ticket.
- Azure Blob and GCS support.

#### Acceptance criteria

- [ ] The S3 driver is selectable from `drivers.yaml`.
- [ ] POSIX and S3 drivers pass the shared conformance suite.
- [ ] Integration tests run against MinIO and cover pagination, ranges, missing objects, and idempotent deletes.
- [ ] Configuration and supported S3 semantics are documented.

#### Dependencies

- None.

#### Roadmap coverage

Multi-backend storage → S3 with multipart support (core driver portion).

### [#18 — [M1] Add a message bus and background worker runtime](https://github.com/melliott18/CogniStore/issues/18)

- **Kind:** Delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `type:feature`, `roadmap`, `area:control-plane`
- **Last updated:** 2026-08-14

Parent epic: [#16](https://github.com/melliott18/CogniStore/issues/16)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Move scans and placement work out of synchronous CLI execution into a durable worker model.

#### Scope

- Record an ADR selecting one message-bus and worker stack.
- Implement enqueue, claim, acknowledge, negative-acknowledge, redelivery, and graceful shutdown.
- Carry job IDs and correlation metadata through the worker boundary.

#### Out of scope

- Periodic scheduling.
- Job-specific retry policy and dead-letter redrive.

#### Acceptance criteria

- [ ] A queued test job survives a worker restart without silent loss.
- [ ] Duplicate delivery is expected and documented for consumers.
- [ ] Health/readiness checks expose bus and worker status.
- [ ] Unit and integration tests cover enqueue, acknowledgement, redelivery, and shutdown.

#### Dependencies

- None.

#### Roadmap coverage

Platform foundation → message bus and background workers.

### [#19 — [M1] Schedule catalog scans and policy passes](https://github.com/melliott18/CogniStore/issues/19)

- **Kind:** Delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `type:feature`, `roadmap`, `area:control-plane`
- **Last updated:** 2026-08-27

Parent epic: [#16](https://github.com/melliott18/CogniStore/issues/16)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Provide configurable periodic execution for recurring control-plane work.

#### Scope

- Implement a scheduler that enqueues catalog scans and policy passes.
- Support per-job intervals, enable/disable controls, and single-run locking.
- Provide an extension point for future repair jobs.

#### Out of scope

- Repair logic itself.
- Calendar-based UI management.

#### Acceptance criteria

- [x] Scheduled work is enqueued rather than executed inline.
- [x] Overlapping runs of the same scoped job are prevented.
- [x] Intervals and disabled jobs are configurable.
- [x] Tests use a controllable clock and cover restart behavior.

#### Dependencies

- [#18](https://github.com/melliott18/CogniStore/issues/18) — [M1] Add a message bus and background worker runtime

#### Roadmap coverage

Platform foundation → periodic scheduler for scans, policy passes, and repair jobs.

### [#20 — [M1] Enforce movement and CLI safety guardrails](https://github.com/melliott18/CogniStore/issues/20)

- **Kind:** Delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `bug`, `roadmap`, `type:chore`, `area:orchestration`
- **Last updated:** 2026-08-11

Parent epic: [#16](https://github.com/melliott18/CogniStore/issues/16)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Prevent known unsafe move plans and ensure preview output truthfully describes side effects.

#### Scope

- Reject same-source/destination moves, unknown tiers, disallowed destinations, and unsafe POSIX paths.
- Define and enforce destination-collision behavior.
- Make dry-run execute zero storage or catalog writes and report actions as planned rather than completed.

#### Out of scope

- Streaming and integrity verification.
- Policy learning behavior.

#### Acceptance criteria

- [ ] Regression tests prove a same-tier move cannot delete data.
- [ ] Resolved POSIX paths must remain inside the configured tier root.
- [ ] Allowed-tier constraints apply consistently to every policy mode.
- [ ] Dry-run output is distinguishable from completed movement in human and JSON modes.

#### Dependencies

- None.

#### Roadmap coverage

M1 guardrails and CLI dry-run; CLI polish.

### [#21 — [M1] Stream object moves with bounded memory and S3 multipart upload](https://github.com/melliott18/CogniStore/issues/21)

- **Kind:** Delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `type:feature`, `roadmap`, `area:orchestration`
- **Last updated:** 2026-08-22

Parent epic: [#16](https://github.com/melliott18/CogniStore/issues/16)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Replace whole-object buffering with a transport that scales to large objects and S3.

#### Scope

- Introduce streaming read/write primitives with bounded buffers.
- Use multipart upload for S3 destinations and clean up incomplete uploads on cancellation.
- Preserve metadata needed by the mover and catalog.

#### Out of scope

- Final checksum enforcement.
- Retry/DLQ policy and job state transitions.

#### Acceptance criteria

- [x] Peak memory remains bounded for objects substantially larger than the configured chunk size.
- [x] POSIX↔POSIX and POSIX↔S3 transfers pass integration tests.
- [x] Interrupted multipart uploads are aborted or resumable without orphaning parts.
- [x] Chunk size and multipart thresholds are configurable.

#### Dependencies

- [#17](https://github.com/melliott18/CogniStore/issues/17) — [M1] Implement an S3-compatible storage driver and conformance suite

#### Roadmap coverage

Scale-out orchestration → zero-copy/streaming where possible and S3 multipart uploads.

### [#22 — [M1] Verify object integrity before deleting the source](https://github.com/melliott18/CogniStore/issues/22)

- **Kind:** Delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `type:feature`, `roadmap`, `area:orchestration`
- **Last updated:** 2026-08-16

Parent epic: [#16](https://github.com/melliott18/CogniStore/issues/16)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Guarantee that source data is retained unless the destination has been verified.

#### Scope

- Compute or obtain canonical source and destination checksums.
- Verify byte count and checksum before catalog commit or source deletion.
- Record verification outcomes and actionable failure details.

#### Out of scope

- Content-addressed deduplication.
- Cross-replica repair.

#### Acceptance criteria

- [ ] The source is never deleted when verification fails or is incomplete.
- [ ] Checksum mismatch produces a failed result with both observed digests.
- [ ] Tests inject truncation and corruption for POSIX and S3 destinations.
- [ ] Verified checksum and size are persisted with the placement.

#### Dependencies

- [#21](https://github.com/melliott18/CogniStore/issues/21) — [M1] Stream object moves with bounded memory and S3 multipart upload

#### Roadmap coverage

Scale-out orchestration → integrity checks before/after movement.

### [#23 — [M1] Make move jobs idempotent with two-phase catalog updates](https://github.com/melliott18/CogniStore/issues/23)

- **Kind:** Delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `type:feature`, `roadmap`, `area:orchestration`
- **Last updated:** 2026-08-16

Parent epic: [#16](https://github.com/melliott18/CogniStore/issues/16)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Represent moves as durable state machines that can be repeated or recovered without duplicate destructive effects.

#### Scope

- Define move-job states, transitions, idempotency keys, and ownership/lease rules.
- Implement prepare/transfer/verify/commit/cleanup phases with two-phase catalog updates.
- Recover incomplete jobs after process failure.

#### Out of scope

- Retry timing and dead-letter redrive.
- General distributed transactions outside move placement.

#### Acceptance criteria

- [ ] Replaying the same idempotency key cannot duplicate or lose an object.
- [ ] Catalog placement changes only after destination verification.
- [ ] Recovery tests cover crashes at every state transition.
- [ ] State transitions and terminal reasons are queryable.

#### Dependencies

- [#18](https://github.com/melliott18/CogniStore/issues/18) — [M1] Add a message bus and background worker runtime
- [#22](https://github.com/melliott18/CogniStore/issues/22) — [M1] Verify object integrity before deleting the source

#### Roadmap coverage

Scale-out orchestration → idempotent jobs and two-phase catalog updates.

### [#24 — [M1] Add retry, backoff, dead-letter, and redrive handling](https://github.com/melliott18/CogniStore/issues/24)

- **Kind:** Delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `type:feature`, `roadmap`, `area:control-plane`
- **Last updated:** 2026-08-17

Parent epic: [#16](https://github.com/melliott18/CogniStore/issues/16)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Handle transient movement failures predictably while isolating permanent failures.

#### Scope

- Classify retryable and terminal errors.
- Implement bounded exponential backoff with jitter and attempt accounting.
- Send exhausted jobs to a dead-letter queue with an operator-triggered redrive path.

#### Out of scope

- Automatic data repair.
- User-facing administration UI.

#### Acceptance criteria

- [ ] Transient failures retry without violating move idempotency.
- [ ] Terminal and exhausted jobs retain complete diagnostic context.
- [ ] Redrive preserves the original idempotency key and audit chain.
- [ ] Tests cover timeout, throttling, unavailable backend, malformed request, and exhaustion.

#### Dependencies

- [#18](https://github.com/melliott18/CogniStore/issues/18) — [M1] Add a message bus and background worker runtime
- [#23](https://github.com/melliott18/CogniStore/issues/23) — [M1] Make move jobs idempotent with two-phase catalog updates

#### Roadmap coverage

Scale-out orchestration → retries with backoff and DLQs.

### [#25 — [M1] Add per-tier concurrency, rate limiting, and backpressure](https://github.com/melliott18/CogniStore/issues/25)

- **Kind:** Delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `type:feature`, `roadmap`, `area:orchestration`
- **Last updated:** 2026-08-19

Parent epic: [#16](https://github.com/melliott18/CogniStore/issues/16)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Keep movement workloads within backend and host capacity limits.

#### Scope

- Add independent concurrency pools for source and destination tiers.
- Support configurable byte/operation rate limits.
- Propagate queue saturation as measurable backpressure rather than unbounded buffering.

#### Out of scope

- Autoscaling workers in Kubernetes.
- Policy cost/carbon budgets.

#### Acceptance criteria

- [ ] Limits can be configured per tier without restarting in-flight jobs unsafely.
- [ ] Load tests demonstrate bounded queue and memory growth.
- [ ] Fairness prevents one tier from starving unrelated tiers.
- [ ] Metrics expose active jobs, queue depth, throttling, and saturation.

#### Dependencies

- [#18](https://github.com/melliott18/CogniStore/issues/18) — [M1] Add a message bus and background worker runtime
- [#21](https://github.com/melliott18/CogniStore/issues/21) — [M1] Stream object moves with bounded memory and S3 multipart upload

#### Roadmap coverage

Scale-out orchestration → concurrency pools, rate limiting, and backpressure.

### [#26 — [M1] Standardize CLI configuration, profiles, dry-run, verbose, and JSON output](https://github.com/melliott18/CogniStore/issues/26)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `type:feature`, `roadmap`, `area:api`
- **Last updated:** 2026-08-27

Parent epic: [#16](https://github.com/melliott18/CogniStore/issues/16)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Make the CLI predictable for both operators and automation.

#### Scope

- Add a global configuration file and named profiles with documented precedence.
- Standardize dry-run semantics across mutating commands.
- Add verbose diagnostics and stable JSON output without mixing human text into stdout.

#### Out of scope

- A graphical administration UI.
- Remote authentication flows.

#### Acceptance criteria

- [x] Configuration precedence is covered by tests and documented.
- [x] Every mutating command either supports dry-run or clearly documents why it cannot.
- [x] JSON output has versioned schemas and non-zero exit codes on failure.
- [ ] Secrets are redacted from verbose and error output.

#### Dependencies

- [#20](https://github.com/melliott18/CogniStore/issues/20) — [M1] Enforce movement and CLI safety guardrails

#### Roadmap coverage

API, CLI, and UI → CLI polish.

#### Verification follow-ups — 2026-08-27

- [ ] Usage-error redaction covers space-separated values after sensitive option names without exposing the value.
- [ ] `move-resume --dry-run` reports which ownership and phase-specific storage preconditions were and were not checked; it must not imply readiness when it only inspected journal state.
- [ ] The exit-status contract is made consistent and tested, or its versioned documentation explicitly defines the narrower guarantee automation can rely on.
- [ ] A CLI option can disable verbosity enabled by a lower-precedence environment, profile, or default value.

### [#27 — [M1] Establish Python packaging and CI quality gates](https://github.com/melliott18/CogniStore/issues/27)

- **Kind:** Delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `roadmap`, `type:chore`, `area:delivery`
- **Last updated:** 2026-08-22

Parent epic: [#16](https://github.com/melliott18/CogniStore/issues/16)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Create a reproducible package and automated quality baseline for supported Python versions.

#### Scope

- Populate `pyproject.toml` with build metadata, dependencies, and tool configuration.
- Run Ruff, mypy, pytest across a Python test matrix, and collect coverage.
- Add coverage thresholds plus dependency, secret, and static security checks.

#### Out of scope

- Container image publishing and Kubernetes deployment.
- Release automation beyond producing a verified Python artifact.

#### Acceptance criteria

- [x] A clean checkout can build and install the package.
- [x] Pull requests run lint, type, unit, and integration checks.
- [x] Coverage and security failures block CI with actionable output.
- [x] Supported Python versions and local commands are documented.

#### Dependencies

- None.

#### Roadmap coverage

Delivery and DX → CI/CD; packaging baseline.

### [#28 — [M1] Provide a Docker-based development and integration environment](https://github.com/melliott18/CogniStore/issues/28)

- **Kind:** Delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `roadmap`, `type:chore`, `area:delivery`
- **Last updated:** 2026-08-27

Parent epic: [#16](https://github.com/melliott18/CogniStore/issues/16)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Make the application, S3 backend, and worker infrastructure reproducible without host-specific setup.

#### Scope

- Build a minimal runtime image and a developer/test image.
- Provide Compose services for CogniStore, MinIO, and the selected message bus/worker dependencies.
- Add health checks, persistent test volumes, and one-command integration startup.

#### Out of scope

- Production Kubernetes manifests.
- Terraform-managed cloud infrastructure.

#### Acceptance criteria

- [x] The stack starts from a clean checkout with documented commands.
- [x] Integration tests can run entirely inside the stack.
- [x] Images run as non-root and do not bake in secrets.
- [x] Shutdown leaves no incomplete test movement without a diagnosable job record.

#### Dependencies

- [#17](https://github.com/melliott18/CogniStore/issues/17) — [M1] Implement an S3-compatible storage driver and conformance suite
- [#18](https://github.com/melliott18/CogniStore/issues/18) — [M1] Add a message bus and background worker runtime

#### Roadmap coverage

Delivery and DX → Docker images and production-like configuration samples (development portion).

### [#29 — [M1] Qualify one-million-object moves and failure recovery](https://github.com/melliott18/CogniStore/issues/29)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `roadmap`, `area:observability`, `type:chore`
- **Last updated:** 2026-08-27

Parent epic: [#16](https://github.com/melliott18/CogniStore/issues/16)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Demonstrate the M1 reliability target with repeatable scale and resilience evidence.

#### Scope

- Create a benchmark harness for configurable object counts and mixed sizes.
- Measure throughput plus p50/p95/p99 latency for POSIX and S3 paths.
- Inject timeouts, worker termination, throttling, and backend unavailability and verify recovery.

#### Out of scope

- Long-term production SLO alerting.
- Multi-region cloud qualification.

#### Acceptance criteria

- [ ] A documented run moves 1 million small objects hot↔warm without silent loss or corruption.
- [x] All injected failures respect idempotency, retry limits, and source-retention rules.
- [x] Results include environment, configuration, throughput, tail latency, failures, and recovery time.
- [ ] The harness is repeatable in CI at reduced scale and manually at full scale.

#### Dependencies

- [#17](https://github.com/melliott18/CogniStore/issues/17) — [M1] Implement an S3-compatible storage driver and conformance suite
- [#22](https://github.com/melliott18/CogniStore/issues/22) — [M1] Verify object integrity before deleting the source
- [#23](https://github.com/melliott18/CogniStore/issues/23) — [M1] Make move jobs idempotent with two-phase catalog updates
- [#24](https://github.com/melliott18/CogniStore/issues/24) — [M1] Add retry, backoff, dead-letter, and redrive handling
- [#25](https://github.com/melliott18/CogniStore/issues/25) — [M1] Add per-tier concurrency, rate limiting, and backpressure
- [#27](https://github.com/melliott18/CogniStore/issues/27) — [M1] Establish Python packaging and CI quality gates
- [#28](https://github.com/melliott18/CogniStore/issues/28) — [M1] Provide a Docker-based development and integration environment

#### Roadmap coverage

M1 success criterion; benchmarks, scale tests, and chaos/resilience.

### Verification follow-up tickets

### [#89 — [M1] Recover stale scheduled runs safely after worker loss](https://github.com/melliott18/CogniStore/issues/89)

- **Kind:** Bug follow-up
- **Status:** Open
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `bug`, `roadmap`, `area:control-plane`, `area:orchestration`
- **Last updated:** 2026-08-27

Parent epic: [#16](https://github.com/melliott18/CogniStore/issues/16)

#### Problem

If a worker exits after a scheduled occurrence enters `running`, JetStream
redelivers the same envelope and job ID, but the scheduler coordinator rejects
that occurrence indefinitely. The active job continues to own its scope, later
occurrences cannot be reserved, and the delivery can redeliver forever.

#### Scope

- Add read-only list/status inspection for quarantined or stale `running` scheduled occurrences.
- Add an explicit, audited recovery operation that requires the prior worker to be fenced or stopped.
- Atomically transition the same job and generation to a retryable state while preserving its logical scope and identity.
- Clear execution ownership only through that fenced recovery path.
- Add a live JetStream test that kills a worker process after the `running` transition, restarts processing, and proves recovery without overlap.

#### Safety constraints

- Never authorize takeover solely because a lease TTL expired.
- Never create a successor occurrence while the original occurrence remains unresolved.
- Preserve job ID, redrive generation, transition history, and operator-supplied recovery reason.

#### Acceptance criteria

- [ ] Operators can list and inspect stale/quarantined scheduled runs without direct database queries.
- [ ] An explicitly fenced recovery resumes the same occurrence without overlapping the former worker.
- [ ] Recovery is durable, audited, idempotent, and safe across process restarts.
- [ ] A live JetStream hard-kill/restart test proves the scope resumes and later intervals can run.
- [ ] The operator runbook documents fencing, inspection, recovery, and failure handling.

#### Dependencies

- [#18](https://github.com/melliott18/CogniStore/issues/18) — message bus and worker runtime
- [#19](https://github.com/melliott18/CogniStore/issues/19) — recurring scheduler
- [#24](https://github.com/melliott18/CogniStore/issues/24) — retry, dead-letter, and redrive handling

### [#90 — [M1] Make the default pytest command collect the full test suite](https://github.com/melliott18/CogniStore/issues/90)

- **Kind:** Bug follow-up
- **Status:** Open
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `bug`, `roadmap`, `type:chore`, `area:delivery`
- **Last updated:** 2026-08-27

Parent epic: [#16](https://github.com/melliott18/CogniStore/issues/16)

#### Problem

The documented `python -m pytest` command fails collection because the unit and
integration qualification files import as the same top-level module. CI avoids
the collision by running the groups separately.

#### Acceptance criteria

- [ ] `python -m pytest` collects and runs the full default suite from a clean development install.
- [ ] Unit and integration files may not collide through their import names.
- [ ] CI exercises the default invocation on every supported Python version or in one dedicated full-suite job.
- [ ] Coverage and documented external-service skip behavior remain intact.

## M2 – Knowledge layer and search

- **GitHub milestone:** [M2 – Knowledge layer and search](https://github.com/melliott18/CogniStore/milestone/1)
- **Delivery tickets:** 13
- **Open:** 14 including the epic

### [#13 — [Epic] M2 – Knowledge layer and search](https://github.com/melliott18/CogniStore/issues/13)

- **Kind:** Milestone epic
- **Status:** Open
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `type:epic`, `roadmap`, `area:indexing`
- **Last updated:** 2026-08-11

Parent roadmap: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Outcome

CogniStore has a durable Postgres/pgvector catalog and can extract, index, embed, search, and answer questions about stored content through stable external APIs.

#### Milestone success criteria

- Objects are queryable by metadata, vector similarity, and keyword.
- PDF and DOCX content can be extracted, chunked, and deduplicated.
- The Ask service blends retrieval signals and returns source-backed results.
- Public APIs, a Python SDK, and a search UI expose the supported workflow.
- Placement policies can consume MIME and embedding-derived features.

#### Child issues

- [ ] [#30](https://github.com/melliott18/CogniStore/issues/30) — Migrate the catalog to Postgres and pgvector through a DAL
- [ ] [#31](https://github.com/melliott18/CogniStore/issues/31) — Persist move, policy, failure, and retry audit events
- [ ] [#32](https://github.com/melliott18/CogniStore/issues/32) — Detect MIME types using libmagic with safe fallback
- [ ] [#33](https://github.com/melliott18/CogniStore/issues/33) — Extract normalized text and metadata from PDF and DOCX
- [ ] [#34](https://github.com/melliott18/CogniStore/issues/34) — Add canonical checksums, deterministic chunking, and CAS keys
- [ ] [#35](https://github.com/melliott18/CogniStore/issues/35) — Implement safe deduplication reference and deletion semantics
- [ ] [#36](https://github.com/melliott18/CogniStore/issues/36) — Generate embeddings and query them through pgvector
- [ ] [#37](https://github.com/melliott18/CogniStore/issues/37) — Build a rebuildable keyword search index
- [ ] [#38](https://github.com/melliott18/CogniStore/issues/38) — Implement hybrid metadata, vector, and keyword Ask retrieval
- [ ] [#39](https://github.com/melliott18/CogniStore/issues/39) — Expose a versioned REST API and OpenAPI contract
- [ ] [#40](https://github.com/melliott18/CogniStore/issues/40) — Publish a typed Python SDK for the public API
- [ ] [#41](https://github.com/melliott18/CogniStore/issues/41) — Deliver a content-search UI and end-to-end sample corpus
- [ ] [#42](https://github.com/melliott18/CogniStore/issues/42) — Feed MIME and embedding features into placement policies

Dependencies listed inside each child issue are authoritative; checklist order is the suggested implementation sequence.

#### Source

`docs/roadmap.md`: persistent control plane, indexing and knowledge layer, external APIs, sample datasets, and the M2 success criteria.

### Delivery tickets

### [#30 — [M2] Migrate the catalog to Postgres and pgvector through a DAL](https://github.com/melliott18/CogniStore/issues/30)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `type:feature`, `roadmap`, `area:control-plane`
- **Last updated:** 2026-08-11

Parent epic: [#13](https://github.com/melliott18/CogniStore/issues/13)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Replace the prototype catalog with a durable, migration-managed control-plane store while preserving a clean domain interface.

#### Scope

- Define normalized schemas for objects, placements, tiers, pools, and vector extension setup.
- Add reversible migrations and a small transactional data-access layer.
- Provide a documented SQLite-to-Postgres migration path for existing prototype data.

#### Out of scope

- Tenant isolation and RBAC.
- Search indexing beyond pgvector schema support.

#### Acceptance criteria

- [ ] Clean install, upgrade, downgrade, and failed-migration paths are tested.
- [ ] Current catalog behavior passes backend-parity tests.
- [ ] Concurrent placement updates preserve uniqueness and placement invariants.
- [ ] Application code uses the DAL rather than direct database-specific SQL.

#### Dependencies

- None.

#### Roadmap coverage

Platform foundation → Postgres + pgvector, migrations, and DAL.

### [#31 — [M2] Persist move, policy, failure, and retry audit events](https://github.com/melliott18/CogniStore/issues/31)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `type:feature`, `roadmap`, `area:control-plane`
- **Last updated:** 2026-08-11

Parent epic: [#13](https://github.com/melliott18/CogniStore/issues/13)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Create an append-oriented operational history that correlates decisions, jobs, objects, and outcomes.

#### Scope

- Define event schemas with actor, object, job, policy/version, timestamps, outcome, and structured details.
- Write events for moves, policy decisions, failures, retries, and manual actions.
- Provide retention configuration and query methods.

#### Out of scope

- Tamper-evident or immutable storage guarantees.
- Admin UI rendering.

#### Acceptance criteria

- [ ] Every terminal move and policy decision has a correlated event chain.
- [ ] Retry and failure details remain queryable after worker restarts.
- [ ] Schema evolution and retention behavior are tested.
- [ ] Sensitive values are redacted before persistence.

#### Dependencies

- [#30](https://github.com/melliott18/CogniStore/issues/30) — [M2] Migrate the catalog to Postgres and pgvector through a DAL

#### Roadmap coverage

Platform foundation → event/audit tables for moves, policy decisions, failures, and retries.

### [#32 — [M2] Detect MIME types using libmagic with safe fallback](https://github.com/melliott18/CogniStore/issues/32)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `type:feature`, `roadmap`, `area:indexing`
- **Last updated:** 2026-08-11

Parent epic: [#13](https://github.com/melliott18/CogniStore/issues/13)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Replace extension-only MIME inference with content-aware detection and provenance.

#### Scope

- Integrate libmagic behind an indexing adapter.
- Retain filename inference as a documented fallback.
- Record detector, confidence/provenance, and disagreements.

#### Out of scope

- Document text extraction.
- PII classification.

#### Acceptance criteria

- [ ] Representative binary and text fixtures produce expected MIME types.
- [ ] Missing native libraries degrade without aborting an entire scan.
- [ ] Malformed and empty files are handled per object.
- [ ] Detection results and provenance persist in the catalog.

#### Dependencies

- [#30](https://github.com/melliott18/CogniStore/issues/30) — [M2] Migrate the catalog to Postgres and pgvector through a DAL

#### Roadmap coverage

Indexing and knowledge layer → robust MIME detection with libmagic.

### [#33 — [M2] Extract normalized text and metadata from PDF and DOCX](https://github.com/melliott18/CogniStore/issues/33)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `type:feature`, `roadmap`, `area:indexing`
- **Last updated:** 2026-08-11

Parent epic: [#13](https://github.com/melliott18/CogniStore/issues/13)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Create a bounded, pluggable document extraction pipeline for the first two roadmap formats.

#### Scope

- Define a parser adapter and choose/document the initial parsing runtime.
- Extract normalized text plus core document metadata from PDF and DOCX.
- Apply file-size, execution-time, and output-size limits and record per-object failures.

#### Out of scope

- OCR and scanned-image recognition.
- Formats beyond PDF and DOCX.

#### Acceptance criteria

- [ ] Licensed fixtures yield deterministic normalized text and metadata.
- [ ] Corrupt, encrypted, and unsupported files fail without stopping unrelated indexing.
- [ ] Reprocessing is idempotent and versioned by parser implementation.
- [ ] Timeout and size-limit behavior is covered by tests.

#### Dependencies

- [#32](https://github.com/melliott18/CogniStore/issues/32) — [M2] Detect MIME types using libmagic with safe fallback

#### Roadmap coverage

Indexing and knowledge layer → PDF/DOCX parsing pipeline.

### [#34 — [M2] Add canonical checksums, deterministic chunking, and CAS keys](https://github.com/melliott18/CogniStore/issues/34)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `type:feature`, `roadmap`, `area:indexing`
- **Last updated:** 2026-08-11

Parent epic: [#13](https://github.com/melliott18/CogniStore/issues/13)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Represent content with stable full-object and chunk identities suitable for deduplication and retrieval.

#### Scope

- Compute full-object SHA-256 while streaming.
- Define deterministic chunk boundaries and versioned chunk metadata.
- Derive content-addressed storage keys and persist object-to-chunk mappings.

#### Out of scope

- Garbage-collecting unreferenced content.
- Model embeddings.

#### Acceptance criteria

- [ ] The same bytes always produce the same object digest, chunks, and CAS keys.
- [ ] Large objects are processed with bounded memory.
- [ ] Chunking version changes cannot silently mix incompatible layouts.
- [ ] Catalog scan regression tests cover one-byte, empty, and multi-chunk objects.

#### Dependencies

- [#30](https://github.com/melliott18/CogniStore/issues/30) — [M2] Migrate the catalog to Postgres and pgvector through a DAL
- [#33](https://github.com/melliott18/CogniStore/issues/33) — [M2] Extract normalized text and metadata from PDF and DOCX

#### Roadmap coverage

Indexing and knowledge layer → chunking, checksums, and CAS keys.

### [#35 — [M2] Implement safe deduplication reference and deletion semantics](https://github.com/melliott18/CogniStore/issues/35)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `type:feature`, `roadmap`, `area:indexing`
- **Last updated:** 2026-08-11

Parent epic: [#13](https://github.com/melliott18/CogniStore/issues/13)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Allow logical objects to share content without allowing one deletion to break remaining references.

#### Scope

- Track reference counts or equivalent ownership for shared CAS objects and chunks.
- Define logical deletion, physical reclamation eligibility, and race handling.
- Add a non-destructive reconciliation report for reference inconsistencies.

#### Out of scope

- Automated orphan deletion in production.
- Cross-region replication.

#### Acceptance criteria

- [ ] Identical content is stored once while each logical object remains independently addressable.
- [ ] Deleting one duplicate leaves all other references readable.
- [ ] Concurrent create/delete tests preserve reference invariants.
- [ ] Physical reclamation requires zero references and a configurable grace period.

#### Dependencies

- [#34](https://github.com/melliott18/CogniStore/issues/34) — [M2] Add canonical checksums, deterministic chunking, and CAS keys

#### Roadmap coverage

Indexing and knowledge layer → checksum/dedup pipeline.

### [#36 — [M2] Generate embeddings and query them through pgvector](https://github.com/melliott18/CogniStore/issues/36)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `type:feature`, `roadmap`, `area:indexing`
- **Last updated:** 2026-08-11

Parent epic: [#13](https://github.com/melliott18/CogniStore/issues/13)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Index document chunks as versioned embeddings and expose filtered similarity search.

#### Scope

- Define an embedding-provider interface with sentence-transformers and API-compatible implementations.
- Batch and retry embedding work with model/version metadata.
- Store vectors in pgvector and support similarity queries with metadata filters.

#### Out of scope

- Model fine-tuning.
- Hybrid ranking with keyword results.

#### Acceptance criteria

- [ ] Chunks can be embedded, queried, and re-embedded reproducibly.
- [ ] Incompatible model versions are never mixed in one search space.
- [ ] Provider failures are retryable without duplicating vectors.
- [ ] Similarity query latency and index configuration are measured on the sample corpus.

#### Dependencies

- [#30](https://github.com/melliott18/CogniStore/issues/30) — [M2] Migrate the catalog to Postgres and pgvector through a DAL
- [#34](https://github.com/melliott18/CogniStore/issues/34) — [M2] Add canonical checksums, deterministic chunking, and CAS keys

#### Roadmap coverage

Indexing and knowledge layer → embeddings into pgvector.

### [#37 — [M2] Build a rebuildable keyword search index](https://github.com/melliott18/CogniStore/issues/37)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `type:feature`, `roadmap`, `area:indexing`
- **Last updated:** 2026-08-11

Parent epic: [#13](https://github.com/melliott18/CogniStore/issues/13)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Provide ranked full-text retrieval over extracted chunks and object metadata.

#### Scope

- Record an ADR selecting OpenSearch or a Tantivy-based implementation.
- Index normalized chunks and searchable metadata through an adapter.
- Support ranking, filters, updates, deletes, and full rebuild from Postgres.

#### Out of scope

- Hybrid Ask ranking.
- Multi-region search clusters.

#### Acceptance criteria

- [ ] Fixture queries return expected ranked results and metadata filters.
- [ ] Updates and deletes become visible within documented consistency bounds.
- [ ] The entire index can be reconstructed from the catalog.
- [ ] Backend-specific failures do not corrupt catalog state.

#### Dependencies

- [#33](https://github.com/melliott18/CogniStore/issues/33) — [M2] Extract normalized text and metadata from PDF and DOCX
- [#34](https://github.com/melliott18/CogniStore/issues/34) — [M2] Add canonical checksums, deterministic chunking, and CAS keys

#### Roadmap coverage

Indexing and knowledge layer → keyword index.

### [#38 — [M2] Implement hybrid metadata, vector, and keyword Ask retrieval](https://github.com/melliott18/CogniStore/issues/38)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `type:feature`, `roadmap`, `area:indexing`
- **Last updated:** 2026-08-11

Parent epic: [#13](https://github.com/melliott18/CogniStore/issues/13)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Blend catalog, pgvector, and keyword results into grounded answers and source-backed retrieval responses.

#### Scope

- Define a retrieval contract with filters, limits, and score components.
- Fuse metadata, similarity, and keyword results with deterministic ranking rules.
- Return object/chunk citations and optionally synthesize an answer through a provider interface.

#### Out of scope

- Conversation memory and autonomous agents.
- Admin workflows unrelated to search.

#### Acceptance criteria

- [ ] Every returned passage identifies its source object and chunk.
- [ ] Score components and retrieval mode are inspectable.
- [ ] Missing vector, keyword, or generation providers degrade to supported retrieval modes.
- [ ] Golden fixture queries validate ranking and citations.

#### Dependencies

- [#36](https://github.com/melliott18/CogniStore/issues/36) — [M2] Generate embeddings and query them through pgvector
- [#37](https://github.com/melliott18/CogniStore/issues/37) — [M2] Build a rebuildable keyword search index

#### Roadmap coverage

Indexing and knowledge layer → Ask service blending metadata, vector, and keyword.

### [#39 — [M2] Expose a versioned REST API and OpenAPI contract](https://github.com/melliott18/CogniStore/issues/39)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `type:feature`, `roadmap`, `area:api`
- **Last updated:** 2026-08-11

Parent epic: [#13](https://github.com/melliott18/CogniStore/issues/13)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Provide a stable external interface for objects, catalog/search, policies, and asynchronous actions.

#### Scope

- Implement versioned FastAPI endpoints for supported object, catalog, search/Ask, policy, and action operations.
- Standardize pagination, validation, error envelopes, and asynchronous job responses.
- Generate and validate an OpenAPI contract; record whether gRPC has a justified near-term use case.

#### Out of scope

- Python SDK implementation.
- Authentication and tenant enforcement beyond explicit extension hooks.

#### Acceptance criteria

- [ ] Contract tests cover success, validation, pagination, missing resources, and backend failure mapping.
- [ ] Long-running actions return job IDs and expose status.
- [ ] OpenAPI generation is deterministic and checked in CI.
- [ ] No endpoint bypasses the service/DAL abstractions.

#### Dependencies

- [#30](https://github.com/melliott18/CogniStore/issues/30) — [M2] Migrate the catalog to Postgres and pgvector through a DAL
- [#38](https://github.com/melliott18/CogniStore/issues/38) — [M2] Implement hybrid metadata, vector, and keyword Ask retrieval

#### Roadmap coverage

API, CLI, and UI → REST and/or gRPC external API (REST selected for this milestone).

### [#40 — [M2] Publish a typed Python SDK for the public API](https://github.com/melliott18/CogniStore/issues/40)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `type:feature`, `roadmap`, `area:api`
- **Last updated:** 2026-08-11

Parent epic: [#13](https://github.com/melliott18/CogniStore/issues/13)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Give Python clients a versioned, testable interface to CogniStore without coupling to server internals.

#### Scope

- Generate or hand-maintain typed clients from the OpenAPI contract.
- Support configuration, timeouts, pagination, errors, and asynchronous action polling.
- Package examples and compatibility tests.

#### Out of scope

- SDKs for other languages.
- Direct database or driver access.

#### Acceptance criteria

- [ ] SDK contract tests run against the API test server.
- [ ] All public endpoints used by the M2 workflow have typed methods.
- [ ] Version compatibility and deprecation policy are documented.
- [ ] Package installation and a minimal query example work from a clean environment.

#### Dependencies

- [#39](https://github.com/melliott18/CogniStore/issues/39) — [M2] Expose a versioned REST API and OpenAPI contract

#### Roadmap coverage

API, CLI, and UI → Python SDK.

### [#41 — [M2] Deliver a content-search UI and end-to-end sample corpus](https://github.com/melliott18/CogniStore/issues/41)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `type:feature`, `roadmap`, `area:ui`
- **Last updated:** 2026-08-11

Parent epic: [#13](https://github.com/melliott18/CogniStore/issues/13)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Demonstrate that users can discover stored content through the M2 knowledge layer.

#### Scope

- Provide search and Ask views with filters, ranked results, citations, and object metadata.
- Create a licensed, sanitized sample corpus covering PDF, DOCX, and duplicate content.
- Add an end-to-end workflow that ingests, indexes, searches, and opens cited results.

#### Out of scope

- Driver, tier, and policy administration.
- Conversational memory.

#### Acceptance criteria

- [ ] A fresh environment can load the sample corpus using documented steps.
- [ ] Users can run keyword, vector, and Ask queries and inspect citations.
- [ ] Loading, empty, degraded-provider, and error states are handled.
- [ ] The end-to-end workflow runs automatically at reduced scale.

#### Dependencies

- [#39](https://github.com/melliott18/CogniStore/issues/39) — [M2] Expose a versioned REST API and OpenAPI contract
- [#40](https://github.com/melliott18/CogniStore/issues/40) — [M2] Publish a typed Python SDK for the public API

#### Roadmap coverage

M2 success criterion → query objects by content via API/UI; sample datasets.

### [#42 — [M2] Feed MIME and embedding features into placement policies](https://github.com/melliott18/CogniStore/issues/42)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `area:policy`, `type:feature`, `roadmap`
- **Last updated:** 2026-08-11

Parent epic: [#13](https://github.com/melliott18/CogniStore/issues/13)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Make the new knowledge-layer signals available to placement evaluation without coupling policy code to indexing backends.

#### Scope

- Define a versioned policy-feature projection for MIME and embedding-derived signals.
- Load features through the catalog/service boundary.
- Expose missing/stale feature state and deterministic fallbacks.

#### Out of scope

- Learning-based policy training.
- Cost/carbon optimization.

#### Acceptance criteria

- [ ] Policies can select on MIME and configured embedding-derived classifications or similarity signals.
- [ ] Feature provenance and freshness are included in dry-run output.
- [ ] Missing indexing providers never cause unsafe movement.
- [ ] Integration tests cover reindexing and policy reevaluation.

#### Dependencies

- [#32](https://github.com/melliott18/CogniStore/issues/32) — [M2] Detect MIME types using libmagic with safe fallback
- [#36](https://github.com/melliott18/CogniStore/issues/36) — [M2] Generate embeddings and query them through pgvector
- [#39](https://github.com/melliott18/CogniStore/issues/39) — [M2] Expose a versioned REST API and OpenAPI contract

#### Roadmap coverage

M2 success criterion → policy uses MIME and embeddings features.

## M3 – Explainable policy engine

- **GitHub milestone:** [M3 – Explainable policy engine](https://github.com/melliott18/CogniStore/milestone/2)
- **Delivery tickets:** 11
- **Open:** 12 including the epic

### [#15 — [Epic] M3 – Explainable policy engine](https://github.com/melliott18/CogniStore/issues/15)

- **Kind:** Milestone epic
- **Status:** Open
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `type:epic`, `roadmap`
- **Last updated:** 2026-08-11

Parent roadmap: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Outcome

Placement decisions use behavioral and content signals, remain stable under guardrails, expose structured explanations, and optimize within cost and carbon budgets.

#### Milestone success criteria

- Automated moves do not flap between tiers.
- Policy inputs, decisions, and reasons are reproducible and queryable.
- Learning and LLM-assisted modes have offline evaluation and safe fallbacks.
- Budget, locality, and sustainability constraints are enforceable and explainable.

#### Child issues

- [ ] [#43](https://github.com/melliott18/CogniStore/issues/43) — Capture access events and compute recency/frequency signals
- [ ] [#44](https://github.com/melliott18/CogniStore/issues/44) — Add importance tags and minimum-residency rules
- [ ] [#45](https://github.com/melliott18/CogniStore/issues/45) — Prevent tier flapping with hysteresis and cooldowns
- [ ] [#46](https://github.com/melliott18/CogniStore/issues/46) — Log versioned policy features and outcome labels
- [ ] [#47](https://github.com/melliott18/CogniStore/issues/47) — Train and evaluate a supervised placement baseline
- [ ] [#48](https://github.com/melliott18/CogniStore/issues/48) — Integrate schema-validated LLM-assisted placement decisions
- [ ] [#49](https://github.com/melliott18/CogniStore/issues/49) — Persist structured policy decision reasons
- [ ] [#50](https://github.com/melliott18/CogniStore/issues/50) — Expose placement explanations and before/after diffs
- [ ] [#51](https://github.com/melliott18/CogniStore/issues/51) — Model tier pools, regions, latency, cost, and carbon attributes
- [ ] [#52](https://github.com/melliott18/CogniStore/issues/52) — Build calibrated storage cost and carbon estimators
- [ ] [#53](https://github.com/melliott18/CogniStore/issues/53) — Enforce cost/carbon budgets and provide what-if simulation

Dependencies listed inside each child issue are authoritative; checklist order is the suggested implementation sequence.

#### Source

`docs/roadmap.md`: policy engine v2, tier/pool abstractions, explainability, and cost/carbon modeling.

### Delivery tickets

### [#43 — [M3] Capture access events and compute recency/frequency signals](https://github.com/melliott18/CogniStore/issues/43)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `type:feature`, `roadmap`
- **Last updated:** 2026-08-11

Parent epic: [#15](https://github.com/melliott18/CogniStore/issues/15)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Give placement policies reliable behavioral signals derived from object access history.

#### Scope

- Define access-event semantics for reads, writes, listings, and policy-relevant touches.
- Persist events or aggregates and compute configurable recency/frequency windows.
- Expose signal freshness, sampling, and missing-data state to policies.

#### Out of scope

- Importance tags and residency rules.
- Model training.

#### Acceptance criteria

- [ ] API and supported driver access paths emit correlated events without double-counting retries.
- [ ] Windowed features are reproducible for fixture histories.
- [ ] Sparse or unavailable history produces documented safe defaults.
- [ ] Volume, retention, and aggregation behavior are measured.

#### Dependencies

- [#31](https://github.com/melliott18/CogniStore/issues/31) — [M2] Persist move, policy, failure, and retry audit events
- [#39](https://github.com/melliott18/CogniStore/issues/39) — [M2] Expose a versioned REST API and OpenAPI contract

#### Roadmap coverage

Policy engine v2 → recency/frequency from access logs.

### [#44 — [M3] Add importance tags and minimum-residency rules](https://github.com/melliott18/CogniStore/issues/44)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `type:feature`, `roadmap`
- **Last updated:** 2026-08-11

Parent epic: [#15](https://github.com/melliott18/CogniStore/issues/15)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Allow explicit business importance and placement residency to constrain automated movement.

#### Scope

- Define validated importance tags with actor and provenance.
- Track placement start time and configurable minimum residency by tier/policy.
- Apply both controls before optimization or learned decisions.

#### Out of scope

- Hysteresis and cooldown behavior.
- RBAC for who may set tags.

#### Acceptance criteria

- [ ] Importance and residency constraints appear in dry-run decisions.
- [ ] A move cannot violate an active minimum-residency rule.
- [ ] Tag changes are audited and trigger deterministic reevaluation.
- [ ] Boundary-time and missing-tag behavior are tested.

#### Dependencies

- [#43](https://github.com/melliott18/CogniStore/issues/43) — [M3] Capture access events and compute recency/frequency signals
- [#30](https://github.com/melliott18/CogniStore/issues/30) — [M2] Migrate the catalog to Postgres and pgvector through a DAL

#### Roadmap coverage

Policy engine v2 → importance tags and residency timers.

### [#45 — [M3] Prevent tier flapping with hysteresis and cooldowns](https://github.com/melliott18/CogniStore/issues/45)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `type:feature`, `roadmap`
- **Last updated:** 2026-08-11

Parent epic: [#15](https://github.com/melliott18/CogniStore/issues/15)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Keep automatic placement stable when signals fluctuate near a decision boundary.

#### Scope

- Define policy-level hysteresis bands and post-move cooldown periods.
- Persist enough state to apply guardrails across worker restarts.
- Instrument suppressed moves and their reasons.

#### Out of scope

- Cost/carbon budget optimization.
- General model evaluation.

#### Acceptance criteria

- [ ] Repeated evaluation of unchanged or boundary-adjacent objects does not flap tiers.
- [ ] Cooldown and hysteresis are configurable and visible in explanations.
- [ ] Property/soak tests cover noisy signals and clock boundaries.
- [ ] Emergency or compliance overrides are explicit and audited.

#### Dependencies

- [#44](https://github.com/melliott18/CogniStore/issues/44) — [M3] Add importance tags and minimum-residency rules

#### Roadmap coverage

Policy engine v2 → hysteresis/cooldowns; M3 no-tier-flapping success criterion.

### [#46 — [M3] Log versioned policy features and outcome labels](https://github.com/melliott18/CogniStore/issues/46)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `type:feature`, `roadmap`
- **Last updated:** 2026-08-11

Parent epic: [#15](https://github.com/melliott18/CogniStore/issues/15)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Create a reproducible dataset contract for offline placement evaluation and supervised learning.

#### Scope

- Define versioned feature, decision, outcome, and label schemas.
- Record policy/model versions and data provenance.
- Provide privacy-aware export and validation tooling.

#### Out of scope

- Training a production model.
- Online learning.

#### Acceptance criteria

- [ ] A decision can be reconstructed from its stored feature snapshot.
- [ ] Schema changes are versioned and migration-compatible.
- [ ] Exports exclude configured sensitive fields and document sampling.
- [ ] Validation detects missing, leaking, or temporally invalid labels.

#### Dependencies

- [#43](https://github.com/melliott18/CogniStore/issues/43) — [M3] Capture access events and compute recency/frequency signals
- [#42](https://github.com/melliott18/CogniStore/issues/42) — [M2] Feed MIME and embedding features into placement policies
- [#31](https://github.com/melliott18/CogniStore/issues/31) — [M2] Persist move, policy, failure, and retry audit events

#### Roadmap coverage

Policy engine v2 → log features/labels for a supervised baseline.

### [#47 — [M3] Train and evaluate a supervised placement baseline](https://github.com/melliott18/CogniStore/issues/47)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `type:feature`, `roadmap`
- **Last updated:** 2026-08-11

Parent epic: [#15](https://github.com/melliott18/CogniStore/issues/15)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Establish a repeatable learned baseline and compare it with existing rules before any promotion.

#### Scope

- Select a simple interpretable baseline and version its training configuration.
- Build repeatable offline train/evaluate commands and time-aware splits.
- Compare accuracy, movement cost, constraint violations, and stability against rule policies.

#### Out of scope

- Online training and contextual bandits.
- Automatic promotion to production.

#### Acceptance criteria

- [ ] Training and evaluation are reproducible from a versioned dataset snapshot.
- [ ] Leakage and class-imbalance checks run automatically.
- [ ] A promotion threshold and safe rule fallback are documented.
- [ ] Evaluation artifacts capture metrics, model version, code version, and data window.

#### Dependencies

- [#46](https://github.com/melliott18/CogniStore/issues/46) — [M3] Log versioned policy features and outcome labels
- [#45](https://github.com/melliott18/CogniStore/issues/45) — [M3] Prevent tier flapping with hysteresis and cooldowns

#### Roadmap coverage

Policy engine v2 → supervised baseline and offline evaluation.

### [#48 — [M3] Integrate schema-validated LLM-assisted placement decisions](https://github.com/melliott18/CogniStore/issues/48)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `type:feature`, `roadmap`
- **Last updated:** 2026-08-11

Parent epic: [#15](https://github.com/melliott18/CogniStore/issues/15)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Replace the threshold mock with a provider-neutral, auditable LLM decision path that fails safely.

#### Scope

- Define provider adapters, prompt/version metadata, and a strict decision JSON schema.
- Apply timeouts, retries appropriate to inference, redaction, and allowed-tier validation.
- Run all existing policy guardrails after parsing and support a zero-write dry-run.

#### Out of scope

- Fine-tuning and unrestricted agent/tool use.
- Using LLM output to bypass compliance controls.

#### Acceptance criteria

- [ ] Malformed, late, or unavailable provider responses cannot trigger a move.
- [ ] Deterministic fallback behavior is covered by tests.
- [ ] Prompts, redacted responses, schema errors, and final decisions are auditable.
- [ ] No external provider is required for the default test suite.

#### Dependencies

- [#45](https://github.com/melliott18/CogniStore/issues/45) — [M3] Prevent tier flapping with hysteresis and cooldowns
- [#31](https://github.com/melliott18/CogniStore/issues/31) — [M2] Persist move, policy, failure, and retry audit events

#### Roadmap coverage

Policy engine v2 → LLM-assisted decisions with schema validation, dry-run, and safe fallbacks.

### [#49 — [M3] Persist structured policy decision reasons](https://github.com/melliott18/CogniStore/issues/49)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `type:feature`, `roadmap`
- **Last updated:** 2026-08-11

Parent epic: [#15](https://github.com/melliott18/CogniStore/issues/15)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Represent why a placement was proposed or suppressed in a stable, queryable schema.

#### Scope

- Define reason codes, decisive signals, constraints, policy/model versions, and confidence fields.
- Persist both move and stay/suppressed decisions.
- Link decisions to feature snapshots, audit events, and resulting jobs.

#### Out of scope

- Rendering explanations in the UI.
- Counterfactual simulation.

#### Acceptance criteria

- [ ] Every evaluated object produces a structured reason or an explicitly sampled record.
- [ ] Reason schemas are versioned and documented.
- [ ] Secrets and raw sensitive content are excluded.
- [ ] A decision can be traced from inputs through action and final outcome.

#### Dependencies

- [#31](https://github.com/melliott18/CogniStore/issues/31) — [M2] Persist move, policy, failure, and retry audit events
- [#45](https://github.com/melliott18/CogniStore/issues/45) — [M3] Prevent tier flapping with hysteresis and cooldowns
- [#46](https://github.com/melliott18/CogniStore/issues/46) — [M3] Log versioned policy features and outcome labels

#### Roadmap coverage

Policy engine v2 → persist structured reasons.

### [#50 — [M3] Expose placement explanations and before/after diffs](https://github.com/melliott18/CogniStore/issues/50)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `type:feature`, `roadmap`, `area:ui`
- **Last updated:** 2026-08-11

Parent epic: [#15](https://github.com/melliott18/CogniStore/issues/15)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Make policy behavior understandable through the API and UI before and after execution.

#### Scope

- Expose structured reasons and decisive signals through versioned API resources.
- Render current versus proposed placement, changed fields, and guardrail outcomes.
- Use one schema for dry-run and executed decisions, with execution state layered on top.

#### Out of scope

- Editing policies through a visual workflow builder.
- General administration UI.

#### Acceptance criteria

- [ ] Users can inspect why an object moved, stayed, or was suppressed.
- [ ] Dry-run diffs cannot be confused with completed actions.
- [ ] API/UI handle unavailable model details without hiding rule or constraint reasons.
- [ ] End-to-end tests trace a decision through its final job outcome.

#### Dependencies

- [#49](https://github.com/melliott18/CogniStore/issues/49) — [M3] Persist structured policy decision reasons
- [#39](https://github.com/melliott18/CogniStore/issues/39) — [M2] Expose a versioned REST API and OpenAPI contract
- [#41](https://github.com/melliott18/CogniStore/issues/41) — [M2] Deliver a content-search UI and end-to-end sample corpus

#### Roadmap coverage

Policy engine v2 → render reasons and before/after placement diffs in API/UI.

### [#51 — [M3] Model tier pools, regions, latency, cost, and carbon attributes](https://github.com/melliott18/CogniStore/issues/51)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `type:feature`, `roadmap`, `area:storage`
- **Last updated:** 2026-08-11

Parent epic: [#15](https://github.com/melliott18/CogniStore/issues/15)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Give policies a validated topology and attribute model beyond the current hot/warm labels.

#### Scope

- Define pools, membership, region, latency, capacity, price, and carbon-intensity attributes.
- Record source, units, timestamps, and freshness for measured or configured values.
- Expose hard locality constraints separately from optimization objectives.

#### Out of scope

- Cloud billing reconciliation.
- Policy budget optimization itself.

#### Acceptance criteria

- [ ] Invalid units, missing required topology, and stale attributes are explicit.
- [ ] Policies can enumerate eligible placements without backend-specific logic.
- [ ] Hard region/locality constraints always filter candidates before scoring.
- [ ] Migrations and configuration examples cover multi-pool tiers.

#### Dependencies

- [#30](https://github.com/melliott18/CogniStore/issues/30) — [M2] Migrate the catalog to Postgres and pgvector through a DAL

#### Roadmap coverage

Multi-backend storage → tier/pool abstractions with regions and cost/latency/carbon attributes.

### [#52 — [M3] Build calibrated storage cost and carbon estimators](https://github.com/melliott18/CogniStore/issues/52)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `type:feature`, `roadmap`
- **Last updated:** 2026-08-11

Parent epic: [#15](https://github.com/melliott18/CogniStore/issues/15)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Estimate the financial and carbon impact of current and candidate placements using versioned assumptions.

#### Scope

- Estimate storage, request, transfer, and retrieval costs by tier/backend.
- Estimate carbon using documented intensity and energy assumptions.
- Support calibration inputs and expose uncertainty or unavailable data.

#### Out of scope

- Invoice-grade accounting.
- Budget enforcement and what-if UX.

#### Acceptance criteria

- [ ] All units, sources, effective dates, and formulas are versioned.
- [ ] Golden fixtures produce reproducible totals.
- [ ] Unknown attributes cannot silently become zero cost or zero carbon.
- [ ] Estimator outputs are available to policies and explanations.

#### Dependencies

- [#51](https://github.com/melliott18/CogniStore/issues/51) — [M3] Model tier pools, regions, latency, cost, and carbon attributes

#### Roadmap coverage

Reliability, performance, cost → estimators per tier.

### [#53 — [M3] Enforce cost/carbon budgets and provide what-if simulation](https://github.com/melliott18/CogniStore/issues/53)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `type:feature`, `roadmap`
- **Last updated:** 2026-08-11

Parent epic: [#15](https://github.com/melliott18/CogniStore/issues/15)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Let operators test and enforce placement policies within financial and sustainability constraints.

#### Scope

- Define budget periods, scopes, hard limits, and soft objectives.
- Integrate estimator outputs into candidate scoring and constraint checks.
- Simulate policy changes without moving data and compare current versus proposed cost/carbon/placement.

#### Out of scope

- Billing settlement and carbon offsets.
- Operational paging configuration.

#### Acceptance criteria

- [ ] Hard budgets cannot be exceeded by an automated action without an explicit audited override.
- [ ] What-if runs perform zero storage/catalog mutations.
- [ ] Results show assumptions, deltas, affected objects, and binding constraints.
- [ ] Tests cover competing cost, performance, locality, and carbon objectives.

#### Dependencies

- [#52](https://github.com/melliott18/CogniStore/issues/52) — [M3] Build calibrated storage cost and carbon estimators
- [#50](https://github.com/melliott18/CogniStore/issues/50) — [M3] Expose placement explanations and before/after diffs

#### Roadmap coverage

Policy engine v2 and reliability/cost → budget guardrails and what-if simulations.

## M4 – Production platform

- **GitHub milestone:** [M4 – Production platform](https://github.com/melliott18/CogniStore/milestone/3)
- **Delivery tickets:** 20
- **Hardening follow-ups:** 1
- **Open:** 22 including the epic and hardening follow-up

### [#14 — [Epic] M4 – Production platform](https://github.com/melliott18/CogniStore/issues/14)

- **Kind:** Milestone epic
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `type:epic`, `roadmap`, `area:delivery`
- **Last updated:** 2026-08-27

Parent roadmap: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Outcome

CogniStore is multi-tenant, secure, observable, repairable, deployable on Kubernetes, and operable across multiple cloud storage backends.

#### Milestone success criteria

- Tenant isolation and RBAC are enforced end to end.
- Audit, metrics, traces, logs, SLOs, and repair workflows support production operation.
- Azure Blob and GCS are supported alongside POSIX and S3.
- Helm-based deployment, autoscaling, administration UI, and operator documentation are complete.

#### Child issues

- [ ] [#54](https://github.com/melliott18/CogniStore/issues/54) — Implement an Azure Blob storage driver
- [ ] [#55](https://github.com/melliott18/CogniStore/issues/55) — Implement a Google Cloud Storage driver
- [ ] [#56](https://github.com/melliott18/CogniStore/issues/56) — Add JWT and OIDC authentication
- [ ] [#57](https://github.com/melliott18/CogniStore/issues/57) — Define and enforce role-based authorization
- [ ] [#58](https://github.com/melliott18/CogniStore/issues/58) — Enforce per-tenant ownership and isolation
- [ ] [#59](https://github.com/melliott18/CogniStore/issues/59) — Integrate Vault/KMS-backed secrets and key providers
- [ ] [#60](https://github.com/melliott18/CogniStore/issues/60) — Enforce encryption at rest and in transit
- [ ] [#61](https://github.com/melliott18/CogniStore/issues/61) — Add pluggable PII detection and policy hooks
- [ ] [#62](https://github.com/melliott18/CogniStore/issues/62) — Implement legal holds and deletion protection
- [ ] [#63](https://github.com/melliott18/CogniStore/issues/63) — Make the audit trail tamper-evident and coverage-complete
- [ ] [#64](https://github.com/melliott18/CogniStore/issues/64) — Enforce data-locality constraints
- [ ] [#65](https://github.com/melliott18/CogniStore/issues/65) — Add Prometheus metrics, OpenTelemetry traces, and structured logs
- [ ] [#66](https://github.com/melliott18/CogniStore/issues/66) — Define SLOs, error budgets, and operational alerts
- [ ] [#67](https://github.com/melliott18/CogniStore/issues/67) — Build catalog-to-storage consistency checks
- [ ] [#68](https://github.com/melliott18/CogniStore/issues/68) — Add idempotent auto-repair workflows
- [ ] [#69](https://github.com/melliott18/CogniStore/issues/69) — Clean confirmed orphaned content with grace periods
- [ ] [#70](https://github.com/melliott18/CogniStore/issues/70) — Deliver the production administration UI
- [ ] [#71](https://github.com/melliott18/CogniStore/issues/71) — Deploy CogniStore with Helm and Kubernetes autoscaling
- [ ] [#72](https://github.com/melliott18/CogniStore/issues/72) — Publish Terraform and production reference configurations
- [ ] [#73](https://github.com/melliott18/CogniStore/issues/73) — Publish operator runbooks, migration guides, and reference architectures

Dependencies listed inside each child issue are authoritative; checklist order is the suggested implementation sequence.

#### Source

`docs/roadmap.md`: multi-backend storage, security/compliance/tenancy, observability/ops, admin UI, resilience, deployment, and documentation.

#### Hardening follow-ups

- [ ] [#91](https://github.com/melliott18/CogniStore/issues/91) — Make POSIX path containment race-safe against symlink swaps

### Delivery tickets

### [#54 — [M4] Implement an Azure Blob storage driver](https://github.com/melliott18/CogniStore/issues/54)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `type:feature`, `roadmap`, `area:storage`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Add Azure Blob as a conforming, stream-capable storage backend.

#### Scope

- Implement the shared driver contract and capability declarations.
- Support configuration, ranged reads, block uploads, pagination, metadata, and normalized errors.
- Add emulator-based tests and an opt-in live-cloud validation path.

#### Out of scope

- Azure Files and Data Lake-specific APIs.
- Cloud infrastructure provisioning.

#### Acceptance criteria

- [ ] The shared driver conformance suite passes.
- [ ] Large uploads use bounded memory and safe block commit semantics.
- [ ] Credentials are loaded through approved configuration and never logged.
- [ ] Provider throttling, missing objects, and partial upload cleanup are tested.

#### Dependencies

- [#17](https://github.com/melliott18/CogniStore/issues/17) — [M1] Implement an S3-compatible storage driver and conformance suite
- [#21](https://github.com/melliott18/CogniStore/issues/21) — [M1] Stream object moves with bounded memory and S3 multipart upload

#### Roadmap coverage

Multi-backend storage → Azure Blob driver.

### [#55 — [M4] Implement a Google Cloud Storage driver](https://github.com/melliott18/CogniStore/issues/55)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `type:feature`, `roadmap`, `area:storage`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Add GCS as a conforming, resumable storage backend.

#### Scope

- Implement the shared driver contract and capability declarations.
- Support configuration, ranged reads, resumable uploads, pagination, metadata, and normalized errors.
- Add emulator-based tests and an opt-in live-cloud validation path.

#### Out of scope

- Filestore and BigQuery integrations.
- Cloud infrastructure provisioning.

#### Acceptance criteria

- [ ] The shared driver conformance suite passes.
- [ ] Interrupted large uploads can resume or cleanly abort.
- [ ] Credentials are loaded through approved configuration and never logged.
- [ ] Provider throttling, missing objects, and partial upload behavior are tested.

#### Dependencies

- [#17](https://github.com/melliott18/CogniStore/issues/17) — [M1] Implement an S3-compatible storage driver and conformance suite
- [#21](https://github.com/melliott18/CogniStore/issues/21) — [M1] Stream object moves with bounded memory and S3 multipart upload

#### Roadmap coverage

Multi-backend storage → GCS driver.

### [#56 — [M4] Add JWT and OIDC authentication](https://github.com/melliott18/CogniStore/issues/56)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `type:feature`, `roadmap`, `area:security`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Authenticate human and service clients through standards-based identity providers.

#### Scope

- Validate issuer, audience, signature, expiry, and required claims.
- Support OIDC discovery/JWKS rotation and service-to-service identities.
- Propagate a normalized principal through API and job submission boundaries.

#### Out of scope

- Role and permission evaluation.
- Tenant data partitioning.

#### Acceptance criteria

- [ ] Invalid, expired, wrong-audience, and unknown-key tokens fail closed.
- [ ] JWKS refresh and rotation are tested without restart.
- [ ] Authentication failures expose safe, consistent API errors.
- [ ] Principal identity is available for audit events without persisting raw tokens.

#### Dependencies

- [#39](https://github.com/melliott18/CogniStore/issues/39) — [M2] Expose a versioned REST API and OpenAPI contract
- [#31](https://github.com/melliott18/CogniStore/issues/31) — [M2] Persist move, policy, failure, and retry audit events

#### Roadmap coverage

Security, compliance, tenancy → JWT/OIDC authentication.

### [#57 — [M4] Define and enforce role-based authorization](https://github.com/melliott18/CogniStore/issues/57)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `type:feature`, `roadmap`, `area:security`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Ensure every protected API and operation checks an explicit permission model.

#### Scope

- Define roles and permissions for read, write, policy, movement, administration, and audit operations.
- Centralize authorization checks at service boundaries and worker job validation.
- Audit allow/deny outcomes at an appropriate sampling and sensitivity level.

#### Out of scope

- Tenant row/data isolation.
- External policy engines beyond the initial RBAC model.

#### Acceptance criteria

- [ ] An endpoint/operation permission matrix is documented and tested.
- [ ] No protected endpoint relies only on UI hiding.
- [ ] Workers revalidate authorization-sensitive job context where required.
- [ ] Authorization failures are fail-closed and do not leak resource existence.

#### Dependencies

- [#56](https://github.com/melliott18/CogniStore/issues/56) — [M4] Add JWT and OIDC authentication
- [#39](https://github.com/melliott18/CogniStore/issues/39) — [M2] Expose a versioned REST API and OpenAPI contract
- [#31](https://github.com/melliott18/CogniStore/issues/31) — [M2] Persist move, policy, failure, and retry audit events

#### Roadmap coverage

Security, compliance, tenancy → RBAC.

### [#58 — [M4] Enforce per-tenant ownership and isolation](https://github.com/melliott18/CogniStore/issues/58)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `type:feature`, `roadmap`, `area:security`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Prevent data, jobs, search results, and telemetry from crossing tenant boundaries.

#### Scope

- Add tenant ownership to persisted resources and job envelopes.
- Enforce tenant scoping in DAL, API, index/search, workers, caches, and storage key namespaces.
- Provide adversarial cross-tenant integration tests.

#### Out of scope

- Billing and tenant self-service.
- Cross-tenant sharing.

#### Acceptance criteria

- [ ] Cross-tenant reads, writes, search, policy actions, and job status access fail closed.
- [ ] Unique constraints and cache keys include tenant scope where required.
- [ ] Background jobs cannot lose or change tenant context.
- [ ] Isolation tests cover direct IDs, enumeration, filters, and crafted job payloads.

#### Dependencies

- [#57](https://github.com/melliott18/CogniStore/issues/57) — [M4] Define and enforce role-based authorization
- [#30](https://github.com/melliott18/CogniStore/issues/30) — [M2] Migrate the catalog to Postgres and pgvector through a DAL
- [#38](https://github.com/melliott18/CogniStore/issues/38) — [M2] Implement hybrid metadata, vector, and keyword Ask retrieval
- [#18](https://github.com/melliott18/CogniStore/issues/18) — [M1] Add a message bus and background worker runtime

#### Roadmap coverage

Security, compliance, tenancy → per-tenant isolation.

### [#59 — [M4] Integrate Vault/KMS-backed secrets and key providers](https://github.com/melliott18/CogniStore/issues/59)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `type:feature`, `roadmap`, `area:security`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Remove production credentials and keys from static configuration while supporting controlled rotation.

#### Scope

- Define provider interfaces for Vault and cloud KMS/secret managers.
- Resolve backend credentials and encryption keys at runtime with caching and expiry.
- Implement redaction, least-privilege guidance, rotation, and failure handling.

#### Out of scope

- A custom secrets server.
- Data encryption policy and TLS enforcement.

#### Acceptance criteria

- [ ] Production examples contain no plaintext secrets.
- [ ] Rotation can occur without rebuilding images or losing in-flight job state.
- [ ] Secret values never appear in logs, traces, errors, or audit payloads.
- [ ] Provider outage and expired-cache behavior fail according to documented policy.

#### Dependencies

- [#54](https://github.com/melliott18/CogniStore/issues/54) — [M4] Implement an Azure Blob storage driver
- [#55](https://github.com/melliott18/CogniStore/issues/55) — [M4] Implement a Google Cloud Storage driver
- [#56](https://github.com/melliott18/CogniStore/issues/56) — [M4] Add JWT and OIDC authentication

#### Roadmap coverage

Security, compliance, tenancy → KMS/Vault integration.

### [#60 — [M4] Enforce encryption at rest and in transit](https://github.com/melliott18/CogniStore/issues/60)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `type:feature`, `roadmap`, `area:security`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Define and verify encryption requirements for services, catalogs, and storage backends.

#### Scope

- Require verified TLS for external and internal service connections in production profiles.
- Configure supported backends and database for provider-managed or application-approved encryption at rest.
- Document key ownership, rotation, and exception handling.

#### Out of scope

- Inventing custom cryptographic primitives.
- End-user file encryption formats.

#### Acceptance criteria

- [ ] Production startup rejects insecure connections unless an explicit development-only mode is selected.
- [ ] Tests validate certificate verification and common misconfiguration failures.
- [ ] At-rest encryption status/configuration is observable without exposing keys.
- [ ] Rotation and backup/restore procedures are documented and exercised.

#### Dependencies

- [#59](https://github.com/melliott18/CogniStore/issues/59) — [M4] Integrate Vault/KMS-backed secrets and key providers
- [#30](https://github.com/melliott18/CogniStore/issues/30) — [M2] Migrate the catalog to Postgres and pgvector through a DAL

#### Roadmap coverage

Security, compliance, tenancy → encryption at rest and in transit.

### [#61 — [M4] Add pluggable PII detection and policy hooks](https://github.com/melliott18/CogniStore/issues/61)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `type:feature`, `roadmap`, `area:security`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Identify potentially sensitive content and make classification available to placement and governance controls.

#### Scope

- Define a replaceable detector interface and normalized finding schema.
- Run detection on supported extracted content with configurable limits.
- Persist redacted classifications and expose policy hooks without storing sensitive snippets by default.

#### Out of scope

- Automated regulatory certification.
- A universal classifier for every file type.

#### Acceptance criteria

- [ ] Detectors can be enabled per tenant/policy and replaced without schema changes.
- [ ] Findings include type, confidence, provenance, and detector version.
- [ ] Sensitive raw matches are not logged or exposed by default.
- [ ] Detection failure produces explicit unknown state and safe policy behavior.

#### Dependencies

- [#33](https://github.com/melliott18/CogniStore/issues/33) — [M2] Extract normalized text and metadata from PDF and DOCX
- [#58](https://github.com/melliott18/CogniStore/issues/58) — [M4] Enforce per-tenant ownership and isolation

#### Roadmap coverage

Security, compliance, tenancy → PII detection hooks.

### [#62 — [M4] Implement legal holds and deletion protection](https://github.com/melliott18/CogniStore/issues/62)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `type:feature`, `roadmap`, `area:security`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Prevent deletion or destructive movement of held data across all execution paths.

#### Scope

- Model scoped, auditable legal holds with lifecycle and actor metadata.
- Enforce holds in APIs, policies, workers, repair, and cleanup eligibility.
- Provide read-only inspection and authorized release workflows.

#### Out of scope

- Legal case-management software.
- Retention scheduling beyond hold semantics.

#### Acceptance criteria

- [ ] Held objects cannot be deleted, overwritten, garbage-collected, or moved in violation of the hold.
- [ ] Policy and worker paths fail closed and emit an audit event.
- [ ] Concurrent hold placement and deletion races preserve the hold.
- [ ] Release requires explicit authorization and retains historical audit records.

#### Dependencies

- [#58](https://github.com/melliott18/CogniStore/issues/58) — [M4] Enforce per-tenant ownership and isolation
- [#23](https://github.com/melliott18/CogniStore/issues/23) — [M1] Make move jobs idempotent with two-phase catalog updates
- [#31](https://github.com/melliott18/CogniStore/issues/31) — [M2] Persist move, policy, failure, and retry audit events

#### Roadmap coverage

Security, compliance, tenancy → legal holds.

### [#63 — [M4] Make the audit trail tamper-evident and coverage-complete](https://github.com/melliott18/CogniStore/issues/63)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `type:feature`, `roadmap`, `area:security`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Provide verifiable, complete evidence for security- and lifecycle-relevant actions.

#### Scope

- Define an event-coverage matrix for API, policy, worker, storage, governance, and admin actions.
- Add append-only/tamper-evident storage controls and integrity verification.
- Document retention, access, export, and verification procedures.

#### Out of scope

- A full SIEM product.
- Blockchain-based storage.

#### Acceptance criteria

- [ ] Automated tests prove required actions emit the expected correlated events.
- [ ] Unauthorized update/delete of audit records is denied.
- [ ] Integrity verification detects missing or altered records.
- [ ] Audit access and export are tenant-scoped and themselves audited.

#### Dependencies

- [#31](https://github.com/melliott18/CogniStore/issues/31) — [M2] Persist move, policy, failure, and retry audit events
- [#58](https://github.com/melliott18/CogniStore/issues/58) — [M4] Enforce per-tenant ownership and isolation
- [#59](https://github.com/melliott18/CogniStore/issues/59) — [M4] Integrate Vault/KMS-backed secrets and key providers

#### Roadmap coverage

Security, compliance, tenancy → immutable audit logs; M4 audit-complete criterion.

### [#64 — [M4] Enforce data-locality constraints](https://github.com/melliott18/CogniStore/issues/64)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `type:feature`, `roadmap`, `area:security`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Ensure placement candidates and moves respect configured geographic and residency requirements.

#### Scope

- Define tenant/object locality rules over the tier/pool region model.
- Filter placement candidates before optimization and revalidate at execution.
- Audit rejected plans and explicit, authorized exceptions.

#### Out of scope

- Legal interpretation of residency regulations.
- Cloud account provisioning.

#### Acceptance criteria

- [ ] A move cannot cross a prohibited locality boundary even if another policy favors it.
- [ ] Unknown or stale region data fails according to documented conservative behavior.
- [ ] Dry-run explains the binding locality constraint.
- [ ] Tests cover conflicting cost, performance, carbon, and locality objectives.

#### Dependencies

- [#51](https://github.com/melliott18/CogniStore/issues/51) — [M3] Model tier pools, regions, latency, cost, and carbon attributes
- [#58](https://github.com/melliott18/CogniStore/issues/58) — [M4] Enforce per-tenant ownership and isolation
- [#57](https://github.com/melliott18/CogniStore/issues/57) — [M4] Define and enforce role-based authorization

#### Roadmap coverage

Security, compliance, tenancy → data locality constraints.

### [#65 — [M4] Add Prometheus metrics, OpenTelemetry traces, and structured logs](https://github.com/melliott18/CogniStore/issues/65)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `type:feature`, `roadmap`, `area:observability`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Make API, worker, policy, index, and storage behavior observable end to end.

#### Scope

- Define bounded-cardinality metrics for requests, jobs, drivers, policies, and indexes.
- Propagate trace and correlation context across API and queue boundaries.
- Emit structured, redacted logs and provide baseline Grafana dashboards.

#### Out of scope

- Formal SLOs and paging policies.
- Vendor-specific hosted-observability configuration.

#### Acceptance criteria

- [ ] A request can be traced through queued work to storage and catalog operations.
- [ ] Metrics document units, labels, and cardinality constraints.
- [ ] Secrets and content are redacted from logs and spans.
- [ ] Local dashboards visualize movement throughput, failures, queue depth, and latency.

#### Dependencies

- [#39](https://github.com/melliott18/CogniStore/issues/39) — [M2] Expose a versioned REST API and OpenAPI contract
- [#18](https://github.com/melliott18/CogniStore/issues/18) — [M1] Add a message bus and background worker runtime
- [#31](https://github.com/melliott18/CogniStore/issues/31) — [M2] Persist move, policy, failure, and retry audit events

#### Roadmap coverage

Observability and ops → Prometheus/Grafana, OpenTelemetry, and structured logs.

### [#66 — [M4] Define SLOs, error budgets, and operational alerts](https://github.com/melliott18/CogniStore/issues/66)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `type:feature`, `roadmap`, `area:observability`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Turn telemetry into measurable service expectations and actionable alerts.

#### Scope

- Define move latency/error, API availability, indexing lag, and throughput SLOs.
- Implement burn-rate, capacity, cost, and carbon alerts with runbook links.
- Expose dashboards for SLO attainment and budget consumption.

#### Out of scope

- On-call staffing and escalation policy.
- Invoice-grade cost reporting.

#### Acceptance criteria

- [ ] Every SLO has an owner, formula, data source, target, and review window.
- [ ] Alert tests exercise breach and recovery behavior.
- [ ] Cost/carbon alerts use the same versioned estimators as policy budgets.
- [ ] Full-scale M1 qualification results can be evaluated against the SLO model.

#### Dependencies

- [#65](https://github.com/melliott18/CogniStore/issues/65) — [M4] Add Prometheus metrics, OpenTelemetry traces, and structured logs
- [#53](https://github.com/melliott18/CogniStore/issues/53) — [M3] Enforce cost/carbon budgets and provide what-if simulation
- [#29](https://github.com/melliott18/CogniStore/issues/29) — [M1] Qualify one-million-object moves and failure recovery

#### Roadmap coverage

Observability and ops → SLOs, throughput targets, cost/carbon guardrails, dashboards, and alerts.

### [#67 — [M4] Build catalog-to-storage consistency checks](https://github.com/melliott18/CogniStore/issues/67)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `type:feature`, `roadmap`, `area:observability`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Detect missing, duplicate, partial, mismatched, and untracked objects without modifying data.

#### Scope

- Compare catalog placements, backend listings/stats, checksums, and job state.
- Classify discrepancies with stable reason codes and severity.
- Support scoped, resumable, read-only scans and reports.

#### Out of scope

- Automatic repair and deletion.
- Cross-tenant aggregate reports.

#### Acceptance criteria

- [ ] Dry-run/read-only behavior is guaranteed.
- [ ] Fixtures cover missing source/destination, checksum mismatch, duplicate placement, partial job, and untracked object.
- [ ] Large scans are resumable and rate-limited.
- [ ] Results are tenant-scoped, audited, and exportable.

#### Dependencies

- [#30](https://github.com/melliott18/CogniStore/issues/30) — [M2] Migrate the catalog to Postgres and pgvector through a DAL
- [#22](https://github.com/melliott18/CogniStore/issues/22) — [M1] Verify object integrity before deleting the source
- [#31](https://github.com/melliott18/CogniStore/issues/31) — [M2] Persist move, policy, failure, and retry audit events

#### Roadmap coverage

Observability and ops → consistency checks.

### [#68 — [M4] Add idempotent auto-repair workflows](https://github.com/melliott18/CogniStore/issues/68)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `type:feature`, `roadmap`, `area:observability`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Repair unambiguous consistency failures safely while quarantining uncertain cases.

#### Scope

- Map eligible discrepancy classes to idempotent repair jobs.
- Require verification, audit, and policy/hold/locality checks before commit.
- Quarantine ambiguous cases for operator review.

#### Out of scope

- Deleting confirmed orphaned content.
- Undelete after retention expiry.

#### Acceptance criteria

- [ ] Repair defaults to plan-only mode and requires explicit enablement.
- [ ] Repeated repair attempts converge without duplicate placement or loss.
- [ ] Held, locality-constrained, or uncertain objects are never auto-modified.
- [ ] Failure-injection tests cover interruption at each repair phase.

#### Dependencies

- [#67](https://github.com/melliott18/CogniStore/issues/67) — [M4] Build catalog-to-storage consistency checks
- [#23](https://github.com/melliott18/CogniStore/issues/23) — [M1] Make move jobs idempotent with two-phase catalog updates
- [#62](https://github.com/melliott18/CogniStore/issues/62) — [M4] Implement legal holds and deletion protection
- [#64](https://github.com/melliott18/CogniStore/issues/64) — [M4] Enforce data-locality constraints

#### Roadmap coverage

Observability and ops → auto-repair.

### [#69 — [M4] Clean confirmed orphaned content with grace periods](https://github.com/melliott18/CogniStore/issues/69)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `type:feature`, `roadmap`, `area:observability`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Reclaim backend content only after proving it is unreferenced and unprotected.

#### Scope

- Define orphan eligibility across catalog, CAS references, in-flight jobs, holds, and retention windows.
- Provide report, quarantine, grace-period, and explicit execution stages.
- Record irreversible actions in the audit trail.

#### Out of scope

- General retention policy management.
- Immediate deletion on first detection.

#### Acceptance criteria

- [ ] Dry-run is the default and reports every blocking or qualifying condition.
- [ ] No object with a reference, active job, legal hold, or unresolved tenant can be deleted.
- [ ] Concurrent reference creation invalidates pending cleanup safely.
- [ ] Deletion failures are retryable and never hide partially completed cleanup.

#### Dependencies

- [#67](https://github.com/melliott18/CogniStore/issues/67) — [M4] Build catalog-to-storage consistency checks
- [#35](https://github.com/melliott18/CogniStore/issues/35) — [M2] Implement safe deduplication reference and deletion semantics
- [#62](https://github.com/melliott18/CogniStore/issues/62) — [M4] Implement legal holds and deletion protection
- [#63](https://github.com/melliott18/CogniStore/issues/63) — [M4] Make the audit trail tamper-evident and coverage-complete

#### Roadmap coverage

Observability and ops → orphan cleanup.

### [#70 — [M4] Deliver the production administration UI](https://github.com/melliott18/CogniStore/issues/70)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `type:feature`, `roadmap`, `area:ui`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Provide authorized operational views and actions for drivers, tiers, policies, jobs, audits, and repairs.

#### Scope

- Add driver/tier health and configuration views, policy/action management, job history, and repair workflows.
- Render audit trails, explanations, and dry-run diffs with tenant scoping.
- Apply RBAC to every view and mutation.

#### Out of scope

- A visual policy programming language.
- Cloud billing administration.

#### Acceptance criteria

- [ ] Operators can inspect health, preview and submit allowed actions, follow jobs, and review audit evidence.
- [ ] Unauthorized controls are absent and server-side checks still enforce every action.
- [ ] Loading, partial failure, empty, and stale-data states are handled.
- [ ] Critical destructive actions require an explicit confirmation and show scope.

#### Dependencies

- [#50](https://github.com/melliott18/CogniStore/issues/50) — [M3] Expose placement explanations and before/after diffs
- [#57](https://github.com/melliott18/CogniStore/issues/57) — [M4] Define and enforce role-based authorization
- [#58](https://github.com/melliott18/CogniStore/issues/58) — [M4] Enforce per-tenant ownership and isolation
- [#65](https://github.com/melliott18/CogniStore/issues/65) — [M4] Add Prometheus metrics, OpenTelemetry traces, and structured logs
- [#68](https://github.com/melliott18/CogniStore/issues/68) — [M4] Add idempotent auto-repair workflows

#### Roadmap coverage

API, CLI, and UI → admin UI for drivers, tiers, policies, actions, audit trail, previews, and diffs.

### [#71 — [M4] Deploy CogniStore with Helm and Kubernetes autoscaling](https://github.com/melliott18/CogniStore/issues/71)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `roadmap`, `type:chore`, `area:delivery`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Provide a secure, upgradeable Kubernetes deployment for API, workers, scheduler, and UI.

#### Scope

- Create a Helm chart with production profiles, probes, resources, network/security settings, and secret references.
- Support migrations, upgrades, rollback, and persistent dependencies.
- Configure HPA/KEDA behavior for API and workers and validate it under load.

#### Out of scope

- Every cloud topology and managed-service permutation.
- Terraform provisioning.

#### Acceptance criteria

- [ ] A clean cluster install passes an automated smoke test.
- [ ] Upgrade and rollback preserve catalog/job invariants.
- [ ] Workloads run as non-root with least-privilege defaults.
- [ ] Autoscaling responds to documented request/queue signals without duplicate work.

#### Dependencies

- [#28](https://github.com/melliott18/CogniStore/issues/28) — [M1] Provide a Docker-based development and integration environment
- [#56](https://github.com/melliott18/CogniStore/issues/56) — [M4] Add JWT and OIDC authentication
- [#58](https://github.com/melliott18/CogniStore/issues/58) — [M4] Enforce per-tenant ownership and isolation
- [#65](https://github.com/melliott18/CogniStore/issues/65) — [M4] Add Prometheus metrics, OpenTelemetry traces, and structured logs

#### Roadmap coverage

Delivery and DX plus M4 success → Helm, production configs, Kubernetes, and autoscaling.

### [#72 — [M4] Publish Terraform and production reference configurations](https://github.com/melliott18/CogniStore/issues/72)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `roadmap`, `type:chore`, `area:delivery`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Provide reproducible examples for the infrastructure surrounding a production deployment.

#### Scope

- Publish at least one supported reference topology with network, database, object storage, secrets, and Kubernetes dependencies.
- Parameterize environments without embedding credentials.
- Document cost, security, scaling, backup, and destroy considerations.

#### Out of scope

- Turnkey support for every cloud.
- Automatic production deployment from the repository.

#### Acceptance criteria

- [ ] Example plans validate in CI without creating resources.
- [ ] Inputs, outputs, provider versions, and state assumptions are documented.
- [ ] The topology is compatible with the Helm production profile.
- [ ] Destructive operations and data-retention implications are explicit.

#### Dependencies

- [#71](https://github.com/melliott18/CogniStore/issues/71) — [M4] Deploy CogniStore with Helm and Kubernetes autoscaling
- [#54](https://github.com/melliott18/CogniStore/issues/54) — [M4] Implement an Azure Blob storage driver
- [#55](https://github.com/melliott18/CogniStore/issues/55) — [M4] Implement a Google Cloud Storage driver
- [#59](https://github.com/melliott18/CogniStore/issues/59) — [M4] Integrate Vault/KMS-backed secrets and key providers

#### Roadmap coverage

Delivery and DX → Terraform samples and reference architectures.

### [#73 — [M4] Publish operator runbooks, migration guides, and reference architectures](https://github.com/melliott18/CogniStore/issues/73)

- **Kind:** Delivery ticket
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `documentation`, `roadmap`, `type:chore`, `area:docs`
- **Last updated:** 2026-08-11

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Goal

Give operators tested procedures for installing, upgrading, recovering, and troubleshooting CogniStore.

#### Scope

- Write install/upgrade/rollback, backup/restore, incident, queue/DLQ, repair, and security runbooks.
- Document SQLite-to-Postgres and backend/deployment migrations.
- Publish supported reference architectures and troubleshooting decision trees.

#### Out of scope

- Formal training curriculum.
- Undocumented experimental topologies.

#### Acceptance criteria

- [ ] A fresh-user walkthrough succeeds from a clean environment.
- [ ] Backup/restore and at least one incident/repair drill are exercised and recorded.
- [ ] Commands, diagrams, and configuration match the shipped release.
- [ ] Every SLO alert links to an actionable runbook.

#### Dependencies

- [#30](https://github.com/melliott18/CogniStore/issues/30) — [M2] Migrate the catalog to Postgres and pgvector through a DAL
- [#24](https://github.com/melliott18/CogniStore/issues/24) — [M1] Add retry, backoff, dead-letter, and redrive handling
- [#66](https://github.com/melliott18/CogniStore/issues/66) — [M4] Define SLOs, error budgets, and operational alerts
- [#68](https://github.com/melliott18/CogniStore/issues/68) — [M4] Add idempotent auto-repair workflows
- [#71](https://github.com/melliott18/CogniStore/issues/71) — [M4] Deploy CogniStore with Helm and Kubernetes autoscaling
- [#72](https://github.com/melliott18/CogniStore/issues/72) — [M4] Publish Terraform and production reference configurations

#### Roadmap coverage

Delivery and DX → operator runbooks, migration guides, and reference architectures.

### Hardening follow-up

### [#91 — Make POSIX path containment race-safe against symlink swaps](https://github.com/melliott18/CogniStore/issues/91)

- **Kind:** Bug follow-up
- **Status:** Open
- **Milestone:** M4 – Production platform
- **Labels:** `bug`, `roadmap`, `area:storage`, `area:security`
- **Last updated:** 2026-08-27

Parent epic: [#14](https://github.com/melliott18/CogniStore/issues/14)
Roadmap tracker: [#12](https://github.com/melliott18/CogniStore/issues/12)

#### Problem

The POSIX driver rejects static symlinks and validates below-root paths, but
later path-based operations remain vulnerable to a concurrent directory or
symlink swap. The repository does not yet define and enforce a trusted,
non-mutating tier-root boundary.

#### Acceptance criteria

- [ ] A concurrent symlink or directory swap cannot make CogniStore read, publish, or delete outside the configured tier root.
- [ ] Containment guarantees and platform limitations are documented.
- [ ] POSIX conformance and mover cleanup tests cover adversarial swaps.
- [ ] Unsupported platforms or filesystems fail closed rather than silently weakening containment.
