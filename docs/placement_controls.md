# Importance, residency, and tier stability

CogniStore applies importance and minimum residency as hard movement constraints
before policy preferences. They use authoritative catalog state and apply to
synchronous evaluations, background policy jobs, and durable moves. Dry-runs and
API evaluations expose the same constraint evidence without changing placement.
Optional cooldown and numerical hysteresis reduce repeated tier changes when
policy inputs fluctuate around a boundary. These stability controls default to
zero, preserving existing behavior when they are not configured.

## Object importance

An object's optional `importance` is a typed catalog field, separate from
ordinary object metadata. It carries `level`, `actor_type`, `actor_id`,
`provenance`, and `updated_at`. Changing it increments `importance_revision`
and atomically appends an `importance.changed` audit event. Scans, content
replacement, and moves preserve the tag; raw storage metadata cannot set or
clear it.

Default destination restrictions are:

| Importance | Allowed destination tiers |
| --- | --- |
| Missing, `low`, or `normal` | Unrestricted by importance |
| `high` | `hot`, `warm` |
| `critical` | `hot` |

Policy configuration can override each level's allowed destinations with
`movement_constraints.importance_tiers`. Unspecified levels retain their
defaults. The effective destinations are the intersection of the importance
restriction and the policy's `allowed_tiers`. A policy proposing another tier
returns `stay` with an importance reason. Importance constrains movement; adding
a critical tag does not immediately relocate bytes or bypass an active residency
timer. An empty tier list permits no movement for that level.

Set or clear importance through the CLI:

```bash
cognistore --drivers drivers.yaml --catalog-db catalog.sqlite3 \
  importance-set demo-bucket reports/current.txt critical \
  --actor operator@example.com --provenance 'Current incident response material' \
  --policy simple --json

cognistore --drivers drivers.yaml --catalog-db catalog.sqlite3 \
  importance-set demo-bucket reports/current.txt clear \
  --actor operator@example.com --provenance 'Incident closed' --json
```

`LEVEL` accepts `low`, `normal`, `high`, `critical`, or `clear`. Each change
requires an actor and provenance. The command reevaluates that object with the
selected policy and returns its decision; it does not execute that decision.
A later policy run executes an eligible move.

The REST equivalent is `POST /v1/catalog/importance`:

```json
{
  "bucket": "demo-bucket",
  "key": "reports/current.txt",
  "level": "critical",
  "actor_id": "operator@example.com",
  "provenance": "Current incident response material",
  "config": {
    "policy": "simple",
    "allowed_tiers": ["hot", "warm"],
    "movement_constraints": {
      "minimum_residency_seconds": {"hot": 3600},
      "importance_tiers": {"critical": ["hot"], "high": ["hot", "warm"]}
    }
  }
}
```

Use `"level": null` to clear a tag. The response is a policy evaluation with
constraint evidence. The update and reevaluation are audited with the same
request correlation ID. Reevaluation uses the exact committed tag revision, so
a concurrent later edit cannot replace the decision attributed to this change. `POST /v1/policies/evaluate` and
`POST /v1/actions/policy-runs` accept the same policy configuration.

## Residency clock and duration

`placement_started_at` records when the object entered its current tier. A new
catalog object starts its clock at publication. Changing tiers starts a new
clock, including an authoritative move commit. Repeated scans, same-tier
content writes, metadata changes, importance updates, and pool changes within
the same tier preserve the clock. Deleting and recreating a logical object
starts a new placement.

Migration uses an existing placement's last recorded update as a conservative
start. Prototype-import sentinel timestamps remain unknown; a configured timer
then fails closed. Current catalog imports preserve the explicit clock and tag.

The effective duration is the maximum of:

- `minimum_residency_seconds` in the current tier's catalog metadata;
- that tier's entry in policy configuration
  `movement_constraints.minimum_residency_seconds`.

Both durations are optional and default to zero. Values must be integers from
0 through 315,360,000 seconds; booleans, fractional values, negative values,
and larger values are rejected. Residency applies to leaving the current tier,
including promotion. It does not delay staying in that tier.

Movement is blocked while evaluation time is strictly earlier than
`placement_started_at + minimum_residency_seconds`. Movement is eligible at
the exact expiry boundary, subject to importance and the policy's remaining
rules. A nonzero duration with an unknown placement start fails closed; elapsed
residency is not inferred from object modification times or incomplete access
history. A future placement start also remains blocked until its expiry.

Use repeatable CLI options to configure a policy pass:

```bash
cognistore --drivers drivers.yaml --catalog-db catalog.sqlite3 \
  policy-run demo-bucket --dry-run --json \
  --minimum-residency hot 3600 --minimum-residency warm 86400 \
  --importance-tier high hot --importance-tier high warm \
  --importance-tier critical hot
```

