# Independent review of the ticket #162 scanner fix

**Review result: no actionable defect found in the existing changes.** This
automated peer review does not establish production qualification or human
acceptance. Reviewed on 2026-09-21 UTC against base
`8d3d50bb649cea617fe030859c51b196457e4648`; the reviewed working-tree file
digests and exact validation command are in
[scanner-review-validation.json](scanner-review-validation.json).

Reviewed changes:

- [`cognistore/core/scanner.py`](../../../../cognistore/core/scanner.py) reserves
  each non-dry-run object before reading storage and retains the reservation
  through publication. A busy or held key produces no observation this pass.
- [`cognistore/db/catalog.py`](../../../../cognistore/db/catalog.py) routes scan
  publication through the existing `_object_publication` context.
- [`test_api_scan_coordination.py`](../../../../tests/integration/test_api_scan_coordination.py)
  covers stale observations around API PUT/DELETE, separate-process exclusion,
  and busy-key retry. Its PostgreSQL cases remain unexecuted in this record.

The lock order agrees with the API: the lifecycle/hold guard precedes the
nonblocking per-object reservation. Existing nested hold checks remain
reentrant, so a waiting hold writer does not prevent the current observer from
finishing its publication. Context-manager unwinding releases the reservation
on exceptions. Audit start/terminal writes remain outside the object span.

Tenant isolation continues to use the existing tenant-scoped catalog and
storage handles. SQL lock identity includes the tenant/schema, and the outer
lifecycle guard checks the active tenant. The change introduces no new tenant
selection or identity authority.

For PostgreSQL, `_object_publication` uses the connection retaining the object
reservation, rejects an invalidated connection, and owns a short publication
transaction. Storage reads do not hold that transaction. Rollback/error paths
leave ownership cleanup to the existing contexts. This source assessment is
supported by existing architecture, but real PostgreSQL behavior was not
validated in this review.

Dry runs still use null contexts and return before catalog fence capture or
publication. Holds remain checked before non-dry-run storage observation. The
existing move fence still rejects observations invalidated by movement. The
documented busy-key behavior can defer observation until a later scan; a scan
result is not proof that every listed object was observed.

Independent local validation completed before the parent reported the blocked
agent step: **112 passed, 12 skipped in 17.61 seconds**, using CPython 3.12.2 on
Darwin arm64. The six modules cover scanner/API coordination, scanner/move
coordination, object fences, content manifests, orphan cleanup and legal-hold
catalog behavior. All 12 skips were PostgreSQL cases because
`COGNISTORE_TEST_POSTGRES_DSN` was not configured.

Retained evidence: [test log](scanner-independent-review.log.gz),
[command, counts and checksums](scanner-review-validation.json).

After the parent reported the mutation agent's safeguard rejection, no retry
of that step or additional fault/adversarial execution was performed. This
record only retains the already-completed review and ordinary local results.
Hosted CI: `skipped: user instruction; known GitHub billing/spending restriction`.
