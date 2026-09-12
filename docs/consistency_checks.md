# Catalog-to-storage consistency checks

`consistency-scan` compares catalog records and placements, backend inventories,
object metadata and full SHA-256 checksums, and durable move jobs. It reports
discrepancies without updating the source catalog or writing, moving, repairing,
or deleting stored objects. The report is a separate SQLite file containing
findings, a resumable checkpoint, and scan/export audit events.

## Tenant scope and trust boundary

Every scan and export requires an operator-managed JSON or YAML scope file,
`--tenant`, `--drivers`, and a persistent `--catalog-db` (SQLite path/URL or
PostgreSQL DSN). For example:

```yaml
tenants:
  acme:
    bucket: customer-data
    prefix: acme/
    tiers: [hot, warm]
  example:
    bucket: customer-data
    prefix: example/
    tiers: [hot, warm]
```

The tenant selects a fixed bucket, literal key prefix, and allowed tiers.
`--prefix acme/reports/` and repeated `--tier hot` options can narrow a scan;
they cannot widen this binding. Use a trailing slash when the namespace is a
directory: the literal prefix `acme` also matches `acme-other`. An explicitly
empty prefix grants the entire bucket. Duplicate configuration keys, unknown
binding fields, unknown tenants, and unauthorized tiers/prefixes are rejected.
Bucket names must be a single namespace component (no slash, backslash, dot
or parent-directory component). Tenant namespace overlaps are checked
conservatively after Unicode normalization and case folding, and POSIX tier
roots must be disjoint. These restrictions prevent filesystem path aliases
from exposing another configured tenant's objects.
Tenant ownership follows the logical bucket/key namespace across all tiers.
Overlapping prefixes for tenants in the same bucket are rejected even when
their tier sets differ. The same prefix in separate buckets is allowed.

The repository has no authenticated tenant identity or tenant column in its
catalog. These commands therefore enforce an explicit namespace binding at the
CLI boundary. A tenant name is not an authentication credential. Operators must
control the configuration, report directories, catalog access, and storage
credentials using OS permissions or their deployment's identity system. Bind
each tenant to its own namespace and keep scope files unwritable by untrusted
callers. Do not expose arbitrary CLI/configuration access as a tenant API.

Each report belongs to exactly one tenant. Resume and export verify a digest
of the catalog locator, driver configuration contents, resolved POSIX roots,
and complete selected tenant binding. Neither credentials nor the catalog locator are stored in the
report. Changing the binding or driver configuration requires a new report;
moving a SQLite catalog also changes the binding. Resume additionally requires
the original scan prefix and tiers. Cross-tenant aggregate reports are not
supported.

## Scan, pause, and resume

Prepare an existing catalog using the ordinary migration/indexing workflow, then
choose a report directory outside every POSIX tier root:

```sh
mkdir -p reports
cognistore --drivers drivers.yaml --catalog-db catalog.sqlite3 \
  consistency-scan --tenant acme --scope-config tenants.yaml \
  --report reports/acme.sqlite3 --max-items 1000 --json

cognistore --drivers drivers.yaml --catalog-db catalog.sqlite3 \
  consistency-scan --tenant acme --scope-config tenants.yaml \
  --report reports/acme.sqlite3 --resume --max-items 1000 --json
```

Repeat `--resume` until `summary.complete` is true, or omit `--max-items` to
finish in one invocation. The work budget covers inventory pages and object
inspection; it is not a count of findings. Inventory and findings are spooled
to the report database so working memory does not grow with total object count.
A report is exclusively created; an existing path requires `--resume` and a
recognized report schema with the matching scope and source binding.

`--page-size` accepts 1–1000 and defaults to 100. `--requests-per-second` defaults to 20 and
`--bytes-per-second` to 8388608 (8 MiB/s). Rates must be finite and positive;
page size and work budgets must be positive integers. Request throttling covers
logical driver operations per scanner process; a driver's internal retries may
issue additional requests. The byte limit paces actual streamed bytes with at
most one 1 MiB read chunk of burst. Scans hash
complete objects and compare against trusted full SHA-256 evidence when
available. Backend ETags are not assumed to be SHA-256 checksums, and unavailable integrity evidence is
reported explicitly. Generation-bound reads detect an object changing during
verification instead of treating mixed-generation data as valid. Legacy sample
hashes are usable only when the recorded sample covers the whole object; move
journal checksums are usable only for their recorded backend generation.

