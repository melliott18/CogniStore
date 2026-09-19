# Operational audit events

CogniStore records an append-oriented operational history in the catalog so an
operator can follow a policy decision through its job and object move, including
retries and terminal outcomes. A tenant-local SHA-256 evidence chain protects
the redacted history, and database controls reject changes to retained records.
See the [event coverage matrix](audit_coverage.md) for required actions, evidence
locations, and automated checks.

These controls protect against accidental or unauthorized application-level
mutation and expose missing or altered records. A database administrator can
disable triggers or replace an entire database. Retain integrity checkpoints in
an independently controlled archive to detect a rewritten or rolled-back prefix.
The chain is not a digital signature and does not establish who originally
supplied an event. Normal database backup, access control, and monitoring remain
necessary.

## Event contract

Data-locality execution rejections use `locality.decision` with outcome
`rejected`; approved exception use at execution checkpoints uses
`locality.exception` with outcome `allowed`. Both retain destination and rule
evidence. Policy decisions retain locality explanations, and durable move
events include their planning evidence. Read-only previews do not append
events. See [data locality](data_locality.md) for configuration and recovery.

Every row in `audit_events` uses the versioned `cognistore.audit` contract. The
initial schema version is `1` and contains:

- an event UUID, event type, outcome, occurrence time, recording time, and
  retention expiry;
- correlation and optional causation IDs;
- actor type and actor ID;
- optional bucket/object coordinates, job ID, move ID, and policy name/version;
- a JSON object for event-specific details.

The initial event vocabulary covers policy decisions, prepared/transitioned/
completed/failed moves, move retries, job failures, scheduled job retries,
dead-letter publication, and manual operator actions. Readers accept positive
future schema versions and unknown event type/outcome strings so additive
producers can be deployed before every reader is upgraded.

The expanded vocabulary also records unsampled authorization decisions, storage
operation intent/outcome, catalog scan start/completion/failure, and audit
access/export/verification/retention. A storage operation's started event is
persisted before I/O; its terminal event points to that intent. A crash or audit
store failure after external storage I/O can leave an unresolved started event.
Investigate that state instead of assuming the storage operation failed or
succeeded. Storage and the SQL catalog do not share one transaction.

`correlation_id` groups one operation. `causation_id` points to the event that
directly caused another event. Policy-selected moves therefore begin with a
`policy.decision` event and their move lifecycle points back to that decision;
background jobs also carry their durable job ID on every event. Manual CLI
moves and inline policy runs begin with `manual.action`. A catalog-assigned,
monotonic move sequence serializes every decision, manual resume, retry, and
transition for one move, so the terminal event's causation path includes the
whole operational history even when timestamps tie.

With [JWT/OIDC authentication](authentication.md), API operations and their
queued worker operations use actor type `authenticated`. Their actor ID is
`principal:sha256:<digest>`, derived from an unambiguous encoding of the verified
issuer and subject; use `Principal.actor_id` to obtain the exact query value.
The same subject under two issuers yields distinct actors. Client-supplied
attribution cannot replace the verified audit actor. The normalized principal
propagates across job retries without storing the bearer token or raw claims.
Broker publishers remain trusted producers of internal principal metadata.

Policy decision details include a versioned `structured_reason` beside the
feature snapshot in `dataset`. See [structured policy reasons](policy_reasons.md)
for the reason vocabulary, privacy rules, and queries that follow a decision
through its move outcome. Older audit events may have neither payload.

The catalog retains each move's latest sequence and event ID separately from
the prunable event rows. Pruning can therefore leave a causation pointer whose
event has expired, but it cannot reuse a sequence number or fork later history.
Pruning also keeps a compact tombstone containing only the event UUID, a replay
digest, and non-sensitive lifecycle metadata. Exact deterministic replays stay
idempotent after event details expire, while conflicting reuse of an old UUID
is rejected. Tombstones intentionally outlive event-detail retention.

Move state changes and their audit events are written in the same catalog
transaction. A failed audit insert rolls back the SQL move transition instead
of leaving an unaudited terminal state. Migration `0003_audit_events` and
pre-audit SQLite imports synthesize deterministic transition events for existing
move journals, allowing an upgraded in-flight move to continue the same chain.

## Querying

The backend-neutral catalog API provides bounded, stably ordered queries:

```python
from cognistore.core.audit import AuditEventType, AuditQuery
from cognistore.db import open_catalog

with open_catalog("/var/lib/cognistore/catalog.sqlite3") as catalog:
    chain = catalog.list_audit_events(
        AuditQuery(
            correlation_id="policy-run-correlation",
            event_types=frozenset(
                {
                    AuditEventType.POLICY_DECISION,
                    AuditEventType.MOVE_COMPLETED,
                }
            ),
            limit=500,
        )
    )
```

`AuditQuery` can filter by correlation, job, move, object, policy/version,
actor, event type, outcome, and an occurrence-time window. Results sort by
`(occurred_at, event_id)` and default to a limit of 100; the maximum limit is
10,000. `get_audit_event(event_id)` retrieves one known event.

