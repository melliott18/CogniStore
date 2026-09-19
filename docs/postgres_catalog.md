# PostgreSQL catalog operations

CogniStore's catalog data-access layer supports migration-managed SQLite and
PostgreSQL catalogs. PostgreSQL is the durable control-plane deployment target;
SQLite remains useful for local operation and as the source format for
prototype catalog upgrades.

Application components depend on `CatalogStore`, the backend-neutral contract
for object state, placement, scan fencing, durable move journals, operational
audit events, access history, placement controls, and budget admission.
`SQLCatalog` implements that contract with one short database transaction per
operation. Its normalized schema owns tiers, pools, objects, the single current
placement for each object, mutation/claim fences, move jobs, and ordered move
transitions, plus versioned audit history, access observations, importance tags,
residency and cooldown clocks, and budget definitions and reservations.
The in-memory `Catalog` remains
available for isolated inline work and tests; `SQLiteCatalog` is a compatibility
wrapper over `SQLCatalog`. See [Operational audit events](audit_events.md) for
the event/query and retention contract.

Tier/pool registration, membership, locality filtering, and atomic pool
assignment also use `CatalogStore` on every backend. See
[Tier pools and placement attributes](tier_pools.md) for the validated model,
multi-pool configuration, and the distinction between current placement
references and retained move-journal names.

## Provision a clean PostgreSQL catalog

Create a dedicated database and role using the normal controls for the target
environment. The role that performs the first open must be able to create the
`vector` extension when it is not already installed. If the platform team
preinstalls `vector`, CogniStore records that it is available without claiming
ownership of the extension.

Set the standard CLI/configuration variable to the DSN:

```bash
export COGNISTORE_CATALOG_DB='postgresql://cognistore@db.example/cognistore'
```

Keep the password in the deployment secret mechanism. The DSN may remain
passwordless when libpq receives credentials through `PGPASSWORD`, a password
file, or the platform's equivalent injection; the Compose stack uses this
split. `--catalog-db "$COGNISTORE_CATALOG_DB"` and
`--catalog-url "$COGNISTORE_CATALOG_DB"` are equivalent CLI forms; there is no
separate `COGNISTORE_CATALOG_URL` variable.

Opening a writable catalog applies the packaged Alembic migration chain to the
current head:

```python
import os

from cognistore.db.catalog import SQLCatalog

with SQLCatalog(os.environ["COGNISTORE_CATALOG_DB"]) as catalog:
    print(catalog.backend)
```

Both `postgresql://` and `postgres://` locators are normalized to the psycopg 3
driver. A PostgreSQL advisory transaction lock serializes migrations when
multiple workers start concurrently. Read-only opens never migrate: they fail
if the schema is absent or behind the current head, which prevents an old
reader from silently operating against a partially upgraded catalog.

## Migration lifecycle

The current migration head is `0014_legal_holds`. The chain includes the
legacy baseline, normalized catalog, audit events, content identity, content
references, embeddings, tier topology, access events, placement controls, tier
stability, policy budgets, tenant ownership, audit integrity, and legal holds.
The content-identity revision adds global
content blobs, versioned chunk manifests, ordered manifest chunks, and each
logical object's active manifest reference. Existing objects are not backfilled
from legacy `sha256` metadata because that value may cover only a sample; the
next successful scan creates their canonical identity.

The content-reference revision materializes each blob's active reference count
and records when that count last reached zero. One mapped object contributes a
reference to its full-content blob and one reference for every chunk occurrence
in its manifest; repeated chunks retain their multiplicity. Migration derives
these counts from the existing active mappings and starts a fresh grace period
for every zero-reference blob instead of using its older creation timestamp.

`SQLCatalog.reconcile_content_references()` compares the stored count with the
same active mapping graph and returns a deterministic, read-only report. A blob
is only an advisory reclamation candidate when both counts are zero, its
zero-reference timestamp is valid, and the requested grace period has elapsed.
Logical object deletion updates the mapping and counts but never deletes shared
manifest rows, blob identities, or physical CAS bytes. Physical CAS reclamation
is outside the catalog transaction boundary and is not implemented by this
revision.

The embedding revision adds immutable model spaces, normalized text documents
and passages, vectors, completion gates, and active object-to-passage layouts.
Existing objects are not backfilled: a successful extraction must be indexed
through an explicitly configured provider.

On PostgreSQL, the normalization revision also:

- converts move-job JSON fields to PostgreSQL `json` while preserving metadata
  strings that contain embedded NUL characters;
- enables the `vector` extension when it is absent; and
- records whether CogniStore created the extension.

The normalization revision treats the extension as schema readiness only; the
later embedding revision creates the embedding tables. Space-specific HNSW
indexes are created by the embedding DAL after a concrete vector dimension and
immutable model identity are registered. A downgrade removes `vector` only
when the normalization migration itself created it; a platform-managed
extension is left in place.

