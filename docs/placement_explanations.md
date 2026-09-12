# Placement explanations and diffs

The **Placement** view at `/ui/` explains policy moves, stays, and suppressed
decisions. Inspect an object's history or the decisions belonging to a policy
job, or preview a policy against one object. Previewing evaluates current
catalog evidence without moving data or recording a completed action.

## Versioned resources

All three endpoints return the same version 1 decision resource:

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/policies/preview` | Evaluate one object without persisting a decision or executing it. |
| `GET /v1/policy-decisions` | Read retained decisions, newest first, with bounded cursor pagination. |
| `GET /v1/policy-decisions/{decision_id}` | Inspect one retained decision and its execution evidence. |

The existing `POST /v1/policies/evaluate` response remains compatible. Use
`/v1/policies/preview` for the shared explanation and diff representation.
It accepts the same `bucket`, `key`, and `config` request:

```shell
curl --json '{"bucket":"documents","key":"report.pdf","config":{"policy":"simple","threshold":5,"allowed_tiers":["hot","warm"]}}' \
  http://127.0.0.1:8080/v1/policies/preview
```

History accepts `bucket`, exact `key` (requires `bucket`), `job_id`, and
`correlation_id` filters. `limit` is 1–200, default 50. Responses contain
`items` and `page: {"limit": 50, "next_cursor": null}`. Pass a non-null cursor
unchanged with the same filters to read the next page. Cursors include an
event-ID tie-breaker so decisions with identical timestamps remain inspectable.
Changing filters invalidates the cursor.

History displays retained audit identities. Coordinates that the audit layer
redacts or pseudonymizes, including long keys, can therefore appear as stable
pseudonyms; filtering by the original bucket and key still finds their history.

## Read a decision

The [Python SDK](python_sdk.md) provides typed methods for the same resources:

```python
from cognistore.sdk import CogniStoreClient, PolicyEvaluationRequest

with CogniStoreClient("http://127.0.0.1:8080") as client:
    preview = client.preview_policy_decision(PolicyEvaluationRequest(
        bucket="documents", key="report.pdf",
    ))
    print(preview.current, preview.proposed, preview.execution.state)
    history = client.list_policy_decisions(bucket="documents", key="report.pdf")
    for decision in history.items:
        print(decision.decision_id, decision.execution.state)
```

- `current.tier` is the placement **at evaluation time**. Historical decisions
  retain this input even after later moves.
- `proposed.tier` is the effective placement after guardrails. A stay or
  suppressed move keeps the original tier. A rejected destination or an
  unguarded candidate remains inspectable in the structured constraints.
- `changed_fields` lists proposed changes, currently `tier`. It describes the
  proposal and does not prove that a move occurred.
- `disposition` distinguishes `move`, `stay`, `suppressed`, `rejected`, and
  unavailable evidence.
- `explanation.structured_reason` uses the existing
  [structured reason schema](policy_reasons.md), including reason code,
  decisive signals, policy identity, and guardrail evidence. Numerical signals
  retain their comparison operators and thresholds.

Guardrail evidence includes importance restrictions, minimum residency,
cooldown, allowed destinations, hysteresis checks, and cost/carbon budget
checks. Hysteresis exposes both
baseline and effective boundaries. An attributed stability override is visible
by kind; its free-text justification and model response text are not copied
into this resource.

Budget evidence retains limits, balances, projected charges, binding
constraints, and estimator assumptions. The UI displays each checked budget's
admission result and projected commitments. An explicit budget override
includes the operator's identity and rationale, which the UI also displays;
these operational fields must not contain sensitive object content. Unknown
amounts remain unavailable rather than becoming zero. When an `EstimatePolicy`
scored candidates, the API also retains its weights, candidate rankings, and
selection in `constraints.objectives`. See [policy budgets and what-if
simulation](policy_budgets.md) for scenario comparisons and forecast limits.

## Preview and execution are separate

`execution.mode` is `preview` or `persisted`. Every preview has state `dry_run`,
no decision ID, and no execution job. A proposed tier change is never shown as
completed merely because policy evaluation succeeded.

Retained decisions have a durable `decision_id`. Their execution state describes
the available causal evidence: `not_requested`, `planned`, `running`, `retrying`,
`completed`, `failed`, or `unavailable`. When present, `job_id`, `correlation_id`,
`move_id`, and the evidence `event_id` connect the decision to its execution.
`execution.job` carries the same job resource as `GET /v1/jobs/{job_id}`.

A successful policy job can contain moves, stays, and suppressed decisions.
Inspect each decision's execution state as well as the job state; job success
alone does not prove that an object moved. Refresh history to observe later
execution outcomes. The preview and history cards display their execution
mode and state separately from the placement diff.

## Missing evidence

An older decision without a structured reason reports `explanation.state` as
`legacy`. Invalid or unsupported reason evidence is `unavailable`. Neither is
reconstructed from current policy configuration or arbitrary provider prose.
Available rule and guardrail reasons remain visible when model details are
unavailable. `model_details` is explicitly `available`, `not_applicable`, or
`unavailable`; similarity remains a decisive signal, not calibrated confidence.

History follows [audit retention](audit_events.md). A missing or pruned decision
returns 404; incomplete execution evidence must not be read as proof of success.
Configure retention or export evidence before pruning when longer history is
required.

## Verification

`tests/integration/test_placement_explanations.py` exercises the versioned API,
durable worker state, SQLite catalog, and real filesystem moves without
external queue services. It checks previews against retained decisions and
final execution outcomes. UI behavior tests exercise the shared renderer,
including unavailable model details and the distinction between proposed and
completed actions.