These catalog methods are trusted internal primitives used by policy and worker
code. Use the authenticated API for end-user access: it applies authorization,
tenant scoping, and disclosure auditing. Direct library/CLI operators control
database credentials and remain inside the trusted process boundary. CLI
`put`/`get` and synchronous scans require a persistent `--catalog-db` to retain
their evidence across invocations; without it, their development catalog is
in memory.

## Authenticated access and export

All `/v1/audit` endpoints require an authenticated principal with `audit`
permission (the `auditor` or `admin` role), including local API configurations.
The trusted membership policy selects the tenant. There is no caller-controlled
tenant parameter and cursors/checkpoints cannot grant access to another tenant.

| Endpoint | Result |
| --- | --- |
| `GET /v1/audit/events` | Newest-first retained events with bounded filters and pagination. |
| `GET /v1/audit/events/{event_id}` | One retained event in the caller's tenant; unknown or foreign IDs return 404. |
| `GET /v1/audit/export` | Evidence ledger entries and available payloads, in sequence order, with a fixed checkpoint. |
| `POST /v1/audit/verify` | Integrity result; accepts an optional independently retained `checkpoint`. |

List filters include `bucket`, `key`, `job_id`, `correlation_id`, `actor_id`,
`event_type`, `outcome`, `occurred_after` (inclusive), and `occurred_before`
(exclusive). `key` requires `bucket`. List/export pages allow `limit=1..200`.
Follow `page.next_cursor` until absent. Export reports `complete` explicitly;
one bounded page is not a complete archive. Its checkpoint stays fixed while
new access/export events are appended, so pagination can finish under traffic.
Preserve sequence order, payloads, tombstones, and the checkpoint together.

Every permitted disclosure records `audit.access`, `audit.export`, or
`audit.verification` with the actor and correlation ID before returning data.
If that append fails, the request fails closed. Rejections use
`authorization.decision`. Read authorization decisions are no longer sampled.
Self-audit events occur after the snapshot they describe; a later export will
include them. No audit record update/delete HTTP endpoints exist.

## Integrity verification and checkpoints

The integrity ledger assigns consecutive sequence numbers independent of event
timestamps, hashes every field of the persisted redacted event, and links each
entry to its predecessor and tenant. A durable head catches suffix loss; payload
digests catch edits, and sequence/previous-hash checks catch missing entries.
Retention records bind tombstones into the same chain. Deterministic replay
does not create a second entry.
Move identifiers and move sequence numbers also enter the hashed link; the
verifier compares mutable move-head bookkeeping with that evidence so a changed
causal head cannot silently redirect subsequent move history.

PostgreSQL catalog mutations acquire the tenant's integrity-head lock before
other catalog locks. This prevents competing writers from forking the chain or
deadlocking in opposite audit/move lock order, and serializes potentially
audit-producing catalog writes within one tenant. Independent access telemetry
append/prune transactions retain their existing row locks. Different tenant
schemas retain independent heads. Account
for this throughput tradeoff when sizing high-volume deployments.

Use a writable tenant catalog for operations that also need disclosure auditing;
the API manages this automatically. A trusted operator can inspect the internal
contract directly:

```python
from dataclasses import asdict
import json

# catalog is already bound to the authorized tenant.
result = catalog.verify_audit_integrity()
if not result.valid:
    raise RuntimeError(result.issues)
checkpoint_json = json.dumps(asdict(result.checkpoint), sort_keys=True)
# Save checkpoint_json to an independently controlled append-only archive.
```

A checkpoint contains `tenant_id`, `sequence`, `entry_hash`, and
`algorithm="sha256-v1"`. Supply the saved object to
`catalog.verify_audit_integrity(checkpoint=saved_checkpoint)` or POST
`{"checkpoint": saved_checkpoint}` to `/v1/audit/verify`. Later appended events
are allowed; the saved prefix must still match. `valid=false` or a failed
verification request requires investigation. `anchored=true` means a caller
supplied a checkpoint; its trust depends on that caller's archive.

To verify an exported archive without connecting to CogniStore, assemble every
page's `records` in the received sequence order and use the independently saved
checkpoint:

```python
from cognistore.core.audit_export import verify_audit_export

verification = verify_audit_export(all_records, saved_checkpoint)
if not verification.valid:
    raise RuntimeError(verification.issues)
```

The offline verifier checks full-chain completeness, every link and retained
payload, and authorized-retention tombstones. Missing pages, a deleted suffix,
reordered records, and altered payloads cannot pass as a complete export.
Malformed archives raise `ValueError`. An embedded checkpoint alone establishes
internal consistency; use your separately retained copy to detect replacement
of both the archive and its embedded checkpoint. If retention changes payloads
within an export's fixed checkpoint while paging, restart the export.

