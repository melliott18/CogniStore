# Structured policy decision reasons

Every policy decision persisted by `PolicyRunner` has a machine-readable
`details.structured_reason`. It records the rule or constraint responsible for
the evaluated action, the decisive measurements, and the policy/model identity.
It is captured with the original decision and remains unchanged when the same
logical decision is retried. It does not reconstruct an explanation from the
object's current metadata.

The [placement explanations API and UI](placement_explanations.md) expose these
reasons beside frozen placement diffs and separately derived execution status.

Normal planning persists every evaluated decision, including moves, stays,
constraint suppressions, and rejected destinations, before storage preflight.
Each completed evaluation is retained immediately, so an exception in a later
custom policy evaluation cannot discard earlier decisions in the batch.
If preflight fails, the decisions remain available even though no bytes moved.
This also means a selected decision is evidence of selection, not evidence of
successful execution. `evaluate_once`, `preview_once`, and dry-run planning do
not write audit events; explicit reevaluation persists a decision without
moving bytes.

## Version 1 contract

The reason is a separate versioned object beside the immutable decision snapshot
at `details.dataset`. The audit event schema and snapshot schema remain version
`1`; the snapshot's established fields and deterministic replay rules do not
change. No database migration or historical audit rewrite is required.

The top-level reason fields are:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1`; booleans, strings, and other versions are rejected. |
| `code` | Stable reason code from the vocabulary below. |
| `disposition` | `move`, `stay`, `suppressed`, or `rejected`. |
| `decisive_signals` | Ordered, typed evidence emitted by the trusted evaluator. |
| `constraints` | Evaluation time, importance/residency controls, destination eligibility, cooldown, hysteresis, budget checks, and objective scoring evidence. |
| `policy` | Policy name and version, plus nullable model identity/version. |
| `confidence` | Explicit availability of calibrated confidence; currently always a null value. |

`validate_policy_reason(value)` returns a detached JSON-compatible dictionary.
Validation rejects unknown fields at the typed contract boundaries, invalid enum values,
non-finite numbers, booleans in numeric fields, and unredacted credential-like
values. Timestamps must include a timezone and are canonicalized to UTC.
The `constraints.budgets` entries and `constraints.objectives` are evidence
mappings rather than strict nested reason models; their numeric amounts use
decimal strings and their unavailable values remain null.

Each decisive signal contains `name`, `value`, `operator`, `threshold`, and
`rule_index`. Unused fields are explicitly null. Signal names are limited to
`size_bytes`, `name_match`, `mime_match`, `mime_state`, `embedding_state`,
`features_evaluated`, and `embedding_similarity`. Measurements are numeric,
matches are boolean, and feature states are `fresh`, `stale`, `missing`, or
`unavailable`. For example:

```json
{
  "name": "embedding_similarity",
  "value": 0.91,
  "operator": ">=",
  "threshold": 0.8,
  "rule_index": 0
}
```

Rule indexes are zero-based. Filename and MIME indexes follow their configured
hot, warm, then cold rule order; embedding indexes follow the embedding rule
list. The reason retains indexes instead of filename patterns, MIME values,
embedding rule names, or free-text queries.

The constraints object records `evaluated_at`; importance level, revision and
allowed tiers; the runner's effective allowed destination tiers; placement and
last-move timestamps; minimum-residency and cooldown durations, expiry times and
active flags; configured size and similarity hysteresis; and an optional
stability override kind. Nullable rejected/candidate destinations and candidate
action describe relevant suppression evidence. Hysteresis checks contain their
kind, configured band, baseline and effective thresholds, observed value, and
optional embedding rule index. Checks preserve actual numerical boundaries,
including deliberately unreachable thresholds; they are not probabilities.

`constraints.budgets` defaults to an empty list for decisions without budget
checks. Each check retains the allowance ID, limits, combined opening and held commitments,
projected charges, binding constraints, estimator assumptions, and any explicit
budget override. `constraints.objectives` is null unless an `EstimatePolicy`
scored candidates; it retains weights, eligible candidate rankings, normalized
scores, selected placement, and estimate evidence. See [policy budgets and
what-if simulation](policy_budgets.md) for the charging and scoring contracts.
Estimate-policy decisions currently use `custom_policy` with this objective
evidence; a budget suppression takes precedence with `budget_constraint`.

No current provider has a calibrated confidence contract. `confidence.value`
is always `null`. `confidence.source` is `not_applicable` for deterministic
rules and movement constraints, or `not_reported` for external-provider/custom
decisions and provider failures. Similarity scores remain measurements in
`decisive_signals`; they never become confidence values. Provider model identity
can be configured through `PolicyRunner(model_identity=..., model_version=...)`;
an unknown model version stays null.

## Reason codes

| Code | Interpretation |
| --- | --- |
| `size_threshold` | The built-in size rule or threshold mock selected the action. |
| `name_rule` | A configured filename rule matched. |
| `mime_rule` | A configured MIME rule matched fresh MIME evidence. |
| `embedding_rule` | A configured semantic rule matched fresh similarity evidence. |
| `required_features_unavailable` | Required feature evidence was missing, stale, unavailable, or not evaluated. |
| `provider_decision` | An external provider returned a valid move or stay. |
| `provider_error` | Inference failed, timed out, was unavailable, or reached capacity; the policy stayed safely. |
| `provider_invalid_response` | The provider response did not describe a valid action; the policy stayed safely. |
| `provider_invalid_input` | The inference input failed validation; the policy stayed safely. |
| `custom_policy` | A custom policy supplied the action; no trusted decisive signals are inferred. |
| `minimum_residency` | Minimum residency suppressed movement. |
| `importance_restriction` | Importance eligibility suppressed movement. |
| `cooldown` | An active post-move cooldown suppressed movement. |
| `hysteresis` | A numerical stability boundary held the object in its current tier. |
| `destination_not_allowed` | A preferred destination was excluded by policy/provider tiers, or a proposed move failed the runner's eligibility filter. |
| `destination_missing` | A proposed move omitted its destination. |
| `already_in_tier` | A proposed move targeted the object's current tier. |
| `invalid_action` | A custom policy proposed an unsupported action. |
| `budget_constraint` | A cost/carbon allowance, unavailable charge, inactive period, or conflicting physical pool binding suppressed movement. |

Hard residency/importance decisions and stability suppressions take precedence
over an underlying rule code. Consult `disposition` to distinguish a rule that
kept the current tier from a constraint that suppressed movement. A built-in
rule returning a stay because its matched tier is already current retains its
original rule code. If an eligibility constraint removes its preferred tier,
the proposed destination is retained and the reason identifies the suppression,
including importance restrictions applied before the built-in evaluator runs.

There is one compatibility exception between reason disposition and the parent
audit outcome: an unsupported custom action has `invalid_action`/`rejected` in
its structured reason, while the existing audit/snapshot-v1 outcome remains
`stayed`. Dataset validation recognizes this narrow legacy outcome mapping.

## Querying a decision and its outcome

The parent audit event supplies its event UUID, correlation ID, optional job and
move IDs, object coordinates, occurrence time, and retention expiry. These
identifiers connect a reason to job activity and the move lifecycle without
duplicating those identifiers inside the reason. Use `AuditQuery` and the reason
reader to inspect retained decisions:

```python
from cognistore.core.audit import AuditEventType, AuditQuery
from cognistore.core.policy_reasons import reason_from_audit_details
from cognistore.db import open_catalog

