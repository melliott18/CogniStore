# Installation, upgrades, and recovery

Owners: API/platform maintains deployments; catalog and storage owners approve
the recovery set and verify data. Use the [handbook](operator_handbook.md) for
incident records and the [architecture matrix](reference_architectures.md) to
select a supported topology.

## Install

### Clean local walkthrough

Prerequisites: a clean checkout, CPython 3.10–3.14 with `venv`, package-index
access, and a writable local filesystem. Install native libmagic using the
[host setup instructions](../README.md#local-python-environment) for content MIME
detection; without it, scans record their filename-inference fallback. No broker,
database server, cloud credentials, or embedding provider is required here.

The executable walkthrough creates a new virtualenv, installs an immutable copy
of this checkout non-editably, invokes its installed CLI outside the source tree,
and records each command, assertion, source hashes, and dependency versions:

```bash
python3 scripts/operator_drills.py \
  --workspace /tmp/cognistore-operator-73 \
  --evidence /tmp/cognistore-operator-73-evidence.json
```

Choose a new workspace and evidence path for each run. The walkthrough owns only
its disposable workspace; it does not connect to a running deployment. Success
requires a zero exit status and successful install, object round-trip, indexing,
backup/restore, and repair checks in the JSON evidence. See the retained
[drill results and limitations](evidence/m4/README.md).

For a manual first installation, start in a shell without inherited
`COGNISTORE_*` settings and run:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
python -m pip check
export COGNISTORE_SECURITY_PROFILE=development
export OPERATOR_DEMO="$(mktemp -d /tmp/cognistore-first-use.XXXXXX)"
python - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["OPERATOR_DEMO"])
for tier in ("hot", "warm"):
    (root / tier).mkdir()
(root / "reports").mkdir()
(root / "input.txt").write_text("CogniStore operator walkthrough\n")
(root / "drivers.json").write_text(json.dumps({"tiers": {
    tier: {"driver": "posix", "path": str(root / tier)}
    for tier in ("hot", "warm")
}}))
(root / "scope.json").write_text(json.dumps({"tenants": {"demo": {
    "bucket": "operator-demo", "prefix": "demo/", "tiers": ["hot", "warm"]
}}}))
PY
operator_demo() {
  cognistore --no-config --drivers "$OPERATOR_DEMO/drivers.json" \
    --catalog-db "$OPERATOR_DEMO/catalog.sqlite3" "$@"
}
operator_demo put operator-demo demo/welcome.txt "$OPERATOR_DEMO/input.txt"
operator_demo get operator-demo demo/welcome.txt "$OPERATOR_DEMO/download.txt"
cmp "$OPERATOR_DEMO/input.txt" "$OPERATOR_DEMO/download.txt"
operator_demo ls-tier hot operator-demo --prefix demo/
operator_demo catalog-scan hot operator-demo --prefix demo/ --sync --json
operator_demo consistency-scan --tenant demo \
  --scope-config "$OPERATOR_DEMO/scope.json" \
  --report "$OPERATOR_DEMO/reports/initial.sqlite3" --json
