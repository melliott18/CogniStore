# M3 closeout evidence

M3's explainable policy engine was accepted on 2026-09-12. All eleven delivery
issues (#43–#53) are implemented and merged. This record supports closing
[epic #15](https://github.com/melliott18/CogniStore/issues/15) and
[milestone 2](https://github.com/melliott18/CogniStore/milestone/2).

## Provenance and validation

The qualified implementation is
[`fe700326f3cc99ba498536afd07b897575709d53`](https://github.com/melliott18/CogniStore/commit/fe700326f3cc99ba498536afd07b897575709d53),
the final delivery in [PR #131](https://github.com/melliott18/CogniStore/pull/131).
The closeout changes documentation, a stale migration-test expectation, and
secret-scanner annotations for a synthetic test retry identity. It does not
change application behavior. [validation.json](validation.json) records source
identity, commands, results, report hashes, and the corrected test's checksum.
[requirements.txt](requirements.txt) records the installed development packages.

GitHub [Actions run 34657835198](https://github.com/melliott18/CogniStore/actions/runs/34657835198)
could not start any jobs: its annotation reports failed account payments or an
Actions spending limit requiring an increase. No remote test result is claimed.
Qualification below was performed locally with CPython 3.13.7 on macOS arm64,
using isolated Compose PostgreSQL/pgvector, NATS, and MinIO services for the
integration run.

| Gate | Result |
| --- | --- |
| Default full suite | 2,857 passed, 151 skipped; 84.90% combined statement/branch coverage, exceeding the 80% gate |
| Service-enabled integration suite | 245 passed, one capability skip for unsupported S3 range writes |
| Independent focused M3 reviews | 219 guardrail/topology tests, 509 learning/LLM tests, and 421 reason/estimator/budget tests passed; these overlap the full suites |
| Quality | Ruff, mypy across 118 application files, and deterministic OpenAPI verification passed |
| Packaging | Wheel and sdist build/metadata passed; clean wheel import, migration to `0011_policy_budgets`, packaged SDK/UI assets, CLI help, and sample verification passed outside the checkout |
| Security | Application/development dependency audits, configured Bandit gate, and current tracked-source Gitleaks scan passed |
| Offline evaluation | Reproducible baseline artifacts and eleven deterministic LLM adapter boundary cases passed |

Retained reports are [default JUnit](default.xml.gz),
[integration JUnit](integration.xml.gz), and [default-suite coverage](coverage.xml.gz).
Their gzip copies decompress to the original report bytes. Do not add test
counts across overlapping suites. The default skips include opt-in services and
four unavailable native-libmagic cases; live integrations cover the services.
The default run emitted 53 ResourceWarnings about unclosed SQLite test
connections. This closeout does not claim a fresh Python 3.10–3.14 matrix,
native-libmagic qualification, or a full container image/worker-shutdown campaign.

The first live integration pass found one obsolete expectation in
`test_postgres_topology_downgrade_and_reupgrade_preserve_placement_identity`.
Revision `0010_tier_stability` deliberately seeds a conservative cooldown clock
when re-upgrading an older schema; the test still expected an unset clock.
The corrected test verifies that backfill while preserving its identity,
referential-integrity, and audit checks. All 34 PostgreSQL topology tests and
the complete integration suite then passed. An initial standalone integration
coverage threshold failure reflected partial-suite measurement; the 80% gate
is enforced on the full default suite, not on that subset.

A supplementary complete-history Gitleaks scan reported twelve historical
matches, all reviewed synthetic idempotency-key fixtures in two test files.
The current-source scan passes after narrowly annotating the remaining fixture
lines. This is not a claim that the unmodified historical scan returned zero
matches. The clean environment's initial bundled pip was upgraded before the
passing installed-development-dependency audit.

## Acceptance mapping

Paths below are repository-relative. The JUnit reports retain executed case
names and results. Each row was also checked against its issue's written
acceptance criteria and the implementation, rather than inferred from issue
state alone.

| Issue and delivery | Accepted behavior and evidence |
| --- | --- |
| #43 / [PR #121](https://github.com/melliott18/CogniStore/pull/121) | Correlated, retry-safe access capture; reproducible recency/frequency windows; explicit missing/partial history; retention and volume measurement. `tests/unit/test_access_capture.py`, `test_access_events.py`, `test_access_policy_features.py`; `tests/integration/test_access_events.py`, `test_sqlite_access_import.py`; [retained 10,000-event benchmark](../access/README.md) and [access contract](../../access_history.md). |
| #44 / [PR #125](https://github.com/melliott18/CogniStore/pull/125) | Trusted importance, atomic tag/audit changes and revision-specific reevaluation; residency constraints in previews and execution, with boundary and missing-data tests. `tests/unit/test_catalog_placement_controls.py`, `test_placement_controls.py`, `test_placement_controls_surfaces.py`; [controls guide](../../placement_controls.md). |
| #45 / [PR #126](https://github.com/melliott18/CogniStore/pull/126) | Directional hysteresis, persisted cooldowns across restart, explicit audited stability overrides, unchanged/noisy-signal stability, and execution-time rechecks. `tests/unit/test_policy_stability.py`, `test_policy_hysteresis.py`, `test_policy_stability_cli.py`, `test_policy_stability_surfaces.py`; the seeded noise test and exact clock-boundary tests cover the no-flapping criterion under configured controls. |
| #46 / [PR #124](https://github.com/melliott18/CogniStore/pull/124) | Immutable versioned inputs/decisions; supported deterministic replay; privacy-filtered, reproducibly sampled exports; causal outcome labels and leakage/time validation. `tests/unit/test_policy_snapshot.py`, `test_policy_dataset.py`, `test_policy_dataset_cli.py`; [dataset contract](../../policy_datasets.md). |
| #47 / [PR #129](https://github.com/melliott18/CogniStore/pull/129) | Reproducible interpretable baseline, chronological splits, leakage/imbalance checks, artifact integrity, documented promotion threshold and rule fallback. `tests/unit/test_policy_baseline.py`, `test_policy_stump.py`, `test_policy_baseline_cli.py`; retained artifacts below and [baseline guide](../../policy_baseline.md). |
| #48 / [PR #128](https://github.com/melliott18/CogniStore/pull/128) | Strict schema and eligible destinations, bounded deadlines/retries, redaction and audit, safe stay on malformed/late/unavailable output, zero-write previews, and shared runner guardrails. `tests/unit/test_placement_llm.py`, `test_placement_llm_http.py`, `test_llm_policy.py`; `tests/integration/test_llm_policy_runner.py`; [LLM contract](../../llm_placement.md). Default tests use no hosted provider. |
| #49 / [PR #127](https://github.com/melliott18/CogniStore/pull/127) | Every completed writable evaluation persists a versioned reason; decisions survive later evaluation/preflight failure; retries freeze inputs/reasons; privacy and causal action/outcome links. `tests/unit/test_policy_reason_contract.py`, `test_policy_reason_signals.py`, `test_policy_reason_runner.py`, `test_policy_reason_dataset.py`; [reason schema](../../policy_reasons.md). |
| #50 / [PR #130](https://github.com/melliott18/CogniStore/pull/130) | REST/SDK/Placement UI show moved, stayed, suppressed and failed outcomes, frozen diffs, explicit preview state, and unavailable model details. `tests/unit/test_placement_explanations_api.py`, `test_sdk_placement_decisions.py`; `tests/integration/test_placement_explanations.py` traces real filesystem moves and integrity failure through durable worker outcomes; [explanation contract](../../placement_explanations.md). |
| #51 / [PR #122](https://github.com/melliott18/CogniStore/pull/122) | Backend-neutral topology and eligibility; explicit units/freshness; locality before scoring; atomic pool assignments and concurrency; migration/import and historical move-reference rules. `tests/conformance/topology_store.py`; `tests/unit/test_topology.py`, `test_topology_migrations.py`; `tests/integration/test_postgres_topology.py`; [pool guide](../../tier_pools.md). |
| #52 / [PR #123](https://github.com/melliott18/CogniStore/pull/123) | Versioned formulas, units, source/effective-date/calibration evidence, deterministic golden totals, unavailable inputs distinct from zero, and policy/explanation injection. `tests/unit/test_estimation.py`, `test_estimation_policy_features.py`; [estimator guide](../../storage_estimation.md). |
| #53 / [PR #131](https://github.com/melliott18/CogniStore/pull/131) | Atomic overlapping hard allowances under concurrency, immutable configuration, audited numeric overrides, conservative retry charging, stale-plan rejection, and zero-mutation simulations with assumptions/deltas/affected objects/binding constraints. `tests/unit/test_budget_catalog.py`, `test_policy_budget_runner.py`, `test_budgets.py`, `test_policy_simulation.py`; `tests/integration/test_budget_postgres.py`; [budget guide](../../policy_budgets.md). |

## Retained offline evaluation

[baseline-model.json](baseline-model.json) and
[baseline-evaluation.json](baseline-evaluation.json) use the checked
`tests/fixtures/policy_baseline/dataset-v1.json` and
`configs/policy-baseline-v1.json`, with 20 examples in each chronological
train/validation/test partition. The synthetic fixture intentionally separates
outcomes by size. Its perfect learned classification and 0.5 balanced-accuracy
rule comparator establish reproducibility, not production quality.

[llm-offline-evaluation.json](llm-offline-evaluation.json) retains eleven
deterministic adapter scenarios: valid move/stay, malformed and duplicate JSON,
ineligible destination, absent/unavailable/error/timeout providers, transient
retry recovery, and a late valid reply that cannot alter the frozen safe stay.
There are no external provider calls or catalog/storage mutations. An eligible
move here is a proposal; full runner and transport behavior is covered by tests.

From the repository root, with the qualified application source and checked
fixture/config unchanged:

```bash
python docs/evidence/m3/reproduce_learning.py
```

The retained generator checks the application diff against the qualified
revision, binds its Python source digest to that revision, and asserts repeated
baseline results. Reproduction during this closeout produced identical file
hashes across repeated runs. Later application changes require a new evaluation
record rather than overwriting this historical evidence.

## Scope limits and operational prerequisites

- Cooldown and hysteresis default to zero; configure them to obtain the tested
  stability protections. Stability overrides cannot bypass importance,
  residency, locality, or budget constraints.
- Budgets enforce modeled charges using explicit opening commitments, pool
  bindings, forecasts, and fresh versioned assumptions. They do not discover
  provider usage, settle invoices, predict future tariffs, or account for carbon
  offsets. Missing required evidence fails closed. Budget override actor and
  rationale are deliberately visible in budget explanations.
- Snapshot replay and baseline comparisons have the limits in their guides.
  Version 1 predicts move execution success, not preferred placement. Movement
  bytes and gate sensitivity are proxies; measured movement cost, complete
  constraint compliance, object overlap, and object-level flapping are not
  established. Production promotion remains disabled, with the existing
  guarded rule policy as fallback.
- LLM fixtures establish safety and audit boundaries, not hosted-model quality.
  UI verification here is contract/API/filesystem integration; no automated
  browser-interaction run is claimed.
- M4 still owns production authentication, tenancy, observability, operational
  alerts, repair, deployment, and POSIX concurrent-path hardening (#91).

## Verify retained files

From this directory:

```bash
shasum -a 256 -c SHA256SUMS
python -m json.tool validation.json >/dev/null
python - <<'PY'
import gzip
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

evidence = json.loads(Path("validation.json").read_text())
for report in evidence["retained_reports"]:
    data = gzip.decompress(Path(report["path"]).read_bytes())
    assert len(data) == report["original_bytes"]
    assert hashlib.sha256(data).hexdigest() == report["original_sha256"]
    ElementTree.fromstring(data)
print("Retained reports match their original bytes.")
PY
```
