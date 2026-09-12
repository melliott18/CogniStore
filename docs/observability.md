# Local observability

CogniStore exposes Prometheus metrics at the API and worker `/metrics`
endpoints. OpenTelemetry traces connect an HTTP request to its durable queue
publication, worker attempt, and nested policy, movement, storage, catalog,
and index operations. Structured telemetry events go to stderr so CLI JSON
results remain on stdout.

## Start the local stack

From the repository root:

```bash
COGNISTORE_OTEL_ENABLED=true docker compose --profile observability up --build --wait
```

The optional profile adds a REST API using the worker's drivers, PostgreSQL
catalog, and NATS queue, plus Prometheus, Grafana, and Jaeger. The regular
worker stack remains usable without this profile or a trace exporter.

| Service | Default local URL | Purpose |
| --- | --- | --- |
| API | `http://127.0.0.1:8082` | REST API, `/healthz`, `/metrics` |
| Worker | `http://127.0.0.1:8081` | `/healthz`, `/readyz`, `/metrics` |
| Grafana | `http://127.0.0.1:3000/d/cognistore-operations` | Provisioned **CogniStore operations** dashboard |
| Prometheus | `http://127.0.0.1:9090` | Query metrics and inspect scrape targets |
| Jaeger | `http://127.0.0.1:16686` | Search and inspect traces |
| OTLP/HTTP | `http://127.0.0.1:4318/v1/traces` | Local trace ingestion endpoint |

Grafana's development login is `cognistore` /
`cognistore-development-only`. Override the initial password with
`COGNISTORE_GRAFANA_PASSWORD` before creating its data volume. All published
ports bind to loopback. Override ports with `COGNISTORE_API_PORT`,
`COGNISTORE_HEALTH_PORT`, `COGNISTORE_GRAFANA_PORT`,
`COGNISTORE_PROMETHEUS_PORT`, `COGNISTORE_JAEGER_PORT`, and
`COGNISTORE_OTLP_PORT` if another local project uses them. For example:

```bash
COGNISTORE_OTEL_ENABLED=true COGNISTORE_GRAFANA_PORT=13000 \
  COGNISTORE_OTLP_PORT=14318 \
  docker compose --profile observability up --build --wait
```

Changing the published OTLP port does not change the Compose-internal endpoint
`http://jaeger:4318/v1/traces`. The content-search `sample` profile has its own
API on port 8080 and separate sample driver paths; use the observability API
on port 8082 for the queued-work walkthrough below.

