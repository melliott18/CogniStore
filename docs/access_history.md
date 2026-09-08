# Object access history

Access history supplies behavioral evidence for placement policies. The catalog
keeps demand events separately from audit events and object metadata, so a scan,
move, or metadata refresh cannot silently turn a cold object into a hot one.
History follows the logical `(bucket, key)` coordinate across tier changes.

## What an event means

| Kind | Observation | Coordinate |
| --- | --- | --- |
| `read` | An observed consumer starts reading object bytes successfully, including a successful EOF for an empty object | Object |
| `write` | A user write completes successfully | Object |
| `touch` | A user requests object metadata successfully | Object |
| `list` | A user lists a bucket namespace successfully | Bucket; key is absent |

A read measures observed demand, not proof that the client received the complete
object. Once the first read succeeds, a later disconnect or stream failure does
not retract the event. Opening a stream without consuming it, a failed open, and
a failed initial read do not count. Internal `stat` calls made while serving a
read or write do not add touches. A bucket listing does not heat every returned
object.

The versioned REST API captures object GET/PUT/HEAD and catalog object lookup/list
requests. Deletes, Ask retrieval, indexing, scans, moves, and policy evaluation
do not imply consumer demand and do not emit access events. Direct storage
access is observable only through an explicitly installed
`ObservedStorageDriver`; opening files or accessing S3 outside that wrapper is
outside this history. An empty history therefore never proves that an object
has no users.

## Correlation and retries

API requests carry `X-Request-ID` as their correlation ID. Set `Idempotency-Key`
when retrying a logical access operation; a valid value contains 1–128 letters,
numbers, periods, underscores, colons, or hyphens. Without it, the request ID is
the operation ID. Reusing a correlation ID is useful for tracing; clients should
use a stable idempotency key to identify retries independently of tracing.

The event identity combines the operation ID, kind, bucket, and key. Repeated
capture of the same operation produces one retained event, including capture at
multiple observation layers. Tier and source do not change that identity. Use a
new operation ID for a new access, even if it fetches identical bytes. API
idempotency here applies to access counting; it does not cache HTTP responses or
make storage writes transactional.

For direct POSIX or S3 access, wrap the configured driver and bind a stable
operation context around each logical request. Reading or exhausting a listing
later retains the context from when that operation started. A direct listing
counts only when its iterator is exhausted successfully. An access-event append
failure propagates; the caller cannot assume recording succeeded merely because
the storage operation already completed.

```python
from cognistore.core.access import AccessConfig
from cognistore.drivers.observed import ObservedStorageDriver, access_operation

# raw_driver is the configured POSIX or S3 driver; catalog is an open catalog.
observed = ObservedStorageDriver(raw_driver, catalog, tier="hot", config=AccessConfig())
with access_operation(operation_id="download:report:123", correlation_id="request:123"):
    with observed.open_object_reader("reports", "annual.pdf") as reader:
        first_chunk = reader.read(65536)
```

The first persisted occurrence time and provenance win on replay. Stream events
freeze the request context at invocation and stamp the occurrence time when the
first read succeeds; they do not measure stream completion time. Writes and
listings use their successful completion time. Retry identity is bounded by the
retention and deduplication horizons described below.

## Configuration and policy interpretation

The `access_history` section in `drivers.yaml` configures window sizes, retained
history duration, sampling, and freshness. Windows and retention are in seconds.
Omitting the section uses these defaults:

```yaml
access_history:
  windows_seconds: [3600, 86400, 604800]
  retention_seconds: 2592000
  sample_rate: 1.0
  freshness_seconds: 86400
```

Supply 1–16 distinct positive windows, each no longer than retention. Durations
are integer seconds, at most 315360000. Sampling must be between `1e-9` and `1`,
inclusive. Unknown settings, invalid rates, and duplicate windows are rejected.
The API and CLI/worker policy loaders use the same configuration; callers
constructing an observed driver directly pass its `AccessConfig` explicitly.

Policy feature projections expose `features.access`, also serialized as
`features.access` in policy evaluation results. Its schema version is `1`:

