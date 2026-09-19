# Security operations runbook

Assign security, identity, storage/key, and platform owners before deployment.
Use the deployment's approved access and change process for identity and key
changes. CogniStore's CLI, catalog credentials, configuration files, and broker
publisher credentials are trusted operator capabilities, not tenant interfaces.
Keep evidence restricted and preserve tenant ownership throughout recovery.

## Authentication and tenant failures

**Triage.** Capture the API error code, request/correlation ID, onset, and
affected workload without copying a token or full claims. Check these branches:

| Symptom | Check |
| --- | --- |
| `401 authentication_required` | Client sent one Bearer header and the proxy preserved it. |
| `401 invalid_token` | Exact issuer/audience, access-token expiry, UTC clock, allowed algorithm, and trusted discovery/JWKS reachability. |
| `403 forbidden` | Exact issuer/subject RBAC binding, every required permission, tenant membership, policy readability/validity, and audit persistence. |
| A formerly visible object/job returns `404` | Current tenant ownership and actual resource ID; cross-tenant IDs intentionally look absent. |
| Worker dead-letters a previously accepted job | Current RBAC and tenant binding match the saved principal and tenant on every retry/redrive. |
| `/metrics` returns `404` with a tenant policy | Expected isolation behavior; shared metrics belong on private operator infrastructure. |

**Action.** Repair the failing identity-provider route, UTC clock, mounted
policy, or authorized binding and repeat the affected request. Use the exact
[authentication](authentication.md) and [authorization](authorization.md)
contracts. Role/tenant files are reread on each check; publish a fully validated
replacement atomically to every API/worker, retaining restricted ownership and
permissions. An invalid or unreadable replacement fails closed. Roles in a JWT
do not grant RBAC permissions, and changing a tenant binding does not migrate
resources. Do not disable JWT, RBAC, tenant isolation, or certificate validation
to clear a failure. Inspect catalog health when an allowed action cannot persist
its audit record.

JWKS is cached for 300 seconds by default; unknown-key early refresh is limited
to once per 30 seconds. A provider outage can leave fresh cached keys usable,
but authentication fails once the cache expires. Recovery requires valid keys
from the configured trusted endpoint. Token-supplied key URLs are never a fix.

**Verify.** With operator-approved test identities in the affected tenant,
verify one permitted request succeeds, a prohibited operation remains denied,
and a foreign tenant's known object/job remains undisclosed. Confirm API and
worker policy versions agree and a representative protected job completes.
Do not submit anonymous CLI jobs to protected workers: use authenticated REST
actions or the documented trusted scheduler identity.

**Escalate.** Involve identity/security owners if a previously denied operation
succeeds, tenant ownership changes unexpectedly, policies are tampered with,
or 401/403 failures persist after restoring valid configuration. Involve the
catalog owner when audit persistence fails. Never restore availability by
removing a revoked identity's boundary checks.

## Suspected credential compromise and revocation

**Triage.** Identify whether the credential belongs to a user/service JWT,
broker publisher, database, storage backend, or secret/key provider. Record the
first/last suspected use, affected tenant and resource scope, jobs already
submitted, and active moves. Preserve restricted logs and durable audit evidence
before retention expires; do not print the suspected credential.

**Action.** For an API identity, remove its exact issuer/subject RBAC and tenant
bindings through atomic policy replacement, and revoke provider credentials
through the identity owner. Subsequent requests and worker deliveries recheck
the current policy; already-authorized operations may still finish. Background
jobs retain normalized identity, not a JWT, so token expiry/revocation alone
does not cancel submitted work. Do not rewrite queued identities or mass-redrive
their failures.

For a compromised publisher, block its broker access and replace its credential
at the broker before resuming publication. Internal job principal metadata is
trusted, not independently signed; broker access can impersonate a publisher.
For database/storage/provider credentials, restrict the compromised principal
at its authority and install a least-privilege replacement in the protected
deployment configuration. Preserve catalog, queue, and move journals while
draining/restarting affected processes. Coordinate immediate revocation with
the incident owner because active transfers may fail and need normal recovery.