```

The listing contains `demo/welcome.txt`; `cmp` exits zero; the final report has
`summary.complete: true` and `summary.consistent: true`. This is a trusted local
CLI demonstration. Its scope label `demo` bounds consistency work; it does not
create an authenticated API tenant. `--sync` avoids requiring a worker or NATS.

If installation fails, preserve the pip error and check Python compatibility and
network access. If storage access fails, verify writable disjoint POSIX roots.
If the scan is incomplete or inconsistent, retain the report and follow
[incident triage](operator_incidents.md); do not treat CLI success alone as a
clean result.

### Service installation

For disposable service testing, follow [Docker development](setup_guide.md).
For production, complete the [Helm prerequisites and install](kubernetes.md#install-a-release)
or the [Terraform handoff](terraform.md). Pin the application image digest and
record resolved dependency versions. Before admitting traffic, require ready
dependencies, current encryption evidence, successful `helm test`, a permitted
tenant-scoped read/write/queued operation using synthetic data, and denied
cross-tenant access. Verify metrics reach Prometheus and
[SLO alert links](operational_slos.md#runbooks) resolve. A health endpoint alone
does not verify permissions, content integrity, or end-to-end queue execution.

## Backup

### Establish one recovery boundary

Trigger: scheduled protection, upgrade/migration, or preservation before repair.
Record the recovery-set ID and UTC boundary. Stop API request traffic and drain
request-serving processes, recurring publishers, direct CLI writers,
policy/scanner processes, and external writers to the storage namespaces.
Read/list requests and authorization decisions can persist access/audit history,
so blocking only mutation routes is not a catalog write fence.
Allow in-flight workers to drain, then stop and fence
them so none can resume against the protected state. Capture incomplete journal
IDs if draining cannot finish. A timed-out worker must be externally fenced;
lease expiry alone does not establish that it has stopped.

In Kubernetes, suspend HPA/KEDA/GitOps reconciliation through the platform's
maintenance procedure before scaling application workloads to zero. Otherwise
an autoscaler can restart writers during the backup. Keep database/broker
services available for their supported logical snapshot APIs. In the development
Compose stack, stop every active application profile, including an optional
sample API; `docker compose stop cognistore` alone only stops the default worker.

| State to retain together | Required contents |
| --- | --- |
| Catalog | Every database/tenant schema or SQLite partition, migration versions, object placement/content identity, move journals, access/budget history, legal holds, audit chains |
| Storage | All tier payloads and metadata/sidecars, provider object-version inventory, encryption-key versions and retention settings |
| Queue | Main and dead-letter stream data/configuration plus durable consumer state; stream/subject/consumer names and retry settings |
| Scheduler, if enabled | The one shared SQLite coordination file, reservations, leases, schedule configuration and principal identity |
| Runtime and configuration | Image digest, values, drivers/topology, tenant/auth policy, secret references and versions, TLS trust, encryption evidence, reports and audit exports |
| Search | Tenant/model settings and keyword-index rebuild inputs; optional index snapshots taken with the single writer stopped |

An individual database dump is consistent within that database; it does not
atomically capture object storage, the scheduler, or the queue. Keep writers
fenced until the entire recovery set is captured and verified. Store encrypted
backups outside the application's deletion scope, with checksums and a tested
recovery identity. Record any accepted gap in queued submissions or object
versions as recovery-point loss.

### SQLite and POSIX

With all writers stopped, use SQLite's backup API for **each** catalog partition
and the scheduler file. Do not copy only the main file of a live WAL database.
Set these absolute paths to an existing source and a new backup file:

```bash
export SQLITE_SOURCE=/srv/cognistore/catalog.sqlite3
export SQLITE_BACKUP=/encrypted-backups/recovery-set/catalog.sqlite3
python - <<'PY'
import os
import sqlite3
from contextlib import closing
from pathlib import Path

source = Path(os.environ["SQLITE_SOURCE"]).resolve(strict=True)
target = Path(os.environ["SQLITE_BACKUP"])
target.parent.mkdir(parents=True, exist_ok=True)
with target.open("xb"):
    pass
with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
    with closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
        assert dst.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
PY
```

Copy every POSIX root in full, including metadata sidecars and any retained
staging state, to a new backup destination while storage remains quiescent.
Preserve permissions and ownership using the platform's backup tool; take the
filesystem snapshot with the same write fence if using snapshots. Retain the
driver-to-root mapping and file hash manifest. The executable walkthrough
demonstrates the SQLite backup API and full tier copy on disposable data.

### PostgreSQL and JetStream

For PostgreSQL, use matching supported client/server tools and authenticated
verified-TLS libpq settings injected from the recovery secret mechanism. The
following `PGDATABASE` selects a dedicated application database; do not put a
password on the command line. Dump the whole database so tenant schemas are
included. Separately retain required roles and platform extension provisioning:

```bash
# PGHOST, PGPORT, PGUSER, PGDATABASE, PGSSLMODE=verify-full and
# PGSSLROOTCERT/password-file configuration are supplied by the operator.
pg_dump --format=custom --file="$BACKUP_DIR/catalog.dump"
pg_restore --list "$BACKUP_DIR/catalog.dump" > "$BACKUP_DIR/catalog.contents"
```

Check exit status and stderr and verify the archive can actually restore;
listing its contents is only an initial check. See the
[PostgreSQL 16 dump reference](https://www.postgresql.org/docs/16/app-pgdump.html).
For managed point-in-time recovery, record the selected restore instant and
matching object/queue recovery set rather than choosing unrelated latest backups.

Use the broker administrator's authenticated NATS CLI context for the correct
account/domain. After verifying the installed CLI's `stream backup --help`,
capture both configured streams with consumer state, for example:

```bash
nats --context "$NATS_CONTEXT" stream backup "$JOB_STREAM" \
  "$BACKUP_DIR/jobs" --consumers
