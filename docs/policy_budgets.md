# Budget-aware policy decisions and what-if simulation

CogniStore can enforce operator-supplied cost and carbon allowances before
admitting a move, rank destinations by cost, carbon, latency, and locality, and
compare policy configurations without changing objects or catalog state.

The API also exports [operational budget consumption and alerts](operational_slos.md#cost-and-carbon-budgets)
from the same versioned reservation ledger, with no re-estimation at scrape time.
These features use the versioned [storage estimator](storage_estimation.md);
they model usage charges in USD and operational electricity emissions in
gCO2e. They do not fetch provider prices or settle provider invoices.

## Define an allowance

`BudgetDefinition` has an immutable `budget_id`, UTC `period_start` and
`period_end`, an optional exact bucket and literal key prefix, and at least one
of `cost_limit_usd` or `carbon_limit_gco2e`. A missing bucket covers all buckets.
An empty prefix covers every object in that bucket. Periods include the start
and exclude the end: `[period_start, period_end)`.

Supply `opening_cost_usd` and `opening_carbon_gco2e` for commitments that already
consume the allowance. They default to zero; CogniStore cannot discover
existing provider usage automatically. `tier_pools` explicitly binds the
physical pool represented by each source and destination tier's driver.
`profiles` maps those pool IDs to versioned `EstimationProfile` objects.
The pool must exist, match its tier, be active, and match the source object's
assigned pool when the object has one. See [pool topology](tier_pools.md).
Overlapping budgets and an estimate policy must agree on the destination's
physical pool; conflicting bindings suppress the move instead of pricing one
pool and executing against another.

`workload` supplies each admitted object's request and transfer/retrieval
counts for the entire remaining period. All four counts must be explicit;
zero is valid, unknown is not. Stored bytes come from the object record and
duration comes from the time remaining until `period_end`. Observed historical
access is not automatically converted into a forecast.

For example, using `profiles` assembled as in the storage estimation guide:

```python
from cognistore.core.audit import AuditContext
from cognistore.core.budgets import BudgetDefinition
from cognistore.core.estimation import EstimationWorkload

budget = BudgetDefinition(
    budget_id="documents-2026-09",
    period_start="2026-09-01T00:00:00Z",
    period_end="2026-10-01T00:00:00Z",
    bucket="documents", prefix="reports/",
    cost_limit_usd="100", carbon_limit_gco2e="25000",
    opening_cost_usd="30", opening_carbon_gco2e="5000",
    tier_pools={"hot": "hot-west", "warm": "warm-west"},
    profiles=profiles,
    workload=EstimationWorkload(
        read_requests=100, write_requests=0,
        transfer_bytes=1073741824, retrieval_bytes=0,
    ),
)
catalog.configure_budget(
    budget,
    audit_context=AuditContext(
        correlation_id="budget-setup", actor_type="user", actor_id="operator@example.com",
    ),
)
```

`to_dict()` produces JSON-compatible values, including decimal strings and all
profile evidence. `BudgetDefinition.from_mapping()` validates parsed JSON.
Unknown fields, invalid units, nonfinite/negative amounts, and incomplete
workloads are rejected. Configuration requires an attributable user audit
context and emits an audit event. Existing IDs cannot be changed; an identical
registration is idempotent. Register a new ID for a new allowance period.
New definitions require no unfinished moves in their scope, so an already
running action cannot bypass a newly installed allowance. Native catalog
imports preserve budgets and reservations; schema downgrades refuse to discard
registered budget state.

All matching active budgets apply independently, including overlapping scopes.
An active period replaces expired periods of exactly the same bucket/prefix
scope for selection. A narrower expired scope still blocks moves when only a
wider scope has been renewed. A matching scope with no active period fails
closed. Objects outside every configured scope retain their existing behavior.

## What is charged and when

Each admission reserves the destination's full remaining-period holding and
workload estimate, plus modeled source and destination migration I/O. Migration
allows four full source reads, three full destination reads, and one write at
each endpoint. Source reads cover content hashing, copying, reading the retained
source during compensating restoration, and verifying the restored source.
Destination reads cover copy and cleanup verification with one spare read.
Transfer and retrieval are each four object sizes at the source and three
object sizes at the destination.
Metadata probes and backend-specific splitting into multiple requests are not
modeled. Source holding savings never refund an allowance.

For each impact total, a complete upper bound is charged when available;
otherwise the nominal estimate is charged and its unquantified uncertainty is
recorded. Missing, future, or stale evidence required by a configured limit
blocks admission. A configured zero usage quantity still requires a valid
rate, as in the estimator. Rates fresh at admission remain fixed in this
forecast through the period end; future rate changes are not predicted.

The check is `opening commitment + held reservations + new charge <= limit`.
Policy batch evaluation projects earlier selected moves into later decisions.
At execution, the catalog checks the current registered budget and available
balance atomically with move-job admission, before storage I/O. This protects
concurrent policy runs, queued jobs, direct mover calls, and resumes from
independently consuming the same remaining allowance.

Every nonterminal claim is charged again, conservatively covering possible
retry I/O. Completed or failed terminal lookups do not incur another charge.
Reserved amounts remain held after failure: there is no speculative refund
for partially performed work. This is a modeled allowance ledger, not billing
reconciliation. Retain sufficient headroom for retries and unmodeled costs.

Decision evidence retains limits, opening/held balances, projected charges,
binding constraints, and the exact estimator assumptions. Configuration and
admitted reservations are auditable. A Python `BudgetOverride(actor_id=...,
reason=...)` can permit an explicitly attributed numeric limit exception when
the move uses a matching user `AuditContext`; unknown charges and inactive
periods remain blocked. An override must identify the responsible operator
and explain the exception. CLI what-if does not apply overrides to live state.

## Choose among eligible destinations

`EstimatePolicy` integrates soft objective scoring with normal policy
evaluation. Configure the estimator, a complete forecast horizon, tier/pool
bindings, and `ObjectiveWeights(cost=..., carbon=..., latency=..., locality=...)`.
Weights must be nonnegative with at least one positive weight. `latency` is the
performance objective and reads the pool's fresh latency observation in ms.
Locality counts unmet preferred labels and a preferred-region mismatch;
pass `preferred_region` and/or `preferred_localities` when weighting it.

Hard `PlacementConstraints` (regions, locality requirements, tiers, and pools),
runner-allowed tiers, and importance restrictions filter candidates before
scoring. Cooldowns and minimum residency can suppress evaluation entirely.
Only the pool bound to a destination tier is scored for moving there. Same-tier
pool moves are not supported. An eligible current placement participates so an
already optimal object stays in place.

Each objective is min-max normalized across known candidate values; equal
values normalize to zero. The score is the weighted sum, and lower is better.
A missing positive-weight objective makes that candidate unscorable; an
unweighted unknown objective does not exclude it. Equal winning scores prefer
the current placement. If no candidate is scorable, the object stays. The
report retains raw values, normalized values, weights, rankings, selected
candidate, and cost/carbon profile evidence.

Budgets are hard checks on the selected destination. If the best-scoring
destination cannot obtain its allowance, the move is suppressed; this version
does not rerank candidates to search for a cheaper admissible alternative.

```python
from cognistore.core.budgets import ObjectiveWeights
from cognistore.core.impact_policy import EstimatePolicy
from cognistore.core.topology import PlacementConstraints

policy = EstimatePolicy(
    catalog, estimator, workload,
    tier_pools={"hot": "hot-west", "warm": "warm-west"},
    weights=ObjectiveWeights(cost="2", carbon="1", latency="1"),
    placement_constraints=PlacementConstraints(allowed_regions=("us-west-2",)),
)
# Pass policy to the existing PolicyRunner with its actual tier drivers.
```

## Compare scenarios without mutations

`simulate_policy(runner, bucket, prefix="", as_of=..., proposed_policy=...,
proposed_budget_definitions=...)` compares the current catalog placement,
baseline policy, and proposed policy against the same detached object snapshot
and timestamp. It never calls move planning, storage drivers, audit writers,
job creation, or reservation admission. Use read-only policy and feature
providers. `simulation_only=True` runners can be created with an empty driver
map and catalog tier names for this purpose.

Omit proposed budgets to inherit the baseline configuration. A supplied list
replaces the proposed configuration for the comparison only; an empty list
models removing budgets. Change objective weights by constructing a proposed
`EstimatePolicy`. The original runner and registered definitions remain intact.

The returned JSON-compatible report includes:

- Explicit `mode: simulation`, frozen time, scope, and configuration snapshots.
- Per-object current/baseline/proposed placements and estimate evidence.
- Affected objects and changes relative to current and baseline placement.
- Cost/carbon totals for all three scenarios and signed differences between them.
- Per-object decisions, binding movement/budget constraints, and objective rankings.
- Forecast, migration, missing-data, and uncertainty assumptions.

Supply `estimator` and `workload` together to compare both scenarios using the
same forecast; an optional `tier_pools` mapping resolves destination pools.
Otherwise simulation uses each scenario's estimate policy or feature loader.
As a fallback, exactly one active matching budget supplies its profiles and
remaining-period workload. Multiple matching budgets are ambiguous for this
aggregate forecast, so the fallback remains unavailable. All budgets still
apply to admission decisions. A tier-only decision with several possible pools
also has an unavailable destination estimate unless a binding resolves it.

Placement totals exclude one-time migration, which is reported separately in
budget charge evidence. Missing estimates remain `null`, and complete totals
or differences are unavailable whenever a contributing object is unknown.
`known_subtotal` excludes unknown components and is not a complete forecast.
Negative differences indicate reductions. Bounds are scenario intervals,
not statistical confidence intervals. Empty scopes total zero.

## CLI

Use an existing persistent catalog. The read-only commands do not load storage
drivers and never create or migrate a database.

```bash
cognistore --catalog-db catalog.sqlite --json budget-configure \
  --input september-budget.json --actor operator@example.com
cognistore --catalog-db catalog.sqlite --json budget-list
cognistore --catalog-db catalog.sqlite --json policy-what-if documents \
  --prefix reports/ --baseline-threshold 1048576 --threshold 2097152 \
  --allowed-tiers hot,warm --as-of 2026-09-11T12:00:00Z \
  --budget-file proposed-budgets.json
```

`budget-configure --input` accepts exactly one serialized budget definition.
`policy-what-if --budget-file` accepts one definition or a JSON list; `[]`
models no budgets. The CLI compares simple size-threshold policies. Use the
Python API to compare objective weights and explicit estimator forecasts.
Without unambiguous estimate evidence, the CLI still explains decisions and
constraints while reporting unavailable cost/carbon totals.