**Verify.** Confirm the old credential is rejected at its authority, the intended
replacement can perform its required operation, and unauthorized/cross-tenant
access remains denied. Inspect queued identities, terminal failures, move
journals, and audit continuity before using the
[DLQ or repair procedures](operator_incidents.md). Check every instance; a
successful operation from one worker does not cover all cached clients.

**Escalate.** Treat unexpected use after revocation, forged job context, missing
audit evidence, or changed tenant/storage ownership as an active security
incident. Keep producers restricted until security and service owners agree the
scope is understood and recovery has been verified.

## Planned secrets, signing keys, and certificate rotation

**Triage.** Inventory consumers, configured pins, cache TTLs, longest transfer
and retry windows, retained backups, and recovery identities. Verify current
security configuration without dumping environment or secret files:

```bash
cognistore --no-config encryption-status --json
```

This offline status checks configured profile and attestation currency; it does
not inspect physical encryption or a remote TLS handshake. Production is the
default profile. `development` is for isolated tutorials/tests, not a response
to expired attestations or TLS failures. Supply the deployment's environment
when checking its controls; `--no-config` bypasses CLI profile files.

**Action.** Use the existing [runtime secret rotation](secrets.md#caching-rotation-and-outages)
and [encryption rotation](encryption.md#key-ownership-and-rotation) contracts:

1. Authorize the replacement and retain overlapping old/new permissions or CA
   trust. Publish a complete matched credential set as one secret version.
2. Unpinned storage secret references refresh after cache expiry; active streams
   keep their original backend. Changing a pinned version or static driver
   configuration requires restarting clients. Worker `SIGHUP` only reloads tier
   limits, not driver configuration or TLS contexts.
3. For signing keys, publish the new public key before issuing tokens with its
   `kid`; retain the old key through previously issued token lifetimes and cache
   windows. There is no per-token revocation lookup.
4. For TLS, install overlapping trust and restart clients, rotate server leaves,
   verify every configured/discovered hostname, then retire old trust and restart
   clients again. For storage encryption keys, retain versions required by old
   objects and backups; a new write option does not re-encrypt historical data.

**Verify.** Exercise new and existing objects with checksum verification, a new
authorized API request and queued operation, and a retained-backup restore under
its original key. Check each process after cache expiry/restart. Revoke old
storage credentials only after the planned overlap and active transfers end.
Renew attestations using actual deployment evidence, including runtime/report
and backup volumes; a green status command alone is not encryption evidence.

**Escalate.** On TLS or provider-resolution errors, restore a known valid
certificate/trust or credential version while preserving expiry/revocation
requirements. Involve the key owner when historical objects/backups cannot be
read. Do not delete old keys or use expired credentials as a fallback.

## Audit evidence and integrity failure

**Triage.** Use an authenticated `auditor` in the affected tenant. Query
`GET /v1/audit/events` by correlation/job/actor and time window; export using
`GET /v1/audit/export`. These endpoints intentionally require authentication
even in an otherwise anonymous local API. Follow export cursors until
`complete` is true; one page is not an archive. See the exact filters and
checkpoint schema in [audit events](audit_events.md#authenticated-access-and-export).

**Action.** Retain all ordered export records, payloads, tombstones, and the
checkpoint in independently controlled incident storage. Verify against the
independently retained checkpoint with `POST /v1/audit/verify`, supplying
`{"checkpoint": saved_checkpoint}`. For offline exports, use the documented
`verify_audit_export(all_records, saved_checkpoint)` procedure. Preserve the
catalog/queue/report snapshots and restrict writers when a verification fails;
do not edit rows, disable integrity triggers, prune history, or reset the chain.
Consistency reports have a separate checkpoint and must be retained separately.

**Verify.** Require `valid: true` against the independent checkpoint and review
event coverage, including unresolved storage `started` events. Such an event
does not establish success or failure of external I/O; reconcile it with storage
and move evidence. A self-contained archive can be internally consistent yet
replaced wholesale, so retain the independent anchor and verify backups too.

**Escalate.** Immediately involve the security and catalog owners for altered,
missing, or rolled-back evidence. An unavailable audit store also blocks audited
allows/disclosures; repair availability without weakening fail-closed behavior.
Archive scoped evidence within retention and document any unavoidable gap.