nats --context "$NATS_CONTEXT" stream backup "$DLQ_STREAM" \
  "$BACKUP_DIR/dlq" --consumers
```

Record stream message counts/sequences and durable consumer state at the
boundary. A stream absent because it was never created should be recorded as
absent, not replaced with a fabricated empty backup. Follow the
[NATS snapshot procedure](https://docs.nats.io/learn/backup-recovery/stream-backup-restore)
for the deployed broker/CLI version. Replication alone is not backup.

## Restore

Trigger: restore rehearsal, data loss, incompatible upgrade, or corruption.
First isolate and fence the old application and storage writers. Preserve the
failed state for investigation. Restore to new database/storage endpoints;
never overwrite the only remaining recovery evidence. Load the saved application
image and configuration without allowing startup to auto-migrate the catalog.

1. Verify backup hashes, recovery-set identity, encryption-key access, all tenant
   partitions, and the accepted recovery point. Stop if required components are
   missing or from different boundaries.
2. Restore POSIX trees/provider versions and metadata. Restore SQLite backups to
   new files and run `PRAGMA integrity_check`. For PostgreSQL, provision an empty
   isolated database, required roles and compatible pgvector, select it through
   libpq settings, then run the command below. Do not open a writable CogniStore
   catalog against an empty target before restoring.
3. Restore both JetStream snapshots into the isolated recovery account, before
   starting clients that might create empty streams. Restore scheduler SQLite
   state to the shared single-node mount when scheduling is enabled. Preserve
   logical job IDs, stream subjects, and consumer identities. Compare the
   restored stream configuration, message counts, sequence bounds and durable
   consumer positions with the saved boundary before any worker starts.
4. Repoint the saved driver configuration to restored roots/endpoints. Validate
   TLS, credentials, tenant bindings and encryption evidence. Inspect the catalog
   schema and jobs with the compatible application. Old consistency reports are
   evidence only: new paths/configuration change their source binding.
5. Run a **new** complete consistency scan for each supported namespace in the
   default catalog. The CLI scope label does not select an API tenant partition;
   validate non-default partitions through their tenant-bound catalog/driver
   APIs and authorized REST requests, as described in the
   [migration guide](operator_migrations.md). Check object hashes and placement;
   verify legal holds, budget/access history, and audit continuity. Rebuild
   derived keyword indexes under their
   [single-writer contract](keyword_search.md). Use backend-specific validation
   for drivers outside the [consistency scanner's supported inventory](consistency_checks.md).
6. Resolve incomplete moves through [reviewed repair](operator_incidents.md) and
   stale scheduled runs through [fenced recovery](background_workers.md#recover-a-stale-scheduled-run).
   Start a bounded worker pool, observe pending work and one authorized smoke
   job, then reopen API traffic and finally schedule publication. Record observed
   recovery time and data loss. Stop cutover if checks disagree or quarantines
   remain unexplained.

```bash
# PGDATABASE now names the NEW, empty recovery database.
pg_restore --exit-on-error --single-transaction \
  --dbname="$PGDATABASE" "$BACKUP_DIR/catalog.dump"
