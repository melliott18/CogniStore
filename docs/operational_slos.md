# Operational SLOs and alerts

The versioned [SLO model](slo_model.md) assigns an accountable team, formula,
data source, target, and review window to movement success, movement latency,
API availability, indexing lag, and movement throughput. These are initial
engineering objectives for the documented deployment and workload. Review
attainment weekly and after incidents; review thresholds monthly and after
changes to storage tiers, object sizes, concurrency, or index providers. Assign
the team roles to your own operators before production use.

Start the [observability profile](observability.md). Its Prometheus server loads
`docker/observability/rules/slo.yml`; Grafana provisions the **CogniStore SLOs**
dashboard alongside the operations dashboard. Prometheus evaluates alert state
locally. Notification destinations and escalation policies are deployment
configuration; this profile does not send messages to an on-call service.

Open Grafana at `http://127.0.0.1:3000/d/cognistore-slos` for SLO attainment,
consumed error budget, short/long burn rates, workload volume, queue depth,
and separate cost/carbon consumption and unknown-data panels.

| Alert | Breach condition | Required duration |
| --- | --- | --- |
| Critical SLO burn | 5-minute and 1-hour burn both above 14.4 | 2 minutes |
| Warning SLO burn | 30-minute and 6-hour burn both above 6 | 15 minutes |
| Queue capacity | Pending depth at least 5,000, or stored bytes at least 50% of the live byte cap | 2 minutes |
| Queue admission stop | Pending depth at least 8,000, or stored bytes at least 80% of the live byte cap | Immediate |
| Oldest pending age | Oldest pending message older than 300 seconds (private collector required) | 5 minutes |
| Admission rejection | Confirmed broker message/byte-cap rejection counter increases over 5 minutes | Immediate |
| Cost/carbon warning | Consumption at least 80% but below 100% | 5 minutes |
| Cost/carbon critical | Consumption at least 100% | 5 minutes |
| Budget unknown or missing | An applicable budget cannot be evaluated, or metrics are absent | 5 minutes |
| Scrape unavailable | An expected API or worker target is down or absent | 5 minutes |
| Queue telemetry unavailable | Pending depth or byte utilization is absent or unknown | 5 minutes |
| Indexing timestamp unknown | Unknown timestamp counter increases over 5 minutes | Immediate |

## Runbooks

