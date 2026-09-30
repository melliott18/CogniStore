# M5 load evidence schema

This is the supplemental evidence contract for
[`scripts/load_export.py`](../scripts/load_export.py) and
[`scripts/load_acceptance.py`](../scripts/load_acceptance.py). It supplements
the [campaign guide](load_qualification.md) and implements the numerical gates
in [the accepted production-pilot specification](production_pilot.md#numerical-qualification-gates).
It does not authorize a staging campaign or replace owner review.

The deployment collector must retain actual observations from the exact run,
candidate, configuration, and environment. Missing publication times, missing
attempts, unavailable hold/audit checks, and missing operator receipts remain
missing evidence. Never fill them with zero, infer them from successful HTTP
responses, or label local fixtures as staging. Import success means only that
the supplied bytes were copied and their identities checked. The acceptance
report can be `passed`, `failed`, or `incomplete`; even `passed` reports
`production_qualified: false` and `measurements-passed-review-required`.

## Files and encoding

The runner supplies `campaign.json`, `foreground.jsonl`, and `telemetry.jsonl`.
The deployment spool supplies all ten files below, using these exact basenames:

| File | Contents |
| --- | --- |
| `jobs.jsonl` | One reconciled terminal record per acknowledged background job. |
| `scans.jsonl` | Every completed scan attempt, including failed attempts and retries. |
| `movement.jsonl` | Every completed movement call, including failed calls and retries/replays. |
| `movement-backlog.jsonl` | Actual direction-scoped pending/in-flight movement observations during every dedicated window. |
| `reconciliations.jsonl` | Per-tenant integrity checkpoints before/after phases and drills. |
| `faults.jsonl` | Observed faults, recovery boundaries, and induced-alert references. |
| `resource-checkpoints.jsonl` | Post-warm-up baselines and post-drain resource measurements. |
| `alerts.jsonl` | Actual firing, delivery, operator acknowledgement, runbook, and recovery events. |
| `observed-artifacts.jsonl` | Bound source observations supporting the normalized records. |
| `capacity-cost.json` | One observed physical-consumption and cost-input document. |

Use UTF-8 JSON; each JSONL line contains one complete object. A JSONL record,
including its terminating newline, must fit in 65,536 bytes. The cost document
must fit in 4 MiB. Duplicate JSON keys, NaN/infinities, negative counts,
booleans used as numbers, empty files, symlinks, and nonregular files are invalid.
Keep large original manifests and diagnostic exports in protected retention;
the bounded source observation can contain their digests and stable references.
Do not embed bearer tokens, credentials, personal data, or unrestricted HTTP
bodies in the spool.

Every JSONL record and the cost document must include:

| Field | Type and meaning |
| --- | --- |
| `run_id` | Unique campaign identifier, unchanged across the run. |
| `binding_sha256` | Lowercase 64-character SHA-256 of the immutable binding below. |

Compute the binding as follows, with the complete `bindings` object from the
runner's campaign. Do not hash `campaign.json` here: phase completion times are
recorded later.

```python
binding_sha256 = hashlib.sha256(json.dumps(
    {"run_id": campaign["run_id"], "bindings": campaign["bindings"]},
    sort_keys=True, separators=(",", ":"), allow_nan=False,
).encode("utf-8")).hexdigest()
```

`bindings` identifies `source_revision` (40 lowercase hex characters),
`image_digest` (`sha256:` plus 64 hex characters), `configuration_sha256`,
`specification_sha256`, `dependency_manifest_sha256`,
`corpus_manifest_sha256`, and `environment_id`. SHA fields are lowercase hex.
The deployment collector must verify these against its source environment;
copying a supplied identity is not independent attestation.

Unless a field says otherwise, identifiers match
`[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`. Timestamps are UTC ISO-8601 strings with
`Z` or `+00:00`, never naive times or relative durations. Every timestamp must
fall within the campaign's `started_at` and `ended_at`. Use one canonical UTC
spelling when copying the same timestamp into different records: scan
publication and receipt artifact comparisons require matching strings.
Counts and attempt numbers are integers; ratios and quantities must be finite.
All fields listed below are required unless explicitly marked otherwise.

## Campaign boundaries

The runner-owned campaign contains `run_id`, `bindings`, `profile_sha256`,
`execution_scope`, `started_at`, `ended_at`, `status`, `stop_latched`,
`uncertain_mutations`, `phases`, `movement_windows`, `required_faults`,
`retry_limit`, and `induced_alert_ids`. The profile digest binds the shipped
[`release/load/profile.json`](../release/load/profile.json).

Acceptance requires `execution_scope: "isolated-staging"`,
`status: "observations-collected"`, `stop_latched: false`, and integer
`uncertain_mutations: 0`. A failed/interrupted run or local rehearsal cannot
pass. The campaign interval must be positive and no longer than 90 days.

`phases` has `nominal`, `burst`, and `capacity` entries. Each has `started_at`,
`ended_at`, `actual_ended_at`, integer `duration_seconds`, `warmup_seconds`,
integer `object_count`, and boolean `completed`. The planned end equals start
plus duration, `completed` must be true, and the observed actual end must not
precede the planned end. Phases do not overlap. Nominal uses at least 259,200
measured seconds, 1,800 warm-up seconds, and 100,000 objects; burst uses at
least 900 seconds and 100,000 objects; capacity uses at least 7,200 seconds,
1,800 warm-up seconds, and 200,000 objects. Capacity load continues until its
movement pass finishes; a short run cannot be extended by changing metadata.

Each `movement_windows` entry has `window_id`, `direction`, `started_at`,
`ended_at`, and `dedicated: true`. Direction is `hot-to-warm` or `warm-to-hot`.
Each measured window lasts at least 3,600 seconds and an exact multiple of 300
seconds. Windows cannot overlap each other or any foreground phase. Retain
source-placement preparation separately; it does not count toward measured
movement throughput.

Each `required_faults` entry is `{ "fault_id": ..., "kind": ... }`; IDs are
unique and the list includes `saturation`, `backend_throttle`, `bounded_retry`,
and `burst` kinds. `retry_limit` is the frozen configured maximum attempt count,
not a limit chosen after observations. `induced_alert_ids` contains every
induced alert, with unique IDs. Omitting a failed notification is not permitted.

## Jobs and scan attempts

`jobs.jsonl` has one row per acknowledged job, after drain/reconciliation:

| Field | Meaning |
| --- | --- |
| `job_id` | Stable accepted job identity, unique in this file. |
| `tenant` | `pilot-a` or `pilot-b`. |
| `kind` | `scan`, `policy`, or `move`; map worker `catalog.scan` to `scan` and `policy.run` to `policy`. |
| `published_at` | Original broker publication time, preserved across retries/redrives. |
| `accepted_at` | Actual first worker execution start, from the first `job.started` observation. This field measures admission into background execution, not receipt of HTTP 202 at the client. |
| `completed_at` | Actual final worker terminal observation after all attempts, not the next client status-poll time. |
| `outcome` | `succeeded`, `failed`, `cancelled`, or explicitly expected correctness `expected_conflict`. Unknown/nonterminal jobs block acceptance. |
| `object_count` | Positive count of the actual selected objects before submission: for scans, the nonempty selected tier within the prefix (<=100); for policy jobs, the bounded prefix selection (<=1,000). Retain worker result counts separately and reconcile discrepancies. |
| `attempt_count` | Required for scan jobs: positive observed complete attempt count, including retries. |

The chronology is `published_at <= accepted_at <= completed_at`. Source
publication comes from the broker delivery metadata retained by the worker as
`source_published_at`. `JobStatus.created_at`, envelope creation, a `job.queued`
audit timestamp, producer submission time, and client response time are not
substitutes. If original publication cannot be recovered after a redrive, leave
the campaign inconclusive rather than resetting the scan clock. Worker audit
events retain available publication metadata; absent metadata stays absent.

The collector must reconcile the job inventory against acknowledged API/SDK
submissions, durable worker state, and the final queue. Exporting only completed
jobs while silently dropping pending ones violates the contract. Acknowledged
jobs that never started have no valid `accepted_at` and therefore block a
complete qualifying bundle. Peak execution intervals within each foreground
phase must not exceed two. Separately induced fault queues are not pooled into
this normal-load calculation.

The driver submits one scan per tenant per period, choosing an actual nonempty
tier under the prefix locks. Mixed-tier prefixes rotate through their nonempty
tiers. It does not submit an extra empty-tier scan and count the quick success
toward indexing availability. If another producer submitted an empty scan, retain
that fact without manufacturing a positive object count; it is not a qualifying
scan observation.

`scans.jsonl` rows contain `attempt_id`, `job_id`, `attempt_number`,
`published_at`, `completed_at`, and boolean `success`, in addition to the common
binding. Attempt IDs are unique. For each job, attempt numbers are exactly
`1..attempt_count`, completion times do not run backward, and every row carries
the job's same original publication timestamp. Only the final attempt can
succeed; its completion equals the job's terminal time and its success equals
`outcome == "succeeded"`.

At least 1,000 scan attempts are required. At least 99% of all those attempts
must succeed within 300 seconds of original publication, including queue and
retry delays. Failed attempts remain in the denominator. Every scan job must
have its attempts represented. Nominal publication coverage must include a
scan for each tenant in every nonoverlapping 300-second slot and a policy job
for each tenant in every 3,600-second slot, anchored at nominal start.

## Movement calls

Each `movement.jsonl` row adds these fields to the common binding:

| Field | Meaning |
| --- | --- |
| `call_id` | Unique actual call-attempt identity, including retries/replays. |
| `logical_move_id` | Stable identity across attempts of one logical placement transition; a later reverse move is a different transition. |
| `job_id` | Referenced policy/move job from `jobs.jsonl`. |
| `cohort` | `nominal`, `capacity`, `movement`, `fault`, or `correctness`. |
| `direction` | `hot-to-warm` or `warm-to-hot`. |
| `size_bytes` | Exactly 4,096, 65,536, 1,048,576, or 16,777,216. |
| `started_at`, `completed_at` | Observed call boundaries, not whole policy-job duration or status-poll timestamps. |
| `success`, `verified` | JSON booleans. A successful call requires independently verified byte/checksum and placement outcome. |
| `attempt_number` | Positive contiguous number within the logical move. |
| `terminal_state` | `null` until the final attempt, then `succeeded` or `expected_conflict`. |
| `movement_window_id` | Matching campaign window ID for `movement` cohort; explicit JSON `null` otherwise. |

Retain failed and replayed calls, including early failures before transfer.
Do not count placement no-ops as newly moved objects, or deduce call durations
from a batch's start/end. Correlate actual call observations, worker/job
identities, and verified final placement. A completed state-machine audit event
alone does not enumerate earlier failed call attempts. Instrumentation must
cover the full call boundary, or acceptance remains incomplete.

Attempts of one logical move must keep the same job, size, direction, cohort,
and window; they cannot overlap. Only the final attempt may have a terminal
state. `succeeded` requires successful verified completion.
`expected_conflict` is allowed only in the separately labeled correctness
cohort. An unresolved logical move blocks acceptance. Calls must fit their job
publication/terminal interval. Nominal and capacity calls start within their
respective measured phase.

Normal movement calls require >=99.9% success and >=99% successful completion
within 30 seconds, calculated for each of nominal, capacity, and dedicated
movement cohorts as well as reported overall. Fault/correctness calls remain
recorded but do not improve these denominators. Every dedicated window needs
at least 10,000 distinct verified successful logical moves; multiple successful
replays of the same move count once for throughput. At least 99% of its fixed,
nonoverlapping five-minute windows need 300 successful moves. Empty/stalled
windows stay in the denominator. Completions at or beyond the declared end do
not count inside the window.

Both directions need dedicated windows. Both directions also need at least
10,000 terminal successful nominal moves. In each direction's nominal total
and each dedicated window, the exact size mix is 60% 4 KiB, 30% 64 KiB, 9% 1 MiB,
and 1% 16 MiB, using integer counts. Capacity evidence includes the completed
hot-to-warm-to-hot pass and full reconciliation; a direction flag alone is not
evidence that the reviewed capacity pass finished.

### Dedicated movement backlog

`movement-backlog.jsonl` establishes that actual queued work remained available
during the dedicated movement measurement. It is required separately from
successful-call throughput. A campaign declaration `dedicated: true`, a list of
objects ready to submit, or a paced series of batches cannot prove a persistent
platform backlog. Neither can the generic all-job `queue_pending` metric, which
may include scans, unrelated jobs, or the opposite movement direction.

Each record carries the common binding and these fields:

| Field | Source meaning |
| --- | --- |
| `window_id` | Declared dedicated movement window containing the observation. |
| `direction` | The actual observed `hot-to-warm` or `warm-to-hot` direction, matching that window. |
| `observed_at` | Actual private platform/worker **source sample timestamp**, within the half-open window `[started_at, ended_at)`. Do not substitute collector receipt time or refresh an old sample's timestamp. |
| `pending_moves` | Nonnegative integer count of actual pending, direction-scoped movement work available to the workers at that source observation. Planned/unsent client work is excluded. |
| `in_flight_moves` | Nonnegative integer observed in-flight movement count for the same scope. In-flight work alone does not satisfy the pending-backlog requirement. |
| `source_evidence_id` | Unique bound source artifact, with exactly matching `observed_at` and normalized `content.observation`. |

Retain one sample per 15-second slot anchored at each window start, in strict
time order for that window. Duplicate samples in a slot or out-of-window samples
are invalid. Source coverage must be >=99.9% of these slots, with no gap >60
seconds, including leading/trailing gaps. Every observed `pending_moves` value
must be greater than zero. A zero-backlog observation fails the backlog gate
even when average movement throughput passes. Sparse positive samples cannot
establish the required hour of measured backlog; unknown counts are incomplete.

The independent staging producer must supply a configured private query or
worker observation stream that actually distinguishes pending movement direction
and retains source timestamps. The built-in generic resource collector does not
provide this signal and the exporter must not manufacture a metric, a constant,
or a backlog count from the runner's pacing/inventory. Preserve the query/source
identity and raw export digest in the source artifact. Missing instrumentation
blocks acceptance. The positive local regression fixture contains explicitly
synthetic per-slot observations solely to test these formulas; it is not live
backlog evidence.

## Source observation artifacts

Every `observed-artifacts.jsonl` row has the common binding plus `evidence_id`,
`observed_at`, `kind`, and nonempty JSON object `content`. IDs are unique, `kind`
is an identifier describing the observation source, and `observed_at` is its
actual event/checkpoint time. `content` itself must include the same `run_id`
and `binding_sha256`. An old raw artifact cannot acquire a new identity merely
by changing its enclosing row. Identical canonical `content` digests cannot be
reused under different artifact IDs.

Reconciliation, fault, resource, cost, and movement-backlog records each have a unique
`source_evidence_id` referencing one artifact. Its `content.observation` must
exactly equal the normalized record with only `run_id`, `binding_sha256`, and
`source_evidence_id` removed. Additional retained source metadata belongs beside
`content.observation`, not inside the normalized comparison. Record durable
source-export references and digests there so reviewers can trace the result.
A source artifact cannot be shared across multiple checkpoints or repurposed
as an alert receipt. For reconciliations, artifact `observed_at` must exactly
match the normalized checkpoint time.

For example, the *shape* of a bound source artifact is:

```json
{
  "run_id": "unique-run-id",
  "binding_sha256": "<computed SHA-256>",
  "evidence_id": "nominal-after-pilot-a",
  "observed_at": "<actual UTC timestamp>",
  "kind": "reconciliation",
  "content": {
    "run_id": "unique-run-id",
    "binding_sha256": "<same computed SHA-256>",
    "observation": {"<all normalized checkpoint fields except the three excluded fields>": "..."},
    "source_export_sha256": "<digest of retained original source export>"
  }
}
```

This example is intentionally incomplete and cannot qualify a run. The source
content hash is SHA-256 of the canonical JSON encoding used for the binding
above. File hashes, by contrast, cover exact on-disk bytes including newlines.

## Reconciliation checkpoints

Each row in `reconciliations.jsonl` contains `checkpoint_id`, `scope`,
`boundary`, `tenant`, `observed_at`, `expected_objects`, `observed_objects`,
`checks`, and `source_evidence_id`, plus the common binding. Scope is
`phase:nominal`, `phase:burst`, `phase:capacity`, or `fault:<fault_id>` for each
declared drill. Boundary is `before` or `after`; tenant is `pilot-a` or `pilot-b`.
Every scope needs both boundaries for both tenants, exactly once. Checkpoint IDs
are unique. A before checkpoint is no later than scope start; an after
checkpoint is no earlier than scope completion/recovery.

Object counts must be nonnegative integers and equal. Reconcile against the
retained expected inventory after acknowledged mutations, not a hard-coded
initial count. `checks` contains every field below as a nonnegative integer:

```text
checksum_mismatches           missing_objects
extra_objects                catalog_mismatches
hold_bypasses                 audit_gaps
cross_tenant_leaks            duplicate_destructive_effects
unresolved_jobs              unknown_outcomes
lost_acknowledged_objects
```

Every count must be zero. Counts come from full object/checksum/placement,
catalog, hold, audit-continuity, tenant-isolation, acknowledged-state and job
reconciliations. A successful GET/catalog snapshot alone cannot establish hold
enforcement, audit continuity, or absence of foreign-tenant disclosure.

## Fault and recovery observations

Each `faults.jsonl` row contains `fault_id`, `kind`, `started_at`, `cleared_at`,
`recovered_at`, `observations`, `alert_ids`, and `source_evidence_id`, plus the
common binding. IDs/kinds must match the complete campaign fault list, with no
duplicates. Times satisfy start <= clear <= recovery. `alert_ids` is a nonempty
list of unique induced-alert IDs; the union across drills must exactly cover
the campaign's induced-alert list.

Required observations by kind are:

| Kind | `observations` fields and checks |
| --- | --- |
| `saturation` | Integer `queue_cap: 10000`; integer `rejected_submissions > 0`; boolean `stop_submissions_observed: true`. Retain actual rejection/stop events. |
| `backend_throttle` | Integer `throttled_calls > 0`, measured at the backend boundary. |
| `bounded_retry` | Integers `attempts`, `max_attempts`, `unresolved`; require `1 < attempts <= max_attempts <= campaign.retry_limit` and `unresolved == 0`. |
| `burst` | Boolean `nominal_latency_restored: true` and `queue_age_restored: true`, backed by post-burst observations. Start equals measured burst start; clear is at or after burst end; recovery occurs within 600 seconds of clear. |

An additional reviewed fault kind must have a nonempty observed result and the
same full reconciliation/alert evidence; the calculator does not invent a new
SLO for it. Hook exit code zero proves only hook execution. It does not prove
backend throttling, bounded retry, nominal-latency recovery, or operator receipt.

## Resource recovery and physical consumption

`resource-checkpoints.jsonl` requires one `baseline` and one `drain` record for
each of `nominal` and `capacity`. Each contains `cohort`, `stage`, `observed_at`,
`values`, `incidents`, and `source_evidence_id`, plus the common binding.
Baseline is measured after warm-up and no later than phase start; drain is
measured after jobs drain and no earlier than phase end.

`values` contains integer byte measurements for `api_rss_bytes`,
`worker_rss_bytes`, `api_tmp_bytes`, and `worker_tmp_bytes`. For each metric,
post-drain usage must be <=110% of its baseline; a zero baseline requires zero
after drain. `incidents` contains integer counts `oom`, `disk_exhaustion`,
`temp_exhaustion`, and `monotonic_leak`; all must be zero. Platform event logs
and the retained resource series must support those counts. Unknown event-log
coverage is not a zero-incident observation. The separate telemetry evaluator
still enforces sample coverage, CPU/RSS p95 limits, disk headroom, and source
measurement coverage for the entire measured phase.

`capacity-cost.json` contains the common binding, `observed_at`,
`source_evidence_id`, and:

| Object/field | Required contents |
| --- | --- |
| `physical_usage` | Nonnegative integer `hot_bytes`, `catalog_bytes`, `broker_bytes`, `ingress_bytes`, `egress_bytes`; measured physical values and byte transfer totals. |
| `cost_inputs` | Three-uppercase-letter `currency`; finite nonnegative `compute_hours`, `storage_gib_hours`, `egress_gib`, `observed_cost`. |
| `scaling_recommendation` | Nonempty string, at most 4,096 characters, explaining the measured fixed-node capacity/growth recommendation and required reruns. |

Record the cost measurement interval, provider/source references, and the
physical/logical distinction in retained source metadata. Logical payload alone
does not represent sidecars, versions, catalog/broker storage, or infrastructure
consumption. There is no financial-savings claim or cost SLO.

## Operator notifications

Every `alerts.jsonl` row contains the common binding and:

```text
alert_id                     receiver_id
condition_started_at         fired_at
received_at                  acknowledged_at
runbook_executed_at           cleared_at
recovery_received_at         runbook_url
receipt_id                   recovery_receipt_id
acknowledgement_id            runbook_evidence_id
source_evidence_sha256
```

All seven timestamps follow the listed event order: condition start, firing,
actual delivery, operator acknowledgement, runbook execution, condition clear,
and actual recovery delivery. Delivery is <=300 seconds after firing,
acknowledgement <=900 seconds after firing, and recovery delivery <=300 seconds
after clear. `receiver_id` identifies the accepted operator. `runbook_url` is a
working HTTPS runbook without embedded credentials; the URL and successful
operator execution must be backed by retained evidence.

Each of the four reference IDs identifies a different observed artifact and
cannot be reused by another alert or checkpoint. Its `content.observation`
must contain exactly the following fields copied from the alert row:

| Reference | Required `content.observation` fields |
| --- | --- |
| `receipt_id` | `alert_id`, `receiver_id`, `receipt_id`, `received_at`. |
| `recovery_receipt_id` | `alert_id`, `receiver_id`, `recovery_receipt_id`, `recovery_received_at`. |
| `acknowledgement_id` | `alert_id`, `receiver_id`, `acknowledgement_id`, `acknowledged_at`. |
| `runbook_evidence_id` | `alert_id`, `receiver_id`, `runbook_evidence_id`, `runbook_url`, `runbook_executed_at`. |

Each artifact's `observed_at` equals its respective event timestamp. The alert's
`source_evidence_sha256` equals the canonical `content` hash of the firing
receipt artifact. All artifacts still carry the common run/candidate binding,
both on the row and inside `content`. Source-provider message/acknowledgement
references and retained export digests can accompany `content.observation`.
Rule tests, webhook-send attempts, private scrape success, and locally invented
receipt timestamps cannot satisfy these evidence requirements.

## Finalization, provenance, and review

The importer copies a validated snapshot without replacing existing evidence
and writes `export-manifest.json` with exact imported file hashes. The runner
then builds `acceptance-manifest.json` with `schema_version: 1`, `run_id`, the
final `campaign_sha256`, and `artifacts`, a map of all thirteen fixed filenames to
their exact-byte SHA-256 digests. No relative paths, alternative filenames, or
additional manifest entries are accepted. The acceptance calculator snapshots
and verifies those bytes before evaluating them; it never combines different
run/candidate identities to fill missing observations.

Hashes detect altered/inconsistent retained files. They are not signatures and
do not prove that a producer told the truth, that the selected endpoint was the
accepted deployment, or that an operator approved pilot entry. Independent
review must compare deployment identities, raw source exports, full inventory,
fault controls and owner approvals. Preserve those materials with the evidence
bundle. Local synthetic regression tests validate arithmetic and failure
handling only; they never replace the 72-hour staging campaign.

Hosted CI remains `skipped: user instruction; known GitHub billing/spending
restriction`. Its skipped status is not a qualification pass.
