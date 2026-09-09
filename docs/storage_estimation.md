# Storage cost and operational carbon estimates

`StorageImpactEstimator` estimates storage, request, transfer, and retrieval
charges in USD and operational electricity emissions in gCO2e for a supplied
workload. Profiles are keyed by physical pool ID, so pools in the same logical
tier can use different backend, region, pricing, and energy assumptions. See
[tier pools](tier_pools.md) for topology registration and hard eligibility.

The estimator is a read-only Python API. It does not fetch provider prices,
predict demand, move objects, enforce budgets, or choose a winning destination.
Operators supply and version their own evidence. The examples below are
**synthetic arithmetic fixtures, not live prices or measured carbon factors**.

## Estimate a workload

```python
from cognistore.core.catalog import Catalog
from cognistore.core.estimation import (
    EstimationProfile,
    EstimationWorkload,
    RATE_UNITS,
    RateAssumption,
    StorageImpactEstimator,
)

catalog = Catalog()
catalog.register_tier("hot")
catalog.register_pool(
    "hot-west", "hot", region="us-west-2", members=("example-device",),
)
pool = catalog.get_pool("hot-west")
assert pool is not None

values = {
    "storage_price": "0.02",
    "read_request_price": "0.000001",
    "write_request_price": "0.000002",
    "transfer_price": "0.03",
    "retrieval_price": "0.01",
    "storage_energy": "0.001",
    "read_request_energy": "0.0000001",
    "write_request_energy": "0.0000002",
    "transfer_energy": "0.002",
    "retrieval_energy": "0.001",
    "carbon_intensity": "400",
}
profile = EstimationProfile(
    version="synthetic-profile-v1",
    backend="example-object-storage",
    rates={
        name: RateAssumption(
            value=value,
            unit=RATE_UNITS[name],
            source="synthetic/documentation-fixture",
            source_version="fixture-v1",
            effective_from="2026-09-01T00:00:00Z",
            effective_until="2026-10-31T23:59:59Z",
        )
        for name, value in values.items()
    },
)
estimator = StorageImpactEstimator({"hot-west": profile})
workload = EstimationWorkload(
    stored_bytes=1073741824,
    duration_hours="730",
    read_requests=1000,
    write_requests=100,
    transfer_bytes=1073741824,
    retrieval_bytes=0,
)
as_of = "2026-09-09T12:00:00Z"
result = estimator.estimate(pool, workload, as_of=as_of).to_dict()
assert result["cost"]["value"] == "0.0512"
assert result["carbon"]["value"] == "1.248"
```

`backend` identifies the profile's modeled backend; it does not load or
configure a driver. Values may be decimal strings, `Decimal`, integers, or
finite floats. Prefer decimal strings for configuration. All numeric inputs
must be nonnegative; booleans, NaN, infinity, and unsupported decimal magnitudes
are rejected. Bytes and request counts must be integers; hours may be
fractional. Unknown mapping fields and mismatched units are errors.

`RateAssumption.from_mapping()`, `EstimationProfile.from_mapping()`, and
`EstimationWorkload.from_mapping()` accept parsed dictionaries, including those
obtained from JSON. Their `to_dict()` methods produce serializable snapshots.

## Units and formulas

A GiB is exactly 1,073,741,824 bytes. A modeled month is always 730 hours;
calendar month length and decimal GB are not inferred. Each workload contains
totals for one horizon, with a constant stored size over that horizon.

| Component | Workload quantity | Price unit | Energy unit |
| --- | --- | --- | --- |
| Storage | `stored_bytes / 1073741824 * duration_hours / 730` | `USD/GiB-month` | `kWh/GiB-month` |
| Reads | `read_requests` | `USD/request` | `kWh/request` |
| Writes | `write_requests` | `USD/request` | `kWh/request` |
| Transfer | `transfer_bytes / 1073741824` | `USD/GiB` | `kWh/GiB` |
| Retrieval | `retrieval_bytes / 1073741824` | `USD/GiB` | `kWh/GiB` |