Every alert rule in `docker/observability/rules/slo.yml` carries a `runbook_url`
to a response section below. Use the
[incident decision tree](operator_incidents.md#first-response-and-decision-tree)
for initial evidence collection and the [security runbook](operator_security.md)
for identity, tenant, secret, and audit failures. Assign the escalation owners
before enabling production alert delivery.

| Shipped alert | Response |
| --- | --- |
| `CogniStoreSLOBurnRateCritical`, `CogniStoreSLOBurnRateWarning` | [SLO burn rate](#slo-burn-rate) |
| `CogniStoreQueueCapacity`, `CogniStoreQueueAdmissionStop`, `CogniStoreQueueByteCapacity`, `CogniStoreQueueByteAdmissionStop`, `CogniStoreQueueOldestPendingAge`, `CogniStoreQueueAdmissionRejected` | [Queue capacity](#queue-capacity) |
| `CogniStorePolicyBudgetWarning`, `CogniStorePolicyBudgetCritical` | [Policy budget](#policy-budget) |
| `CogniStorePolicyBudgetUnknown`, `CogniStorePolicyBudgetTelemetryUnavailable` | [Telemetry unavailable](#telemetry-unavailable), then [policy budget](#policy-budget) |
| `CogniStoreIndexingLagUnknown` | [Telemetry unavailable](#telemetry-unavailable) |
| `CogniStoreTelemetryUnavailable`, `CogniStoreQueueTelemetryUnavailable`, `CogniStoreQueueByteTelemetryUnavailable` | [Telemetry unavailable](#telemetry-unavailable) |

## Measurement and error budgets

For event-based objectives, attainment is `good events / eligible events`.
The allowed bad fraction is `1 - target`; consumed error budget is
`observed bad fraction / (1 - target)`. A value of one means the allowance is
exhausted. Multiwindow burn alerts require both a long-window breach and a
short-window breach: sustained failures page while a recent recovery clears
the short-window condition. These are rolling 30-day objectives; an installation
with less history shows only the available history, not a full-window claim.

Counter increases are calculated per process before aggregation, preserving
restart handling. The movement numerator and denominator count calls, including
retries and idempotent replays. They do not count unique objects. A failed move
is bad for both success and latency; a fast failed call cannot improve latency
attainment. API availability measures completed responses on registered `/v1/`
routes; client errors count as available service, server errors do not. Health
probes, metric scrapes, and unmatched paths are excluded. Target-down alerts
cover failures where no request completion can be recorded; request availability
alone cannot observe failures before reaching the API.

Indexing lag measures the age from the original broker publication timestamp
through completion of each `catalog.scan` attempt, including queue wait, earlier
attempts, and retry delay. Successful attempts within 300 seconds are good;
failed or cancelled attempts are bad. A redrive has a new publication time.
Terminal coordinated replays that skip execution contribute no indexing sample.
Unknown or future broker timestamps increment a separate data-quality counter
and are excluded from the histogram. The unknown-data alert must be resolved
before trusting this objective. Inline scans and external index refreshes are
outside this cohort. Scan completion measures completion of the configured scan
pipeline, not independent verification that every external index is fresh.
Long-running work has no completion sample until it finishes or fails.

Throughput targets 99% of eligible measurement windows at one successful move
per second or more. A window covers five minutes and is evaluated every 15
seconds, so adjacent windows overlap. Eligibility requires a continuously
positive broker pending backlog and observed movement attempts. This excludes
idle periods and scan-only activity but cannot identify the composition of a
mixed queue, or diagnose a complete stall once movement attempts age out.
Use queue capacity and target health alongside this objective. Tune the floor
to the qualified workload before applying it to large objects or other tiers.

An empty denominator is **No data**, never 100% attainment. Check scrape
health and data-quality alerts before interpreting absent series. Prometheus
retains at least the review window in the local profile. Historical gaps remain
gaps even after service recovery; evaluate evidence coverage separately from
the numeric result.

## Cost and carbon budgets

The API metrics endpoint takes a read-only, consistent catalog snapshot of
budget definitions and held reservations. It applies the same `evaluate_budget`
accounting as policy admission, using zero additional charge. Opening
commitments and frozen reservation charges therefore retain the same versioned
estimator, rate evidence, upper-bound choices, retry charges, and overrides as
the [policy budget ledger](policy_budgets.md). Scraping does not fetch rates,
re-estimate admitted work, or mutate budgets. These remain modeled commitments,
not invoices or measured emissions.

Cost and carbon each expose the maximum consumption ratio across independently
enforced scopes. Overlapping budgets are never added together. No budget IDs,
bucket names, prefixes, or profile IDs appear in metric labels. Inspect
`cognistore --catalog-db CATALOG --json budget-list` and retained reservations
to identify the particular scope and its original estimate evidence.

An unused zero limit consumes zero; positive commitments against zero consume
an infinite ratio and must alert. Missing ledger amounts or unavailable budget
periods are unknown, rather than zero usage. Renew a budget for the same scope
to recover from expiry; renewing a wider scope cannot repair a narrower expired
scope. No configured budgets means no commitments to monitor, not evidence
that an unbudgeted deployment is within a spending target.

## SLO burn rate

Applies to `CogniStoreSLOBurnRateCritical` and `CogniStoreSLOBurnRateWarning`.

**Triage.** Open the SLO dashboard and record the alert's `slo` label, burn
windows, operation volume, onset, recent changes, and consumed error budget.
Check worker readiness and scrape health before interpreting the series.

**Action.** Select the affected objective:

| `slo` label | Response |
| --- | --- |
| `move_success`, `move_latency` | Correlate worker errors and backend latency; follow [queue/DLQ diagnosis](operator_incidents.md#dead-letter-diagnosis-and-redrive), then [scoped repair](operator_incidents.md#interrupted-move-and-consistency-repair) for incomplete journals. Restore the dependency before replaying work; retain source copies and integrity checks. |
| `move_throughput` | Follow [queue capacity](#queue-capacity); confirm a movement workload is actually pending and compare tier waiters, retries, and backend capacity before scaling. |
| `api_availability` | Correlate failing route/request IDs with API, catalog, and storage logs/traces. Restore the failing dependency or roll back the triggering release using the deployment runbook. An auth-related client error may affect users without contributing to this 5xx-based objective. |
| `indexing_lag` | Compare queue wait with extractor, embedding, and index-provider duration/errors. Restore the failing provider, reduce admission pressure, and complete a new authorized scan. Resolve unknown timestamps before trusting lag. |

**Verify.** Require successful representative work in the affected scope and
falling short-window burn. Follow long-window burn and remaining allowance
through recovery; a cleared alert alone is not a fresh 30-day attainment claim.
Confirm expected catalog/index results and queue progress as applicable.

**Escalate.** Contact the accountable team in the [SLO model](slo_model.md)
immediately for critical burn, or if warning burn persists after dependency
recovery. Bring scoped correlation/job/move IDs and recent change evidence.
Missing/corrupt data goes to the storage owner; identity/audit anomalies go to
security. Record remaining allowance before resuming risky changes.

## Queue capacity

Applies to `CogniStoreQueueCapacity`, `CogniStoreQueueAdmissionStop`,
`CogniStoreQueueByteCapacity`, `CogniStoreQueueByteAdmissionStop`,
`CogniStoreQueueOldestPendingAge`, `CogniStoreQueueAdmissionRejected`, and
backlog-associated throughput burn.

**Triage.** Check `max(cognistore_job_queue_depth{state="pending"})` across
workers and a fresh worker `/readyz` response. Compare outstanding ACKs,
in-flight jobs, tier saturation, retry/dead-letter counts, and successful move
throughput. Confirm whether jobs are scans or policy movement; a mixed queue
can invalidate the throughput cohort. Preserve affected IDs and consumer settings.
Compare `cognistore_job_queue_byte_utilization_ratio` with the live stream's
stored bytes and configured `max_bytes`. The selected 1 GiB stream byte cap
can reject large envelopes before the 10,000-message cap. Worker replicas
observe the same stream: use `max`, never add their depths or utilization.

**Action.** Follow the [backlog decision table](operator_incidents.md#queue-backlog-and-saturation).
Restore consumers/dependencies or slow producers first. Qualify any added worker
or per-tier capacity against backend limits and divide aggregate limits across
processes. Preserve stream contents and use a coordinated change for shared
consumer settings. Warn at 5,000 pending messages or 50% byte utilization for
two minutes. Stop new submissions immediately at 8,000 pending or 80% byte
utilization, or after oldest pending age exceeds 300 seconds for five minutes.
Any confirmed broker capacity rejection alerts without an additional `for`
delay; collection/evaluation latency still applies. These alerts instruct the
operator or external admissions controller; they do not themselves enforce
an application admission stop. Retain the broker hard caps and discard-new
behavior, reconcile uncertain jobs before retries, and never purge to regain
headroom. The byte thresholds preserve headroom even when message count is low.

`cognistore_job_admission_rejections_total` counts each explicit main-stream
message/byte rejection at the NATS publication boundary, including redrive.
Uncertain publication errors and unrelated broker failures are excluded.
It is a process-local, content-free counter; scrape each producer before
load and preserve per-process labels so `increase` handles restarts before
aggregation. A producer that starts and exits between scrapes can lose its
rejection observations, so retain client admission results as independent
evidence. Tenant API `/metrics` remains disabled: the selected deployment must
supply private producer/client collection without exposing tenant metrics.
The existing worker endpoint alone cannot report API producer rejections.

The age rule requires the private collector to publish
`cognistore_job_queue_oldest_pending_age_seconds`: current age in seconds of
the oldest original publication still pending for the selected consumer,
including retry delay. Use zero only for a verified empty pending set; emit
NaN or omit the series when measurement fails. A publication-to-claim
histogram or age of the most recently claimed job does not satisfy this
contract. This repository does not yet provide that private collector or a
real notification route. Missing/unknown age, producer coverage or alert
delivery evidence blocks #165 qualification; a non-firing rule is not proof
of a healthy queue. See the [pilot gates](production_pilot.md#numerical-qualification-gates).

**Verify.** Require continuing completions/ACKs, falling backlog below the
threshold, restored byte headroom, known oldest pending age and successful new work.
For the pilot, prove receipt within five minutes, acknowledgement within
15 minutes, a usable runbook, and recovery delivery within five minutes of
clearing each induced alert. Eligible movement windows should meet the
floor. A throughput alert clearing because attempts aged out of the cohort does
not prove a stalled queue recovered.

**Escalate.** Involve the broker/platform owner if claims or ACKs stop, the
storage owner for persistent backend throttling, and the SLO owner if the
estimated drain time exceeds the workload objective. Do not purge jobs to
silence the alert.

## Policy budget

Applies to `CogniStorePolicyBudgetWarning`, `CogniStorePolicyBudgetCritical`,
and the accounting branch of `CogniStorePolicyBudgetUnknown`.

**Triage.** Record the cost/carbon `dimension`, consumption ratio, and unknown
signals. On the trusted catalog used by this deployment, inspect definitions:

```bash
cognistore --catalog-db /var/lib/cognistore/catalog.sqlite3 --json budget-list
```

Use the deployment's PostgreSQL locator instead when appropriate; do not infer
non-default tenant coverage from a local default-catalog command. Read periods,
scopes, opening commitments, reservations, and original estimator evidence
using the [budget ledger workflow](policy_budgets.md). Metric labels deliberately
omit scope IDs, so the dashboard alone cannot identify the offending budget.

**Action.** Reduce admissions and compare alternatives through documented
what-if simulation. Request an approved replacement allowance/period from the
budget owner and apply it through the audited workflow. Renew expired budgets
at the same scope; a wider scope does not repair a narrower expired one.
Restore missing evidence before resuming work. Failed attempts, retries, and
overrides retain modeled charges; do not edit/subtract ledger entries to clear
the alert, or add overlapping budget amounts together.

**Verify.** Require every applicable scope to be evaluable and the maximum
ratio to return below the alert threshold, with no unknown series. Check a new
policy preview respects the intended scope/period. An expired period or missing
metric is not proof of recovered budget headroom.

**Escalate.** Engage the budget owner for exhausted headroom and the catalog/
service owner for missing ledger evidence or inexplicable commitments. Include
the dimension and restricted reservation/estimator evidence; modeled charges
are not invoices or measured emissions.

## Telemetry unavailable

Applies to `CogniStoreTelemetryUnavailable` (API and worker),
`CogniStoreQueueTelemetryUnavailable`, `CogniStoreQueueByteTelemetryUnavailable`,
`CogniStoreIndexingLagUnknown`,
`CogniStorePolicyBudgetUnknown`, and
`CogniStorePolicyBudgetTelemetryUnavailable` (cost and carbon).

**Triage.** Inspect Prometheus Targets at the deployment's private Prometheus
endpoint and the [API/worker probes](operator_incidents.md#first-response-and-decision-tree).
Record the `job` or `dimension` label, scrape error, last good sample time, and
whether other targets are healthy. A reachable metrics endpoint does not prove
that its catalog or broker reads succeeded.

**Action.** Use the alert-specific branch:

| Alert | Response |
| --- | --- |
| `CogniStoreTelemetryUnavailable` | Restore the expected process/target, scrape routing, verified TLS, and permissions. Check deployment target names and scrape configuration before changing rules. A tenant-enabled API deliberately returns 404 from aggregate `/metrics`; use an approved private operator telemetry design, not disabling tenant isolation. |
| `CogniStoreQueueTelemetryUnavailable` | Inspect fresh worker broker/account/stream/consumer readiness and connectivity. Restore the correct durable topology and broker permissions; never substitute zero for unknown queue depth. |
| `CogniStoreQueueByteTelemetryUnavailable` | Inspect the live stream byte count, positive finite `max_bytes` and worker probe readiness. Restore a bounded byte cap and healthy collection; an unbounded or unknown byte cap does not establish free capacity. |
| `CogniStoreIndexingLagUnknown` | Inspect broker publication metadata and UTC clock synchronization. Correct missing/future timestamps at the source and complete a new scan; a redrive creates a new publication time and cannot repair historical measurements. |
| `CogniStorePolicyBudgetUnknown` | Check catalog connectivity and missing/expired budget periods/evidence, then follow [policy budget](#policy-budget). |
| `CogniStorePolicyBudgetTelemetryUnavailable` | Confirm the API version/configuration exposes both expected dimensions and can access its catalog. Restore instrumentation/catalog access or the approved private telemetry path. |

**Verify.** Confirm expected targets scrape successfully, queue depth and byte
utilization are finite and known, both budget dimensions are present/evaluable, and unknown timestamp
counters stop increasing across fresh scan completions. Allow the five-minute
timestamp increase window to age out. Preserve the missing-history interval;
evaluate evidence coverage before making SLO claims spanning that gap.

**Escalate.** Engage the observability/platform owner for absent targets or an
unimplemented tenant-safe scrape topology, the broker owner for unavailable
queue metadata, and catalog/budget owners for accounting failures. Treat a
telemetry outage concurrent with service failure as an incident, not merely a
dashboard problem.

## Qualification evidence and checks

Use the model's read-only evaluator on an existing M1 JSON report:

```bash
python -m cognistore.slo evaluate-qualification \
  docs/evidence/m1/full-20260827-205845.json
```

It retains profile/provenance and evaluates the available movement evidence.
M1 phase averages are throughput baselines, not proof of five-minute window
attainment. Its logical move latency includes retries and differs from the live
call histogram; quantified bounds are labeled accordingly. API and indexing
data absent from M1 remain unavailable. A reduced campaign is never promoted
to full-scale evidence. See [model details](slo_model.md) for JSON fields and
exit statuses, and [M1 qualification](scale_qualification.md) for the original
campaign procedure.

The alert fixtures exercise threshold breaches, pending/firing transitions,
recovery, idle input, and unknown data with the real Prometheus rule engine.
Run these checks from the repository root (Docker is required for Promtool):

```bash
./docker/observability/verify.sh
python -m pytest tests/unit/test_slo.py tests/unit/test_indexing_slo.py \
  tests/unit/test_budget_telemetry.py
```

CI also validates the deployed rules, keeping tests tied to the configuration
used by the observability profile.
