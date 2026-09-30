# Ticket #165 campaign automation

This follow-up supplies a real HTTPS service adapter, campaign orchestration,
private telemetry and stop controls, bounded fault/recovery hooks, a bound
evidence importer and supplemental acceptance evaluation. It follows the
[initial component delivery](../ticket-165/README.md). See the
[operator guide](../../../load_qualification.md) and
[evidence contract](../../../load_evidence_schema.md).

**Qualification remains blocked.** Local API tests, synthetic telemetry and
acceptance arithmetic are development evidence. They do not establish the
required 72-hour staging soak, actual operator delivery, measured capacity/cost,
or owner acceptance. #159/#161 still need an accepted deployment handoff. This
record does not close #165 or authorize pilot entry.

## Implemented behavior

- Authenticated HTTPS traffic verifies complete object bytes/checksums, catalog
  semantics, Ask and preview responses. Requests use rotating token files,
  trusted CAs, bounded reads/deadlines and no redirects or ambient proxies.
- Tenant/size/hotspot pools preserve the declared initial corpus. Full-size
  churn pools cover the deterministic request sequence with concurrency
  headroom; uncertain mutations stop admissions and retain durable intent.
- Campaign stages monitor resources during setup, load, background-job drain,
  verification, movement and cleanup. Capacity load extends while its placement
  round trip is unfinished. Failed windows cannot become shorter passing runs.
- Worker audit records preserve an actual broker publication timestamp when
  available. Job/scan export joins worker start/terminal events and keeps gaps
  explicit. Poll response time is never substituted for publication time.
- Fault subprocesses have explicit scope, bounds and recovery. Stops remain
  latched; intentional saturation can end the campaign and requires operator
  intervention. Prior measured evidence is retained before the final drills.
- Evaluation binds exact artifacts and rejects incomplete attempts, stale or
  missing telemetry, unverified reconciliation, unmatched alert artifacts and
  synthetic qualification. Passing arithmetic still requires independent review.

## Validation and limits

`validation.json` records tested revisions, source hashes, commands, runtime,
results and retained log hashes. Checks use isolated local storage, synthetic
tenant identities and fixture credentials. Native libmagic was installed only
in the development environment to exercise real PDF/DOCX scanner handlers.

Deployment-specific inputs remain necessary: private metric expressions,
directional backlog observations, backend-call timing/retries, scoped fault
controls, full backend integrity/hold/isolation evidence, actual alert/ack/runbook
artifacts and measured physical consumption/cost. The paced directional driver
does not itself prove a continuous platform backlog. Unknown facts must remain
missing; the importer does not create successful observations.

The adapter cannot discover backend objects invisible to the tenant catalog.
Public scan metadata excludes private extraction provenance. Those checks require
the accepted deployment's private observation sources. The operator must reconcile
uncertain jobs/writes before any new run; automatic resumption is intentionally
absent. A stopped run remains nonqualifying even if its recovery hook succeeds.

Hosted CI: `skipped: user instruction; known GitHub billing/spending restriction`.
No hosted workflow was dispatched, rerun or polled.
