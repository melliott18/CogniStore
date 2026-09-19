# Catalog-to-storage consistency checks

`consistency-scan` compares catalog records and placements, backend inventories,
object metadata and full SHA-256 checksums, and durable move jobs. It reports
discrepancies without updating the source catalog or writing, moving, repairing,
or deleting stored objects. The report is a separate SQLite file containing
findings, a resumable checkpoint, and scan/export audit events.

## Tenant scope and trust boundary

Every scan, export, and repair requires an operator-managed JSON or YAML scope file,
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

These operator commands enforce an explicit namespace binding at the CLI
boundary, separately from the authenticated API's tenant membership policy.
A tenant name is not an authentication credential. Operators must
control the configuration, report directories, catalog access, and storage
credentials using OS permissions or their deployment's identity system. Bind
each tenant to its own namespace and keep scope files unwritable by untrusted
callers. Do not expose arbitrary CLI/configuration access as a tenant API.

Each report belongs to exactly one tenant. Resume, export, and repair verify a digest
of the catalog locator, driver configuration contents, resolved POSIX roots,
and complete selected tenant binding. Neither credentials nor the catalog locator are stored in the
report. Changing the binding or driver configuration requires a new report;
moving a SQLite catalog also changes the binding. Resume and repair additionally require
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

## Plan and enable safe repair

`consistency-repair` consumes a completed report from the same tenant, source
binding, prefix, and tiers. Complete a paused scan before repairing it. If the
scan used `--prefix` or repeated `--tier`, supply the same options for repair.
Report schema, audit integrity, binding, and scope are checked before opening
the catalog for writes.

Repair is disabled by default. Review its plan, then explicitly enable the safe
actions:

```sh
cognistore --drivers drivers.yaml --catalog-db catalog.sqlite3 \
  consistency-repair --tenant acme --scope-config tenants.yaml \
  --report reports/acme.sqlite3 --json

cognistore --drivers drivers.yaml --catalog-db catalog.sqlite3 \
  consistency-repair --tenant acme --scope-config tenants.yaml \
  --report reports/acme.sqlite3 --enable-repair --json
```

The default command opens the catalog read-only and records its decisions in
the report's audit chain. It returns `status: "planned"` and
`summary.plan_only: true`. `--enable-repair` permits catalog and backend changes
only after current state passes the same conservative checks. The command
returns action decisions in `summary.actions` and aggregate `summary.counts`;
successful execution does not mean every discrepancy was repaired.

The supported automatic action resumes one existing, nonterminal move through
the durable mover using its original idempotency key. It rechecks current
catalog, job, backend generation, and full-checksum evidence; a scan finding
alone is never authority to move or delete bytes. Live leases, legal holds,
configured locality constraints, unsupported drivers, changed evidence, and
ambiguous state prevent automatic repair. A repeated run reuses the original
move and does not create a replacement job.

| Report finding and current evidence | Decision |
| --- | --- |
| `partial_job` with one eligible nonterminal job and intact phase-appropriate copies | Resume the original move journal and idempotency key. |
| A historical finding with no remaining discrepancy | Mark resolved; take no repair action. |
| Missing required data, size/checksum mismatch, unexplained duplicates, or an untracked object | Quarantine for operator review. |
| Failed or ambiguous jobs, live leases, legal holds, or configured locality constraints | Quarantine for operator review. |
| No trusted full checksum, backend errors, incomplete scope, or changed catalog/job/generation evidence | Quarantine for operator review. |

Missing objects without a safe resumable move, mismatched copies, untracked
objects, and other uncertain discrepancies are quarantined for operator
review. Here quarantine means an audit decision in the report: it does not
move, hide, or delete the suspect object. Safe resumption may perform the
original move's verified, generation-conditional source cleanup. There is no
automatic deletion of unrelated duplicates or reconstruction of missing bytes.
Keep the report for its decision history and run a new scan after repair to
measure current consistency; the original scan findings remain historical.

`--requests-per-second` and `--bytes-per-second` throttle repair's verification
reads and default to 20 and 8388608. They must be finite and positive. These
limits do not throttle the durable mover's transfer or cleanup operations.
Repairs use the existing catalog schema and never install migrations.

## Export and audit

```sh
cognistore --drivers drivers.yaml --catalog-db catalog.sqlite3 \
  consistency-export --tenant acme --scope-config tenants.yaml \
  --report reports/acme.sqlite3 --output reports/acme.jsonl --json
```

The export streams JSONL: one `type: "report"` summary, followed by
`type: "finding"` records and `type: "audit"` events. Findings include stable
reason codes and severity, plus evidence for operator review. Scan lifecycle,
repair decisions, and export audit records use the ordinary CogniStore audit
event structure and redaction, but are stored only in the isolated report.
Scans, exports, and repair plans leave the source catalog's audit history and
access counters unchanged. Enabled repairs additionally use the durable
mover's normal catalog audit trail. Protect reports as operational
data: scoped object names and metadata can still be sensitive.

Export requires the same trusted tenant/source configuration as scanning. It
does not need to instantiate backend drivers or contact the source catalog.
The output must be a new file. Both report and export paths are rejected if
they alias protected catalog files, SQLite journals, configuration files, or
the input report, including symlinks and hardlinks. They must also remain
outside configured POSIX roots.

Report schema version 2 adds a separate append-only SHA-256 audit chain.
Opening, reading, resuming, or exporting a report verifies its chain and durable
head. An export includes `audit_checkpoint` (`algorithm`, `scan_id`, `tenant_id`,
`sequence`, `entry_hash`), and each audit record includes `integrity` with its
sequence, previous hash, and entry hash. Retain the checkpoint in an independent
archive to detect replacement or rollback of the whole report:

```python
from pathlib import Path
from cognistore.core.consistency_report import open_report, verify_report_integrity

with open_report(Path("reports/acme.sqlite3"), read_only=True) as connection:
    checkpoint = verify_report_integrity(connection, expected_checkpoint=saved_checkpoint)
```

A mismatch raises `ValueError`. This chain protects the audit evidence, not
the separate findings or resumable scan-state tables. Its checkpoint is
independent of the catalog audit checkpoint. Sidecar audit records do not
expire or support pruning; archive the report as a unit. Version 1 reports
must be preserved as historical artifacts and replaced by a fresh scan before
using the version 2 resume/export workflow. They cannot acquire trustworthy
historical integrity through an automatic migration.

## Dry-run behavior

Scan and export are always read-only toward the catalog and backend. A normal
scan writes only its report/checkpoint/audit; an export writes a new JSONL file
and records the export in that report.

`consistency-scan --dry-run` performs the scoped scan using a disposable
temporary report and returns the observed summary. It leaves no report or
checkpoint artifact. With `--resume --dry-run`, it previews from a read-only
snapshot of the existing checkpoint and discards progress. Temporary files
are removed on exit. `consistency-export --dry-run` validates the binding and
output target and returns the saved summary without writing an export or audit
event. `consistency-repair --dry-run` checks current state and returns its plan
without appending decision audit events or changing catalog/backend state.
`--dry-run` takes precedence over `--enable-repair`. None of these preview modes
creates storage roots or installs catalog migrations.
