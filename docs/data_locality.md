# Data locality enforcement

Set `COGNISTORE_LOCALITY_CONFIG` to a server-owned JSON file to constrain physical
movement destinations by tenant, object prefix, region, and locality labels.
The file is reread at each decision; replacement or revocation applies to queued
moves as well as new requests. [The complete example](../examples/data_locality.json)
includes tenant rules, an object rule, tier-to-pool bindings, and one illustrative
exception. Its timestamp is an example, not a standing approval.

```sh
export COGNISTORE_LOCALITY_CONFIG=/etc/cognistore/data-locality.json
```

When this variable is unset, new ordinary moves retain the legacy behavior.
When it is set, an unreadable or malformed file denies movement. An empty path
is a configured error, not an instruction to disable enforcement. A durable job
created under locality enforcement remains constrained during recovery: removing
the variable does not allow that job to resume without its policy. A requested
exception also fails closed when the policy is unavailable.

## Trusted configuration

```json
{
  "version": 1,
  "tenants": {
    "default": {
      "allowed_regions": ["eu-west-1"],
      "required_localities": ["EU"],
      "tier_pools": {"hot": "hot-eu", "cold": "cold-us"},
      "objects": [
        {
          "bucket": "records",
          "key_prefix": "patient/",
          "required_localities": ["regulated"]
        }
      ],
      "exceptions": []
    }
  }
}
```

The top-level fields `version` and `tenants` are required; version must be the
integer `1`. Every tenant entry requires `tier_pools`. Unknown fields, duplicate
JSON keys, duplicate names in an allowlist, duplicate exception IDs, invalid
timestamps, and files larger than 1 MiB are rejected. Malformed entries anywhere
in the file invalidate the file. Tenant IDs follow the existing
[tenant identity contract](tenancy.md). An unlisted tenant receives no allowed
destinations; another tenant's rules or exceptions cannot authorize its moves.

`allowed_regions` is an optional exact, case-sensitive list. Omitted or `null`
means unrestricted; `[]` permits no region. `required_localities` is an optional
list, empty by default. Every label must appear on the destination pool.
Locality labels have no inferred geographic hierarchy or regulatory meaning.

`objects` is an optional list of rules with an exact `bucket` and a `key_prefix`.
An empty prefix matches the entire bucket. Every matching object rule and the
tenant rule apply together: allowlists intersect and required localities
accumulate. A more specific rule cannot relax a tenant requirement. These are
server-owned rules; object metadata, policy model output, and caller-supplied
region labels cannot broaden them.

## Bind actual drivers to physical pools

The current mover has one driver per tier. `tier_pools` therefore binds each
movable tier to exactly one physical pool for this tenant. The destination tier
must exist and be active, and its configured pool must exist, be active, belong
to that tier, and have a region and at least one member. Unbound tiers are
ineligible. Other pools in the same tier do not provide alternative routes.
The mover preserves the selected pool in the durable job and checks the current
binding again before continuing; a changed binding cannot redirect queued work.

Operators must ensure the binding describes the actual configured driver's
physical destination. Pool registration and this policy do not configure cloud
resources, inspect cloud endpoint geography, or verify a provider's region
claims. Use trusted deployment configuration and inventory to maintain that
association. These constraints govern destination selection; an unknown source
region can be remediated by moving into a known, allowed destination.

## Require fresh locality evidence

Register trusted locality provenance in each destination pool's metadata:

```python
from datetime import datetime, timezone

catalog.register_pool(
    "hot-eu", "hot", region="eu-west-1", members=("storage-resource-eu",),
    localities=("EU", "regulated"),
    metadata={
        "locality_evidence": {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "max_age_seconds": 3600,
            "source": "deployment-inventory/verified-region",
        }
    },
)
```

`locality_evidence` requires exactly `observed_at`, `max_age_seconds`, and
`source`. The timestamp must include a timezone, maximum age must be finite and
positive, and source must be a nonempty provenance string. Evidence is fresh
when its age is between zero and the maximum age, inclusive. Missing, malformed,
future-dated, or stale evidence denies that destination, even when there are no
geographic restrictions. Refresh it through the trusted topology workflow.
Ordinary optimization observations such as latency or carbon intensity do not
substitute for this evidence. See [pool topology](tier_pools.md).