Cost is the sum of quantity times effective price for each component. Carbon
is the sum of quantity times effective energy times effective
`carbon_intensity` in `gCO2e/kWh`. Calibration multiplies the underlying rate.
Price and carbon evidence resolve independently: unavailable energy evidence
does not invalidate an otherwise complete cost estimate.

`transfer_bytes` means traffic billed to the pool being estimated. The same
traffic may incur a separate retrieval charge, represented by
`retrieval_bytes`; neither field is inferred from the other. Pool profiles
provide the applicable rates. Candidate comparison applies the same supplied
traffic quantity to every candidate's profile. If routes or destinations have
different traffic quantities, estimate each pool with its own explicit
workload. This is not a route-aware source/destination migration quote.

Provider minimum charges, request classes, billing tiers, free allowances,
taxes, and early-deletion charges are not automatically modeled. Rates must
represent the intended scenario. Carbon covers operational electricity only;
it excludes embodied hardware emissions and lifecycle accounting. Avoid
double-counting energy when deriving storage, transfer, and retrieval factors.

## Evidence, dates, and unknown values

Each `RateAssumption` records its value, exact unit, `source`, `source_version`,
and inclusive `effective_from` / `effective_until` timestamps. Dates must be
timezone-aware and normalize to UTC. Use source identifiers that locate the
price sheet, measurement run, carbon dataset, or derivation, and immutable
versions that distinguish updates. Profile `version` identifies the complete
assembled assumption set. CogniStore does not verify a source externally.

Evaluation at explicit `as_of` returns `future`, `fresh`, or `stale` evidence.
Freshness is checked at that instant, and the resulting rate is applied over
the entire supplied horizon. The estimator does not split a horizon across
price changes; callers must divide changing-rate scenarios themselves.

An explicit profile rate takes precedence over topology, even when it is
expired. If a profile omits `storage_price` or `carbon_intensity`, the
estimator can use the pool's `price` or `carbon_intensity` attribute. Its
observation time and freshness window become the effective interval, and its
source version is `topology-attribute-v1`. Other rates have no implicit
fallback. No profile or attribute means missing evidence, not a free service.

Workload `None` means unknown. A component is `unavailable` if its workload,
rate, carbon intensity where needed, or configured calibration is missing or
not current. Its `value` is `null`, with machine-readable reasons such as
`workload.read_requests:missing`, `storage_price:stale`, or
`storage_energy:calibration_future`. Even explicit zero usage requires valid
rate evidence for an available component. To model a free component, supply a
current rate of zero with provenance.

Totals are available only when every component is available. Otherwise the
total `value` is `null`, while `known_subtotal` sums the available components.
Do not compare that subtotal to a complete total as if it were a complete
estimate. Each result retains all rate evidence and component reasons for
policy decisions and inspection.

## Calibration and uncertainty

`EstimationProfile.calibration` maps a rate name to a `RateAssumption` with
unit `multiplier`. The multiplier has its own provenance, version, and
effective interval. For example, a measured energy correction can be supplied
as follows:

```python
from dataclasses import replace

calibrated_profile = replace(profile, calibration={
    "storage_energy": RateAssumption(
        value="1.2", lower="1.1", upper="1.3", unit="multiplier",
        source="synthetic/calibration-fixture", source_version="fixture-v1",
        effective_from="2026-09-01T00:00:00Z",
        effective_until="2026-10-31T23:59:59Z",
    ),
})
```

Missing calibration means identity multiplier 1. Configured but expired
calibration makes the rate unavailable; it does not silently revert to 1.
For measured calibration, retain the measurement period, workload, backend,
energy boundary, and calculation in the referenced source, then publish a new
profile version when the assumptions change.

Optional `lower` and `upper` must be supplied together and contain the point
value. They represent nonnegative scenario intervals, not statistical
confidence intervals or probabilities. Bounds propagate by multiplying lower
endpoints together and upper endpoints together, then summing component
bounds. Every contributing rate and configured multiplier must supply bounds
for a component to have bounded uncertainty; all components must be bounded
for the total to be bounded. Omitting bounds leaves `uncertainty` as
`unquantified`, even when a point estimate is available. The example rates
above intentionally leave uncertainty unquantified; adding calibration bounds
alone does not supply missing base-rate bounds.

