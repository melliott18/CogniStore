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
| Queue capacity | Pending depth above 10,000 | 10 minutes |
| Cost/carbon warning | Consumption at least 80% but below 100% | 5 minutes |
| Cost/carbon critical | Consumption at least 100% | 5 minutes |
| Budget unknown or missing | An applicable budget cannot be evaluated, or metrics are absent | 5 minutes |
| Scrape unavailable | An expected API or worker target is down or absent | 5 minutes |
| Queue telemetry unavailable | Pending depth is absent or unknown | 5 minutes |
| Indexing timestamp unknown | Unknown timestamp counter increases over 5 minutes | Immediate |

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

1. Open the SLO dashboard, identify the affected objective, then compare recent
   and long-window burn with request/operation volume and target health.
2. For movement failures or latency, inspect worker readiness, retries, dead
   letters, and driver latency. Use [consistency checks](consistency_checks.md) and
   [background workers](background_workers.md) to diagnose retained sources
   and retry bounds before redrive. Do not bypass integrity verification.
3. For API failures, inspect structured events and correlated traces, catalog
   connectivity, and storage operations. Restore the failing dependency or
   roll back the triggering deployment.
4. For indexing lag, compare queue age with scan duration and index operations.
   Repair the extractor/provider or reduce admission pressure; verify a new
   scan completes and the short burn window returns below threshold.
5. Recovery requires both restored service and falling burn. Record the consumed
   budget and review remaining allowance before increasing risky change volume.

## Queue capacity

1. Check broker pending depth with `max` across workers; each worker reports the
   same durable consumer, so summing would overcount. Compare in-flight jobs,
   worker readiness, limiter saturation, retries, and successful move throughput.
2. Confirm whether backlog contains moves, scans, or policy jobs. A mixed queue
   can invalidate the throughput cohort; inspect durable job history.
3. Restore workers or dependencies, adjust proven per-tier limits, or reduce
   producers. Qualify a capacity increase against destination limits first.
4. Recovery means backlog falls below its threshold and eligible throughput
   windows meet the floor. A throughput alert clearing because the cohort went
   idle does not prove a stalled queue recovered.

## Policy budget

1. Inspect the dimension (cost or carbon), warning/critical threshold, and
   unknown-data signal. Read the registered budget periods, scopes, opening
   commitments, and immutable reservation evidence from the catalog.
2. Compare commitments with admission evidence and estimator profiles. Failed
   attempts retain charges; retry and override reservations legitimately increase
   usage. Do not subtract them merely to silence an alert.
3. Slow admission, use [what-if simulation](policy_budgets.md) to compare
   alternatives, or configure an approved replacement period/allowance through
   the audited budget workflow. Repair missing evidence before resuming work.
4. Recovery means the maximum applicable ratio falls below threshold for the
   configured interval, with no unknown scope remaining. An ended period alone
   is not a healthy replacement budget.

## Telemetry unavailable

1. Inspect Prometheus targets and the API `/healthz` and worker `/readyz`
   endpoints. Check service reachability, catalog access, and broker connectivity.
2. For unknown scan timestamps, verify broker metadata and UTC clock sync;
   investigate future timestamps before trusting apparently fast scans.
3. For budget unknowns, inspect missing/stale periods or unavailable ledger
   evidence. A reachable metrics endpoint does not guarantee the catalog read
   succeeded.
4. Confirm scrapes resume and unknown counters stop increasing. Reassess any SLO
   conclusions spanning the gap; do not fill missing history with healthy data.

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
