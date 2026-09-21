# M5 system safety and security audit

[Ticket #162](https://github.com/melliott18/CogniStore/issues/162) audits the
selected [m5-pilot-v1 deployment](production_pilot.md). The versioned
[audit matrix](../release/audit-matrix.json), revision 1, is the executable
scope inventory. Every row names owned code, local adversarial tests,
PostgreSQL/service tests where available, and required live staging checks.
An empty service-test list means that row needs the stated staging procedure;
it does not mean the production boundary has passed.

The [retained audit record](evidence/m5/ticket-162/README.md) contains findings,
baseline reproductions, test outcomes and the current conclusion. Local
development evidence cannot grant candidate, staging or pilot signoff.

## Scope and responsibility

| Matrix ID | Code review and adversarial focus | Required staging evidence |
| --- | --- | --- |
| `authentication` | JWT canonicalization, algorithms, issuer/audience, key fetching, RBAC before body/existence/effects | Actual OIDC and per-user browser proxy, expired/revoked sessions, forged headers, CSRF and upstream TLS |
| `tenant-isolation` | Trusted membership, schema/prefix rebinding, reserved aliases, scoped search/citations/downloads | Both named tenants plus denied default/unmapped identities; real filesystem comparison semantics |
| `broker-worker` | Trusted identity assertions, current grants on execution/retry/redrive, durable settlement | Real publisher/consumer ACLs, revoked credentials, denied network paths, TLS reconnect and disabled schedules |
| `legal-holds` | Hold guards and immutable placement/release evidence across destructive work | Hold/PUT/DELETE/move/cleanup overlap across API and worker processes |
| `audit-integrity` | Intent/outcome ordering, retention tombstones, export verification and checkpoint tampering | Independent checkpoint custody, fail-closed disclosure/settlement, coherent restore |
| `secrets-redaction` | Provider outage/rotation, bounded caching, credential redaction and transport/encryption requirements | Actual secret/CA/KMS rotation, platform encryption and old-key restore |
| `untrusted-payloads` | API size limits, isolated parser failures/output bounds, explicit PII behavior | Resource measurements, corrupt/encrypted inputs, synthetic-only admission |
| `put-delete` | Both mutation orders, catalog publication failures, cross-process exclusion and generation fences | Actual POSIX/S3 bytes/catalog reconciliation and interrupted API processes |
| `move` | Durable checkpoints, retries, conditional deletion, stale source/destination generations | Hot/warm moves overlapping foreground work and killed/restarted workers |
| `scan` | Revalidation through publication, active/completed move coordination and scan/API exclusion | Scan/PUT/DELETE/move overlaps against selected PostgreSQL and both backends |
| `repair` | Tenant/source-bound reports, reviewed plans, changed generations, leases and holds | Concurrent repair, foreground operations and interruption/recovery |
| `cleanup` | Quarantine/references/holds, conditional reclaim and terminal audit recovery | Concurrent reclaim and changing ownership/references across processes |
| `production-config` | API/worker/CLI privilege, state ownership, fixed topology and metadata-only providers | Exact image/config/service/filesystem IDs, real network/telemetry/backup controls |
| `artifacts` | Reproducible source/dependencies/native inventory/migrations and security findings | Exact installed artifact and registry identity, current scans, reviewed dispositions and durable retention |

Entrypoints and state ownership are inventoried in the
[artifact/configuration review](evidence/m5/ticket-162/artifact-config-review.md#entrypoints-authority-and-state-ownership).
The [security review](evidence/m5/ticket-162/security-review.md) and
[mutation review](evidence/m5/ticket-162/mutation-review.md) record actual code
review, triggers, expected/observed outcomes and limitations. The separate
[audit event matrix](audit_coverage.md) describes event-producer coverage;
it is not a full-system audit signoff.

## Reproduce local checks

Install development dependencies as in [CONTRIBUTING](../CONTRIBUTING.md), then
run from the checkout using its Python environment:

```bash
python scripts/system_audit.py check-matrix
python scripts/system_audit.py run --profile local --output /tmp/cognistore-audit-local
python scripts/system_audit.py verify --output /tmp/cognistore-audit-local
```

Each output directory must be new and outside the checkout. The runner executes
the union of matrix test files once, using pytest's importlib mode; retains
JUnit and raw output; and records exact command, UTC interval, runtime, source
commit and input hashes, matrix revision and counts. Failures, collection errors,
unrelated cases, empty/missing test files and skips cannot produce `passed`.
A change during execution invalidates the run. The runner checks every
unignored/tracked source input except `docs/evidence/`, so preserve local scratch
files outside the checkout too. Evidence-only commits do not invalidate matching
source bytes; the original tested commit remains recorded.

`verify` checks current source/config/test bytes, matrix scope, retained outputs
and their counts. It can verify an accurately recorded incomplete/failed run;
verification itself is **not a test pass**. Checksums detect changes relative to
the record, not malicious rewriting of the whole record or reviewer identity.
Retain the accepted record/checksums independently with reviewer attribution.

The `postgres` profile uses `COGNISTORE_TEST_POSTGRES_DSN` through the existing
[isolated test-database fixture](../tests/integration/conftest.py). Configure
only a disposable local/test PostgreSQL service whose account may create/drop
test databases. It exercises both catalog implementations and cross-process
coordination; it does not establish production TLS, physical storage or network
enforcement. The `services` profile selects the opt-in NATS/MinIO integration
tests; follow their documented isolated service setup. These profiles do not
provision or authorize external systems, and missing configuration yields
incomplete evidence. MinIO is supplemental to the selected **native AWS S3**
qualification, never a substitute.

The local profile includes Helm tests. Configure `HELM`/`PATH` and the pinned
`COGNISTORE_NATS_CHART` where their fixtures require them. Native case-insensitive
tests may skip on a case-sensitive volume; retain real applicable filesystem
evidence from a separate run instead of relabeling the skip. Inherited test
fixtures may opt into services when configured; run with only the intended
disposable service settings. Do not capture environment dumps or real tokens.

Run Ruff, mypy, the full coverage suite, Bandit, dependency audit and artifact
checks as applicable under CONTRIBUTING. Security dependency lookup may use
public package/advisory indexes; no customer or production data is needed.
Hosted CI is **skipped: user instruction; known GitHub billing/spending
restriction**. Never dispatch, rerun or poll it under this campaign.

Review raw reports for secrets and irrelevant local paths before copying to
`docs/evidence/m5/ticket-162/` or restricted durable storage. Preserve failure
reports and baseline negative controls alongside successful retests. Record
sanitization/compression and hash the retained bytes. Local ignored paths alone
do not satisfy retention.

## Finding and signoff contract

Every demonstrated new defect needs a separate remediation ticket linked to
#162, severity, precise trigger, sanitized reproduction, proposed/accepted
owner, release-blocking disposition and retest evidence. Filing a ticket does
not resolve a blocker. Inherited findings retain their original identity and
evidence; qualification gaps are distinguished from demonstrated code defects.

Final signoff requires an explicit reviewer decision bound to all of:

- Accepted #159 specification revision/commit and responsible owners.
- Exact #160 source, registry image digest, wheel/dependency/native identities,
  migration versions and provider composition, with resolved blocking findings.
- #161 environment, rendered/input configuration hashes, OS/filesystem/mount,
  PostgreSQL/S3/JetStream versions and actual trust/encryption/network controls.
- Every matrix row's review, applicable adversarial and multi-process tests,
  staged checks, actual outcomes, sanitized durable artifacts and checksums.
- Both historical defect reproductions and candidate retests; all newly found
  release blockers remediated/retested; unexplained divergence, hold bypass or
  cross-tenant disclosure count **zero**.
- Residual operating limits, review timestamp, reviewer identity and accepted
  disposition. Code/config/dependency/environment changes name impacted rows
  and invalidate their prior signoff until scoped re-audit is complete.

The local runner deliberately has no signoff command. The
[release change-control rules](release_candidate.md#change-control) apply:
changed code means a new candidate, not an evidence edit to the old one. The
selected Linux/ext4/native S3 production combination, service TLS and actual
replica topology must be exercised before concluding the system audit.