Prometheus scrapes the API and worker every five seconds and retains seven
days of metrics in its named volume. Grafana stores its state in a named
volume and provisions the dashboard and Prometheus/Jaeger data sources from
`docker/observability/`. Jaeger stores traces in memory: restarting Jaeger
clears them. The pinned upstream images use the documented
[Prometheus configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/),
[Grafana file provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/),
and [Jaeger all-in-one OTLP receiver](https://www.jaegertracing.io/docs/2.20/getting-started/).
There is no hosted backend, alert policy, or SLO configuration in this profile.

## Trace an API request through queued work

This Python 3 walkthrough uses a unique object key, scans it through NATS,
then runs a simple placement policy that moves it from `hot` to `warm`.
It prints the trace IDs to search in Jaeger. Each action has a distinct trace;
its queued work continues the submitting request's trace even after the API
returns `202 Accepted`.

```python
import json
import time
from urllib.request import Request, urlopen
from uuid import uuid4

base = "http://127.0.0.1:8082"
key = f"demo-{uuid4().hex}.txt"


def request(method, path, payload=None, trace_id=None):
    headers = {}
    body = payload
    if isinstance(payload, dict):
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if trace_id:
        headers["traceparent"] = f"00-{trace_id}-0123456789abcdef-01"
        headers["X-Request-ID"] = str(uuid4())
    with urlopen(Request(base + path, body, headers, method=method), timeout=10) as response:
        result = response.read()
        return json.loads(result) if result else None


def wait_for_job(job):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status = request("GET", job["status_url"])
        if status["status"] == "succeeded":
            return
        if status["status"] == "failed":
            raise RuntimeError(f"Job failed: {status['job_id']}")
        time.sleep(0.5)
    raise TimeoutError(f"Job did not finish: {job['job_id']}")


request("PUT", f"/v1/objects/hot/observability/{key}", b"Local trace walkthrough.\n")
scan_trace = uuid4().hex
wait_for_job(request("POST", "/v1/actions/catalog-scans", {
    "tier": "hot", "bucket": "observability", "prefix": key,
}, scan_trace))
move_trace = uuid4().hex
wait_for_job(request("POST", "/v1/actions/policy-runs", {
    "bucket": "observability", "prefix": key,
    "config": {"policy": "simple", "threshold": 1, "allowed_tiers": ["hot", "warm"]},
}, move_trace))
request("HEAD", f"/v1/objects/warm/observability/{key}")
print("Scan trace:", scan_trace)
print("Movement trace:", move_trace)
```

Allow a few seconds for the batched exporter. Open a trace by ID in Jaeger,
or select the `cognistore-api` / `cognistore-worker` service in search. The scan
trace includes `api.request`, `queue.enqueue`, `job.scan`, driver calls, and
catalog operations. The policy trace adds `policy.*` and `movement.move`.
Index spans appear when the configured scan/retrieval path uses an extractor,
keyword index, or embedding provider; the baseline Compose drivers do not
configure every optional provider.

The Grafana dashboard shows movement bytes/second and moves/second, failures,
queue depth, request rates, and p95 request/operation/queue latency. Repeat
the walkthrough to create more samples. A quick job can finish between
scrapes, so queue depth can remain zero. New counter label sets need two
scrapes before a rate exists. Unused operations and idle histogram
percentiles may show **No data**; the scrape-availability panel distinguishes
that from an unreachable service. A broker probe failure makes depth `NaN`,
not a misleading zero.

## Process configuration

For host processes, install the package as usual and configure both API and
worker before starting them:

```bash
export COGNISTORE_LOG_FORMAT=json
export COGNISTORE_OTEL_ENABLED=true
export COGNISTORE_OTEL_ENDPOINT=http://127.0.0.1:4318/v1/traces

OTEL_SERVICE_NAME=cognistore-api cognistore-api \
  --drivers ./drivers.yaml --catalog-db ./catalog.sqlite3 --port 8082
```

In another shell with the same telemetry variables:

```bash
OTEL_SERVICE_NAME=cognistore-worker cognistore \
  --drivers ./drivers.yaml --catalog-db ./catalog.sqlite3 \
  --nats-url nats://127.0.0.1:4222 worker
```

Use a reachable NATS service and matching catalog/driver configuration in both
processes. These host examples use a host SQLite catalog and are separate from
the Compose PostgreSQL catalog. See [worker setup](background_workers.md) for
queue and persistent-state configuration. To scrape host processes, replace
the two targets in `docker/observability/prometheus.yml` with addresses
reachable from the Prometheus container; Docker Desktop exposes the host as
`host.docker.internal`.

| Setting | Behavior |
| --- | --- |
| `COGNISTORE_OTEL_ENABLED` | `true`, `1`, or `yes` enables batched OTLP HTTP/protobuf trace export; disabled by default. |
| `COGNISTORE_OTEL_ENDPOINT` | Exact trace URL, including `/v1/traces`; host default is `http://localhost:4318/v1/traces`. Credentials, query strings, and fragments in this URL are rejected. |
| `OTEL_SERVICE_NAME` | `cognistore`, `cognistore-api`, or `cognistore-worker`; other values fall back to `cognistore`. |
| `COGNISTORE_LOG_FORMAT` | `json` enables structured operational events on stderr; Compose sets it by default. |

Metrics collection and `/metrics` do not require trace export. Exporter
configuration or delivery failure does not fail application work. The local
trace SDK uses parent-based sampling, so an incoming valid unsampled parent
remains unsampled; the walkthrough sends the sampled flag `01`. Metrics
remain independent of trace sampling. Application code does not install
automatic HTTP, SQL, or storage SDK instrumentation that might record URLs,
SQL parameters, or payloads.

Metrics are process-local and counters reset on process restart. Run one API
process per scrape target and scrape every worker process. Combining
multiple processes behind one `/metrics` endpoint without a multiprocess
registry loses accurate counter continuity; this profile uses one API and one
worker. The shared durable consumer depth is repeated across workers and
must be aggregated with `max`, not summed.

## Metric contract and cardinality

| Metric | Type and unit | Labels | Meaning |
| --- | --- | --- | --- |
| `cognistore_operations_total` | Counter, operations | `component`, `operation`, `outcome` | Completed instrumented operation attempts, including failures. |
| `cognistore_operation_duration_seconds` | Histogram, seconds | `component`, `operation`, `outcome` | Wall-clock operation duration; nested durations overlap. |
| `cognistore_http_requests_total` | Counter, requests | `method`, `route`, `status_class` | Completed responses, including health probes; excludes metrics scrapes. |
| `cognistore_http_request_duration_seconds` | Histogram, seconds | `method`, `route`, `status_class` | Full response time, including streamed bodies. |
| `cognistore_job_events_total` | Counter, events | `event`, `operation` | Job lifecycle events; not unique jobs. |
| `cognistore_job_queue_depth` | Gauge, messages | `state` | Broker consumer pending or delivered/unacknowledged messages; `NaN` when unavailable. |
| `cognistore_job_queue_latency_seconds` | Histogram, seconds | `operation` | Broker publication to the current attempt claim, including earlier attempts and retry delay. |
| `cognistore_movement_bytes_total` | Counter, bytes | None | Source bytes after a destination transfer returns successfully, before verification; retried transfers count again, recovery without transfer does not. |

All durations use the same fixed bucket upper bounds, in seconds:
`0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300, +Inf`.
Histograms expose `_bucket` (with the additional bounded `le` label), `_sum`,
and `_count`; the client also emits creation timestamps for counters and
histograms. Quantiles above the largest finite bucket cannot resolve tail
latency beyond 300 seconds.

Labels have these fixed vocabularies:

- `component`: `api`, `driver`, `catalog`, `index`, `policy`, `movement`,
  `queue`, `job`, or `other`.
- `operation`: `request`, `put_object`, `get_object`, `open_object_reader`,
  `open_object_reader_if_generation`, `put_object_stream`, `delete_object`,
  `delete_object_if_generation`, `ensure_object_durable`, `list_objects`,
  `stat_object`, `transaction`, `read`, `extract_bytes`, `extract_stream`,
  `replace`, `delete`, `search`, `rebuild`, `embed`, `query`, `plan`,
  `evaluate`, `execute`, `move`, `recover`, `enqueue`, `dead_letter`,
  `redrive`, `scan`, `run_policy`, or `other`. Job metrics restrict this
  further to `scan`, `move`, `run_policy`, or `other`. Handler names and
  driver class names are never dynamically registered as labels.
- `outcome`: `success` or `error`.
- `method`: `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`, or
  `other`.
- `route`: registered application route templates, capped at 100 templates
  of at most 200 characters, plus `unmatched`; never the request URL/path.
- `status_class`: `1xx` through `5xx`, or `other`.
- Job `event`: `enqueued`, `started`, `succeeded`, `failed`, `retried`,
  `dead_lettered`, `redriven`, `cancelled`, or `other`.
- Queue `state`: `pending` or `in_flight`.

Unknown categorical values collapse to `other`, and unregistered routes to
`unmatched`. Object counts, arbitrary paths, request IDs, trace IDs,
correlation IDs, bucket/key names, tier/pool names, policy/model names, queries,
and exception text cannot add metric label values. For example, each HTTP
counter is bounded by `8 × 101 × 6 = 4,848` label combinations per process;
the general operation counter is bounded by `9 × 32 × 2 = 576` combinations.
Only observed combinations are allocated. Prometheus adds its configured
`job` and `instance` scrape labels independently. Existing per-tier limiter
snapshots remain available in worker health diagnostics; they are not copied
into these Prometheus families as arbitrary tier labels.

A retry can increment operation failures and job retry events before eventual
success. A failure observed by multiple nested components contributes at each
boundary. Neither sum is a count of unique failed objects. Movement call throughput
counts successful calls, including successful idempotent replays. Byte
throughput counts successful destination transfers, before verification; a
retry that transfers again adds bytes, while recovery of an existing
destination does not. It does not measure physical network traffic. Use the durable
[audit history](audit_events.md) for unique job/object accounting.

## Trace context and redaction

The API accepts W3C `traceparent`. Queue publication captures the producer
span's context in the job envelope; each delivery extracts it in a fresh
scope before starting its consumer span. Legacy envelopes without trace
context still execute and start a new trace. Retries retain the logical
correlation identity. `baggage` and `tracestate` are neither recorded nor
forwarded.

Only fixed operation/backend categories, bounded route templates, safe
numeric measurements, outcome/status, and canonical context IDs are emitted
by CogniStore telemetry. Non-UUID correlation values become deterministic
UUIDs before they reach telemetry. Correlation IDs and trace/span IDs belong
in logs/spans, never metric labels. Storage identifiers, credentials, DSNs,
object metadata, object contents, extracted text, user questions, prompts,
responses, raw request headers, and exception messages/tracebacks are omitted.
Span failures set an error status without serializing the exception.
Resources contain an explicit service name, without automatic host or
environment attribute discovery.

The JSON formatter uses an allowlist and omits raw message text, arguments,
and tracebacks from configured application/Uvicorn/exporter logs. This
contract covers CogniStore's telemetry and configured logging paths; an
embedding application's independent handlers or third-party auto-instrumentation
must apply the same filtering policy. Durable catalog/audit data remains a
separate operational record with its own access and retention rules.

Inspect structured events with:

```bash
docker compose --profile observability logs --no-log-prefix api cognistore
```

For troubleshooting, first check Prometheus **Status → Targets** and the
worker `/readyz` endpoint, then verify the exporter variables in both
processes and search the caller-supplied trace ID. If an API trace is present
but its queued work is absent, check worker readiness, queue connectivity,
and job status. Shut the stack down with:

```bash
docker compose --profile observability down
```

Ordinary shutdown preserves named volumes. The
[Docker setup guide](setup_guide.md) documents the separate, destructive
volume-reset command.
