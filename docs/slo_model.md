# Operational SLO model, version 1

The machine-readable source is [`cognistore.slo.SLO_DEFINITIONS`](../cognistore/slo.py).
Each objective has an accountable maintainer group, an explicit good/total
formula, a measurement source, a target, and a review window. These initial
targets are engineering objectives for this deployment profile, not a customer
SLA. Owners review the rolling 30-day window weekly and after incidents; target,
cohort, or threshold changes require a new model version and corresponding rule
and dashboard changes.

| ID | Owner | Good events / eligible events | Target | Data source |
| --- | --- | --- | --- | --- |
| `move_success` | Storage / movement maintainers | Successful completed movement calls / all completed movement calls | 99.9% | `cognistore_operations_total{component="movement",operation="move"}` |
| `move_latency` | Storage / movement maintainers | Successful calls completed within 30 seconds / all completed movement calls | 99% | Movement duration histogram's successful `le="30.0"` bucket and histogram count |
| `move_throughput` | Storage / worker maintainers | Eligible 5-minute window samples achieving at least 1 successful move/second / all eligible window samples | 99% | Movement counter and `cognistore_job_queue_depth{state="pending"}` |
| `api_availability` | API / platform maintainers | Non-5xx matched `/v1` HTTP completions / all matched `/v1` HTTP completions | 99.9% | `cognistore_http_requests_total` |
| `indexing_lag` | Catalog / indexing maintainers | Successful scan attempts completed within 300 seconds of original broker publication / all completed scan attempts with a known publication timestamp | 99% | `cognistore_indexing_lag_seconds` histogram |

Movement uses completed **calls**, including retries and idempotent replays,
not deduplicated logical jobs. Failed and cancelled calls are bad outcomes for
both movement ratios. A later successful retry is another observation. Durations
are inclusive of the threshold. A hard-killed process cannot emit a completion;
queue, process-health, and telemetry coverage alerts remain necessary.

API availability counts registered `/v1` route templates, including expected 4xx
responses; `/healthz`, `/metrics`, and unmatched routes are excluded. It describes
completed requests visible to the process. A process that never receives traffic
or dies without emitting a completion supplies no availability observation.

Indexing lag ends when a scan attempt completes and starts at its original
broker publication time, so queueing and retry age are included. Failed or
cancelled attempts are bad even when quick; successful retry completion does not
erase earlier failures. A coordinated replay skipped because its logical run is
already terminal does not execute a scan and supplies no scan observation.
Unknown, invalid, or future publication timestamps are
excluded from this histogram and counted separately as unavailable observations.
That coverage gap must not become evidence of meeting the objective. This is a
scan-completion age objective, not an object-mutation-to-search-visibility metric.

Throughput eligibility requires pending broker work continuously over the
5-minute window plus at least one observed completed movement attempt within
that window. Each recording-rule evaluation contributes one overlapping window
sample. This measures sustained demand rather than penalizing idle deployments.
The queue is shared with other operations: a mixed scan/movement backlog can
qualify, while a complete stall ages out after the last movement completion.
Use backlog/capacity alerts alongside the SLO; this proxy does not isolate a
movement-only backlog or prove progress during total stalls. Missing queue or
movement telemetry is unavailable, not good throughput.

## Error budgets

For each ratio, `allowed_bad = eligible * (1 - target)`,
`burn_rate = (bad / eligible) / (1 - target)`, and
`budget_remaining_ratio = 1 - bad / allowed_bad`. Negative remaining budget
shows overspend. Throughput uses eligible window samples instead of requests.
No eligible events or missing observations produce **unavailable**, never 100%
success. An observed short window does not establish 30 days of coverage.
[`evaluate_ratio`](../cognistore/slo.py) evaluates exact supplied event or window
counts using this arithmetic; callers are responsible for the stated cohort and
window. Deployment rules, alert handling, and dashboard instructions are in
the [operational SLO guide](operational_slos.md).

## Reproduce the retained M1 baseline

From the repository root, with CogniStore's Python environment activated:

```bash
python -m cognistore.slo evaluate-qualification \
  docs/evidence/m1/full-20260827-205845.json
```

The command emits `cognistore.slo-evaluation` JSON schema version 1 and model
version 1. Exit codes are `0` for movement baseline comparisons met, `1` for a
movement baseline comparison not met, and `2` for unavailable or inconclusive
evidence. Output is always labeled `qualification_baseline`; API availability,
indexing lag, rolling 30-day attainment, and throughput window attainment remain
unavailable. A reduced report keeps `evidence_profile: reduced` and
`reported_full_scale_qualification: false`, even when baseline comparisons pass.
The report includes the source run ID, timestamps, revision, dirty flag, and
original input file's SHA-256 digest (CLI). It labels qualification verification
`input_consistency_only`: a reported full profile is not independent verification
of topology, clean revision, canonical workload, or the fault-injection campaign. The
evaluator validates the fields it consumes and their consistency; it does not
rerun qualification or authenticate arbitrary evidence files. The archived
[M1 evidence index and checksum](evidence/m1/README.md) establish provenance.

The M1 harness retains logical success counts, attempt counts, phase-average
throughput, and nearest-rank logical latency quantiles. It does not retain the
live per-call histogram. The evaluator therefore makes explicit comparisons:

- **Success proxy:** logical completed moves / attempts. Injected failures count
  in the denominator. Replays and the hard-killed child have different telemetry
  coverage from live completed-call instrumentation, so this is not an exact
  runtime event ratio.
- **Latency proxy:** successful logical moves within 30 seconds / attempts.
  Logical durations include retries/backoff and exclude executor queue wait.
  A quantile at or below the threshold proves at least its nearest-rank count
  good; a quantile above it bounds good samples below that rank. The maximum
  below threshold proves every logical success is good. Bounds straddling the
  objective are inconclusive. These describe logical durations; a slow logical
  move may still contain a quick successful final call.
- **Throughput baseline:** each direction's completed logical objects / elapsed
  direction seconds compared with the 1 move/second floor. A passing phase
  average provides no eligible 5-minute window counts or window error budget.

The retained full campaign has four million logical moves and twelve failed
attempts across four direction/path cohorts. Every cohort meets the success and
latency proxy thresholds; the slowest phase averaged approximately 27.15 moves
per second and the largest logical latency was approximately 5.59 seconds.
These are historical baseline observations from the retained POSIX/MinIO
configuration, not evidence that an arbitrary deployment meets production SLOs.
The offline regression tests load this exact archived report and exercise
missing, nonfinite, contradictory, reduced, breached, and ambiguous evidence.
