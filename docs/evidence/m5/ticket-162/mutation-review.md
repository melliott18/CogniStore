# Ticket #162 mutation review and remediation

Source reviewed: `8d3d50bb649cea617fe030859c51b196457e4648`.
Fix: `30b175d` ([#175](https://github.com/melliott18/CogniStore/issues/175)).
Permitted environment: owned CogniStore, synthetic fixture identities and bytes,
temporary local POSIX storage, isolated test catalogs and a disposable
PostgreSQL16 container; no deployed API, external identity provider or cloud
account was exercised.

## Known defects: complete baseline and native filesystem

The [retained reproduction](../../../../scripts/audit/reproduce_known_mutations.py)
loaded the complete archived source tree at
`2cce6ff4d43fd287ad008197f1e17f168c3580c4` in a fresh interpreter, then the
current source tree in another process. It did not replace individual candidate
modules or emulate filename lookup. Reports record macOS 14.8.7 ARM64,
CPython 3.12.2, SQLite 3.45.1 and an actual temporary volume where the uppercase
probe resolves to the lowercase file.

| Case | [Affected baseline](known-mutations-baseline.json) | [Fixed candidate source](known-mutations-candidate.json) |
| --- | --- | --- |
| #156 reserved tenant alias / hold | Owner overwrite denied (409), default-tenant alias PUT accepted (201), victim bytes replaced; alias DELETE accepted (204), victim GET became 404 | Alias PUT rejected (422), original bytes retained; alias DELETE returned404, victim GET remained200. Original victim hold stayed effective. |
| #157 old DELETE / replacement PUT | Replacement PUT returned201 while old DELETE paused; DELETE then204; backend retained replacement bytes but catalog was missing and GET404 | Concurrent PUT rejected with retryable409; DELETE204; retry PUT201 and GET200 returned replacement bytes. |

Both reports' `assertions: passed` means the requested vulnerable/fixed
expectation was observed, not that the vulnerable baseline is safe. Native
HFS+/Linux-ext4 and live-cloud behavior are not inferred from macOS. The
existing #156 matrix covers additional Unicode aliases deterministically.

Reproduction uses
`/tmp/cognistore-160-smoke-venv/bin/python scripts/audit/reproduce_known_mutations.py`
with `--source-root /tmp/cognistore-162-affected-baseline --expect vulnerable`
or `--source-root . --expect fixed`, redirecting stdout to the respective JSON report.
The source archive was obtained with `git archive` of the affected SHA above.

## New finding: stale scan publication (#175)

**Severity P1, release blocking until remediated/retested. Proposed owner:
Mitchell Elliott.** Pause the scanner after its final storage-generation
validation and before `upsert_scan_observation`. A successful concurrent API
replacement PUT or DELETE can then be superseded by the old scan publication.
Replacement bytes remain with obsolete size/hash/content references, or a
catalog record reappears for absent bytes. The existing move-specific scan
fence does not exclude these API mutations.

The authenticated [regression tests](../../../../tests/integration/test_api_scan_coordination.py)
failed in **all six pre-fix cases** (PUT and DELETE across memory, SQLite and
PostgreSQL). These expected failures are retained in
[scan-before.xml.gz](scan-before.xml.gz) and [scan-before.log.gz](scan-before.log.gz).

The fix holds the existing per-key object mutation fence from observation
through publication, inside the established lifecycle/hold guard. Contended
keys are deferred to a later scan; dry-run keeps null contexts. PostgreSQL
`upsert_scan_observation` uses `_object_publication` so publication remains on
the session owning the mutation fence. Scope, holds and move fingerprints
still apply. API contention returns its existing retryable conflict; retry
after scan completion succeeds.

Retained from completed or already-started local runs; the blocked agent
subtask was not retried:

- [New regressions](scan-after.xml.gz): **14 passed**, including separate
  catalog handles, separate processes, busy-key deferral and disposable
  PostgreSQL session-loss behavior. Local PostgreSQL was
  `pgvector/pgvector:0.8.6-pg16-bookworm`, a unique loopback-only port and tmpfs
  data; test databases were uniquely created/dropped by the existing fixture.
- [Broader fixed-code regressions](mutation-after.xml.gz): **645 passed**,
  spanning PUT/DELETE, native tenant aliases, object fences, scanner/move
  coordination, repair, orphan cleanup, legal holds, content scanning and
  movement. This supersedes the earlier 527-pass narrower run for those
  selected cases; it is not a full product-suite claim.
- [Independent review](scanner-fix-review.md): no actionable source finding;
  112 passes/12 PostgreSQL skips in the reviewer's separately scoped run.

Exact test outcomes and the full broader command are retained with
[mutation validation](mutation-validation.json). The tested source-file
hashes bind the uncommitted implementation at execution to the committed fix.
Retained logs replace local user/host paths with placeholders; no credential,
customer data or production identifier is retained.

## Mutation coverage and unresolved limits

The [matrix](../../../../release/audit-matrix.json) links each family to its
reviewed implementation and tests: API PUT/DELETE fences; mover checkpoints,
generation checks and publication ordering; scan observation/references;
source-bound consistency repair; orphan quarantine/conditional cleanup; and
hold/audit protection shared across those paths. Existing tests include
storage/catalog failures, interrupted durable moves, stale generations,
active move fencing, reclaimed/re-referenced content and hold conflicts.

This is bounded component coverage, not every pairwise operation interleaving
against every backend. The selected native S3 backend, ext4 mount and actual
API/worker processes remain required staging checks. No fresh production
candidate includes this fix yet; the old candidate's scan-related evidence is
invalidated. There is no claim of zero unexplained divergence across an
unexecuted staging campaign. No audit signoff is granted by this remediation.
