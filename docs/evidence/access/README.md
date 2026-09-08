# Access-history qualification evidence

Ticket [#43](https://github.com/melliott18/CogniStore/issues/43) is qualified by
[`sqlite-10000-20260908.json`](sqlite-10000-20260908.json), a real local SQLite DAL
run completed at `2026-09-08T19:53:29.252136Z`. The workload and comparison oracle
are in [`tests/perf/access_benchmark.py`](../../../tests/perf/access_benchmark.py).
The synthetic fixture is project-authored and requires no customer data.

The run used the ticket implementation in a dirty worktree based on
`2dcde3b70bf085c79d4b79ed9a93e280d6a92332`, CPython 3.13.7, SQLite 3.50.4, and
Darwin/arm64. SQLite used 4096-byte pages, `journal_mode=delete`, and
`synchronous=2`. The environment was an ordinary development machine with other
work potentially running; the results are observations, not throughput targets
or production SLOs. The report's fixture SHA-256 fixes event identities,
timestamps, kinds, coordinates, and declared sampling rates.

## Workload and measured result

- 10,000 unique events: 2,500 each of read, write, list, and touch, over 100 object
  coordinates plus the bucket-list namespace.
- Sixty days of fixture history, `as_of=2026-09-01T00:00:00Z`, 30-day retention,
  and 1-, 7-, and 30-day windows. Explicit boundary events exercise exclusion of
  exact lower bounds and one future event.
- 1,000 injected retained events declare a 0.25 sampling rate. The benchmark
  verifies weighted sums; it does not measure the recorder's selection process.
- 1,000 retry replays change occurrence time, source, tier, and correlation while
  preserving logical operation identity. A second 1,000 replay attempts after
  expiration produce zero added rows and zero resurrected events.
- 612 aggregate comparisons against an independent Python oracle pass, including
  raw/weighted counts, summary timestamps, minimum sample rate, exact namespace
  isolation, missing coordinates, and wider windows before/after expiration.

| Measurement | Observed result |
| --- | ---: |
| Committed single-writer appends | 732.1 events/second; 13.660 seconds total |
| Append latency p50 / p95 | 0.862 / 2.744 ms |
| Aggregate query latency p50 / p95 | 1.876 / 2.387 ms over 306 measured queries |
| Serialized compact event JSONL | 3,378,500 bytes |
| Database growth above migrated empty baseline | 6,066,176 bytes; 606.62 bytes/event |
| Initial expiration | 5,001 rows; at most 500 per batch |
| Initial expiration call latency p50 / p95 | 8.085 / 12.907 ms |
| Physical deletion after advancing the dedup horizon | 5,001 previously expired rows; at most 500 per batch |
| Advanced cleanup call latency p50 / p95 | 16.411 / 26.568 ms |

The initial expiration leaves 4,999 active events and all 10,000 stored
identities. In-retention aggregates remain unchanged; the extra-wide 60-day
view falls from 9,997 observed events to 4,998 because expired rows are excluded
regardless of the requested window. The discrepancy between active rows and
observed rows is the deliberately future-dated event. Before expiration the two
oldest events sit exactly on the 60-day boundary and are also excluded.

Advancing the maintenance cutoff by 30 days physically removes 5,001 old
identities and expires 4,997 newer events. The remaining 4,999 physical rows
include two still-active events, one exactly at the advanced cutoff and one in
the future. Each maintenance call can expire up to 500 rows and purge another
500; the report records the categories separately and drains until both are
zero. After cleanup SQLite has 659 free pages, while the file remains 6,451,200
bytes. No `VACUUM` or compaction was performed.

These results measure catalog operations only. They exclude driver and HTTP
latency, automatic sampling cost, PostgreSQL performance, concurrent writers,
and a sustained-production retention schedule. Allocated growth includes
indexes and page slack; serialized JSON is a separate logical-volume metric.

## Reproduce and verify

From the repository root with development dependencies installed:

```bash
python -m pytest tests/perf/test_access_benchmark.py
python -m tests.perf.access_benchmark \
  --events 10000 --objects 100 --query-runs 3 --prune-batch-size 500 \
  --output /tmp/cognistore-access-report.json
```

The harness creates and disposes a fresh SQLite database. It raises on an
incorrect aggregate, retry identity, expiration/purge count, or batch bound;
it only writes a `status: passed` report after all checks succeed. Latencies and
generation time vary per run, while fixture hash and correctness results remain
reproducible. The focused tests also inject an incorrect DAL result to ensure
qualification fails instead of producing a passing report.

From this evidence directory, verify the archived report:

```bash
shasum -a 256 -c SHA256SUMS
python -m json.tool sqlite-10000-20260908.json >/dev/null
```

Source checksums in `SOURCE_SHA256SUMS` identify the benchmark and catalog
implementation used for this artifact. Verify them from the repository root:

```bash
shasum -a 256 -c docs/evidence/access/SOURCE_SHA256SUMS
```

Operational semantics, safe policy defaults, configuration, and maintenance
examples are documented in [`access_history.md`](../../access_history.md).