## Current placement, eligible candidates, and policy features

`estimate_object(catalog, record, workload, constraints, as_of=...)` returns an
`ObjectPlacementEstimates` with `current` and deterministic `candidates`.
The object record's `size` replaces `workload.stored_bytes` for every estimate.
Candidates come from `CatalogStore.eligible_placements()`: inactive pools and
destinations outside hard region, tier, pool, or locality constraints never
reach estimation. Eligible candidates sort by tier name then pool ID. The
current placement is reported separately and may also appear among candidates.

A current object without a resolvable, matching pool retains its known tier
and pool ID and reports `placement_pool_unavailable`; no destination is
invented. Current-placement reporting does not imply destination eligibility.
Missing optimization evidence does not itself remove an otherwise eligible
candidate. Policies must explicitly handle unavailable estimates before
ranking or comparing budgets.

`CatalogPolicyFeatureLoader` can attach this result to
`PolicyFeatures.placement_estimates`:

```python
from cognistore.core.policy_features import CatalogPolicyFeatureLoader
from cognistore.core.topology import PlacementConstraints

loader = CatalogPolicyFeatureLoader(
    impact_estimator=estimator,
    estimation_catalog=catalog,
    estimation_workload_factory=lambda record: workload,
    estimation_constraints=PlacementConstraints(allowed_regions=("us-west-2",)),
)
# records contains ObjectRecord snapshots from this catalog.
# features = loader.load(records, as_of=as_of)
# features[(bucket, key)].placement_estimates.to_dict()
```

Supply the estimator, catalog, and callable workload factory together.
Constraints are optional. Without estimation injection, the existing policy
projection remains unchanged and omits the serialized `placement_estimates`
field. The workload factory must supply an explicit usage forecast;
[observed access history](access_history.md) is not automatically extrapolated
into future reads, writes, or transfer. This injection exposes estimates for
policy consumption without changing existing placement rules.

Recorded policy decisions retain the estimates in their versioned feature
snapshots. Snapshot validation and offline replay preserve the component
values, unknowns, and assumption evidence without consulting live rates or
topology. Dataset export also retains estimate evidence, subject to the
configured privacy exclusions; see [policy datasets](policy_datasets.md).

## Deterministic output and replay

Every estimate includes `schema_version: 1` and a formula manifest named
`storage-impact-v1`, effective from `2026-09-09T00:00:00Z`. The manifest records
the implementation source, units, conversions, equations, scope, 50-digit
Decimal precision, and `ROUND_HALF_EVEN` rounding. Arithmetic uses a private
context independent of the caller's Decimal settings. Numeric values in
`to_dict()` are decimal strings, not JSON floating-point numbers; unknown
values remain `null`. Components and rates have a stable order.

Retain the serialized result and its original input snapshots to replay an
estimate. This example round-trips the standalone estimate above through JSON:

```python
import json
from cognistore.core.topology import Pool

saved = json.loads(json.dumps({
    "pool": pool.to_mapping(),
    "profile": profile.to_dict(),
    "workload": workload.to_dict(),
    "as_of": as_of,
    "result": result,
}))
replay_pool = Pool.from_mapping(saved["pool"])
replay_estimator = StorageImpactEstimator({
    replay_pool.pool_id: EstimationProfile.from_mapping(saved["profile"]),
})
replayed = replay_estimator.estimate(
    replay_pool,
    EstimationWorkload.from_mapping(saved["workload"]),
    as_of=saved["as_of"],
).to_dict()
assert replayed == saved["result"]
```

Retain the implementation revision supporting the recorded schema and formula
versions; a version label alone does not select historical code. Replaying
candidate enumeration also requires the original topology, object record,
constraints, and forecast. Re-evaluating a live catalog or using a new `as_of`
can change eligibility, evidence freshness, and results.
