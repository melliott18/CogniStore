# M5 load, capacity and alert qualification

Ticket [#165](https://github.com/melliott18/CogniStore/issues/165) applies the
[selected pilot specification](production_pilot.md) to the exact candidate in
[isolated staging](staging.md). **Status: locally tested components; live
qualification is blocked.** No deployed environment, owner acceptance, real
operator notification or measured capacity recommendation is claimed.

The [original local record](evidence/m5/ticket-165/README.md) and
[automation validation record](evidence/m5/ticket-165-automation/README.md)
identify what was actually tested. Historical [movement qualification](scale_qualification.md)
and synthetic Prometheus tests cannot stand in for this campaign. Hosted CI is
`skipped: user instruction; known GitHub billing/spending restriction`.

## Inputs and responsibilities

Before live execution, supply the accepted #159 specification/owner review,
qualified #160 source/image/dependency identities and completed #161 deployment
handoff. The handoff must include restricted service access, two synthetic tenant
principals, trusted CA/OIDC setup, private telemetry, approved fault controls,
spend/expiry bounds and the accepted operator's actual notification route.
The current retained #159–#161 evidence does not supply these prerequisites.
Keep tokens, endpoints containing credentials, personal contacts and raw
service responses out of public evidence.

The checked-in [profile](../release/load/profile.json) freezes the engineering
targets from specification revision 1. It does not approve them on the owner's
behalf. Hash the profile before testing. Threshold or scope changes require a
reviewed specification revision and affected reruns; never alter a threshold
to make an existing failed run pass.

This implementation supplies the campaign driver and independently usable components:

| Component | Implemented behavior | Required deployment input |
| --- | --- | --- |
| `scripts/load_corpus.py` | Exact deterministic corpus and streamed manifests | Candidate native MIME/extraction evidence |
| `scripts/load_workload.py` | Bounded open-loop request scheduling and offered-slot records | Actual measured service traffic |
| `scripts/load_service.py` | HTTPS tenant adapter, bounded mutation pool, byte/catalog verification, job polling, audit checkpoints and owned-prefix cleanup | Service origin, trusted CA, rotating token files |
| `scripts/load_campaign.py` | Warm-up, soak, burst/recovery, capacity, movement, background jobs, fault hooks and stop controls | Accepted configuration and scoped fault commands |
| `scripts/load_observations.py` | Private Prometheus collection, source-age checks, latched stops, bounded fault/recovery execution and receipt validation | Private metric expressions and actual operator artifacts |
| `scripts/load_export.py` | Exact-byte import of bound deployment observations, exclusive outputs and hashes | Private collector spool in the evidence contract |
| `scripts/load_qualification.py`, `scripts/load_acceptance.py` | Client/resource arithmetic and supplemental acceptance gates | Actual platform observations and independent owner review |

The workload CLI remains a **synthetic transport fixture**. The campaign CLI
uses the real service adapter. Configuration examples contain unaccepted
placeholders and cannot launch a campaign. The base calculator always leaves full
qualification incomplete; the supplemental evaluator reports passing measurements
only when every supplied evidence gate passes. Both keep `production_qualified:
false`: deployment attestation and owner acceptance are separate decisions.

## Running the service campaign

Copy [staging.example.json](../release/load/staging.example.json) and
[telemetry.example.json](../release/load/telemetry.example.json) to restricted
operator storage. Fill every identity, accepted review, credential path, metric
expression and fault command from the #159/#161 handoff. Service and collector
origins require verified HTTPS; requests never follow redirects or inherit proxy
credentials. Token files reload for each request. Do not put tokens in JSON,
command arguments or public artifacts. Verify the 15-second query budget against
the selected Prometheus deployment before starting the long campaign.

```bash
python scripts/load_campaign.py preflight --config /private/pilot/staging.json --output /private/pilot/preflight.json
python scripts/load_campaign.py run --run-staging --config /private/pilot/staging.json --output /private/pilot/load-run-unique
python scripts/load_campaign.py evaluate --input /private/pilot/load-run-unique
```

Preflight validates configuration locally; it does not prove service reachability
or approvals. The run performs authenticated service checks before seeding. Use
fresh output paths. Failed runs retain planned windows and uncertain mutations;
there is no automatic resume. Reconcile retained objects/jobs before a separately
reviewed new run. Cleanup only deletes acknowledged objects in its own prefix
after complete reconciliation. Final capacity data remains for review.

Fault controls use explicit absolute executable/argument arrays without a shell
or inherited credential variables. Injection commands must hold and exercise the
fault until their experiment is complete; recovery runs even after timeout,
cancellation or injection failure. Recovery commands must be idempotent and able
to restore the scoped service independently. Hard termination of the host still
requires the staging operator's recovery procedure. Subprocess output is bounded
and omitted from public evidence. Only `{output}`, `{run_id}` and
`{binding_sha256}` are substituted.

Resource/queue stops remain latched after recovery. Faults must target the approved
isolated fault scope while retaining measurements of the protected workload. A
fault that trips the campaign stop threshold produces a stopped run requiring
investigation; it cannot silently resume to obtain a pass. No command acknowledges
an alert on the operator's behalf.

The configured export command runs the deployment's read-only exporter or the
supplied spool importer. The importer requires the supplemental files below,
already carrying actual run bindings. It rejects missing files, symlinks,
duplicate JSON keys, nonfinite numbers, reused output and mismatched identities.
It preserves exact bytes and never fills an unknown observation with zero.
Collector expressions, fault controls and platform/receiver exports remain
deployment inputs; the examples are contracts, not installed exporters.

The [supplemental evidence schema](load_evidence_schema.md) defines movement,
worker jobs/scans, reconciliation, resources, faults, receiver artifacts and
cost observations. Every row binds the run ID and canonical candidate bindings;
the final manifest binds the exact campaign windows and all input files. Retain
the sanitized source artifacts as well as normalized rows. Hash correspondence
detects mismatches; it does not attest that supplied observations are truthful.

## Repeatable local commands

Run from the implementation checkout with the development dependencies installed:

```bash
python -m pytest tests/unit/test_load_*.py
python scripts/load_corpus.py --output test-results/m5-corpus-reduced --objects 1000 --seed m5-pilot-v1
python scripts/load_workload.py --run-synthetic-fixture --cohort nominal --duration-seconds 10 --output test-results/m5-fixture.jsonl --summary test-results/m5-fixture-summary.json
bash docker/observability/verify.sh
```

Use fresh output paths; prior evidence is never overwritten. Fixture timings
describe only the scheduler, never service latency or throughput. Preserve the
raw records and summary together. Generating a manifest still generates and
hashes every payload; it is real CPU/I/O work even without storing the payloads.

For the selected datasets, after confirming local disk/memory/cost bounds:

```bash
python scripts/load_corpus.py --output test-results/m5-corpus-nominal --objects 100000 --seed m5-pilot-v1 --write-payloads
python scripts/load_corpus.py --output test-results/m5-corpus-capacity --objects 200000 --seed m5-pilot-v1 --write-payloads
```

The nominal payload is exactly 28,426,240,000 bytes and capacity is twice that.
Each tenant gets half the objects and bytes. Size bins are 4 KiB/64 KiB/1 MiB/
16 MiB at 60/30/9/1 percent; MIME counts are 80% opaque, 10% PDF and 10% DOCX.
PDFs contain real text/xref structures; DOCX files contain valid OOXML with
bounded normalized text, rather than arbitrary appended bytes. Joint histograms
are retained globally and per tenant. The 1,000-object reduced fixture has odd
large-document joint cells split between tenants; full/capacity datasets have
exact per-tenant joint distributions. Run native MIME and extraction checks on
the selected Linux candidate as part of seeding. Local parser tests alone do
not establish that production extraction handles every fixture.

## Connecting the request recorder

`await load_workload.run(Settings(cohort, duration_seconds, seed), transport,
record)` calls an async `transport(Request) -> Result` once per admitted arrival
and invokes `record(dict)` for every offered slot. It caps outstanding requests
at 16 nominal/capacity or 32 burst, with at most four PUTs. It neither queues
overflow nor retries submissions. A 30-second deadline includes delay since
scheduled arrival. The adapter must obey cancellation, check the full expected
response, and never turn an uncertain mutation into an automatic retry.
Recorder failures abort execution and preserve partial evidence for review.

Descriptors select the exact 100-request operation mix, operation-independent
size cycles, balanced tenants and 80% hot-spot requests. `selection` is a stable
fraction for choosing within the adapter's matching key pool. The adapter must
honor size/tenant/hotspot, including scarce 16 MiB objects, rather than choosing
an easier fallback. Materialize hot spots as 20% of each tenant's eligible keys.
Keep the initial dataset and a recorded bounded create/delete pool coherent;
use per-key coordination for ordinary traffic and label same-key/hold conflicts
as separate correctness cohorts. Observe storage placement while policy jobs
move objects. Missing matching keys or uncertain submissions are recorded
failures requiring reconciliation, not skipped arrival slots.

Use authenticated tenant APIs for object and catalog operations, metadata Ask,
policy preview and queued scans/policy passes. Full response verification must
include expected bytes/checksum or catalog semantics, not just HTTP status.
Keep redirects disabled, use verified TLS and bounded reads, refresh tokens
through approved credentials, and do not record Authorization or response text.
The adapter must check tenant `/metrics` remains 404. Do not point ordinary CLI
writers at the named tenants' namespace.

Records arrive in **completion order**. `sequence` is the unique, zero-based
offered slot within its cohort. Missing arrivals and concurrency rejections
have records even though the transport was never invoked. All offered slots
remain in the availability denominator, including an interrupted/partial file.
Before using a live adapter, test slow/reordered responses, fast errors, TLS
failures, invalid credentials, uncertain writes, cancellation and saturation
against the explicit staging setup.

## Campaign and evidence contract

Keep nominal, burst, capacity, dedicated movement and faults in distinct
measured windows. Start nominal after 30 minutes of warm-up, run 72 continuous
hours at 10 offered requests/s, and extend for minimum sample counts. Run burst
at 30/s for 15 minutes; measure return to nominal latency/queue age within ten
minutes. Run capacity at 200,000 objects for at least two hours and until the
round-trip movement pass finishes. Record actual UTC starts/durations including
maintenance, pauses and failures. The campaign driver enforces these stages around the reusable recorder.

Add one scan per tenant every five minutes over rotating prefixes of at most
100 objects and one policy pass per tenant per hour over at most 1,000 objects,
with at most two in-flight jobs. Move at least 10,000 objects each direction
during soak with the exact size mix. Poll every accepted job to a reconciled
terminal outcome. Separately run a movement-only backlog for at least an hour
and at least 10,000 verified moves in each direction. Preserve every attempt,
retry and five-minute window, including stalled windows. Required ratios are
in the [specification](production_pilot.md#numerical-qualification-gates).

For client/resource calculation, copy the
[campaign template](../release/load/campaign.example.json) to a fresh evidence
directory as `campaign.json`. Replace placeholders with actual bindings and
UTC windows, set `execution_scope` to `isolated-staging` only for actual staging,
and record the profile SHA-256. Collect these two files in the same directory:

| File | Required fields |
| --- | --- |
| `foreground.jsonl` | `cohort`, unique integer `sequence`, `scheduled_seconds`, `elapsed_seconds` since arrival, `actual_seconds` for observed responses, `operation`, `tenant`, boolean `success`, fixed `outcome`; `size_bytes` for PUT/GET. The request recorder emits this schema. |
| `telemetry.jsonl` | `cohort`, actual `seconds` since the window start; numeric finite fields below, at least once per 15-second bucket in time order. Missing/unknown observations must remain gaps, never fabricated zeros. |

Telemetry fields are `api_cpu_ratio`, `worker_cpu_ratio`, `api_rss_ratio`,
`worker_rss_ratio` (relative to each configured limit), `hot_free_ratio`,
`catalog_free_ratio`, `broker_free_ratio`; `api_rss_bytes`, `worker_rss_bytes`,
`api_tmp_bytes`, `worker_tmp_bytes`, `api_shm_bytes`, `worker_shm_bytes`;
`api_connections`, `worker_connections`, `catalog_connections`,
`broker_connections`; `queue_pending`, `queue_oldest_seconds`, `queue_bytes`;
`hot_used_bytes`, `catalog_used_bytes`, `broker_used_bytes`,
`ingress_bytes_total`, `egress_bytes_total`. Counters require reset-aware
interpretation for throughput/cost; the calculator reports ranges, not an
invented physical-bandwidth result. Retain original private collector exports
with their source/sample timestamps and hashes as well as these normalized rows.

```bash
python scripts/load_qualification.py --input test-results/m5-observations --output test-results/m5-evaluation.json
```

The evaluator uses temporary SQLite for exact nearest-rank percentiles without
holding millions of samples in RAM. Provision report scratch disk independently
of the service. It accepts out-of-order completions but rejects duplicate or
out-of-window slots, invalid/nonfinite values, duplicate JSON keys and unknown
outcomes. It scores failures at least 30 seconds while retaining actual observed
duration in the raw file. Missing records count against availability and make
per-bin latency inconclusive. Percentiles are per operation and read/write size
bin. Burst results do not affect nominal/capacity SLOs. Empty denominators,
missing identities, unknown queue age and telemetry gaps cannot pass.

The fixed whitelist of report fields excludes tokens, endpoint strings and
exception text. It records raw input/profile SHA-256s; caller-supplied identity
strings are checked for structure, not attested against a cluster. Retain the
source revision and command/environment with the output. `status` is failed
when a measured check fails, otherwise incomplete pending independent evidence.
`automated_status` can describe passed arithmetic without approving the campaign.

## Capacity, faults and real alert delivery

The [queue runbook](operational_slos.md#queue-capacity) now covers reachable
depth and byte limits: warn at 5,000 pending or 50% bytes for two minutes; stop
new submissions at 8,000 or 80% bytes. The hard 10,000-message/1 GiB caps remain.
Oldest pending age >300 seconds for five minutes and any confirmed broker
admission rejection also alert. Alerts do not themselves stop admissions: the
controlled adapter/operator must enforce the selected stop/resume policy.

Worker byte utilization comes from live stream information; broker rejection
counters are process-local. The tenant API's aggregate metrics stay disabled.
The staging handoff must supply a private producer/client collector and a
private, current **oldest pending age** measurement. Publication-to-claim
histograms do not measure the oldest unclaimed message. A missing collector
blocks acceptance; a Prometheus rule referencing a metric is not collection.

Exercise message and byte saturation separately, holding unrelated work safe;
retain rejected request IDs/outcomes, stream state, serialized envelope sizes
and backlog until reconciled. Do not purge diagnostic work to force a green
graph. Test bounded retries, actual backend throttling/disconnect and recovery
using approved fault controls. Record start, failure, restored health, drain,
checksums/catalog/audit/hold/tenant verification and operator runbook actions.

For **every** induced alert retain sanitized alert ID, threshold crossing,
firing, actual receiver receipt, operator acknowledgement, working runbook
reference/execution, clearing and actual recovery receipt times. Require receipt
within five minutes, acknowledgement within fifteen and recovery delivery within
five. Test the real route with the accepted operator; local rule tests and
in-memory callbacks cannot satisfy this gate. Include private telemetry access
and tenant-isolation negative checks without enabling public aggregate metrics.

After nominal/capacity drain, compare RSS/tmp/shm with post-warm-up baselines;
investigate growth, open connections, retries, S3 versions/multipart parts,
catalog/audit size and broker/DLQ storage. Require 30% free hot/catalog/broker
disk, CPU/RSS p95 below 70%, no exhaustion/leaks and RSS/tmp within 110% baseline.
Enforce the pilot's temporary-memory, storage, queue and failure stop thresholds.
GCS spool allowance remains zero because GCS is excluded; SQLite scheduler and
keyword index are disabled. Enabling any requires a revised scope and rerun.

Collect actual instance/volume/storage-version/backup/transfer/request usage
and pricing inputs with dated references; do not invent cost or scaling results.
The selected single-node POSIX topology has no demonstrated horizontal-scaling
claim. Recommendations must link measured nominal/doubled-corpus headroom and
failures to a supported topology, with blocker fixes and affected reruns.

Retain failed/partial runs alongside successful evidence in the repository or
durable restricted storage with checksums and at least the M5 retention period.
Complete the [M5 evidence contract](evidence/m5/README.md), link each independent
gate and obtain the accepted owner's decision. This implementation does not
close #165 or authorize pilot entry.