Revision `0006_embeddings` requires pgvector 0.8.0 or newer, including when the
extension is platform-managed. The migration rejects an older version before
creating embedding tables so iterative filtered HNSW behavior cannot silently
degrade. Upgrade the extension deliberately before retrying the catalog
migration.

The M3 revisions extend the existing catalog without inferring unavailable
behavioral or topology evidence:

| Revision | Added state and upgrade behavior |
| --- | --- |
| `0007_tier_pools` | Region, membership, locality labels, attributes and lifecycle flags. Existing pools become inactive until their topology is explicitly configured. |
| `0008_access_events` | Access observations, sampling provenance, and expired retry identities. Existing objects start with no observed access history. |
| `0009_placement_controls` | Trusted importance and its revision, plus `placement_started_at`. Existing placement update times provide conservative residency starts; the prototype epoch sentinel remains unknown. |
| `0010_tier_stability` | `last_tier_move_at` for cooldowns. Existing rows use their known placement start, or the migration instant when that history is unknown. |
| `0011_policy_budgets` | Immutable budget definitions and per-move-attempt reservations. No budgets or charges are inferred for existing placements or jobs. |

The subsequent revisions extend the same chain:

| Revision | Added state and upgrade behavior |
| --- | --- |
| `0012_tenant_ownership` | Immutable ownership marker for each catalog partition. Existing unowned catalogs belong to `default`; tenant membership changes do not migrate data. |
| `0013_audit_integrity` | Append-only integrity ledger and head, with a baseline for existing retained audit events, replay tombstones, and move heads. A baseline does not prove history before it was established. |
| `0014_legal_holds` | Tenant-owned exact-object, prefix, and bucket hold state and lifecycle evidence; existing catalogs begin with no holds. |

