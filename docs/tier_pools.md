# Tier pools and placement eligibility

A **tier** is a logical storage class such as `hot` or `warm`. A **pool** is a
named physical destination within one tier. A tier can contain multiple pools,
each with its own region, members, locality labels, and observed attributes.
Pool IDs are globally unique in the catalog. Members are opaque storage resource
identifiers; registration records membership and does not configure drivers or
provision storage. A member may appear in more than one pool.

Application and policy code uses `CatalogStore`. Its in-memory, SQLite, and
PostgreSQL implementations expose the same topology operations; callers do not
need database tables or a concrete `SQLCatalog` dependency.

## Register and inspect topology

```python
from cognistore.core.catalog import Catalog, CatalogStore
from cognistore.core.topology import AttributeValue

catalog: CatalogStore = Catalog()  # A durable catalog can be injected instead.
catalog.register_tier("hot", {"description": "Low latency storage"})
catalog.register_pool(
    "hot-west",
    "hot",
    region="us-west-2",
    members=("ssd-west-1", "ssd-west-2"),
    localities=("US", "tenant-a"),
    attributes={
        "latency": AttributeValue(
            value=3.5,
            unit="ms",
            source="probe/p95-read",
            kind="measured",
            observed_at="2026-09-08T12:00:00Z",
            max_age_seconds=300,
        ),
    },
)

assert catalog.get_pool("hot-west").members == ("ssd-west-1", "ssd-west-2")
hot_pools = catalog.list_pools(tier="hot")
tiers = catalog.list_tiers()
```

An active pool requires a nonempty region, at least one member, and an active
registered tier. Names must be nonempty and have no surrounding whitespace.
Duplicate members and locality labels are rejected.
An inactive pool may lack its region or members, but registering it still
requires an active tier. Attributes are optional; missing topology on an active
pool raises `ValueError`, while missing attributes are reported to policies.

`get_tier()` and `get_pool()` return `None` for unknown names. Listings are
deterministic and include inactive records, so configuration tools can inspect
and repair retained topology. Returned models are detached catalog snapshots;
changing their nested metadata or attribute mappings does not update the store.
Persist changes through registration methods.

Registering an existing pool replaces its region, members, localities,
attributes, metadata, and active flag. To replace membership while retaining
other fields, read the pool and pass its other fields back to `register_pool()`.
Omitting these arguments uses empty defaults and can therefore fail validation
for an active pool. For `register_tier()`, omitted metadata preserves existing
metadata; an explicit `{}` clears it.

## Attributes, provenance, and freshness

Each `AttributeValue` has a finite non-negative numeric `value`, an exact `unit`,
a nonempty `source`, a `kind` of `configured` or `measured`, a timezone-aware
ISO-8601 `observed_at`, and a finite positive `max_age_seconds`. Booleans are not
numeric values. Timestamps normalize to UTC; naive or invalid timestamps are
rejected. Both configured values and measurements expire.

| Attribute | Required unit | Interpretation |
| --- | --- | --- |
| `latency` | `ms` | Latency statistic identified by the source, such as p95 read latency |
| `capacity` | `bytes` | Pool capacity estimate; the source identifies its usable/free/total meaning |
| `price` | `USD/GiB-month` | Storage rate estimate, separate from billing reconciliation |
| `carbon_intensity` | `gCO2e/kWh` | Electricity carbon intensity estimate, not per-object emissions |

Unknown attribute names and mismatched units raise `ValueError` during pool
validation. Unit conversion is explicit caller work; a value in seconds cannot
be silently interpreted as milliseconds. Sources identify how values were
obtained; registration does not fetch live measurements or reconcile bills.

`attribute.freshness(now=...)` returns `fresh` when its age is between zero and
`max_age_seconds`, inclusive, `stale` when older, and `future` when its timestamp
is later than the evaluation time. `attribute.is_fresh(now=...)` is the boolean
equivalent. Supplying `now` makes evaluation reproducible; its default is the
current UTC time. Candidate `attribute_states` includes all four supported
attribute names, with `missing` for absent values. Missing, stale, and future
values are never converted to zero or implicitly treated as current.

## Hard constraints precede scoring

`PlacementConstraints` contains hard region, tier, and pool allowlists, plus
required locality labels. `None` means an unrestricted allowlist; an empty tuple
means no candidates. Every required locality must occur on the pool. Matching
is exact and case sensitive, and no geographic hierarchy or regulatory
certification is inferred from a label.

