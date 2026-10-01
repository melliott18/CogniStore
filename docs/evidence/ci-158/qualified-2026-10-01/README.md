# #158 hosted CI qualified — 2026-10-01

**Qualified main: `b7e2b3f6fb3f3fefe4b0e9d2f155204ee2bb71b6`. All 15 required checks passed in fresh hosted runs.**
The user explicitly authorized hosted CI resumption and merging validated fixes on 2026-10-01.
[PR #187](https://github.com/melliott18/CogniStore/pull/187) merged the repairs after all 15 PR checks passed.
Main then ran all three workflows independently at the exact merged revision:

| Run | Result | Event | Attempt |
| --- | --- | --- | --- |
| [Terraform reference — 36913846163](https://github.com/melliott18/CogniStore/actions/runs/36913846163) | success | push | 1 |
| [CI — 36913846345](https://github.com/melliott18/CogniStore/actions/runs/36913846345) | success | push | 1 |
| [Kubernetes acceptance — 36913846462](https://github.com/melliott18/CogniStore/actions/runs/36913846462) | success | push | 1 |

This archive is a documentation-only follow-up on a separate branch so it does not
change or relabel the qualified main revision. Historical failed, skipped and cancelled
attempts remain historical outcomes. Final frozen-candidate qualification remains in
[#160](https://github.com/melliott18/CogniStore/issues/160).

## Executed checks and scope

- Python 3.10–3.14 each executed unit/conformance and isolated service integrations with
  native libmagic. Combined statement/branch coverage is 88.63–88.64%,
  above the unchanged 80% gate. Exact per-suite counts, coverage and every skip/xfail reason
  are in [main-qualification-review.json](main-qualification-review.json). Overlapping suites
  must not be added together.
- Default collection, Ruff/mypy/OpenAPI, Prometheus alerts, package installation/operator
  drill, dependency audit/Bandit, full-history Gitleaks, non-root container checks, Compose
  integration, shutdown/restart and reduced movement passed.
- PostgreSQL, NATS, MinIO and Azurite are isolated fixtures. GCS uses an HTTP emulator;
  its known media-GET generation-precondition failure remains a strict expected failure.
  Live Azure/GCS credentials, native case-insensitive filesystem cases, optional httptools,
  unsupported range writes and optional local NATS chart coverage remain explicitly skipped
  where unavailable. The service phases cover services intentionally absent in default collection.
- Disposable kind acceptance passed 23 chart contracts, installation, migration, upgrade,
  rollback, persistence after dependency restarts, worker/API autoscaling and security checks.
  The planned backend recovery took 0.043 seconds across 1
  recorded read attempts, within its 120-second bound; payload/catalog/job/audit invariants then passed.
- Terraform passed six mocked plan cases and production Helm handoff rendering. No live
  infrastructure was provisioned. Reduced movement is not the million-object campaign;
  none of this establishes live-staging, production-cloud, full-scale, soak or pilot qualification.

## Fixes and failure visibility

The [remediation record](../remediation/README.md) retains local commands/results,
source review, exact secret-scan dispositions and regression evidence. Repairs address
public OTLP session observation, schema-backed typing, verified-source MinIO fixtures,
exact historical Gitleaks fingerprints, reliable required-check triggers, a matrix time
budget sufficient for existing suites, a deterministic capacity regression, and bounded
planned-backend recovery with negative tests. No test assertion or 80% coverage gate was removed.

The original main campaign at `fae9f64811c477703ac25e04ab938be038677d16` failed;
its durable evidence remains in [PR #186](https://github.com/melliott18/CogniStore/pull/186).
Subsequent PR attempts are retained separately:

| Archive | Revision | Result |
| --- | --- | --- |
| first-attempt.tar.gz | `d7873e90bd85e89506a05406ee36c9e362651f4a` | CI failed: stale fixture assertion and matrix timeout; Kubernetes/Terraform passed |
| second-attempt.tar.gz | `1088c73144a65a3c15304734e1ecb98aec1973ad` | CI failed: Python 3.11 capacity-test scheduling assumption; other 14 jobs passed |
| third-attempt.tar.gz | `7966b1f14af917f8eb0a1281d986364d41dd828e` | MCR fetch timeout and Kubernetes post-restart read timeout; unfinished CI superseded/cancelled; Terraform passed |
| pr-qualified.tar.gz | `5c8eae0c4a3d1c3b277a6a4bdefb7b390c169251` | All 15 PR checks passed |
| main-qualified.tar.gz | `b7e2b3f6fb3f3fefe4b0e9d2f155204ee2bb71b6` | All 15 fresh main checks passed |

The PR checkout's synthetic merge commit and proposed head have identical trees;
[pr-final-tree-identity.json](pr-final-tree-identity.json) records both identities.
Runtime reports identify actual checkouts, Python/OS/dependencies and configuration;
service reports/build logs retain image identities and checksummed source inputs.

## Enforcement, ownership and retention

[final-protection-readback.json](final-protection-readback.json) independently confirms
all 15 exact contexts bound to GitHub Actions app 15368, strict/up-to-date checks,
PR requirements, administrator enforcement, and disabled force-push/deletion.
The account/repository owner @melliott18 owns future outages and check enforcement;
the [CI runbook](../../../ci_runbook.md) describes triage, failure visibility, evidence
retention and frozen-candidate requalification. Failures in the retained attempts
remain visible in job conclusions, step logs and diagnostic reports.

Archives retain complete available reports, individual job logs, run/job metadata,
artifact inventories and internal SHA-256 manifests. Each archive member was read
back and verified. Raw reports were secret-scanned before archiving; the only reported
values were three provenance SHA-256 digests verified against their source Git objects,
recorded inside each archive. Separately expanded job logs contained only the known
loopback Azurite development fixture credential; each occurrence was checked against
the exact workflow value and is recorded in `expanded-log-review.json`. No
secret-scanner rules were disabled.

Verify this directory with `shasum -a 256 -c SHA256SUMS`. Each `.tar.gz` additionally
contains its own `SHA256SUMS`; the included collector, reviewer and packer show the
read-only evidence process. Local ignored paths and expiring Actions links are not
the sole retained evidence.
