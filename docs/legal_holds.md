# Legal holds and deletion protection

Legal holds preserve a tenant's logical objects across every storage tier.
A hold covers an exact bucket/key, a literal key prefix within a bucket, or
the entire bucket. Prefix and bucket holds also cover future matching keys.
Matching conservatively includes path aliases: case differences, Unicode
canonical equivalents, repeated separators, and dot components cannot bypass a
hold. This may protect additional names on case-sensitive object stores; the
original stored names and hold scope evidence remain unchanged. Prefix matching
is literal (no wildcard expansion), and `records/` and `records-old/` remain
distinct prefixes.
Holds do not expire automatically. Overlapping holds apply independently:
releasing one leaves every other active hold in force.

An active hold blocks uploads, overwrites, deletes, catalog replacement and
placement changes, policy moves, and source cleanup by resumed move jobs.
Stability overrides, including compliance overrides, cannot bypass a hold.
Reads, downloads, and read-only inspection remain available. This release
conservatively prohibits new uploads within a held scope as well.

## Place, inspect, and release

Use the authenticated REST API with trusted role bindings. `hold_manager`
grants `legal_hold_manage`; `hold_releaser` grants `legal_hold_release`.
These are separate permissions so placement authority need not grant release
authority. `admin` has both. Existing reader, writer, policy manager, operator,
auditor, and admin roles can inspect holds through `legal_hold_inspect`.
Lifecycle endpoints require an authenticated, authorized principal even in
an otherwise anonymous local API deployment.

```http
POST /v1/legal-holds
Content-Type: application/json
Authorization: Bearer <token>

{"bucket":"records","prefix":"contracts/","reason":"Preserve investigation records"}
```

Supply `key` instead of `prefix` for one exact object. Omit both for the whole
bucket. The response includes a generated hold ID, tenant, scope, placement
reason, time, and server-derived actor and correlation metadata. Clients cannot
choose an actor or a tenant through the request body.

```http
GET /v1/legal-holds?bucket=records&key=contracts/example.pdf&active_only=true
Authorization: Bearer <token>
```

The key filter returns all holds that cover that key, including prefix and
bucket holds. Omit `active_only=true` to include released history. Inspection
does not change lifecycle state.

```http
POST /v1/legal-holds/<hold-id>/release
Content-Type: application/json
Authorization: Bearer <token>

{"reason":"Preservation requirement explicitly withdrawn"}
```

Release requires its own permission and an explicit reason. It retains the
original placement metadata and adds release time, actor, reason, and correlation
metadata. It does not delete the hold or its audit records. Held object writes
return HTTP `409` with error code `legal_hold`; missing permission returns `403`.
All lookups and permissions remain within the authenticated tenant.

## Execution and concurrency

Hold placement and destructive work serialize through a catalog-owned guard.
The guard spans the storage operation, closing the check/delete race. Ordinary
destructive operations share the guard, so unrelated transfers can run in
parallel. Hold placement and release take exclusive access and wait for those
operations to finish. In-memory catalogs use a reentrant reader/writer lock;
SQLite coordinates processes through a sidecar file lock, and PostgreSQL uses
session advisory locks. Coordination is conservatively tenant-wide, so a long
storage operation can delay a hold change within that tenant. Catalog reads and
worker heartbeats remain available during storage work.
Windows SQLite uses a conservative exclusive CRT file-lock fallback, so its
destructive storage operations serialize; other catalog reads remain available.

The operation that acquires the guard first completes before the competing
operation proceeds. Once hold placement succeeds, later destructive work is
blocked. A hold cannot restore bytes removed before it was placed. Move phases
recheck authoritative hold state, including a committed move awaiting source
cleanup. Held move attempts fail closed and retain existing copies; worker
retries and redrive do not bypass the check.

Every lifecycle change and denied destructive attempt records a `legal_hold.*`
audit event. Lifecycle changes and their audit insert commit together. These
events are retained indefinitely and excluded from routine age-based pruning,
including after release. Each event also joins the tenant's tamper-evident audit
chain and is included in audit verification and evidence export. An audit or
hold lookup failure cannot authorize a destructive operation.

## Cleanup, local tools, and deployment

`content-reference-report` remains read-only. While any hold is active in a
tenant, it conservatively marks every content blob in that catalog
`legal_hold: true` and `reclamation_eligible: false`, including apparent orphans.
This avoids reclaiming evidence when content ownership is incomplete. Hold
status alone is not a reference-count inconsistency. After all holds are
released, ordinary reference and grace-period checks determine eligibility.
Consistency scans and exports remain read-only; this release adds no automatic
repair or garbage collector.

Use the same persistent catalog for the API, workers, and local CLI operations.
The sample corpus loader also honors holds before each upload.
`put --catalog-db ...` checks holds before writing, and upload/movement dry runs
inspect holds without changing hold state or appending denial events. A local
tool with no catalog, direct storage/database credentials, or an independently
configured catalog is inside the trusted operator boundary; legal holds do not
replace backend IAM or provider-native object lock. Keep such access restricted.
SQLite writers must share both the catalog and its lock-file namespace on a
filesystem supporting process locks; do not remove lock sidecars while running.

Migration `0014_legal_holds` installs hold storage in each tenant partition after
`0013_audit_integrity`, preserving the existing audit checkpoint.
Offline SQLite catalog import preserves active and released holds together with
their audit history, and rejects missing or inconsistent hold state. Schema
downgrade refuses to discard a catalog's hold history, even after release.
Deploy the updated API and workers together; older binaries do not enforce this
contract. Existing data has no holds until one is explicitly placed. This is
preservation control, not legal case management or retention scheduling.
