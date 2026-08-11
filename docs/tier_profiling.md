# Tier profiling and data-driven policies

CogniStore can profile each storage tier automatically (no manual input) and use the measured performance to inform placement policies.

## What gets measured

For each tier path (e.g., a POSIX mount), the profiler gathers:
- Capacity: total and free bytes, filesystem block size
- Sequential write throughput (MB/s) using a modest temp file
- Sequential read throughput (MB/s)
- First-byte latency (ms): open + read 1 byte
- Random read IOPS: many small (4 KiB) reads at random offsets

Notes:
- Measurements run against a small temporary file under the tier path and are deleted afterwards.
- Results reflect OS page cache; they are useful for relative comparisons between tiers.
- Sizes are intentionally modest to avoid undue wear on flash devices.

## Producing metrics

Use the `tier-profile` command with your drivers config. It prints a one-line summary per tier and can write a JSON file with all metrics using `--metrics-out`.

## Using metrics in policies

`policy-run` can optionally consume a metrics JSON via `--metrics-in`. When present, it derives a hot↔warm size threshold using a simple break-even model based on first-byte latency and sequential throughput. That threshold is applied to:
- `SimplePolicy(size_threshold=...)`
- `ContentAwarePolicy(size_threshold=...)` (name/MIME rules still take precedence)

If no metrics are provided, existing defaults and CLI `--threshold` behave as before.

## Limitations and tips

- Currently the profiler supports POSIX paths. Non-POSIX drivers are skipped.
- For small-object random access workloads, also consider the random IOPS metric when tuning policies.
- Re-run profiling after hardware or mount changes, or on different devices (NVMe vs SSD vs HDD).
- You can check metrics into source or store them alongside your drivers YAML for reproducibility.

## Auto modes

- On-demand: `policy-run` supports `--auto-discover` which will scan hardware and profile tiers if no `--metrics-in`/`--hardware-in` are provided. Caches are written under `--cache-dir` (default `.cognistore`) and respected until `--cache-ttl` expires.
- Background: `auto-refresh` keeps `.cognistore/hardware.json` and `.cognistore/tier_metrics.json` up to date on a schedule. Point `policy-run` at these files (or use `--auto-discover`) to consume them.

## Safety

- The profiler writes and deletes a temporary file under each tier base path. It keeps the file small (default ~64 MiB) and uses large I/O blocks to reduce wear.
- If a tier has very low free space, run profiling on an alternate path or reduce the file size.
