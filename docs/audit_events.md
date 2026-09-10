# Operational audit events

CogniStore records an append-oriented operational history in the catalog so an
operator can follow a policy decision through its job and object move, including
retries and terminal outcomes. This history is operational evidence; it is not
tamper-evident storage and does not replace application logs or backups.

## Event contract

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

`correlation_id` groups one operation. `causation_id` points to the event that
directly caused another event. Policy-selected moves therefore begin with a
`policy.decision` event and their move lifecycle points back to that decision;
background jobs also carry their durable job ID on every event. Manual CLI
moves and inline policy runs begin with `manual.action`. A catalog-assigned,
monotonic move sequence serializes every decision, manual resume, retry, and
transition for one move, so the terminal event's causation path includes the
whole operational history even when timestamps tie.

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