| Field | Meaning |
| --- | --- |
| `as_of` | One timezone-aware evaluation instant shared by all objects in a policy load |
| `windows` | Counts by kind under string-valued window lengths in seconds |
| `estimated_windows` | Per-kind sums of `1 / sample_rate` for retained events |
| `observed_events` | Number of observed events in the configured retention horizon |
| `observed_since` / `last_access_at` | Earliest/latest retained observation; neither is a collection-coverage guarantee |
| `recency_seconds` | Seconds since the last observed read, write, or touch for this object; `null` if unknown |
| `freshness` | `fresh`, `stale`, `missing`, or `unavailable` |
| `sampling` | Configured rate, minimum retained event rate, and whether sampling applies |
| `missing` / `partial` | No observed events / incomplete coverage; `partial` is always `true` |
| `coverage` | Always `observed_operations_only` |

Windows use event time with the exact interval `(as_of - window, as_of]`.
Future events and events at the lower bound are excluded. Retention applies the
same interval to summary fields even before physical cleanup. For repeatable
fixture evaluation pass `as_of` explicitly to `compute_access_features` or
`CatalogPolicyFeatureLoader.load`; timestamps must include a timezone.

Sampling is deterministic from the event identity, so retries with the same
sampling configuration make the same sampling decision. The source records each
selected event's rate alongside the event. Raw counts are observed accesses;
weighted estimates are estimates and cannot establish a complete history. A
zero-count sample cannot establish zero demand.

Freshness is the age of the latest observed event against `freshness_seconds`;
it is not a collector-health heartbeat. A stale history can contain valid older
observations. Empty history returns zero counts, `null` recency/timestamps,
`missing: true`, and `freshness: missing`. An inaccessible catalog returns the
same unknown numeric defaults with `freshness: unavailable` and
`reason: history_unavailable`; a loader without history configured uses
`reason: history_not_configured`. Backend error text is not exposed to policies.

Missing, sparse, stale, sampled, and unavailable history are incomplete evidence.
Policies must check the feature state and coverage before using a zero count as
a reason to demote or delete an object. Unknown last-access timestamps remain
unknown. Maintenance activity and policy feature reads do not refresh demand
recency.

## Retention maintenance

Queries enforce the configured horizon immediately. Physical cleanup is an
explicit catalog maintenance operation; normal reads and writes do not run a
background pruning schedule. Use the deployment's maintenance scheduler to
invoke bounded cleanup with the same retention configuration:

```python
from datetime import datetime, timedelta, timezone

config = AccessConfig()
cutoff = datetime.now(timezone.utc) - timedelta(seconds=config.retention_seconds)
while catalog.prune_access_events(
    cutoff, limit=1000, retention_seconds=config.retention_seconds
):
    pass
```

Each call expires at most `limit` active rows whose occurrence time is strictly
before the cutoff. It also physically deletes at most `limit` already-expired
rows older than `cutoff - retention_seconds`. The returned count includes both
steps, so it can reach `2 * limit`; zero means the current pass found no work.
The limit must be between 1 and 10000. Newly expired rows remain stored but are
excluded from all aggregates, even if a later query requests a longer window.

Keeping expired identities for an additional retention horizon prevents a late
retry from resurrecting expired demand. The first event, including its timestamp
and provenance, remains stored during that horizon. After physical deletion,
the same operation identity can be accepted as a new observation. Deduplication
therefore does not have an unlimited lifetime. A 30-day configuration retains
expired rows for roughly another 30 days when maintenance runs promptly; disk
usage also depends on maintenance cadence. SQLite cleanup frees reusable pages
but does not shrink the database file without separate compaction.

## Qualification

The reproducible SQLite harness and archived measurements live in
[`evidence/access`](evidence/access/README.md). It checks deterministic window
aggregates against a Python oracle before and after bounded retention pruning,
retry deduplication, raw and sample-weighted counts, namespace isolation, and
unknown coordinates. It measures committed appends, repeated aggregate queries,
retention batches, serialized event volume, and allocated database growth.

These measurements describe the declared local SQLite workload. They do not
measure production PostgreSQL throughput, remote storage latency, HTTP streaming
latency, or concurrent writers.