# RESTORE_NATS_CONTEXT targets an isolated account with no same-name streams.
nats --context "$RESTORE_NATS_CONTEXT" stream restore "$BACKUP_DIR/jobs"
nats --context "$RESTORE_NATS_CONTEXT" stream restore "$BACKUP_DIR/dlq"
nats --context "$RESTORE_NATS_CONTEXT" stream info "$JOB_STREAM"
nats --context "$RESTORE_NATS_CONTEXT" stream info "$DLQ_STREAM"
nats --context "$RESTORE_NATS_CONTEXT" consumer info "$JOB_STREAM" "$JOB_CONSUMER"
```

Retain ownership and privileges unless a reviewed role remapping is required;
an indiscriminate `--no-owner`/`--no-acl` restore changes security semantics. See
[PostgreSQL restore options](https://www.postgresql.org/docs/16/app-pgrestore.html).
If a broker restore reports an existing stream, stop and verify the recovery
target instead of purging it. Restoration success alone does not establish
application consistency.

Restoring bytes can change backend generation identities. A POSIX copy receives
new inode/device/change-time evidence; cloud restoration can create new object
versions. A restored nonterminal move may therefore be quarantined for a
generation mismatch even when the bytes match. Drain moves before backup when
possible. Preserve such journals and escalate to the storage/catalog owners for
reviewed reconciliation; never rewrite recorded generations to force repair.
The local drill restores a catalog without pending moves and exercises repair
separately in the original namespace. It does not prove automatic recovery of
pending moves across a filesystem or provider restore.

## Upgrade

1. Record the running source/image digest, schema heads, dependency versions,
   values/configuration revision, tenant list, queue identity, and scheduler
   topology. Review migrations and envelope compatibility between both releases.
   Current packaged head is `0014_legal_holds`; verify from the actual candidate:

   ```bash
   python -c 'from cognistore.db import MigrationManager; print(MigrationManager().heads())'
   ```

2. Take the [coherent backup](#backup), then rehearse candidate installation,
   migration and smoke/recovery checks on its isolated restore. Verify every
   tenant partition, including retained partitions absent from current membership
   before reopening them. Writable catalog construction migrates automatically;
   read-only catalog opens refuse an incompatible schema.
3. Fence old producers/writers for incompatible changes. A Helm migration hook
   serializes migration execution but does not stop existing pods. Preserve
   queue/scheduler identities and drain incompatible pending envelopes. Retain
   old image and values throughout the rollback window.
4. For Helm, render/lint the candidate and follow
   [migration and upgrade instructions](kubernetes.md#migrations-upgrades-and-rollback).
   For a Python service, install the candidate into a new virtualenv, apply the
   [packaged migration chain](postgres_catalog.md#migration-lifecycle) deliberately
   to all configured partitions, and switch the supervised service to that
   environment. Keep the previous environment intact.
5. Require ready dependencies, schema-at-head, authorized API/data operations,
   queue completion, clean scoped consistency checks, and present telemetry.
   Compare latency/error/backlog behavior with the pre-change baseline and
   [SLO thresholds](operational_slos.md). Resume producers gradually. Record the
   outcome and actual migration time; escalate to the catalog owner on any
   schema/data mismatch before allowing more writes.

## Rollback

Trigger: failed upgrade verification or a confirmed regression. Stop additional
rollout and record what has already migrated or accepted new writes. If the old
application is **proven compatible with the current schema, configuration and
pending job envelopes**, select the retained image/venv and values. For Helm,
inspect `helm history cognistore -n cognistore`, then use the reviewed revision:

```bash
helm rollback cognistore "$PREVIOUS_REVISION" -n cognistore --wait --timeout 10m
helm test cognistore -n cognistore --logs
```

Helm rollback restores Kubernetes resources, not catalog data, Secrets managed
outside the release, object bytes, or queue/scheduler history. Recheck the
external configuration revision and repeat upgrade validation.

If compatibility is unknown or a schema downgrade would drop data, keep all
writers fenced and follow [restore](#restore) using the complete pre-upgrade
recovery set and matching application. Record accepted loss/reconciliation of
post-backup writes before cutover. Never mix an old catalog with a newer queue
or changed object inventory. Packaged downgrades have
[data-preservation guards](postgres_catalog.md#migration-lifecycle); do not
delete budget, hold, tenant or audit history to bypass them. A writable open by
the newer binary would immediately migrate the database again. Escalate to the
catalog/storage owners if no coherent, compatible recovery point exists.
