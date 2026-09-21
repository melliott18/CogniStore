# Gated pilot operations

Ticket [#166](https://github.com/melliott18/CogniStore/issues/166) turns the
[selected pilot specification](production_pilot.md) into an operator procedure
and review record. **Current status: no-go; pilot not started.** This is the
engineering assessment of the retained prerequisites, not an approval or a
named owner's signed decision. No enrollment, pilot data admission or actual
pilot outcome is recorded in the retained evidence.

The [M5 evidence index](evidence/m5/README.md) records pending owner acceptance,
an unqualified candidate, an unresolved deployment handoff and incomplete live
security, acceptance, recovery and load/alert qualification. These block entry
even where preparation tickets or local checks are complete. Hosted CI is
`skipped: user instruction; known GitHub billing/spending restriction`.
Do not dispatch, rerun or poll it without an explicit user request. The
specification's hosted entry gate remains unmet; suspension is not a waiver.

This procedure covers only the two synthetic tenants and agreed internal
cohort. It does not authorize infrastructure spending, deployment, real customer
data, a public launch or an expansion. The specification controls if a
summary below differs. Changing a gate requires a reviewed specification
revision before collecting replacement evidence.

## Prepare and check a campaign record

The [campaign template](../release/pilot/campaign.example.json) and
[offline checker](../scripts/pilot_review.py) structure the handoff. From the
repository root, create a fresh record and inspect the entry review:

```bash
python scripts/pilot_review.py init --output test-results/pilot-166/campaign.json
python scripts/pilot_review.py check --campaign test-results/pilot-166/campaign.json --stage entry --output test-results/pilot-166/entry-review.json
```

The initialized record is unobserved and must remain incomplete. Replace
placeholders only with actual recorded identities, named reviews and evidence;
never populate a passing example to bypass a gate. Retain SHA-256 references
to the underlying artifacts, including restricted roster/contact manifests.
After a real run, check its exit record with `--stage exit` and a fresh output
path. This checks readiness against the predefined success criteria, not the
validity of a legitimate failed or stopped campaign. A correctly documented
stop or unmet gate remains `incomplete`; preserve the truthful decision and
observations. Do not fabricate success to obtain a zero exit status. Preserve
and publish sanitized records with the evidence bundle; `test-results` is
working storage, not durable acceptance evidence.

The checker verifies record consistency, referenced artifact hashes and
specified duration/sample/task arithmetic. Its `incomplete` or
`complete-for-review` result describes the submitted record only. Both
`pilot_entry_authorized` and `expansion_authorized` remain false. It cannot
independently verify a reviewer's signature, data permissions, actual deployed
controls, raw SLO claims, real alert delivery or an operational decision.
A named reviewer must examine the raw measurements, resource/incident/change/
stop reports and qualification evidence. The tool neither enforces live limits
nor starts, approves or expands a pilot.

### Campaign record fields

Use the template's exact keys. All timestamps use UTC ISO 8601 with a trailing
`Z`, for example `2026-10-01T08:00:00Z`; dates use `YYYY-MM-DD`. Every `evidence`
array contains unique artifact IDs, not paths or URLs. Register their local
files in the top-level `artifacts` object:

```json
{
  "artifact-id": {
    "path": "evidence/review.json",
    "sha256": "<64 lowercase hexadecimal digits from the actual file>"
  }
}
```

This is a shape example with an intentionally invalid placeholder digest.
Paths must be relative to the campaign file's directory and resolve inside
that directory, without `..`, an absolute path or a reference to the campaign
itself. `identity_artifacts` maps each identity field ending in `_sha256` to
its artifact ID; the actual file digest must equal that identity value. The
checker hashes these local files and does not fetch remote evidence.

Use pseudonymous tester IDs only in `cohort.testers`, tasks and feedback. Keep
the actual roster and personal support contacts restricted. A local sanitized,
attested reference manifest can supply the exact nonsecret restricted URI,
remote artifact digest, scope, reviewer and verification time without copying
sensitive raw files into public evidence. The named reviewer must open the
restricted artifact, verify its digest, permissions and contents, and attest
the result. Hashing the local reference manifest proves only that manifest's
integrity. Do not put credentials, signed download tokens or personal contact
values in the URI or manifest.

The template leaves these arrays empty; add records only from actual plans or
observations. `identity` below means an exact copy of the top-level identity
object, not an identity label or a subset. `evidence` means a nonempty array of
registered artifact IDs.

| Object or array item | Required fields and meaning |
| --- | --- |
| Shared review under `qualifications`, `readiness`, `slo_reviews` or `exit_gates` | `result`, `reviewer`, `at`, `identity`, `evidence`. Initial `result` is `pending`; success readiness requires `passed`. Retain a failed review as `failed` with its evidence. The reviewer is a named accountable person, never an invented approval |
| `plan.windows[]` | `start_at`, `end_at`. Each is an eight-hour staffed window, within `plan.start_at`/`end_at`; at least ten nonoverlapping windows on distinct UTC start dates are required. `plan.declared_at` precedes entry and start; retain staffing and schedule approval in `plan.evidence` |
| `daily[]` | `date`, `reviewer`, `at`, `identity`, `evidence`, `checks`, `operator_minutes`, `objects`, `logical_bytes`, `backup_age_hours`. `at` must fall on the recorded UTC date. Add exactly one record per date touched by the campaign, including unstaffed dates. Counts are nonnegative integers; effort and backup age are nonnegative numbers |
| `tasks[]` | Unique `id`, pseudonymous `tester` from `cohort.testers`, `tenant`, `workflow`, `at`, `identity`, `evidence`, boolean `completed`, boolean `maintainer_intervention`, nonnegative numeric `completion_seconds`. `tenant` is `pilot-a` or `pilot-b`; `at` falls in a staffed window. `workflow` is `upload-retrieve`, `discovery-download`, `policy-job` or `administration` |
| `feedback[]` | Pseudonymous `tester`, `at`, nonempty text `usefulness`, nonempty text `failure_clarity`, `evidence`. Feedback is dated between campaign start and exit decision; three distinct enrolled testers are required |
| `exit.followups[]` | `url` for a CogniStore GitHub issue, `priority` (`P0`–`P3`), `owner`, `finding`. Empty is permitted when the retained `exit.followup_review` establishes that no new ticket is needed; do not invent findings to populate it |

`daily.checks` contains exactly these boolean keys: `integrity`, `placement`,
`holds`, `audit`, `growth_and_caps`, `backups`, `alerts_and_incidents`, and
`staffing_and_stop_history`. Set a check true only after reviewing the raw
record; false or missing evidence must remain visible and blocks success
readiness. The daily scalar fields summarize observations, not the full
resource, SLO, staffing, incident or stop history required below.

The task scalars capture completion, intervention and elapsed time; put detailed
role/interface, expected and actual outcomes, start/end times, abandoned
attempts and explanation of intervention in the referenced raw task record.
The checker does not inspect that narrative or independently reproduce an
outcome. `exit.followup_review`, `exit.roadmap_evidence`,
`exit.operator_docs_evidence` and `exit.ticket_mirror_evidence` are also artifact
ID arrays. The linked tickets/raw review carry impact, acceptance criteria,
prioritization rationale and dependencies beyond the small followup summary.

## Review the entry bundle

The accepted owner reviews the durable bundle before enrollment or data
admission. Start with a fresh campaign record; preserve failed/superseded
records. Bind the review to the exact specification revision and commit,
candidate source and image digest, dependency manifest, rendered Helm/drivers/
policy/provider configuration hashes, environment identity and corpus seed/
manifest. An environment or artifact mismatch is a blocker, including two
reports with the same version string but different source or image identities.

| Required review | Evidence to inspect | Entry rule |
| --- | --- | --- |
| Scope and ownership (#159) | Accepted specification commit, named deployment/security/workload owners, UTC review and resolved objections | A proposed name or automated reviewer cannot accept duties for that person |
| Release blockers and runtime matrix (#156–#158) | Fix/regression identities, candidate-applicable hosted runtime evidence | No unresolved release blocker; local checks do not satisfy the unavailable hosted gate |
| Immutable candidate (#160) | [Release bundle](release_candidate.md), exact-artifact checks, dependency/image findings and dispositions | Qualified image/configuration identities; no unreviewed substitutions |
| Environment (#161) | [Staging handoff](staging.md), isolated pilot resources, trusted identity/proxy, private telemetry, limits and active attestations | Verify actual controls and pilot identity; resolve any staging-to-pilot differences before entry |
| Safety and consistency (#162) | [Signed audit](evidence/m5/ticket-162/README.md), finding dispositions and regression evidence | Passing review bound to the selected artifact and environment |
| User acceptance (#163) | [Manual acceptance](manual_acceptance.md), both tenants/roles/interfaces and real browser records | All required cases pass; skip, rehearsal or rendered HTML alone is insufficient |
| Recovery (#164) | [Recovery qualification](recovery_qualification.md), coherent restore sets, repeated faults/rotation/rollback and per-trial RPO/RTO | Passing exact-scope trials and a tested compatible rollback target |
| Load and alerts (#165) | [Load qualification](load_qualification.md), raw client/telemetry data, coverage, capacity and real notification/acknowledgement | Passing live gates, including delivery to the accepted operator |
| Operational readiness (#166) | Private roster/contact references, staffing, data bounds, fresh recovery sets, current attestations, stop controls and support route | Controls active and independently checked before first use |

Record each gate as pass, fail or incomplete, with reviewer, UTC timestamp,
evidence reference/digest, scope and unresolved findings. Read the underlying
reports and check artifact accessibility and retention; a link, ticket closure,
self-reported boolean or checker exit status is insufficient. All required
gates must pass before the owner may record **go**. Otherwise record **no-go**
and the blocking evidence with a responsible owner and next action. Never
backdate an approval to legitimize activity that already occurred.

A named entry decision identifies the accountable person, their explicit
acceptance, UTC decision time, exact campaign binding, gate bundle digest,
conditions and approval record. Mitchell Elliott is currently a proposed owner;
automation must not insert acceptance or sign his name. Store personal contact
information and the actual roster in restricted storage; retain stable references
and digests in public evidence. Owner acceptance, qualification signoffs and
entry approval are separate decisions.

## Prepare the bounded cohort

Before a go decision, demonstrate the following controls and record who checked
each one:

- Prepare a privately accepted roster of at most five internal testers for
  `pilot-a` and `pilot-b`, with permitted data scope and individually bound
  identities. Activate enrollment only after the go decision. At least three
  testers are needed for the exit feedback gate.
  Exercise reader/writer/operator/auditor/admin duties through separate test
  identities as needed. Default or unmapped identities remain denied.
- Admit synthetic data only, with no important sole copy, real PII, customer
  content or confidential material. Start at 50,000 objects / about 13.237 GiB;
  plan no more than 2% net object/byte growth per day. Stop new admission at
  either 100,000 live objects or 30 GiB logical payload. Enforce the 16 MiB
  per-object limit across every interface. The application does not enforce
  this campaign's aggregate bounds; the controlled harness and operator must.
- Retain the selected metadata-only Ask/provider exclusions, single-node POSIX
  placement, fixed replicas, disabled scheduler/HPA/KEDA and exact tenant
  policy. Keep ordinary CLI writers outside named pilot tenant namespaces;
  use the qualified tenant-authenticated interfaces for pilot work.
- Declare fourteen consecutive calendar dates and staffed UTC windows before
  start: at least ten eight-hour shifts, eighty active hours in total. Record
  the accepted on-call operator for each shift and an accepted handoff before
  replacement. Outside staffed windows stop admissions and drain/pause mutable
  work. If the accepted operator is unavailable, stop admission and mutations.
- Test the restricted support/escalation route, actual alert receipt within
  five minutes, acknowledgement within fifteen minutes and recovery notification
  within five minutes of clearing. Link the runbooks and access-controlled
  contact manifest; a synthetic rule test does not prove delivery.
- Verify a coherently recoverable set at least every twenty-four hours and
  before an upgrade, with seven daily sets retained outside application deletion
  credentials. Record backup age, restore proof, retained keys and current
  catalog/queue/runtime/storage attestations. Budget the specification's hot,
  S3-version and independent backup capacity; do not delete recovery points to
  make a cap appear healthy.
- Prove admission stop, external worker fencing and the tested rollback or
  coherent restore path are usable by the accepted operator. Verify telemetry
  coverage from the client, API ingress, workers and dependencies without
  exposing tenant `/metrics` or weakening identity/TLS controls.

## Run and retain daily observations

Start only after the recorded go decision and compare the deployed identities
to the approved binding. Keep the nominal synthetic workload throughout every
predeclared active window: 10 offered requests/s, no more than 16 outstanding
requests/four uploads/two worker jobs, the exact operation/size mix and hot spots,
bounded scans and policy passes from the specification. Human tasks run alongside
this load. Missing arrival slots and maintenance remain in the denominator.
Pauses and failures remain in the declared windows; stopping never erases a day.

Collect at least 2,880,000 offered foreground attempts, 10,000 per operation
class, 1,000 per read/write size bin and 1,000 scan attempts. Apply the
[ongoing pilot formulas](production_pilot.md#numerical-qualification-gates)
for client availability/latency, completed movement/scans, resources, queue and
evidence coverage. Keep retries, timeouts and rejected/unsent eligible attempts;
report each operation/size bin. Do not pool different candidate revisions or
substitute the distinct shipped 30-day API SLO for these client measurements.
The dedicated burst/capacity/movement-backlog/recovery campaigns are entry
qualification and need repeating only when affected by a material change.

Create a daily record for all fourteen dates, including unstaffed dates and
stopped days. Every record must retain:

| Observation | Required contents |
| --- | --- |
| Identity and staffing | Date, candidate/configuration/environment binding, actual and declared UTC windows, accepted operator and handoffs |
| Integrity | Full daily source-manifest/catalog/placement/hold/audit reconciliation for both tenants; expected/actual counts, hashes, missing/extra objects and unresolved jobs |
| Bounds and recovery | Object/byte counts and growth, hot/catalog/broker/S3-version/backup usage, memory/tmp/shared-memory pressure, coherent recovery-set boundary/age, key and attestation validity |
| Service and evidence | Raw client results/telemetry references and hashes, exact numerator/denominator/sample coverage, per-operation/bin latencies, queue age/depth/rejections, movement and scan terminal outcomes |
| Incidents and changes | Alerts/receipt/acknowledgement/recovery times, failures and pauses, stop/resume decisions, change/requalification links, unresolved findings and action owners |
| Human effort | User-task attempts, completion time and intervention, operator minutes by activity, support requests and feedback references |
| Daily review | Named reviewer, UTC timestamp, pass/fail/incomplete findings, evidence gaps and decision for the next declared window |

Review SLO attainment explicitly on days 7 and 14. Missing evidence, zero
eligible samples or unknown queue age are incomplete, never green. The
specification requires at least 99.9% measurement coverage with no unexplained
gap above sixty seconds; a shorter gap may fail qualification even before the
five-minute operational stop threshold. Preserve raw observations alongside
summaries and disclose any collection or clock error.

Record at least thirty user tasks spanning upload/retrieve, discovery/download,
policy/job and admin workflows. For every attempt capture pseudonymous tester,
tenant, role/interface, expected result, start/end/completion time, actual result,
maintainer intervention and evidence. Retain failed, abandoned and repeated
attempts; do not replace them with the successful retry. At least 90% must
complete without maintainer intervention. Collect feedback from at least three
distinct enrolled testers on usefulness and failure clarity. Missing testers
or feedback leaves that gate incomplete. No feedback or operational-effort
result may be inferred from automated load traffic.

## Change control and requalification

Freeze candidate/configuration/workload identities for the campaign. Before a
material change, stop admissions and mutating work, preserve current evidence,
and open a change record with before/after identities, reason, affected gates,
owner and rollback target. Changes to source, image/dependencies, backend,
filesystem/topology, providers, tenancy, credentials/control paths, concurrency,
data bounds, targets or measurement cohort require an explicit impact review.
Scope/specification changes increment its revision. Never loosen a failed
threshold and count the original observations as passing.

The accepted owner approves the affected requalification plan; run it in an
isolated permitted environment and retain passing evidence before a new entry
or resume decision. Preserve unaffected evidence only when its applicability
is explicitly reviewed. For a new material campaign binding, start a separately
identified pilot record and qualifying window; never pool old/new revisions to
meet the fourteen-day or workload totals. Keep the original failed/stopped
campaign and its disposition visible. A routine operational action still needs
its timestamp and identity check even if review finds no requalification impact.

## Stop, rollback and resume

Stop admissions and fence mutating work immediately for integrity, tenant,
hold or audit failures; unexpected sensitive data; TLS/encryption control
failures; expired attestations; exhausted storage; stale/unverified backup; or
an unavailable/unaccepted operator. Also apply these specification triggers:

- Either hard object/byte cap; queue depth at least 8,000 immediately, or oldest
  pending age above 300 seconds for five minutes; any rejection alerts
  immediately. Warn
  at 5,000 pending for two minutes against the frozen 10,000-message cap.
- Temporary or shared-memory usage at 75% of its limit, or pod memory at 80%,
  for five minutes; warn at 60% temporary/shared-memory usage. Any exhaustion
  stops immediately.
- More than 1% unexpected foreground failures over five minutes with at least
  100 attempts; three consecutive thirty-second failed external probes; or
  telemetry missing for more than five minutes.

Retain first-failure time, observed threshold and denominator, affected tenant/
job/correlation identities, actions, operator, sanitized telemetry/logs and
independent audit checkpoints. Use the [incident decision tree](operator_incidents.md)
and [security procedures](operator_security.md); notify through the accepted
route and preserve suspect bytes/state. External fencing must prevent old
workers restarting writes; lease expiry alone is insufficient. Do not purge
queues, delete copies, auto-repair ambiguity or erase evidence to make a check
pass. If a request's acceptance is uncertain, reconcile it before resubmission.

Roll back only to the #164-tested compatible application/schema/configuration
set. If compatibility or data safety is uncertain, keep writers fenced and
restore the coherent recovery set through the
[recovery procedure](recovery_qualification.md). Never use infrastructure
destruction as rollback. Measure every recovery independently: RPO at most
twenty-four hours, RTO at most sixty minutes from first failed probe/interruption
until both tenants reconcile bytes/placement/holds/audit/jobs and work can
safely resume. Record acknowledged changes needing synthetic-source replay.

Only the named accepted owner can authorize resume, after cause and fix are
recorded, integrity is reconciled and affected gates pass again. Record UTC
approval and evidence; a healthy pod, software rollback or checker result
cannot authorize it. Keep the outage and unsuccessful day in the campaign.
An unresolved blocker requires **fix and repeat** or **stop** at exit.

## Exit decision and roadmap

The owner reviews predefined success criteria after the complete window or
immediately on a terminal stop. Compare actual duration, staffed hours,
workload/sample counts, ongoing numeric gates, fourteen daily reconciliations,
user-task completion/intervention ratio, three-testers' feedback and operational
effort against the specification. Retain incidents, recovery outcomes, evidence
gaps and unresolved blockers. Publish one explicit dated, named decision:

| Decision | Required disposition |
| --- | --- |
| Expand | Every current-scope gate passes and no release blocker remains. Propose the next bounded scope and separate capacity/security review; this decision alone does not authorize broader access or important data |
| Fix and repeat | Link defects and missing evidence, responsible owners, prioritized fixes, affected requalification and the next pilot criteria. Preserve failed observations and keep M5 incomplete |
| Stop | State measured reasons, safe shutdown/access revocation and recovery/evidence retention owners. Record unresolved work; a stopped pilot does not satisfy successful M5 acceptance |

For each discovered defect or user need, create a linked ticket with evidence,
impact, priority, responsible owner and acceptance criteria. Order safety/data
integrity blockers first, then reliability/operational and usefulness findings
according to measured impact. Record the rationale and dependencies; do not
invent findings or an expansion roadmap before running the pilot. Update the
[roadmap](roadmap.md), [operator handbook](operator_handbook.md),
[M5 evidence index](evidence/m5/README.md) and generated
[ticket mirror](tickets.md) to the actual decision. Generate the mirror through
`scripts/sync_ticket_mirror.py`; do not manually rewrite its ticket states.

Retain sanitized campaign records and underlying evidence in the repository or
durable restricted storage, with stable links and SHA-256 digests, for at least
ninety days after exit and longer for unresolved findings. Personal rosters,
contacts, credentials, tokens and raw sensitive service responses do not belong
in public artifacts. Local ignored output alone is not a retained pilot record.
