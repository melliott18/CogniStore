# Policy feature projection

CogniStore placement policies consume an ephemeral, backend-neutral feature
projection. The projection is assembled from a detached catalog record and
optional indexing services. Writable policy runs retain the evaluated projection
inside the [versioned policy dataset snapshot](policy_datasets.md).

Authoritative [importance and minimum residency](placement_controls.md) constrain
movement before these policy preferences. Evaluation results expose the active
constraint evidence alongside the feature projection.

## Schema version 1

Every evaluation exposes `features.schema_version = 1` with:

- `mime`: the selected MIME value, feature state, detector/catalog provenance,
  source schema version, and current content digest when available.
- `embeddings`: one entry for each configured named semantic rule. Each entry
  includes the prototype query, max-passage cosine similarity when fresh, the
  exact embedding-space/model identity, current source digest, and indexed
  document/passage evidence.
- `access`: observed read/write/touch counts in configurable windows, recency,
  sampling estimates, and explicit freshness, missing-data, and partial-coverage
  indicators. Runtime loaders include this additive schema-v1 field; older
  producers may omit it. See [Access history](access_history.md) for event
  semantics, configuration, retention, and measured behavior.

Feature state is one of:

- `fresh`: compatible with the evaluated catalog snapshot and safe to select.
- `missing`: the source or requested model-space projection does not exist.
- `stale`: evidence is malformed or does not match the current source/config.
- `unavailable`: the configured feature service failed during evaluation.

The projection is immutable and returned in deterministic signal-name order.
Persisted snapshots preserve these projected values and their provenance for
offline replay and outcome-label export; see [Policy datasets](policy_datasets.md).

Access history follows the logical bucket/key across tier moves and content
updates. It is independent of MIME and embedding content provenance. Its
`freshness` describes the latest observed event's age, and `partial: true` means
it never proves that unobserved intervals were idle. Built-in placement rules
retain their existing precedence; custom policies can consume `features.access`
through `evaluate_features(record, features)`. Unknown or sparse history must
not justify demotion. Use a fixed `as_of` when loading fixture projections to
reproduce window boundaries exactly.

A MIME value is `fresh` only when its detector envelope is source-compatible
with the record's current content identity. Legacy bare `mime` metadata without
that identity and evidence is `stale`; it cannot select placement by itself.

## Embedding rules

A content policy rule has four fields:

```json
{
  "name": "active-project-material",
  "query": "frequently used current project documentation",
  "minimum_similarity": 0.8,
  "destination_tier": "hot"
}
```

`name` is the classification label. `query` is embedded in the policy's exact
configured model space, and the best current passage similarity for each exact
object coordinate becomes the classification score. A fresh score greater
than or equal to `minimum_similarity` selects the destination. Rule names are
unique and ordered; the first matching rule wins.

The REST API accepts these objects in `config.embedding_rules`. The CLI accepts
the same values as a repeatable four-argument option:

```bash
cognistore policy-run BUCKET --policy content --dry-run --json \
  --embedding-rule active-project-material \
  "frequently used current project documentation" 0.8 hot
```

The API server, synchronous CLI policy runner, and background worker load the
same optional top-level `embedding` block from `drivers.yaml`. An
OpenAI-compatible deployment is configured as:

```yaml
embedding:
  provider: openai-compatible
  base_url: https://embeddings.example.com
  api_key_env: COGNISTORE_EMBEDDING_API_KEY
  model: text-embedding-model
  deployment_version: release-2026-09-01
  dimensions: 1536
  request_dimensions: false
```

The local provider uses an immutable model revision:

```yaml
embedding:
  provider: sentence-transformers
  model: sentence-transformers/all-MiniLM-L6-v2
  revision: 0123456789abcdef0123456789abcdef01234567
  dimensions: 384
```

The sentence-transformers variant requires the `embeddings` optional package.
Both variants require the PostgreSQL catalog and its supported pgvector
extension because policy evaluation reads the existing versioned embedding
index; configured embedding features are rejected with SQLite or an in-memory
catalog. Omit the block to retain deterministic `missing` states. Credentials
and raw vectors never enter policy configuration or job payloads. See
[Embedding search](embedding_search.md) for model identity and indexing
requirements.

Queued policy runs containing embedding rules use job-envelope schema v2. New
workers accept legacy v1 jobs, v2, and movement-constraint v3 jobs; older workers
reject unsupported versions instead of
silently ignoring rules they do not understand. Before producers or schedules
enable embedding rules, upgrade and drain or stop every older worker that shares
the durable consumer. Publishing v2 jobs while an older worker can still receive
them sends those jobs to terminal rejection. Feature-free policy jobs omit
`embedding_rules` and remain schema v1 when no explicit movement constraints
are supplied. A payload containing `movement_constraints` uses schema v3; see
[importance and minimum residency](placement_controls.md#scheduled-and-queued-policies).

## Deterministic and safe fallback

Content policy precedence is:

1. object-key glob rules;
2. fresh MIME rules;
3. fresh embedding rules in configured order;
4. size fallback.

If a key rule matches, its allowed destination is decisive because it does not
depend on knowledge-layer providers. After key rules, a configured MIME or
embedding signal that is `missing`, `stale`, or `unavailable` stops evaluation
with `stay`; lower-priority semantic rules and size fallback cannot safely win
while a higher-priority result is unknown. When every signal encountered in
precedence order is fresh but none matches, the normal deterministic size
policy runs.

Replacing content or extraction invalidates omitted MIME evidence and the
active object-to-embedding mapping. Reevaluation therefore stays safe until a
new indexing pass publishes a source-compatible embedding set. Policy dry-run
and `POST /v1/policies/evaluate` include the full projection so operators can
distinguish this state from a fresh nonmatch.

Before executing a selected source-dependent action, the mover verifies the
projected content digest through a generation-bound read and persists that
digest in the durable move contract. Transfer reuses the recorded generation;
a replacement between evaluation, verification, or transfer aborts while the
source remains in place.