These options also apply to `importance-set` reevaluation. The API uses the
nested `config.movement_constraints` object shown above. A tier-wide baseline
can be configured through the backend-neutral catalog API:

```python
catalog.register_tier("warm", metadata={"minimum_residency_seconds": 86400})
```

Pass any other desired tier metadata in the same mapping because registration
replaces supplied metadata.

## Cooldown and hysteresis

The same `movement_constraints` object accepts these stability settings:

| Setting | Default | Accepted values | Effect |
| --- | --- | --- | --- |
| `cooldown_seconds` | `0` | Integer, 0–315,360,000 | Delay another tier change after the last authoritative tier change. |
| `size_hysteresis_bytes` | `0` | Integer, 0–9,223,372,036,854,775,807 | Require a size change beyond a margin around the policy's size threshold. |
| `similarity_hysteresis` | `0.0` | Finite number, 0–2 | Require a similarity change beyond a margin around an embedding rule's threshold. |
| `stability_override` | `null` | Object with `kind` and `reason` | Apply an explicit emergency or compliance exception to stability controls. |

Booleans and numeric strings are rejected. Integer settings reject fractional
values. Stability controls apply to synchronous policy runs, HTTP previews,
queued jobs, scheduled runs, and durable move recovery.

Cooldown uses the catalog's persisted `last_tier_move_at`, independently of
`placement_started_at` and the object's ordinary update timestamp. A newly
cataloged object has no previous tier change and starts without a cooldown.
Authoritative changes to a different tier reset the movement clock; same-tier
scans, metadata refreshes, and content writes preserve it. A policy dry-run,
blocked decision, or move that fails before placement commit does not start a cooldown. Movement is eligible
at the exact cooldown expiry, subject to the other constraints. A future clock
stays active until its expiry. Cooldown evidence reports its anchor, duration,
expiry, and whether it blocked the candidate movement.
Migration conservatively initializes existing placements from their saved
placement start, or migration time when that start is unknown.

For a size threshold `T` and margin `B`, an object in `hot` moves to `warm`
only when its size is strictly greater than `T + B`. An object in `warm` moves
to `hot` when its size is at most `T - B`. Thus a 100-byte threshold and 10-byte
margin retain `hot` through 110 bytes and retain `warm` above 90 bytes. Objects
outside `hot` and `warm` use the ordinary size threshold for initial placement.

For an embedding threshold `T` and margin `H`, a rule targeting the current tier
remains eligible at similarity `T - H` or higher. A rule targeting another tier
requires similarity `T + H` or higher. The existing rule ordering still resolves
multiple eligible matches. Name and MIME rules retain their ordinary precedence;
they have no numerical margin. Hysteresis evidence records the numerical input,
configured margin, effective boundary, and resulting decision.

Size bands apply to the simple policy, content fallback, and explicitly supplied
historical threshold provider. The schema-validated `llm` policy has no defined
numeric boundary; its moves are protected by cooldown, while numeric bands
remain inapplicable. See [LLM-assisted placement](llm_placement.md).
Suppressed decisions carry a `suppression_reason` of `cooldown` or `hysteresis`
in their constraint evidence and policy audit record, and produce a structured
`policy move suppressed` log entry. Read-only previews use their evaluation
instant; executing a previewed action rechecks cooldown at the current time.

For example, a REST policy preview accepts:

```json
{
  "bucket": "demo-bucket",
  "key": "reports/current.txt",
  "config": {
    "policy": "simple",
    "threshold": 1048576,
    "movement_constraints": {
      "cooldown_seconds": 3600,
      "size_hysteresis_bytes": 65536,
      "similarity_hysteresis": 0.05
    }
  }
}
```

This is a `POST /v1/policies/evaluate` request. For
`POST /v1/actions/policy-runs`, replace `key` with the desired `prefix`.
The SDK exposes the same typed fields:

```python
from cognistore.sdk import MovementConstraintsConfig, PolicyConfig

config = PolicyConfig(
    threshold=1048576,
    movement_constraints=MovementConstraintsConfig(
        cooldown_seconds=3600,
        size_hysteresis_bytes=65536,
        similarity_hysteresis=0.05,
    ),
)
```

The CLI exposes these settings on `policy-run` and `importance-set`:

```bash
cognistore --drivers drivers.yaml --catalog-db catalog.sqlite3 \
  policy-run demo-bucket --dry-run --json \
  --cooldown-seconds 3600 --size-hysteresis-bytes 65536 \
  --similarity-hysteresis 0.05
```

## Emergency and compliance exceptions