Each tenant partition has its own Alembic version table. Include retained
partitions absent from the active membership policy in upgrade and backup
inventories. See [tenancy](tenancy.md), [audit integrity](audit_events.md), and
[legal holds](legal_holds.md). The tenant-aware cutover steps are in the
[migration runbook](operator_migrations.md#sqlite-to-postgresql).

See [access history](access_history.md),
[importance, residency, and tier stability](placement_controls.md), and
[policy budgets](policy_budgets.md) for the corresponding runtime contracts.

Writable `SQLCatalog` construction is the normal upgrade entry point. For a
controlled deployment or rollback, use the same packaged chain through
`MigrationManager` and the catalog engine:

```python
import os

from cognistore.db.engine import create_catalog_engine
from cognistore.db.migrations import MigrationManager

engine, sqlite_connection = create_catalog_engine(
    os.environ["COGNISTORE_CATALOG_DB"]
)
try:
    migrations = MigrationManager()
    migrations.upgrade(engine)
    assert migrations.is_at_head(engine)

    # After fencing writers and taking/rehearsing from a database backup:
    # migrations.downgrade(engine, "0001_legacy_catalog")
finally:
    engine.dispose()
    if sqlite_connection is not None:
        sqlite_connection.close()

```

Treat downgrade as a planned operator operation: stop writers, back up the
database, rehearse the target revision, and verify the retained object and move
data before rollback. Opening a writable catalog with the current application
after rollback upgrades it again; coordinate the deployed application revision
with the target schema.

Downgrading below `0014_legal_holds` is refused while **any** legal-hold history
exists, including released holds. Releasing a hold does not authorize erasing
its lifecycle evidence. Downgrading below `0013_audit_integrity` removes the
integrity ledger, head, and database guards; retain verified exports and an
external checkpoint with the database backup before a planned rollback.
Re-upgrading establishes a new baseline, not the original integrity history.
Downgrading below `0012_tenant_ownership` removes the durable owner marker and
must not be used to transfer a partition to another tenant. The generic
`create_catalog_engine` example above targets the default partition; for a
named tenant, operate on its correctly scoped catalog engine under a matching
tenant context, never an arbitrary SQL search path.

Downgrading below `0011_policy_budgets` is refused while either budget table
contains rows, including expired definitions or reservations for completed
moves. An empty budget schema can be removed; a populated one requires a
separately planned restoration or data migration that preserves its admission
history. Exporting a report alone does not satisfy this guard.
Downgrading below `0010_tier_stability` drops the trusted last-move clock.
Re-upgrading derives a conservative replacement from placement history or the
new migration time; it cannot recover the original clock.
Downgrading below `0009_placement_controls` drops trusted importance, its
revision, and the residency start. Existing audit events remain until the
audit revision is removed, but re-upgrading does not reconstruct live tags or
their original clocks from those events. Preserve these fields in the backup
when placement behavior must survive rollback.
Downgrading below `0008_access_events` drops all access observations and expired
retry identities. Preserve both active and expired rows when history and retry
deduplication must survive; a new empty table after re-upgrade cannot retain
either guarantee.

The normalized-to-legacy downgrade refuses to proceed when
the catalog contains pools, tier metadata, tiers referenced by neither an
object placement nor a move journal, or objects without placements because the
legacy schema cannot represent that state. It also refuses noncanonical
imported object or placement UUIDs because the legacy layout has nowhere to
retain them. Remove or export those normalized-only records deliberately
before a downgrade; CogniStore will not discard or rewrite them silently.
Downgrading from `0007_tier_pools` removes pool topology, attribute observations,
and tier/pool lifecycle flags. Export that configuration before rollback; pool
identities, metadata, and current pool/tier placement references remain intact.
Downgrading from `0006_embeddings` drops all normalized passage layouts,
vectors, compatibility spaces, HNSW indexes, and active embedding mappings;
retain or rebuild them deliberately before rollback.
Downgrading from `0004_content_identity` drops active logical-object content
references and global manifests/blob identities; export those mappings first
when they must survive the rollback. Independent logical-object and placement
metadata remains intact, as does the compatibility `sha256` value, but the
catalog-owned `content_identity` summary is removed because its normalized
mapping no longer exists.
Downgrading from `0003_audit_events` to an older revision drops the audit-event
table, durable per-move heads, and replay tombstones; export that history first
when it must survive the rollback.

## Import an existing SQLite catalog

The supported import is an offline, one-shot copy into an empty, migrated
destination. It accepts either of these source layouts:

- the prototype `objects(bucket, key, size, tier, metadata)` layout; or
- the normalized SQL catalog layout produced by the current migrations.

Object metadata and placement, normalized tier and pool topology and attributes,
move-job checkpoints, verification evidence, leases, terminal reasons, every
move transition, versioned audit events, canonical content manifests, access
observations, importance, placement clocks, budget definitions, and budget
reservations, and legal-hold lifecycle state are copied.
Normalized UUIDs and timestamps are retained. Legacy
objects receive deterministic UUIDs and the migration timestamp
`1970-01-01T00:00:00.000000Z`, matching the in-place normalization migration.
Sources that predate the audit table receive a deterministic event chain built
from their move-transition journal during import.
Current normalized sources must contain `audit_events`, `audit_move_heads`, and
`audit_event_tombstones`; durable heads and compact replay tombstones are copied
even when retention already pruned every full event for a move.
Audit-integrity entries and their head are copied and verified together. Older
sources without that feature establish a baseline; a current source missing
either integrity table is rejected. The source's durable tenant owner must
match the destination; genuinely legacy unowned sources can import only as
`default`. The destination creates its own ownership marker rather than
adopting a different source owner.
The four content-identity tables are likewise an all-or-none topology. Sources
from before revision `0004_content_identity` import with no canonical mapping;
current sources retain global blobs, manifests, ordered chunks, and active
object references. The importer removes an unbacked `content_identity` summary
from pre-`0004` or otherwise unmapped objects. Mapped identities are validated
as a complete canonical graph before commit, including digests, CAS keys,
versions, chunk extents, object size, and their metadata projection.

Access imports retain occurrence times, correlation, sampling rates, and the
expiry state of retry identities; sources without access history import with
zero observations. Current placement-control sources retain importance,
`importance_revision`, `placement_started_at`, and `last_tier_move_at`, including
explicitly unknown values. Older sources derive residency from the placement
update time unless it is the prototype sentinel; absent movement history uses
that known start or the import instant conservatively. Legacy pools without
topology remain inactive and ineligible until configured.

Budget definitions and reservations are imported together, preserving each
reservation's move, attempt, charge, and evidence. Sources predating budgets
import with neither. Partial budget tables, inconsistent reservation identities,
malformed access observations, and incomplete placement-control columns abort
the entire copy. The import report includes counts for access events, budget
definitions, budget reservations, and legal holds; its access count includes
expired rows and its hold count includes released history. Invalid hold
scope, ownership, or lifecycle evidence aborts the copy.

SQLite embedding tables are migration-compatible placeholders, not a supported
vector store. An otherwise valid SQLite source must have no rows in those six
tables. The importer rejects non-empty embedding state instead of silently
dropping it; import the catalog and rebuild embeddings in PostgreSQL through
the pinned provider identity.

### 1. Quiesce and back up

Stop catalog writers before taking the backup. The importer reads one
consistent SQLite snapshot, so writes committed after that snapshot starts are
intentionally absent from PostgreSQL.

```bash
export COGNISTORE_SQLITE_CATALOG=/var/lib/cognistore/catalog.sqlite3
sqlite3 "$COGNISTORE_SQLITE_CATALOG" 'PRAGMA integrity_check;'
sqlite3 "$COGNISTORE_SQLITE_CATALOG" \
  ".backup '/var/backups/cognistore-catalog-before-postgres.sqlite3'"
```

Do not remove the source file after import. Besides being the rollback backup,
it may contain scheduler state that is outside the PostgreSQL catalog boundary.

### 2. Run the transactional import

Point `COGNISTORE_CATALOG_DB` at a new database or schema with no catalog
rows, then run:

```bash
python - <<'PY'
import os
from dataclasses import asdict

from cognistore.db.catalog import SQLCatalog
from cognistore.db.sqlite_import import import_sqlite_catalog

source = os.environ["COGNISTORE_SQLITE_CATALOG"]
destination_url = os.environ["COGNISTORE_CATALOG_DB"]

with SQLCatalog(destination_url) as destination:
    report = import_sqlite_catalog(source, destination)

print(asdict(report))
PY
```

The destination migration runs before the data copy. During the copy, the
importer:

1. opens SQLite in read-only/query-only mode and detects its layout;
2. obtains exclusive PostgreSQL table locks and verifies that every
   catalog-owned data table is empty;
3. copies records in bounded batches inside one destination transaction; and
4. commits only after object, topology, content, move, audit, access,
   placement-control, and budget data pass their validation and destination
   constraints.

Any conversion, JSON, foreign-key, or constraint failure rolls the data copy
back. The migrated but empty PostgreSQL schema remains and the import can be
retried after correcting the source or target. The importer deliberately
refuses to merge into a nonempty destination because object and idempotency-key
conflicts require operator-specific reconciliation.

The target's Alembic revision and pgvector ownership record are not copied from
SQLite. They describe the target installation and are managed by its own
migration run.

### 3. Verify and cut over

Compare the report with independent counts before restarting writers:

```sql
SELECT count(*) FROM objects;
SELECT count(*) FROM object_placements;
SELECT count(*) FROM move_jobs;
SELECT count(*) FROM move_job_transitions;
SELECT count(*) FROM audit_events;
SELECT count(*) FROM audit_move_heads;
SELECT count(*) FROM audit_event_tombstones;
SELECT count(*) FROM tiers;
SELECT count(*) FROM pools;
SELECT count(*) FROM access_events;
SELECT expired, count(*) FROM access_events GROUP BY expired;
SELECT count(*) FROM budget_definitions;
SELECT count(*) FROM budget_reservations;
SELECT count(*) FROM legal_holds;
SELECT released_at IS NULL AS active, count(*) FROM legal_holds
GROUP BY released_at IS NULL;
SELECT count(*) FROM audit_integrity_entries;
SELECT sequence, entry_hash FROM audit_integrity_head;
SELECT tenant_id FROM catalog_tenant;
```

These unqualified queries target the default PostgreSQL schema. For a tenant
import, run them against that tenant's schema and compare its source backup.
Also require `catalog.verify_audit_integrity().valid` from the tenant-scoped
catalog; row counts alone cannot establish integrity.

Spot-check current pool/tier assignments, importance and placement clocks,
budget charges and their evidence, and nonterminal move jobs as well. In-flight
move leases are preserved exactly; either allow them to expire or use the
normal move recovery workflow after the former workers are fenced.

Configure catalog consumers with the PostgreSQL locator, for example
`--catalog-url "$COGNISTORE_CATALOG_DB"`. Keep the SQLite backup until the
new catalog has passed application-level verification.

## Scheduler-state boundary

Scheduler timing and recovery state is not part of the catalog DAL. The
`scheduled_jobs`, `scheduled_runs`, and `scheduled_run_recoveries` tables are
owned by `SQLiteScheduleStore`; the SQLite-to-PostgreSQL importer ignores them
even when they share the old catalog file. It also never modifies or deletes
those source rows.

When a worker uses PostgreSQL for `--catalog-url`, give it a separate persistent
SQLite scheduler path with `--schedule-db`. Give the scheduler process that
same path; the scheduler itself does not use PostgreSQL catalog tables. All
workers that consume scheduled jobs must also see the same SQLite state, so the
current scheduler topology is single-host and is not made multi-host-safe by
moving the catalog to PostgreSQL.

Preserve or back up the original SQLite file with SQLite's backup operation
when existing scheduled occurrences must survive the catalog cutover. A plain
filesystem copy can omit committed WAL data. Do not treat a successful catalog
import as evidence that scheduler reservations, execution leases, or recovery
audits moved to PostgreSQL.

## Operational constraints

- Run only one importer and keep application writers stopped until verification
  completes. PostgreSQL table locks protect the target, but cannot prevent a
  separate process from changing the SQLite source after its snapshot begins.
- Import into a fresh destination. Do not truncate a production catalog merely
  to satisfy the empty-target guard.
- Retain the source and database backups through the rollback window.
- Run migrations with an operator identity that has extension/schema
  privileges, then use a least-privilege application role for steady-state
  catalog access.
