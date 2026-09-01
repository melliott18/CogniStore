# PostgreSQL catalog operations

CogniStore's catalog data-access layer supports migration-managed SQLite and
PostgreSQL catalogs. PostgreSQL is the durable control-plane deployment target;
SQLite remains useful for local operation and as the source format for
prototype catalog upgrades.

Application components depend on `CatalogStore`, the backend-neutral contract
for object state, placement, scan fencing, durable move journals, and operational
audit events.
`SQLCatalog` implements that contract with one short database transaction per
operation. Its normalized schema owns tiers, pools, objects, the single current
placement for each object, mutation/claim fences, move jobs, and ordered move
transitions, plus versioned audit history. The in-memory `Catalog` remains
available for isolated inline work and tests; `SQLiteCatalog` is a compatibility
wrapper over `SQLCatalog`. See [Operational audit events](audit_events.md) for
the event/query and retention contract.

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

The migration chain has a legacy baseline, the normalized catalog revision,
the audit-event revision, the content-identity revision, and the content-
reference revision. The content-identity revision adds global content blobs,
versioned chunk manifests, ordered manifest chunks, and each logical object's
active manifest reference. Existing objects are not backfilled from legacy
`sha256` metadata because that value may cover only a sample; the next
successful scan creates their canonical identity.

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

On PostgreSQL, the normalization revision also:

- converts move-job JSON fields to PostgreSQL `json` while preserving metadata
  strings that contain embedded NUL characters;
- enables the `vector` extension when it is absent; and
- records whether CogniStore created the extension.

The extension is schema readiness only. This revision does not create
embeddings, vector indexes, or search APIs. A downgrade removes `vector` only
when the migration itself created it; a platform-managed extension is left in
place.

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
database, rehearse the target revision, and verify the reconstructed legacy
object and move data before rollback. The migration refuses to downgrade when
the catalog contains pools, tier metadata, tiers referenced by neither an
object placement nor a move journal, or objects without placements because the
legacy schema cannot represent that state. It also refuses noncanonical
imported object or placement UUIDs because the legacy layout has nowhere to
retain them. Remove or export those normalized-only records deliberately
before a downgrade; CogniStore will not discard or rewrite them silently.
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

Object metadata and placement, normalized tier and pool metadata, move-job
checkpoints, verification evidence, leases, terminal reasons, every move
transition, versioned audit events, and canonical content manifests are copied.
Normalized UUIDs and timestamps are retained. Legacy
objects receive deterministic UUIDs and the migration timestamp
`1970-01-01T00:00:00.000000Z`, matching the in-place normalization migration.
Sources that predate the audit table receive a deterministic event chain built
from their move-transition journal during import.
Current normalized sources must contain `audit_events`, `audit_move_heads`, and
`audit_event_tombstones`; durable heads and compact replay tombstones are copied
even when retention already pruned every full event for a move.
The four content-identity tables are likewise an all-or-none topology. Sources
from before revision `0004_content_identity` import with no canonical mapping;
current sources retain global blobs, manifests, ordered chunks, and active
object references. The importer removes an unbacked `content_identity` summary
from pre-`0004` or otherwise unmapped objects. Mapped identities are validated
as a complete canonical graph before commit, including digests, CAS keys,
versions, chunk extents, object size, and their metadata projection.

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
4. commits only after objects, placements, move jobs, transitions, audit events,
   and durable move heads all satisfy the destination constraints.

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
```

Spot-check current placements and nonterminal move jobs as well. In-flight
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
