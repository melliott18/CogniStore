# Catalog, backend, and deployment migrations

Use this runbook for a planned change of catalog backend, object-storage
backend, or deployment location. Keep application upgrades separate where
possible so that a failed cutover has one clear cause. Start with the
[operator handbook](operator_handbook.md), the
[backup procedure](operator_lifecycle.md#backup), and the
[supported architectures](reference_architectures.md).

## Cutover contract

Before any migration, record the image/package revision, every tenant and
catalog partition, driver configuration revision, bucket/key inventory,
nonterminal moves, queue stream/consumer/DLQ identities, and optional scheduler
and keyword-index paths. Record current schema revisions and retained audit
checkpoints. Keep credentials in the deployment's secret mechanism; inventories
and reports must not include passwords, bearer tokens, or connection strings
with embedded credentials.

Stop API traffic/request-serving processes, scheduler publication, and other
producers. Reads, listings, and denied requests can append access or audit
state, so blocking only mutation routes does not freeze the catalog. Drain
accepted work when possible, then stop and externally fence old workers before
copying state or changing endpoints. An expired lease alone does not prove a
worker stopped. Include uploads, scans, policy runs, direct storage writers,
and repair processes in the maintenance window. Take a coordinated recovery
checkpoint of catalog, queue, scheduler state, and object bytes. A catalog copy
alone cannot reverse storage changes.

| Gate | Required evidence |
| --- | --- |
| Before cutover | Tested restore, frozen source, destination credentials/TLS, capacity, current encryption evidence, and an identified rollback owner. |
| Before enabling writes | Per-tenant record counts and audit integrity, representative generation-bound reads, expected placement and hold state, and no unexplained in-flight work. |
| After enabling writes | A bounded canary completes through its real queue/storage path; job and move outcomes agree; read-back bytes match; observed service behavior meets the deployment's SLOs. |
| Before retiring the source | Rollback window closed, retained backups verified, and no live placements, pending jobs, or historical recovery procedures depend on the source configuration. |

Never serve two writable catalogs for the same logical deployment during a
cutover. After the destination accepts new writes, returning to a frozen source
is a data-restoration/reconciliation operation, not just a configuration revert.

## SQLite to PostgreSQL

The shipped importer is offline and transactional. It copies one SQLite
catalog partition to an empty, migrated destination of the **same tenant**.
It does not copy object bytes, scheduler tables, or Tantivy files, and it does
not perform live replication, incremental catch-up, partition merging, or
tenant reassignment. See [PostgreSQL catalog operations](postgres_catalog.md)
for supported source layouts, all copied state, and downgrade guards.

1. Provision PostgreSQL with pgvector 0.8.0 or newer, verified TLS, encrypted
   storage/backups, and migration privileges. Supply the password separately
   through the approved secret mechanism. Use the production encryption
   configuration for the operator process as well as the deployed services.
2. Inventory `default` plus every tenant partition, including retained tenants
   no longer present in the active membership policy. SQLite tenant catalogs
   are sibling files under `<root>.tenants/<SHA-256 tenant ID>/catalog.sqlite3`;
   the root SQLite file alone is not a complete backup.
3. Quiesce and back up each partition using SQLite's backup API or `.backup`
   command, as described in the [backup runbook](operator_lifecycle.md#backup).
   Check `PRAGMA integrity_check;` on each backup and retain its checksum. Use
   the backup file as the immutable import source. Preserve the scheduler
   SQLite file separately, even if it used to share the default catalog file.
4. Run the following once per partition from the target release environment.
   Set `COGNISTORE_SQLITE_CATALOG` to that tenant's physical backup file and
   `COGNISTORE_IMPORT_TENANT` to its exact owner (`default` for legacy catalogs).
   Set `COGNISTORE_CATALOG_DB` to the destination PostgreSQL root DSN; the
   catalog selects the tenant schema. Do not append a tenant path yourself.

```bash
python - <<'PY'
import os
from dataclasses import asdict

from cognistore.auth.tenancy import tenant_context
from cognistore.db.catalog import SQLCatalog
from cognistore.db.migrations import MigrationManager
from cognistore.db.sqlite_import import import_sqlite_catalog

tenant = os.environ["COGNISTORE_IMPORT_TENANT"]
with tenant_context(tenant):
    with SQLCatalog(os.environ["COGNISTORE_CATALOG_DB"], tenant_id=tenant) as target:
        report = import_sqlite_catalog(os.environ["COGNISTORE_SQLITE_CATALOG"], target)
        assert MigrationManager().is_at_head(target.engine)
        integrity = target.verify_audit_integrity()
        assert integrity.valid, integrity.issues
        print({"tenant": tenant, "import": asdict(report), "audit_valid": integrity.valid})
PY
```

The destination schema is created before the copy; malformed source state or
a nonempty target aborts the import without a partial data copy. Correct the
cause and retry against the empty target. Do not truncate an existing catalog
to bypass this check. Nonempty SQLite embedding tables are rejected; rebuild
embeddings in PostgreSQL with the pinned provider/model identity. Older
unowned SQLite catalogs can only import as `default`; changing an identity's
tenant membership does not transfer its data.

5. Compare the import report with independent source and target counts, then
   inspect actual records. Include objects/placements, tier/pool topology,
   content mappings, move transitions, access expiry state, budgets and
   reservations, active **and released** legal holds, audit heads/tombstones,
   and audit-integrity evidence. Use each tenant's SQL schema when querying;
   an unqualified query against `public` only checks `default`. Sources older
   than a feature can legitimately have no corresponding table; record that
   difference rather than inventing replacement history.
6. Point every catalog consumer at the new root DSN and preserve its exact
   tenant bindings. Keep driver endpoints, queue identities, and schedule
   identities unchanged for this catalog-only change. For scheduled work,
   give the scheduler and every worker the same preserved persistent
   `--schedule-db` on one host. PostgreSQL does not remove that restriction.
7. Verify read-only application access for each tenant and cross-tenant denial.
   Restore/rebuild each tenant's derived keyword index during a quiesced or
   replayed mutation window. Enable a small write/scan canary, inspect durable
   outcomes, and then resume producers. Retain the SQLite backups through the
   agreed rollback window.

**Rollback:** before new PostgreSQL writes, fence the target processes and
restore the original locator/configuration with the matching source storage,
queue, and scheduler checkpoint. After new writes, first freeze the target and
preserve its evidence; use the [restore runbook](operator_lifecycle.md#restore)
or a separately rehearsed reconciliation. There is no supported incremental
PostgreSQL-to-SQLite reverse-sync command. Do not resume stale SQLite state
against objects that the target workers have since moved or deleted.

## Move object bytes to a different backend

Use a new tier name for a new physical endpoint and keep the old tier mapped
to its original endpoint until migration and recovery are complete. Merely
changing a tier's root or cloud endpoint reinterprets every existing placement
and move journal without transferring bytes. The durable mover supports
configured POSIX, S3-compatible, GCS, and Azure Blob drivers; it retains the
same logical bucket and key across the move. Bucket renaming, account-wide
replication, and copying provider version history are outside this interface.

1. Provision the destination bucket/container or POSIX root and approve its
   locality, access, retention, encryption, and capacity. Cloud buckets are
   operator-managed; keep auto-creation disabled in production. For Azure,
   include the `azure` extra in the tested runtime image; the default runtime
   image does not install it. Follow the [S3](s3_driver.md), [GCS](gcs_driver.md),
   [Azure Blob](azure_blob_driver.md), and
   [POSIX containment](posix_containment.md) contracts.
2. Add the destination tier to the trusted driver configuration used by all
   participating processes. Configure eligible pools/locality when topology
   constraints are in use. Preserve the existing catalog and tenant storage
   namespaces. Pause automatic policy and scheduled moves that could race the
   migration. Catalog legal holds, tier-metadata residency, default importance
   restrictions, locality, and budget admission still constrain manual moves.
   The manual CLI does not load a policy run's custom residency, importance,
   or cooldown configuration; review that configuration separately before
   choosing this path. Existing moves retain their frozen constraints on
   resume. Do not remove catalog protections just to make a migration pass.
3. Inventory current placements and select a bounded canary. Verify the
   destination has no conflicting object at the same bucket/key. A move
   performs a full transfer and SHA-256 verification, commits the placement,
   and conditionally cleans up the observed source generation. It is not a
   nondestructive backup operation.
4. For a trusted **default-tenant** operator session, supply a stable unique
   idempotency key for each object's migration. The following example assumes
   the reviewed drivers file defines `old` and `new`, the catalog already
   contains `documents/canary.txt`, and production secrets/encryption settings
   are supplied to the process:

```bash
MOVE_ID='backend-cutover:canary-001'
cognistore --drivers migration-drivers.yaml --catalog-db "$COGNISTORE_CATALOG_DB" \
  move old new documents canary.txt --idempotency-key "$MOVE_ID" --dry-run --json
cognistore --drivers migration-drivers.yaml --catalog-db "$COGNISTORE_CATALOG_DB" \
  move old new documents canary.txt --idempotency-key "$MOVE_ID" --json
cognistore --catalog-db "$COGNISTORE_CATALOG_DB" move-status "$MOVE_ID" --json
```

A preview does not prove a source generation or destination will remain valid.
Require `completed` in the journal, verify the new catalog placement and
read-back bytes, and run a scoped [consistency scan](consistency_checks.md)
for POSIX/S3 inventories. The current scanner's inventory contract does not
cover every shipped driver; use backend-specific inventory plus
generation-bound full-content verification for GCS/Azure destinations.
For interruption, inspect the journal and use the original key with
[`move-resume`](cli.md#durable-manual-move-recovery); do not invent a replacement
key while the original move could still be active.

The local move CLI has no authenticated tenant selector. For non-default
tenants, use an authenticated [policy preview and queued policy run](rest_api.md)
when the supported policy can express the desired destination, inspect every
resulting decision/guardrail, and verify each move outcome. The REST API has no
arbitrary manual-move or bulk backend-migration endpoint. A migration requiring
arbitrary destinations for non-default tenants needs a separately reviewed
tenant-scoped operator program; passing a tenant's physical SQLite file to the
default CLI does not bypass ownership checks. Never change a membership policy
to pretend that a different tenant owns the source objects.

5. Expand in bounded batches only after the canary passes. Keep a durable
   mapping of tenant, bucket, key, original/destination tier, and move identity.
   Inspect failed and nonterminal moves before retrying; jobs suppressed by
   holds or constraints remain on their current tier. Verify the complete
   placement inventory and storage consistency before retiring the old tier.
   Refresh/rebuild derived retrieval state where placement or content changed.

**Rollback:** a completed move may already have deleted its source. If reversal
is allowed by current holds/constraints, perform a new verified move from the
new tier back to the original tier with a **new** idempotency key, or restore
from the coordinated backup. Reusing the original forward key is an idempotent
forward replay, not a reversal. Do not overwrite a collision, delete an
unexpected source version, or rewrite journal phases to force rollback.

## Move a deployment to Helm or the AWS reference

The [Compose stack](setup_guide.md) is a development environment. A production
cutover must provision production TLS, authentication, authorization, tenant
bindings, secret delivery, and encryption evidence in addition to copying
state. Use [Helm](kubernetes.md) for the application and, on AWS, the separate
[Terraform reference](terraform.md) for infrastructure. Terraform does not
install the application or import existing volumes/catalogs automatically.

1. Build and test the immutable image; prepare namespace Secrets, storage,
   network policies, dependencies, and connectivity without exposing ingress.
   Confirm the target operator can restore backups and decrypt retained data.
   Use distinct disposable queue identities during target testing, so test
   workers cannot consume the source deployment's jobs.
2. Decide explicitly whether each dependency remains in place or is relocated.
   If PostgreSQL stays, retain the catalog root and tenant schemas. If it moves,
   follow the database restore procedure for every partition. If a storage
   endpoint changes, use the backend migration above or an independently
   rehearsed provider restore preserving logical coordinates; catalog restore
   does not copy bytes or preserve a provider generation token by itself.
   Restored POSIX files may have different inode/generation identity, and
   cloud restores may create new versions. Nonterminal move journals can
   therefore remain quarantined even when bytes match; preserve their
   evidence and never rewrite stored generations to force recovery.
3. Stop producers and fence the source deployment. Drain the old queue or
   restore its JetStream state to the destination through the broker's tested
   backup/restore procedure, including DLQ and consumer state. Never run two
   independent brokers consuming copies of the same pending work. Do not
   republish saved envelopes with new job IDs as a queue migration shortcut.
4. For recurring schedules, retain the scheduler SQLite file and use the Helm
   single-node shared-volume topology. To switch to independent workers,
   drain all scheduled occurrences before disabling schedules. The normal
   scalable chart starts workers with `--disable-scheduled-jobs`; such workers
   reject scheduled deliveries and cannot replace the original coordinator.
5. Install with the reviewed values and migration hook. The hook migrates
   `default` plus tenants listed in the configured membership policy; separately
   migrate retained tenant partitions absent from that policy before reopening
   them. A hook does not fence an old application. Keep both releases from
   writing the catalog during incompatible schema changes.
6. Run `helm test`, readiness checks, per-tenant authorization/isolation checks,
   representative object reads, and a bounded queued canary before directing
   ingress and producers to the destination. Record image/chart/values
   revisions, schema heads, queue/consumer settings, and evidence of the
   canary's terminal outcome. An accepted REST action returns a job ID; an
   uncertain submission must be reconciled before resubmission because API v1
   has no caller-provided idempotency key.

**Rollback:** fence the new release first. Revert traffic and application
configuration only when the old image supports the current schema and the
dependency state remains coherent. Otherwise restore the coordinated
checkpoint under the [rollback procedure](operator_lifecycle.md#rollback).
`helm rollback` does not roll back catalog migrations, object moves, cloud
infrastructure, or JetStream contents. Retain old volumes, source endpoints,
backups, and encryption keys until the rollback window closes.