Verify after migration/import, before archiving, after restore, and during
periodic operational checks. Retain checkpoints separately from database
backups and test restoration against them. On a mismatch, preserve the current
database and the saved checkpoint for investigation; do not reset the ledger or
generate a new trusted baseline to hide the mismatch.

Existing history receives explicitly marked baseline entries at migration or
legacy import. Those entries protect the bytes observed at that time; they
cannot prove that older records were authentic or complete before the baseline.

## Database access controls

Run PostgreSQL migrations/imports as a trusted schema owner. Give application
processes a separate role without schema ownership, `CREATE`, superuser,
`BYPASSRLS`, or permission to disable triggers. Do not give that role membership
in the owner or maintenance role. Apply privileges in each tenant's physical
schema; the following uses `public` only as an example:

```sql
GRANT USAGE ON SCHEMA public TO cognistore_runtime, cognistore_audit_maintenance;
GRANT SELECT, INSERT ON public.audit_events, public.audit_integrity_entries
  TO cognistore_runtime, cognistore_audit_maintenance;
GRANT SELECT ON public.audit_event_tombstones
  TO cognistore_runtime, cognistore_audit_maintenance;
GRANT INSERT ON public.audit_event_tombstones TO cognistore_audit_maintenance;
GRANT SELECT, UPDATE ON public.audit_integrity_head
  TO cognistore_runtime, cognistore_audit_maintenance;
REVOKE UPDATE, DELETE, TRUNCATE ON public.audit_events,
  public.audit_event_tombstones, public.audit_integrity_entries
  FROM cognistore_runtime, cognistore_audit_maintenance;
GRANT EXECUTE ON FUNCTION public.cognistore_prune_audit_events(uuid[])
  TO cognistore_audit_maintenance;
```

These audit-specific grants supplement ordinary catalog privileges. The
maintenance role uses the catalog retention methods, which create the required
tombstone/evidence and call the narrowly scoped deletion function. Its
`SECURITY DEFINER` function pins its schema search path, and migration revokes
`PUBLIC` execution. Runtime roles cannot enable retention by setting a session
flag. Immutable-table triggers also reject accidental updates, deletes,
truncation, and head rollback. Only trusted operators should own or alter these
functions, tables, and triggers.

SQLite uses immutable-table triggers and a connection-local retention
capability registered by the catalog. Raw SQL connections cannot update,
delete, or replace retained records. Protect the database and its journal files
with operating-system permissions: a process that can register arbitrary SQLite
functions, drop triggers, or replace the file is a trusted database administrator.
The in-memory catalog provides the same evidence/verification contract for
development; it is not durable storage.

## Retention

Audit events expire 30 days after occurrence by default. Configure a positive
maximum age in seconds with `--audit-retention-max-age`, the
`COGNISTORE_AUDIT_RETENTION_MAX_AGE` environment variable, or the
`audit_retention_max_age` profile key. Programmatic catalog users can pass
`AuditRetentionPolicy(None)` for no automatic expiry.

Expiry is metadata until an operator or maintenance loop prunes it. Deletion is
bounded and repeatable:

```python
from datetime import datetime, timezone

while catalog.prune_expired_audit_events(
    datetime.now(timezone.utc),
    limit=1_000,
):
    pass
```

`prune_audit_events(occurred_before, limit=...)` is also available for an
explicit occurrence-time cutoff. Both methods delete the oldest eligible rows
first.

Retention is an explicit maintenance operation. It first records immutable
retention evidence and replay tombstones and emits `audit.retention`; only then
can eligible event payloads be removed. Tombstones, integrity entries, and the
head outlive payload retention. Expiry does not permit ordinary SQL deletes.
Retain a verified export/checkpoint before pruning when full payloads are
required for the archive. A tombstone proves authorized pruning and replay
identity, but cannot reconstruct the expired event details.

`audit.retention` summaries are retained indefinitely. Excluding those summaries
from pruning prevents a maintenance loop with a future cutoff from continually
pruning and recreating its own summary.

## Sensitive values

The catalog redacts credential-bearing keys, URLs, authorization/cookie values,
private keys, NUL characters, and textual secret assignments before an event
reaches SQL. Sensitive-looking or oversized query dimensions are replaced by
distinct, stable SHA-256 pseudonyms rather than one lossy placeholder, so two
operations cannot collapse onto the same correlation key and callers can query
with the original value. Event types, outcomes, and actor types use a bounded
identifier-style vocabulary instead of accepting persistable free text. SQLite
imports repeat the same validation and redaction at the destination boundary.

Pseudonymization is not encryption and low-entropy values remain guessable.
Producers must still keep secrets out of identifiers and audit details;
redaction is a safety boundary, not a reason to submit credentials. Worker and
move failure events store structured classifications and exception types, not
exception messages or tracebacks.

The exact `[REDACTED:sha256:<hex-digest>]` syntax is reserved for catalog-owned
pseudonyms. Raw identifiers using that syntax are rejected, which keeps a
literal identifier from aliasing a secret-derived identity. Query with the
original identifier rather than copying its stored pseudonym.
