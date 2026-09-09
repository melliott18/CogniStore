# Policy datasets

Writable policy runs save the inputs actually evaluated alongside each
`policy.decision` audit event. The offline dataset exporter joins those immutable
snapshots to observed move outcomes, applies privacy exclusions and deterministic
sampling, and emits one versioned JSON document. It supports placement evaluation
and supervised baseline experiments; it does not train or update a model.

## Export and validate

```bash
cognistore --catalog-db catalog.db policy-dataset-export \
  --output policy-dataset.json \
  --as-of 2026-09-09T00:00:00Z \
  --after 2026-09-01T00:00:00Z \
  --before 2026-09-08T00:00:00Z \
  --observation-seconds 86400 \
  --sample-rate 0.25 --seed september-baseline \
  --exclude-field snapshot.object.pool_id --json

cognistore policy-dataset-validate --input policy-dataset.json --json
```

Export requires an existing persistent SQLite or PostgreSQL catalog at the
current migration head. It opens the catalog read-only and loads no storage
drivers, embedding providers, or queue clients. `--as-of` defaults to the current
UTC time; use a fixed value for repeatable exports. All supplied timestamps must
include a timezone. Decision bounds and the evidence cutoff are distinct:
`--after` (inclusive) and `--before` (exclusive) select decisions by their audit
occurrence time, while `--as-of` prevents future decisions or outcomes from
entering the dataset. Label windows start at the snapshot's actual evaluation
time, which can differ from the audit occurrence time for a queued job.

Queries are bounded to fewer than 10,000 audit events per selection or related
move-history query. An export that reaches this limit fails explicitly instead
of silently truncating history; narrow the time bounds, shorten the observation
window, or export smaller runs. Queries exclude events after the evidence cutoff.

The output is UTF-8 JSON with finite numbers. Its parent directory must exist.
The CLI serializes the complete document before writing and atomically replaces
the output, so a failed export preserves an existing file. Output cannot replace
the SQLite catalog or its journal files. `--dry-run` computes and reports the
export without writing a file or creating directories.

Validation requires no catalog, drivers, or network connection. Omit `--input`,
or pass `--input -`, to read JSON from stdin. It returns exit status `0` for a
valid dataset and `1` for validation or input errors. The JSON report includes
`valid`, an issue `count`, and `issues` with `code`, `path`, and `message`.
`--allow-missing-labels` permits incomplete supervised labels for exploratory
exports while retaining schema, privacy, leakage, and temporal checks.

## Versioned contract and replay

The dataset envelope has `schema_version`, `manifest`, and `rows`. Each row
contains a versioned snapshot, audit identifiers, observed outcomes, and a label.
The snapshot records:

- The object's bucket, key, size, tier, and pool at evaluation time.
- The exact MIME, embedding, and observed-access feature projection, including
  feature schema versions, source provenance, freshness, and sampling metadata.
- Policy name, version, implementation, ordered configuration, and model identity
  and version when a model is involved.
- The runner's allowed destination tiers, actual evaluation time, and decision
  action, destination, reason, and outcome.
- Capture provenance and whether offline replay is supported.

Stored snapshots support deterministic replay of the built-in simple and content
policies and the threshold mock used by `--policy llm`. Replay uses the frozen
record, policy configuration, and projected features; it does not reload current
catalog metadata or rerun embedding queries. Custom policies and external model
providers are recorded as unsupported for deterministic replay. A policy name or
model name alone is not enough to recreate arbitrary provider behavior. Custom
provider callers can pass `model_identity` and `model_version` to `PolicyRunner`
to retain an explicit model revision; the version requires an identity. Built-in
embedding projections also retain their model provenance.

For example, replay a retained audit event without reading the current object:

```python
from cognistore.core.policy_snapshot import replay_policy_snapshot

event = catalog.get_audit_event(decision_event_id)
decision = replay_policy_snapshot(event.details["dataset"])
```

Privacy-filtered exports may omit inputs needed for replay, such as filename
patterns and object keys. Use the original retained snapshot for complete replay;
a filtered dataset must not be mistaken for a complete operational snapshot.
Dry-runs and previews do not persist decision snapshots.

The dataset, decision snapshot, feature, outcome, and label contracts carry
explicit versions. The snapshot is an additive JSON payload inside the existing
audit schema, so existing SQLite/PostgreSQL migrations and audit import/export
retain it without replacing tables or rewriting historical events. Older
`policy.decision` events without a dataset snapshot are skipped and counted in
the export manifest. Unknown incompatible versions are rejected rather than
silently interpreted as the current contract. Future migrations must retain
existing version readers or explicitly transform older envelopes.

## Outcome labels and time

The initial supervised label is `move_succeeded`: whether the selected move has
terminal success or failure evidence within its configured observation window.
The default window is 86,400 seconds after the decision. Labels are resolved
only when the window is mature at the export cutoff. A selected/planned action
is not evidence that a move finished; pending, missing, or censored evidence must
remain distinguishable from failure. Stay and rejected decisions have no selected
move to label.

Label status is `observed` with integer value `1` (completed) or `0` (failed),
`pending` before the window closes, `missing` without a final terminal state,
or `not_applicable` for a decision that did not select a move. The last causal
move state within the closed window determines the target; a retry after a
failure prevents that earlier failure from being treated as the final result.

This label measures observed execution, not whether a tier was optimal or whether
the move improved future latency or cost. Access features explicitly cover only
observed operations. Empty or partially sampled access history does not establish
inactivity and cannot supply a negative demand or placement-quality label.

Validation checks versioned fields, required labels, label/evidence consistency,
and time ordering. Feature observation times cannot be after the decision; label
evidence must follow the decision, lie within its observation window, and not
exceed the export cutoff. Outcome and label fields must not leak into the input
feature snapshot. Keep feature inputs and supervised targets separate when
building downstream training matrices.

Audit events expire under the configured retention policy, normally 30 days.
Pruning can remove decision snapshots or their move evidence. Export the required
period before pruning, or configure appropriate audit retention; a missing
terminal event cannot safely become a failed-move label. The dataset contract
does not extend retention or recover already deleted evidence.

## Privacy and sampling

Default exclusions remove object bucket/key coordinates, free-text queries,
filename patterns, decision reasons, and content digests from nested snapshot
paths. Existing audit redaction also removes credential-like values at ingestion.
Do not put credentials in policy names, rule names, identifiers, or metadata.

Repeat `--exclude-field` to add row-relative dotted paths. Use `*` for array
elements, for example `snapshot.features.embeddings.*.name`; `**` matches any
number of path components. A single field name is excluded at every depth.
Exclusions are
combined with the defaults, and the manifest records the policy used for the
export. Review all retained application-specific fields before sharing data;
privacy exclusions can reduce the inputs available for offline replay.
An exclusion that removes a required contract field fails validation before
the output is written.

Decision sampling is deterministic for a fixed seed, rate, decision identities,
time bounds, and retained source history. `--sample-rate` must be finite and in
`(0, 1]`; it defaults to `1`. The manifest documents the sampling algorithm and
seed, inclusion rate, decision/evidence time bounds, candidate and exported
counts, skipped legacy events, observation window, and excluded paths. Sampling
selects whole decision rows with their evidence and label. Observed-access
sampling remains separately documented inside feature provenance; it is not the
same sampling process as dataset row selection.