## Explicit, scoped exceptions

An exception is a server-owned approval in one tenant's `exceptions` list:

```json
{
  "id": "emergency-recovery-64",
  "bucket": "records",
  "key": "patient/one",
  "destination_pool_id": "cold-us",
  "issuer": "https://identity.example.test",
  "subject": "approved-operator",
  "reason": "Approved emergency recovery",
  "expires_at": "2026-09-18T13:00:00Z"
}
```

All fields are required. The caller must explicitly select the approval ID; its
presence in the file alone has no effect. The approval must match the exact
tenant, issuer, subject, bucket, object key, and destination pool, and evaluation
must occur strictly before expiration. The authenticated identity must currently
hold both `administration` and `movement` permissions through the active
[RBAC authorizer](authorization.md). Trusted local process access without an
authenticated identity or authorizer cannot claim an exception.

An approval can relax region and required-locality rules only for its exact
destination. It cannot bypass unknown or inactive topology, missing or stale
evidence, a changed physical pool binding, or other movement constraints.
Ordinarily eligible destinations remain available. Selecting a missing,
expired, revoked, or unauthorized approval fails closed. File-backed RBAC grants
are reloaded, so queued work loses approval when the identity's grants change.

Select an approval using the trusted Python execution interfaces:

```python
from cognistore.auth.authorization import RBACAuthorizer, authorization_context
from cognistore.auth.principal import principal_context
from cognistore.core.mover import Mover
from cognistore.core.policy_runner import PolicyRunner

# The trusted authentication boundary has already verified verified_principal
# and selected catalog/drivers for that identity's tenant. Never construct this
# identity from unverified request fields or an unverified token payload.
authorizer = RBACAuthorizer(policy_path="/etc/cognistore/rbac.json")
mover = Mover(drivers, catalog)

with principal_context(verified_principal), authorization_context(authorizer):
    plan = mover.plan(
        "hot", "cold", "records", "patient/one",
        locality_exception_id="emergency-recovery-64",
    )
    result = mover.move(
        "hot", "cold", "records", "patient/one",
        idempotency_key="approved-recovery-64",
        locality_exception_id="emergency-recovery-64",
    )
```

For policy-driven movement, pass `locality_exception_id` to the `PolicyRunner`
constructor under the same authenticated principal and authorizer contexts:

```python
with principal_context(verified_principal), authorization_context(authorizer):
    runner = PolicyRunner(
        catalog, drivers, mover, policy,
        locality_exception_id="emergency-recovery-64",
    )
    preview = runner.preview_once("records", prefix="patient/one")
    results = runner.run_once("records", prefix="patient/one")
```

The prefix only selects candidate objects; the approval still matches one exact
key. Other objects selected by that prefix cannot use it. These examples show
alternative execution paths; use either the direct move or policy-driven path
for an object. The approved identity and current authorizer must also be bound
when resuming an exception-bearing move. Public request fields cannot create
an approval or grant permissions.

## Policy evaluation, movement, and audit

Hard locality eligibility is applied before policy scoring. Structured policy
explanations include the matching rules, allowed tiers and pools, rejected
destinations and reasons, current destination evidence, and any selected
exception. A cheaper or faster destination cannot override a locality denial.
Read-only evaluation and dry-run planning do not append persistent catalog audit
events or transfer bytes.

Movement checks the current policy at execution checkpoints as well as initial
selection, and durable jobs retain the expected physical destination. Failed
locality checks produce a `locality.decision` audit event with outcome `rejected`.
Used exceptions
produce `locality.exception` evidence identifying the authenticated actor by
the existing hashed `actor_id`, plus the approval, justification, expiry,
destination, and bypassed rules. Raw issuer and subject values stay in the
server-owned approval and are omitted from public decision and audit evidence.
Normal audit redaction applies. Evaluation itself is read-only; the movement boundary owns
these persistent audit events.

The low-level helpers are `evaluate_locality()` and
`assert_locality_allowed()` in `cognistore.core.locality`. The assertion raises
`LocalityConstraintError`, a `MovementConstraintError` subclass, carrying its
structured decision evidence. Optional `as_of` makes evaluation deterministic
for tests and previews; execution uses current time and current configuration.