with open_catalog("/var/lib/cognistore/catalog.sqlite3") as catalog:
    decisions = catalog.list_audit_events(AuditQuery(
        correlation_id="policy-run-correlation",
        event_types=frozenset({AuditEventType.POLICY_DECISION}),
        limit=500,
    ))
    for decision in decisions:
        reason = reason_from_audit_details(decision.details)
        if reason is None:
            continue  # A legacy event has no recorded structured explanation.
        print(decision.event_id, reason["code"], reason["disposition"])
        if decision.move_id is not None:
            move_history = catalog.list_audit_events(AuditQuery(
                move_id=decision.move_id,
                limit=500,
            ))
```

`reason_from_audit_details` returns `None` only when the reason is absent. A
present but malformed or unsupported-version reason raises `ValueError`;
readers must not treat corrupt evidence as a legacy omission. Follow
`causation_id` with `get_audit_event` for direct causal links, and inspect
terminal move events before concluding that a selection succeeded or failed.
Queries are bounded, so narrow their occurrence-time windows when inspecting
large histories. See [Operational audit events](audit_events.md) for all filters
and move ordering guarantees.

## Privacy, datasets, and retention

The structured projection does not include object contents, object coordinates,
embedding vectors, MIME strings, free-text queries, filename patterns, rule
names, importance provenance, stability override justifications, provider responses, or
exception messages. External provider and custom policy prose is replaced with
a static description in persisted decision reason text as well. Custom policy
subclasses cannot supply trusted built-in reason codes or decisive signals.
The separate `llm_audit` payload retains its existing redacted inference evidence
under the [LLM placement audit contract](llm_placement.md); structured reasons
do not copy its prompt, response, or free-text proposal.

Budget overrides are an explicit exception: budget checks retain the
responsible operator's `actor_id` and `reason` so the API, UI, and exports can
explain a numeric allowance exception. Budget and objective evidence also
retains operator-supplied profile sources and assumption identifiers. Keep
these operational fields free of object contents and sensitive prose;
credential-like values are redacted.

Policy/model and tier identifiers remain in the contract. Treat those as
application metadata and keep sensitive information out of them. This
projection does not anonymize the surrounding audit event or decision snapshot;
those retain their documented operational inputs and coordinate fields.
Credential redaction remains an additional boundary, not a content classifier.

[Policy datasets](policy_datasets.md) include a retained reason as the optional
row-level `structured_reason` field, separate from `snapshot`. Existing rows
and legacy snapshot events remain valid without this field; export never
fabricates historical reasons. A present reason must validate in full. To omit
it during export, exclude `structured_reason` as a whole. Partial nested
exclusions are rejected because they would erase required evidence from the
versioned contract. The existing dataset schema version remains `1`.

Reasons share their parent audit event's retention. Audit details normally
expire after 30 days and can be pruned; tombstones do not retain the explanation.
Pruning can also remove a move's terminal evidence or break a retained causal
chain. Export required history before pruning or configure suitable audit
retention. Neither the reason contract nor dataset export extends retention or
recovers deleted evidence.