An explicit exception can bypass cooldown and numerical hysteresis. It never
bypasses minimum residency, importance restrictions, or allowed-tier checks.
Set `movement_constraints.stability_override` to an object such as:

```json
{
  "kind": "emergency",
  "reason": "Relocate incident response records under incident 45"
}
```

`kind` must be `emergency` or `compliance`. `reason` must be nonempty UTF-8 text
without outer whitespace or control characters, and at most 2,048 UTF-8 bytes.
The SDK exports `StabilityOverrideConfig` for constructing this object.
The exception and its reason appear in decision evidence and in audit records
for executed policy decisions and moves. Actor and correlation attribution use
the existing audit context. Previews expose the exception without writing an
audit event. Durable jobs and move retries retain the originally supplied
exception, including its reason.
On the CLI, use `--stability-override emergency` or
`--stability-override compliance` together with
`--stability-override-reason 'Approved request 45'`; both options are required.

## Direct catalog and policy APIs

Use `ImportanceTag` and `CatalogStore.set_importance` to change trusted
classification. The tag actor must match the audit actor:

```python
from datetime import datetime, timezone

from cognistore.core.audit import AuditContext
from cognistore.core.placement_controls import ImportanceTag, MovementConstraints

now = datetime.now(timezone.utc)
context = AuditContext(
    correlation_id="incident-42",
    actor_type="user",
    actor_id="operator@example.com",
)
tag = ImportanceTag(
    level="critical",
    actor_type=context.actor_type,
    actor_id=context.actor_id,
    provenance="Current incident response material",
    updated_at=now.isoformat(),
)
record = catalog.set_importance(
    "demo-bucket", "reports/current.txt", tag,
    audit_context=context, occurred_at=now,
)
controls = MovementConstraints(minimum_residency_seconds={"hot": 3600})
```

Clear through the catalog method with `tag=None` and an explicit reason:

```python
catalog.set_importance(
    "demo-bucket", "reports/current.txt", None,
    audit_context=context, provenance="Incident closed", occurred_at=now,
)
```

Pass `movement_constraints=controls` when constructing `PolicyRunner`.
`evaluate_once(bucket, key, as_of=now)` provides a deterministic, side-effect-free
evaluation. `reevaluate_object(bucket, key, as_of=now)` also records its policy
decision audit. Changing a tag through the raw catalog method does not implicitly
invoke a policy; callers choose and invoke the appropriate runner afterward.

The evidence includes evaluation time, placement start, effective duration,
expiry, whether residency is active, the complete importance tag and revision,
and allowed destination tiers. Evaluate the same detached state, configuration,
and timestamp to reproduce boundary decisions. See
[Policy features](policy_features.md) for MIME, embedding, and access evidence.

Writable decisions also retain the versioned dataset snapshot introduced by #46.
Snapshot v1 does not encode importance, residency, or stability inputs for offline replay;
decisions influenced by these guards therefore report replay as unsupported
with `movement_constraints_not_in_snapshot_v1`. Their audit events retain the
constraint evidence, and their decision/outcome records remain exportable.

Before transfer and authoritative placement commit, moves recheck current
catalog constraints. Their durable source contract retains configured controls,
so retries and recovery preserve residency, importance, and stability enforcement. Changes
after planning therefore cannot silently reuse an obsolete permission to move.
Recovery of an already committed placement can still finish its cleanup.

## Scheduled and queued policies

A schedule stores controls in its policy payload:

```yaml
jobs:
  place-reports:
    type: policy.run
    enabled: true
    interval_seconds: 900
    payload:
      bucket: demo-bucket
      prefix: reports/
      policy: simple
      threshold: 1048576
      allowed_tiers: [hot, warm]
      movement_constraints:
        minimum_residency_seconds: {hot: 3600, warm: 86400}
        cooldown_seconds: 3600
        size_hysteresis_bytes: 65536
        similarity_hysteresis: 0.05
        importance_tiers:
          high: [hot, warm]
          critical: [hot]
```

Any policy payload containing `movement_constraints` uses envelope schema v3,
including an explicitly supplied default configuration. Workers reject controls
in schema v1 or v2 and reject malformed controls before creating policy or move
resources. Jobs without explicit controls remain v1, or v2 when they contain
embedding rules. Current workers still enforce authoritative importance and
tier metadata defaults on these older payloads.

Upgrade and drain or stop older workers sharing a durable consumer before
enabling importance, residency, or stability controls, including catalog defaults. Older workers do
not enforce those defaults on v1 jobs, and reject unsupported v3 envelopes.
Scheduled occurrences persist their complete normalized controls and schema;
publication recovery and dead-letter redrive retain the original envelope even
when the schedule configuration has since changed. See
[Background workers](background_workers.md) for scheduler operation and recovery.