```python
from cognistore.core.topology import PlacementConstraints, rank_placements

constraints = PlacementConstraints(
    allowed_regions=("us-west-2",),
    required_localities=("US", "tenant-a"),
    allowed_tiers=("hot", "warm"),
)
now = "2026-09-08T12:01:00Z"
candidates = catalog.eligible_placements(constraints, now=now)

def latency_score(candidate):
    if candidate.attribute_states["latency"] != "fresh":
        return 1_000_000  # An explicit policy penalty for unavailable evidence.
    return candidate.pool.attributes["latency"].value

ranked = rank_placements(catalog, constraints, latency_score, now=now)
```

Eligibility first excludes inactive tiers, inactive or incomplete pools, and
every destination outside the hard constraints. Only then does
`rank_placements()` invoke the supplied score callback. A faster or cheaper
pool outside the allowed region cannot win. Eligible enumeration sorts by tier
name then pool ID. Ranking uses ascending finite scores and breaks ties by the
same names; negative scores are allowed, while booleans, NaN, and infinity are
rejected.

Missing or stale optimization attributes do not themselves remove a destination
from topology eligibility. Policies can inspect states and explicitly penalize
or reject such evidence. Optimization objectives and budgets remain separate
from hard constraints; the helper does not implement a budget optimizer.

## Atomic object assignment and lifecycle

`catalog.assign_pool(bucket, key, pool_id)` assigns an existing object to an
active pool and atomically changes both its pool and tier to match that pool.
Reassignment to a pool in another tier uses the same operation. Passing `None`
clears the pool assignment and retains the object's current tier. Unknown
objects or pools raise `KeyError`; inactive destinations are rejected.
Assignment updates the catalog's control-plane record only; it does not
transfer payload bytes. Physical transfer remains the mover's responsibility.

Existing writes that specify only a tier retain a pool when the tier stays the
same and clear it when the tier changes. This applies to upserts, placement
updates, scanner observations, and move placement commits. Backend transaction
and locking rules prevent a concurrent assignment or pool reparent operation
from producing a placement whose tier differs from its pool's tier.

- A pool with assigned objects cannot be deleted, moved to another tier, or
  deactivated. Reassign or clear those objects first.
- A tier with current object placements or active pools cannot be deactivated.
- A tier with any current placements or pools cannot be deleted; remove its
  pools first, including inactive pools.
- Historical move-journal source and destination tier names are intentionally
  retained strings. They do not block deletion or deactivation of a tier that
  has no active placement references.

These rules distinguish current constrained references from audit history. A
completed move can retain an old tier name after that tier is removed. New
placement writes still require an active destination tier.
For compatibility, tier-only writes automatically register a previously unseen
tier as active; they reject an explicitly inactive tier.

## JSON and YAML configuration

[The YAML example](../examples/tier_pools.yaml) and
[equivalent JSON example](../examples/tier_pools.json) define two pools in `hot`
and one in `warm`, with distinct regions and illustrative measured/configured
attributes. Their timestamps are fixed examples and will become stale.

```python
from cognistore.core.topology_config import load_topology_config, register_topology

topology = load_topology_config("examples/tier_pools.yaml")
register_topology(catalog, topology)
```

`TopologyConfig.from_mapping()` accepts already parsed configuration.
`register_topology()` also accepts that mapping directly. Configuration is
self-contained: every pool's tier must be declared in its `tiers` list and
active for registration. All models, duplicate names, references, and units are
validated before the first write. Unknown fields are errors so configuration
typos do not silently disappear.

Registration is additive; omitted catalog entries remain. The helper uses
`CatalogStore.register_tier()` followed by `register_pool()`. Each operation is
atomic, but the whole configuration is not a batch transaction. An existing
catalog lifecycle conflict or a storage failure can stop later registrations
after earlier valid entries have been applied.

## Existing catalogs

The topology migration preserves existing object placements and historical
move journals. Legacy pools have no trusted region or membership, so they are
retained as **inactive** with incomplete topology. Their existing assignments
remain inspectable but are excluded from eligible destinations; fresh
assignments require full configuration and explicit activation. No region or
membership is inferred from arbitrary legacy metadata.

The migration retains the existing composite pool/tier foreign key for current
placements and adds activity, region, members, locality labels, and attribute
provenance.
See [PostgreSQL catalog operations](postgres_catalog.md) for migration and
import behavior. Historical move-journal tier names intentionally remain
outside these current-placement foreign keys.
