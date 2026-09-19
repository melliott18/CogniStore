# Confirmed orphan cleanup

`orphan-cleanup` inspects one backend key and tier. Its default is a read-only
report, including when `--no-dry-run` is supplied. Deletion requires a separate
quarantine, an elapsed grace period, and an explicit `--execute` invocation
with the saved candidate ID. Quarantine records evidence; it does not move or
delete backend bytes.

Use `untracked_object` findings from [consistency checks](consistency_checks.md)
as investigation leads. A finding is not proof that deletion is safe. Cleanup
does not accept a report as authority or process a report in bulk: select one
key and tier, then inspect current catalog and backend state.

## Tenant and source binding

Every invocation requires `--tenant`, `--scope-config`, `--drivers`, and an
existing persistent `--catalog-db`. The tenant's catalog must already have the
current schema. Cleanup does not create catalogs or run migrations.

The scope file uses the same structure as consistency checks:

```yaml
tenants:
  acme:
    bucket: customer-data
    prefix: acme/
    tiers: [hot, warm]
```

`--key` must fall inside the configured literal prefix and `--tier` must be one
of the configured tiers. Directory prefixes should end in `/`; the literal
prefix `acme` also matches `acme-other`. Unknown tenants, overlapping bindings,
unauthorized tiers, traversal, doubled slashes, and other noncanonical paths
are rejected. POSIX tier roots must be disjoint.

Cleanup opens the actual tenant catalog partition and scopes storage through
`TenantStorageDriver`. The `default` tenant retains legacy catalog/storage
locations; another tenant uses its isolated catalog and physical storage
namespace. A logical prefix binding alone does not map another tenant's data
into that partition. Use the existing deployment's tenant identity and catalog
locator; do not relabel legacy data with a new tenant name.

The operator must control the scope file, driver configuration, catalog, and
storage credentials. A tenant name is not an authentication credential.
Quarantine binds the tenant, bucket, key, tier, catalog locator, driver
configuration, resolved POSIX roots, and complete tenant binding. Changing
these inputs requires fresh inspection and quarantine.

## Report every condition

```sh
cognistore --catalog-db catalog.sqlite3 --drivers drivers.yaml \
  orphan-cleanup --tenant acme --scope-config tenants.yaml \
  --key acme/old.bin --tier hot --json
```

The normal CLI envelope has `status: "planned"` and `dry_run: true`. Its
`summary` includes `stage`, `status`, `candidate_id`, `tenant_id`, `bucket`,
`key`, `tier`, `conditions`, and `blockers`. Inspect every condition before
quarantining:

| Condition | Required evidence |
| --- | --- |
| `tenant` | The catalog and storage driver resolve to the selected tenant. |
| `audit` | The catalog audit chain and its retained evidence verify successfully. |
| `catalog` | No logical object or placement references the key on any tier; normalized aliases are treated conservatively. |
| `cas` | Shared-content identity is resolved; stored and derived full-object/chunk reference counts agree and are zero, and the content has passed its unreferenced window. Unknown CAS keys remain protected. |
| `jobs` | No incomplete move protects the key. Failed jobs and expired leases still protect recovery data. For shared content, any incomplete tenant move blocks reclamation. |
| `legal_holds` | No relevant active hold. Any active tenant hold protects shared content whose ownership is ambiguous. |
| `backend` | The object has valid size, generation, and modification time, and the driver supports conditional deletion. Read failures never establish absence. |
| `retention` | The backend object's minimum age has elapsed. |
| `quarantine` | Execution additionally requires a matching persisted candidate and elapsed grace period. |

Without a candidate ID, `summary.status: "eligible"` means the object can be
quarantined. With a candidate ID, it describes the current execution checks.
`blocked` includes the reasons that prevent progress. Inspection does not
change access counters, catalog records, audit history, or backend contents.

## Quarantine, wait, then execute

Both intervals default to `604800` seconds (seven days) and must be finite and
strictly positive:

- `--retention-seconds` is the minimum age since backend modification. It also
  supplies the unreferenced-age requirement for shared content.
- `--grace-period-seconds` begins when an eligible candidate is quarantined.

The backend age requirement must already be satisfied to enter quarantine.
These intervals cannot be bypassed with zero or negative values.

```sh
cognistore --catalog-db catalog.sqlite3 --drivers drivers.yaml \
  orphan-cleanup --tenant acme --scope-config tenants.yaml \
  --key acme/old.bin --tier hot --quarantine --json
```

Save `summary.candidate_id` and review
`summary.conditions.quarantine.eligible_at`. After that time, preview the
existing candidate without writing:

```sh
cognistore --catalog-db catalog.sqlite3 --drivers drivers.yaml \
  orphan-cleanup --tenant acme --scope-config tenants.yaml \
  --key acme/old.bin --tier hot --candidate-id CANDIDATE_UUID --json
```

Explicit execution uses the same coordinates and trusted configuration:

```sh
cognistore --catalog-db catalog.sqlite3 --drivers drivers.yaml \
  orphan-cleanup --tenant acme --scope-config tenants.yaml \
  --key acme/old.bin --tier hot --execute --candidate-id CANDIDATE_UUID --json
```

Execution preserves the stored grace period and uses the larger of the stored
and currently requested retention intervals. For custom intervals, pass the
chosen retention on subsequent invocations too; omitting it requests the
seven-day default again. `--dry-run` converts either `--quarantine` or
`--execute` to inspection. `--execute` always requires `--candidate-id`.

Execution rechecks every protection while holding the catalog's reclamation
fence. Reference publication and cleanup are serialized. A new reference,
hold, protected job, changed shared-content evidence, or replaced backend
generation invalidates the pending candidate. An intervening catalog mutation
of the target or a normalized alias also invalidates quarantine, including a
reference created and then removed before execution. Resolve its cause and begin a
new quarantine; an invalidated candidate cannot be revived. Deletion is
conditional on the exact observed generation, so a replacement cannot be
deleted using an old candidate.

## Audit, failures, and recovery

The tenant catalog retains `orphan.quarantined`, `orphan.delete_started`,
`orphan.deleted`, `orphan.delete_failed`, and `orphan.invalidated` in its
tamper-evident audit chain without expiry. Events attribute the local user,
use the candidate ID as their correlation ID, and link each outcome to the
previous event. An invalid audit chain or failed intent append stops deletion.

A backend deletion failure returns a nonzero CLI exit code, preserves the
candidate and failure evidence, and reports `retryable: true`. Inspect and
retry the same `--execute --candidate-id` command. A crash or failed terminal
audit append may occur after bytes were deleted; the durable
`orphan.delete_started` makes that incomplete outcome visible. A retry
rechecks protections and can complete the journal when backend absence is
confirmed. A backend read error remains unresolved. An already completed
candidate returns `summary.status: "already_deleted"` without deleting again.

Exit code zero means inspection or the requested stage finished; inspect
`summary.status` and `blockers` to distinguish `blocked`, `quarantined`,
`invalidated`, and `deleted`. Keep the candidate ID after any interruption.

Cleanup relies on the normal CogniStore publication fences. Direct database
edits and raw backend writes outside those workflows are trusted operator
actions and must be coordinated separately. This command reclaims backend
bytes; it does not implement general retention policy management.
