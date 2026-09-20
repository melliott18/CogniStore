# Tenant ownership and isolation

Configure `COGNISTORE_TENANT_POLICY` (or `--tenant-policy`) on the API and
workers to assign each authenticated identity to exactly one tenant. JWT
authentication and [role-based authorization](authorization.md) remain
required. Even an administrator can access only their assigned tenant.

The policy is a trusted local JSON file:

```json
{
  "bindings": [
    {"issuer": "https://identity.example.com", "subject": "alice", "tenant_id": "alpha"},
    {"issuer": "https://identity.example.com", "subject": "alpha-scanner", "tenant_id": "alpha"},
    {"issuer": "https://identity.example.com", "subject": "bob", "tenant_id": "beta"}
  ]
}
```

Tenant IDs are exact, case-sensitive ASCII identifiers: 1–128 letters, digits,
dots, underscores, or hyphens, beginning with a letter or digit. Membership
uses the exact issuer and subject together. Token claims, request headers,
query parameters, object metadata, and job payloads cannot select a tenant.
Unknown identities, duplicate bindings, missing files, and malformed policy
replacements fail closed. The policy is reread for each request and worker
delivery; changing membership does not move previously owned resources.

Without a tenant policy, the existing trusted single-tenant deployment operates
as `default`. Configure a policy before serving multiple tenants. Both API and
workers must use the same policy, catalog root, driver configuration, and role
bindings. An explicit membership in `default` grants access to legacy resources;
new tenants receive empty partitions.

## Persistence and namespaces

Each tenant has a physically separate catalog partition. Every object,
placement, content manifest, embedding, access event, policy decision, audit
event, move job, retry fence, topology resource, and budget belongs to that
partition. Migration `0012_tenant_ownership` records its immutable owner in
`catalog_tenant`; opening a partition with a mismatched owner is rejected.

* SQLite uses `<catalog-file>.tenants/<SHA-256 tenant ID>/catalog.sqlite3`.
* PostgreSQL uses `cognistore_t_<first 48 hex characters of SHA-256 tenant ID>`
  schemas within the configured database. Each schema has its own Alembic
  version table; pgvector remains a shared database extension in `public`.
* `default` retains the original SQLite file or PostgreSQL default schema.
* In-memory catalogs use separate tenant-owned stores with the same contract.

Primary keys, unique constraints, deduplication, content references, and foreign
keys are local to the owning partition. Two tenants can use the same bucket,
key, content digest, move ID, audit ID, or budget ID without conflicting or
revealing each other's state. Catalog and embedding connections check the
active tenant on every operation, including retained SQL connections. Custom
SQL and migration tools are trusted operator interfaces, not tenant APIs.

Storage retains logical buckets and keys in public interfaces. Non-default
tenants use `.cognistore-tenants/<SHA-256 tenant ID>/` prefixes within each
physical bucket. The default namespace excludes this reserved directory and
its filename aliases.
Readers, writes, conditional deletes, streaming, scans, and listing cursors all
enforce the namespace. Buckets must be single relative path components; keys
cannot traverse directories or address the reserved first component.

Reserved-name comparisons fold Unicode case and remove the pinned Unicode 17
default-ignorable characters, including the 16 characters ignored by HFS+
filename comparison. For example, `.COGNISTORE-TENANTS` and
`.cogniſtore-tenants` are reserved as both a bucket and a first key component,
including listing prefixes. Returned inventories also hide such names. This
protects shared tenant storage on case-sensitive POSIX, case-insensitive APFS,
HFS+, and casefolded ext4 without changing physical tenant prefixes. Aliases of
empty, `.` or `..` path components are rejected too. POSIX also reserves aliases
of its `.cognistore-staging` bucket so requests cannot access shared uploads.

This is an intentional compatibility change on **every backend**, including
case-sensitive cloud object stores: reserved aliases that were previously
accepted are now inaccessible through tenant wrappers. Move any legacy
objects using those names or ambiguous component aliases through a trusted
offline migration before enabling the updated API or workers. Ordinary buckets
and keys retain their spelling,
case, and backend semantics; `.cognistore-tenants-backup` and
`documents/.COGNISTORE-TENANTS/example` remain ordinary keys. No tenant-storage
migration is required for existing canonical tenant prefixes. See
[POSIX filename support](posix_containment.md#reserved-filename-comparisons)
for the supported comparison boundary.

Tantivy indexes use separate tenant paths and writer caches; pgvector indexes
live with the catalog. Retrieval, generated-answer providers, and policy feature
loaders must use dependencies scoped to the same tenant. Stateful custom
providers must implement `for_tenant(tenant_id)` or expose a matching
`tenant_id`; providers that cannot be scoped are rejected. Public pagination
cursors bind the tenant as well as the query filters.

## Requests, jobs, and telemetry

The API resolves membership before authorizing or looking up a resource. A
cross-tenant object, decision, or job ID returns the same not-found response as
an absent ID. Requests retain their tenant across asynchronous operations,
thread workers, and streamed responses. Policy evaluation and movement use only
the tenant's catalog, drivers, and feature sources.

Jobs snapshot the tenant in `cognistore.tenant_id` envelope metadata alongside
the authenticated principal. Retry and redrive preserve both immutable values;
queue deduplication includes tenant scope. Every worker attempt re-resolves
membership and compares it to the saved owner before handlers, status updates,
or execution coordination. Missing, changed, revoked, or malformed tenant
context is rejected. A worker without a tenant policy rejects non-default
tenant jobs. Legacy unowned jobs remain accepted only in single-tenant mode.
The local CLI does not authenticate principals; use authenticated REST actions
for manual tenant workloads. Recurring schedules can use an operator-controlled
service identity and the same tenant policy; see
[protected schedules](background_workers.md#protected-schedules). This trusted
assertion assigns one scheduler to one tenant and does not verify a JWT.

Durable access history, authorization audits, policy evidence, and job status
are stored in the owning catalog partition. The shared API returns 404 for
`/metrics` when a tenant policy is configured, because process-wide counters
aggregate tenants. Operational logs, traces, queue diagnostics, and exporters
belong on private operator infrastructure. They are not tenant-facing APIs.

Tenant membership changes, backups, and restores are operator-managed. Include
each tenant's catalog, storage namespace, and indexes together. Tenant
self-service, billing, and cross-tenant sharing are not part of this interface.

## Verification

`tests/integration/test_tenant_api.py` exercises real JWT verification, catalog
partitions, and POSIX storage against direct IDs, enumeration, filters, forged
tenant hints, policy mutations, jobs, cursors, and concurrent streamed reads.
Tenant catalog conformance runs against memory, SQLite, and PostgreSQL. Worker
tests cover crafted envelopes, revocation, retry/redrive, and concurrent tenant
deliveries; search/storage tests cover real Tantivy and pgvector indexes,
generation checks, and traversal attempts.

`tests/unit/test_tenant_namespace_aliases.py` covers reserved aliases at every
shared storage entry point, listing filters/cursors, and the staging bucket.
`tests/unit/test_tenant_namespace_api.py` exercises authenticated default-tenant
attacks against another tenant's held object with deterministic case-insensitive
POSIX lookup and a native volume check (skipped on case-sensitive volumes).
Both retain victim bytes and verify rejection before backend access.

Run the default suite with `python -m pytest`. Set
`COGNISTORE_TEST_POSTGRES_DSN` to an isolated test PostgreSQL service with
pgvector available to run the PostgreSQL tenant cases.
