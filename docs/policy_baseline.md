# Offline supervised policy baseline

The version 1 baseline predicts whether a historically selected move will
succeed, using the versioned `move_succeeded` label from a
[policy dataset](policy_datasets.md). It is an interpretable decision stump:
one feature threshold divides examples into two groups. Training gives each
class equal total weight and minimizes weighted Gini impurity. Each group's
weighted success fraction supplies the gate's score. These scores describe a
balanced training population; they are not calibrated probabilities of move
success. The gate accepts or suppresses the rule's recorded move; it does not
select a new destination tier.

This is an offline experiment. Execution success does not establish optimal
placement, future demand, or a latency or cost improvement. Training and
evaluation neither modify runtime policy configuration nor move objects.

## Repeatable commands

Retain an immutable dataset export with a fixed `--as-of`, source window,
observation horizon, and sampling seed. Keep the configuration and resulting
artifacts with that export:

```bash
cognistore policy-baseline-train \
  --input policy-dataset.json \
  --training-config configs/policy-baseline-v1.json \
  --output policy-model.json --json

cognistore policy-baseline-evaluate \
  --input policy-dataset.json \
  --model policy-model.json \
  --output policy-evaluation.json --json
```

Both commands run offline, without a catalog, drivers, model provider, or
network connection. `--dry-run` computes and validates the result without
writing the output file; `--json` selects machine-readable command output.
Evaluation uses the same dataset snapshot as training, including its held-out
partitions. A dataset or configuration change requires a new model artifact.

For a reproducible smoke run, use
`tests/fixtures/policy_baseline/dataset-v1.json` as the input with the checked
configuration. This synthetic snapshot deliberately separates the outcomes by
size; its scores verify the workflow and do not demonstrate production quality.

The checked configuration is a starting point; choose chronological boundaries
that fit the retained data before running training. The version 1 configuration
has these fields:

```json
{
  "schema_version": 1,
  "train_before": "2026-09-03T00:00:00Z",
  "validation_before": "2026-09-05T00:00:00Z",
  "min_leaf_samples": 2,
  "min_class_count": 5,
  "max_class_ratio": 20.0,
  "success_threshold": 0.5,
  "promotion_min_balanced_accuracy_gain": 0.05
}
```

Cutoffs must be timezone-aware and ordered. `min_leaf_samples` bounds the
smallest trained group. `success_threshold` determines whether the gate's
class-balanced score accepts its recorded move. Class-support settings and the
required balanced accuracy gain govern whether an experiment can qualify as an
offline candidate.
Choose settings before inspecting held-out outcomes; using the test set to
tune them invalidates its role as an independent evaluation.

## Features and leakage checks

Training uses a fixed, versioned allowlist of decision-time features: object
size, MIME availability, access availability, and transformed observed access
counts and recency. Missing evidence remains distinguishable from observed
zero activity. The stump cannot consume the recorded decision, destination,
policy identity, move outcomes, label fields, object identifiers, or free text.
Policy identity is used only to group evaluation results.

Dataset validation runs automatically, checking schema versions, privacy
exclusions, label evidence, and feature timestamps. Only selected moves with
mature observed success or failure labels contribute supervised examples.
Pending, missing, and not-applicable labels are excluded; they are never
converted to failures.

Examples are partitioned by their recorded decision time:

| Partition | Decision-time window | Label availability requirement |
| --- | --- | --- |
| Training | Before `train_before` | Observation window and recorded outcome evidence strictly before `train_before` |
| Validation | From `train_before` up to `validation_before` | Observation window and recorded outcome evidence strictly before `validation_before` |
| Test | From `validation_before` onward | Mature observed label at the dataset export cutoff |

Rows crossing a label boundary are purged and counted. An outcome that occurred
before a cutoff but was recorded at or after it is also unavailable at that
cutoff. Shared move or outcome-evidence identities across partitions fail the
leakage check. Privacy-filtered datasets do not retain stable object identities,
so this does not establish separation of objects across partitions.

Training and evaluation automatically check class counts and imbalance.
Training fails if its partition does not meet the configured minimum count for
each class and maximum majority-to-minority ratio. Both holdouts must meet the
same requirements to qualify. Accuracy is reported with balanced
accuracy and the confusion matrix so a frequent-success class cannot hide a
failure to distinguish failed moves.

## Reading the comparison

The rule comparator accepts each logged selected move. The learned gate
accepts only those selected moves for which its success score meets the
configured threshold. Results cover the same observed rows and are also
grouped by the logged policy. They compare a gate with recorded rules; they
do not replay alternative rule configurations or infer outcomes at untried
destinations. Suppressing a historically successful move counts against the
gate's success classification.

| Reported measure | Interpretation and limit |
| --- | --- |
| Accuracy, balanced accuracy, confusion matrix | Prediction of observed move execution success; not placement accuracy |
| Accepted/suppressed move count and planned bytes | Movement-volume comparison on logged decisions; not executed work or measured financial savings |
| Allowed-tier violations | Check against captured allowed destinations; not a complete hard-constraint audit |
| Size-perturbation gate jitter | Change in gate acceptance under a deterministic ±5% size perturbation; a local sensitivity measure |
| Per-policy cohorts | The same comparison within each recorded policy cohort; not a causal comparison between policies |

Actual migration cost is unavailable from this label alone. A stored placement
estimate describes its explicit forecast workload and assumptions; its total
operating cost or transfer component does not necessarily represent the cost
of one move. Missing cost evidence must not become zero cost.

Snapshot v1 does not retain all region, locality, minimum-residency, cooldown,
or hysteresis inputs. Default privacy exclusions also remove the object
identities needed to reconstruct tier reversals. Full constraint violations
and real flapping therefore remain unavailable. Gate jitter is not a measured
flapping rate; held-out historical object states are not resimulated after a
suppressed move.

## Artifacts and reproducibility

The artifacts retain model and schema versions, the feature contract, training
configuration, dataset identity, code identity, decision and label windows,
partition counts, leakage and class checks, and evaluation metrics. The stump's
feature, threshold, raw leaf counts, and weighted scores make its decision
inspectable.
Preserve the exact dataset, configuration, and code revision with the outputs.

Evaluation verifies the artifact's dataset and configuration identity and
reconstructs the deterministic training result to check model integrity. It
does not publish a newly trained model. Reusing a filename for modified data
does not preserve the identity of an experiment.

## Promotion threshold and safe fallback

An experiment qualifies only as `offline_candidate` when both holdouts have
adequate class support and acceptable imbalance, improve balanced accuracy over
the recorded rules by at least `promotion_min_balanced_accuracy_gain` (default
0.05), and have no worse ordinary accuracy. Accepted movement bytes and
allowed-tier violations must not increase. Failed checks keep the experiment
from qualifying; a high aggregate score does not override them.

Every report keeps `production_eligible: false`. The available labels and
snapshots cannot establish counterfactual placement quality, complete
constraint compliance, or real flapping behavior. A future promotion requires
explicit review and additional evidence covering those gaps; these commands
perform no automatic promotion or online training.

The safe runtime fallback remains the configured existing rule policy through
the normal policy runner, with its hard constraints, hysteresis, and cooldowns.
An invalid, missing, or unsuccessful baseline experiment leaves that runtime
path in place. Do not install this offline gate by replacing or bypassing the
runner's safeguards.
