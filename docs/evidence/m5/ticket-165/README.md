# Ticket #165 local tooling and capacity-alert evidence

**Status: implementation components validated locally; live qualification remains
blocked. Do not close #165 from this evidence.** No staging HTTP traffic, cloud
resource, live fault or real operator notification was generated.

The [load qualification guide](../../../load_qualification.md) documents the
components, frozen proposed profile, commands, raw schemas, adapter requirements
and remaining campaign. Implementation revision:
`3f43a27` (full identity and content hashes in [validation.json](validation.json)).
The branch starts at `8d3d50bb649cea617fe030859c51b196457e4648` from `origin/main`
and uses a separate worktree, `chore/165-load-capacity-qualification`.

The application change adds confirmed broker rejection and live byte-utilization
metrics. Queue rules now warn before the selected 10,000-message/1 GiB caps and
include stop, age and missing-byte-telemetry signals. Tenant API aggregate metrics
remain disabled. Offline tooling generates the exact corpus, schedules bounded
mixed foreground work through an adapter, and calculates client/resource gates
without promoting local or incomplete evidence into production qualification.

## Retained local results

- [validation.json](validation.json) records exact revision, commands, results,
  source hashes, runtime and material gaps; [quality.json](quality.json) records
  static check outcomes and their compressed logs.
- [tests.xml.gz](tests.xml.gz) and [tests.log.gz](tests.log.gz) retain the final
  scoped regression run: **584 passed, no failures or skips**. Coverage includes
  the three new tools, queue/worker behavior, observability, SLO calculations,
  authorization, tenant isolation and legal holds. This is not the full product
  suite or a coverage campaign.
- [promtool.log.gz](promtool.log.gz) records local verification with the pinned
  Prometheus image: 55 rules valid and all synthetic rule fixtures passed.
  Exact depth/byte boundaries, recovery, replica aggregation, missing telemetry
  and rejection-counter resets are exercised. No notification receiver was used.
- [corpus summary](corpus-reduced/summary.json) and
  [manifest](corpus-reduced/manifest.jsonl.gz) retain 1,000 generated/hashed
  objects, exactly 284,262,400 logical bytes, equal tenant counts/bytes and the
  declared global joint size/MIME distribution. Payload files were not retained;
  regenerate them from the recorded public seed and source. Parser tests validate
  PDF/DOCX structure and bounded text at every size, including 16 MiB. Native
  production MIME/extraction remains untested here.
- [fixture summary](fixture-final-summary.json) and
  [raw records](fixture-final.jsonl.gz) retain a ten-second local synthetic
  transport execution: 100 offered slots with intentional errors. Its elapsed
  values are scheduler-fixture timings, not service performance. The earlier
  [fixture summary](fixture-summary.json) and [raw records](fixture.jsonl.gz)
  precede added source/seed metadata and are superseded by this final pair.
- [Unresolved template evaluation](unresolved-evaluation.json) rejects the
  shipped [placeholder campaign](unresolved-inputs/campaign.json). Exit 1 is
  expected negative-control evidence. There is no live workload report.

Ruff, mypy over all 164 application files, Bandit over the application and new
scripts, whitespace checks, package build and Twine passed. Dependency definitions
are unchanged; a new dependency audit was not performed. The historical #160
candidate's unresolved qualification/security findings remain independent.

The first broader test collection lacked `openapi_spec_validator`; its
[log](initial-collection.log.gz) and [JUnit report](initial-collection.xml.gz)
are retained. Installing the repository's declared development dependency in the
isolated validation runtime resolved collection. A preliminary run skipped the
optional local NATS Helm render because its explicit paths were unset; the final
run supplies those paths and passes the render. The original workspace Python
has the previously observed native-readline startup crash; tests use the separate
runtime recorded in validation.json. No production dependency changed for these
local environment adjustments.

## Acceptance status and blockers

| #165 acceptance criterion | Current status |
| --- | --- |
| Exact candidate/config/environment, workload, duration, metrics, thresholds and repeatable commands | Profile, generator, recorder, evaluator and runbook prepared; accepted owner/spec, qualified candidate and deployed staging identities remain pending. |
| Sustained latency/throughput/error/recovery targets, bounded backlog and measured headroom | Pending: no real staging adapter, 72-hour soak, burst recovery, doubled-capacity or movement/background campaign. |
| Object/checksum/catalog/audit consistency and isolation after load/faults | Local isolation/legal-hold regressions pass; full live before/after reconciliation and fault evidence remain pending. |
| Actionable saturation/rejection alerts reachable before hard limits | Reachable depth/byte rules and metrics tested locally; live collection, admission-stop enforcement and actual saturation drills pending. |
| Actual operator notification, runbook execution and recovery delivery with private telemetry | Pending: accepted operator/route, private oldest-age and producer-rejection collection, receipt/acknowledgement/recovery evidence required. |
| Measured scaling/cost/capacity recommendations with reruns | Pending: no infrastructure consumption, cost or live capacity measurement; no numerical scaling recommendation asserted. |

The open-loop core requires an environment-specific adapter that maintains the
bounded mutation pool, selects real matching objects and validates full responses.
Background jobs, fault controls, stop enforcement, full integrity/audit/hold
reconciliation and actual private telemetry/notifications must be integrated and
validated against the supplied staging handoff. GCS spool is excluded (allowance
zero), and SQLite scheduler/keyword index remain disabled under the selected
scope. Local SQLite used for report sorting is not service/scheduler evidence.

Hosted CI: `skipped: user instruction; known GitHub billing/spending restriction`.
The existing release-qualification gap remains; no hosted workflows were dispatched,
rerun or polled. Verify all retained files from this directory with
`shasum -a 256 -c SHA256SUMS`.