S3 inventory uses native continuation tokens. POSIX inventory retains only a
page of candidate keys and skips unrelated or completed directory subtrees;
portable filesystems have no durable sorted cursor, so large flat directories
are reread between pages. Symlinks and special files are excluded from POSIX
inventory. Unsupported drivers fail closed rather than silently omitting their
objects. `--max-items` pauses between pages and object inspections: interrupted
hashing restarts the current object, while earlier inspections remain committed.
Resume requires the original page size and rate settings.

The summary distinguishes completion from consistency: a completed report can
contain discrepancies. CLI success means the command ran successfully; inspect
`summary.consistent`, `finding_count`, `reason_counts`, and `severity_counts`
when deciding whether to alert. Interrupted scans can resume their checkpoint.
Scanning a live, changing catalog/storage namespace is not a transactionally
consistent cross-system snapshot. Keys inserted behind an inventory cursor can
be missed by that scan; rerun with a new report to reassess changes made after
its observations. Completed historical move jobs do not require absent source
copies or resurrect catalog objects deliberately deleted since the move.

The version 1 reason codes are:

| Reason code | Severity | Meaning |
| --- | --- | --- |
| `missing_placement` | error | The catalog's current tier has no object. |
| `missing_source` | error | An incomplete move still requires its absent source. |
| `missing_destination` | error | A move phase requires an absent destination. |
| `size_mismatch` | error | Observed object size differs from trusted catalog/job evidence. |
| `checksum_mismatch` | error | Full SHA-256 differs from trusted evidence, or physical copies disagree. |
| `duplicate_placement` | warning | Multiple physical copies exist outside the expected active move pair. |
| `placement_mismatch` | warning | A copy is on an unexpected tier. |
| `partial_job` | info or warning | A move is incomplete; expired/unowned or failed jobs receive warning severity. |
| `untracked_object` | warning | A backend object has no catalog record or active move. |
| `object_changed` | warning | Catalog, job, or backend generation changed during observation; rerun to reassess. |
| `backend_error` | warning | A backend could not be observed; absence is not inferred from this failure. |
| `checksum_unavailable` | info | No trustworthy full-object SHA-256 is available for comparison. |
| `scope_incomplete` | info | Expected placement/job evidence refers to a tier outside the selected scan. |

## Export and audit

```sh
cognistore --drivers drivers.yaml --catalog-db catalog.sqlite3 \
  consistency-export --tenant acme --scope-config tenants.yaml \
  --report reports/acme.sqlite3 --output reports/acme.jsonl --json
```

The export streams JSONL: one `type: "report"` summary, followed by
`type: "finding"` records and `type: "audit"` events. Findings include stable
reason codes and severity, plus evidence for operator review. Scan lifecycle
and export audit records use the ordinary CogniStore audit event structure and
redaction, but are stored only in the isolated report. The source catalog's
audit history and access counters are unchanged. Protect reports as operational
data: scoped object names and metadata can still be sensitive.

Export requires the same trusted tenant/source configuration as scanning. It
does not need to instantiate backend drivers or contact the source catalog.
The output must be a new file. Both report and export paths are rejected if
they alias protected catalog files, SQLite journals, configuration files, or
the input report, including symlinks and hardlinks. They must also remain
outside configured POSIX roots.

## Dry-run behavior

Both commands are always read-only toward the catalog and backend. A normal
scan writes only its report/checkpoint/audit; an export writes a new JSONL file
and records the export in that report. No repair or deletion flags exist.

`consistency-scan --dry-run` performs the scoped scan using a disposable
temporary report and returns the observed summary. It leaves no report or
checkpoint artifact. With `--resume --dry-run`, it previews from a read-only
snapshot of the existing checkpoint and discards progress. Temporary files
are removed on exit. `consistency-export --dry-run` validates the binding and
output target and returns the saved summary without writing an export or audit
event. None of these modes creates storage roots or installs catalog migrations.
