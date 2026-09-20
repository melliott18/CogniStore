# M4 closeout evidence

M4's production-platform delivery was accepted on 2026-09-19. The twenty
delivery issues (#54–#73) and POSIX hardening #91 are implemented and merged.
This record supports closing [epic #14](https://github.com/melliott18/CogniStore/issues/14),
[milestone 3](https://github.com/melliott18/CogniStore/milestone/3), and the
[M1–M4 roadmap tracker #12](https://github.com/melliott18/CogniStore/issues/12).

## Provenance and validation

The qualified application revision is
[`ad20be8fa1d1f00224064d65324527d2a200dc52`](https://github.com/melliott18/CogniStore/commit/ad20be8fa1d1f00224064d65324527d2a200dc52),
the final delivery in [PR #152](https://github.com/melliott18/CogniStore/pull/152).
The closeout changes documentation, retained evidence, and exact secret-scanner
false-positive fingerprints. Application and test sources are unchanged.
[validation.json](validation.json) records exact
commands, results, environment, source identity, and report hashes.

GitHub [CI run 35472317005](https://github.com/melliott18/CogniStore/actions/runs/35472317005)
and [Kubernetes run 35472316956](https://github.com/melliott18/CogniStore/actions/runs/35472316956)
failed before executing any steps. The retained annotation reports failed
account payments or an Actions spending limit. No hosted CI pass is claimed.
Fresh local validation uses CPython 3.13 on macOS arm64 in an isolated virtual
environment, plus dedicated Docker PostgreSQL/pgvector, NATS, MinIO, Azurite,
and GCS emulator services. The repository's older Anaconda virtual environment
exited with signal 11 before pytest startup; the isolated environment replaced
it for qualification without modifying that environment.

PR #150 already records the owner's authorization to accept local Terraform
checks despite the hosted CI startup block. The #72 CI acceptance item is
reconciled with that explicit exception and fresh execution of the same
verification script; it is not represented as a hosted pass.

| Gate | Result |
| --- | --- |
| Default full suite | 5,192 passed, 291 skipped; 86.90% combined statement/branch coverage (80% minimum) |
| Service-enabled integration suite | 391 passed, 31 skipped, one documented strict expected failure; no failures/errors |
| Quality | Ruff, mypy (162 application files), deterministic OpenAPI and configured Bandit passed |
| Dependencies and packaging | Project and development dependency audits, wheel/sdist build, metadata checks and clean installed-wheel smoke passed |
| Secrets | Current-source and 101-commit history scans passed with exact reviewed false-positive fingerprints; new-secret negative control detected the injected fixture |
| Terraform and Helm | Six mocked plan cases, fmt/init/validate, production chart lint/render, and 23 chart contract tests passed |
| Observability | Prometheus configuration, all 49 rules and 16 breach/recovery scenarios passed |
| Installed operator drill | 37 checks and 19 commands passed on a clean captured source revision |
| Historical Kubernetes campaign | Install/upgrade/rollback, persistence and 1→3→1 autoscaling passed under the explicitly limited profile below |

Reports: [full-suite JUnit](default.xml.gz), [integration JUnit](integration.xml.gz),
[coverage](coverage.xml.gz), [quality](quality/report.json), and
[operations](operations/review.json). The default run emitted 108 warnings,
predominantly unclosed SQLite test connections; four native-libmagic cases
were skipped. These limitations are preserved in the logs.


The full and integration JUnit reports overlap; do not add their case counts.
Skipped live-cloud cases and the GCS emulator's documented strict expected
failure do not establish production cloud qualification. This is not a fresh
Python 3.10–3.14 matrix, physical encryption audit, or final-revision cluster
campaign.

The first service-enabled integration attempt was interrupted when parallel
observability cleanup stopped its Docker dependencies. Its resulting connection
errors are retained separately and are not application defects or passing
evidence. The entire integration suite was restarted with healthy services.

The original Gitleaks scans reported 12 current-source and 15 historical
heuristic findings. Review identified only public Azurite development keys,
synthetic move identities and recorded source hashes. The closeout adds exact
path/rule/line and commit fingerprints to `.gitleaksignore`; no files or rules
are broadly excluded. The original findings and dispositions are retained with
the quality report alongside verification using the revised policy.

## Acceptance mapping

Each issue's written acceptance criteria were checked against merged delivery,
current implementation, and test coverage. Paths below are repository-relative.

| Issue / delivery | Accepted behavior and evidence |
| --- | --- |
| #54 / [PR #134](https://github.com/melliott18/CogniStore/pull/134) | Azure Blob configuration, shared conformance, bounded streaming, conditional commit, retry and failure preservation; `tests/unit/test_azure_blob_driver.py`, Azure integration cases and `docs/azure_blob_driver.md`. |
| #55 / [PR #137](https://github.com/melliott18/CogniStore/pull/137) | GCS conformance, resumable offsets, lost commit response, cancellation, retry and credential boundaries; `tests/unit/test_gcs_driver.py`, `tests/integration/test_gcs.py`, and `docs/gcs_driver.md`. |
| #56 / [PR #135](https://github.com/melliott18/CogniStore/pull/135) | JWT/OIDC validation, JWKS rotation, complete endpoint authentication and token-free principal propagation; JWT and API authentication tests. |
| #57 / [PR #139](https://github.com/melliott18/CogniStore/pull/139) | Endpoint and worker authorization, denial, revocation and redrive; `tests/unit/test_api_authorization.py` and `tests/unit/test_worker_authorization.py`. |
| #58 / [PR #143](https://github.com/melliott18/CogniStore/pull/143) | Tenant catalog/storage/search/job ownership and isolation across direct identifiers, listings, filters, cursors and concurrent requests; tenant unit, conformance and PostgreSQL/API integration tests. |
| #59 / [PR #141](https://github.com/melliott18/CogniStore/pull/141) | Provider-backed credentials, bounded caches, expiry/outage handling, redaction and rotation recovery preserving durable move identity; secret provider/cache/client tests and `tests/integration/test_secret_rotation_recovery.py`. |
| #60 / [PR #142](https://github.com/melliott18/CogniStore/pull/142) | Rejection of insecure configurations, TLS CA rotation, restored-catalog enforcement of current encryption evidence; encryption unit and recovery tests. At-rest state is operator-attested. |
| #61 / [PR #145](https://github.com/melliott18/CogniStore/pull/145) | Tenant-selected PII detection, redacted findings, unknown-state failure and execution-time policy revalidation; PII runtime, surface and policy tests. |
| #62 / [PR #146](https://github.com/melliott18/CogniStore/pull/146) | Scoped legal holds, atomic audit, cross-process fencing, deletion protection and retained release history; legal-hold conformance, catalog, API, movement and import tests. |
| #63 / [PR #147](https://github.com/melliott18/CogniStore/pull/147) | Audit coverage and causal chains, SQLite/PostgreSQL mutation controls, tamper/missing-history detection, offline export verification and scoped self-audited APIs; audit coverage/integrity/export tests. |
| #64 / [PR #144](https://github.com/melliott18/CogniStore/pull/144) | Locality constraints precede optimization and survive retries/recovery/commit; locality policy and mover tests cover conflicting goals, stale evidence and frozen destinations. |
| #65 / [PR #136](https://github.com/melliott18/CogniStore/pull/136) | Prometheus metrics, OpenTelemetry traces, structured correlation and redaction; observability runtime tests and fresh configuration validation. |
| #66 / [PR #140](https://github.com/melliott18/CogniStore/pull/140) | SLO/error-budget definitions, alerts, runbook routing and synthetic breach/recovery fixtures; `tests/observability/slo_rules.test.yml` and fresh `docker/observability/verify.sh`. |
| #67 / [PR #133](https://github.com/melliott18/CogniStore/pull/133) | Read-only, scoped, resumable consistency scans with stable findings and queryable reports; consistency unit and PostgreSQL integration suites. |
| #68 / [PR #149](https://github.com/melliott18/CogniStore/pull/149) | Opt-in, audited, generation-fenced idempotent repair, review states and zero-mutation previews; consistency repair/API tests and fresh installed operator drill. |
| #69 / [PR #151](https://github.com/melliott18/CogniStore/pull/151) | Staged orphan discovery and cleanup with grace periods, revalidation and governance/audit controls; orphan cleanup unit and PostgreSQL cases. |
| #70 / [PR #152](https://github.com/melliott18/CogniStore/pull/152) | Production administration surfaces for configuration, policies, actions, jobs, audit and repair; API/SDK tests, `tests/unit/test_admin_ui.py` and executable JavaScript behavior tests. |
| #71 / [PR #148](https://github.com/melliott18/CogniStore/pull/148) | Helm configuration and validation, install/upgrade/rollback persistence and request/queue autoscaling; fresh chart contract/lint checks and historical cluster evidence described below. |
| #72 / [PR #150](https://github.com/melliott18/CogniStore/pull/150) | Terraform infrastructure reference, production configurations, secure input validation and teardown guidance; fresh fmt/init/validate, six mocked plan cases, generated configurations and Helm validation. |
| #73 / [PR #153](https://github.com/melliott18/CogniStore/pull/153) | Operator handbook, installation, backup/restore, migration, incident, architecture and recovery guidance; documentation tests, [original drill record](operator-drill.md), and fresh installed-package drill. |
| #91 / [PR #138](https://github.com/melliott18/CogniStore/pull/138) | Race-safe POSIX containment under the documented namespace assumptions; deterministic shared conformance swaps cover read, publish, stat, list, delete and staging cleanup. |

## Deployment evidence and limits

The retained Kubernetes evidence is the actual 2026-09-19 PR #148 campaign,
not a new final-revision run. It records successful installation, upgrade,
rollback, catalog/object/scheduler persistence, queued job survival, and
PostgreSQL/JetStream/storage restart recovery. The load exercised 2,454 HTTP
requests and 251 jobs with one start and one success; API and worker workloads
each reached three ready replicas and returned to one. The archived chart was
checked against the current chart; only equivalent Chart.yaml serialization
differs, and the acceptance harness is unchanged. Its runtime predates the
later audit, legal-hold and administration changes. Current application tests
cover those changes; they do not substitute for a new cluster campaign.

The Kubernetes profile uses kind, the development security profile with API
authentication disabled, synthetic data, and local dependencies. Production TLS, OIDC, encryption, network enforcement,
provider credentials, storage classes, backups and capacity require deployment
qualification. Scheduled workers share one node and an RWO volume for SQLite
coordination. Fresh Terraform plans use mocked providers; no AWS resources were
provisioned and no cloud apply is claimed.

The fresh operator drill installs a captured clean copy of the qualified
revision into a new virtual environment. All 37 checks and 19 commands cover
first use, SQLite/POSIX quiesced backup and restore, and a synthetic abandoned
move repaired only after explicit opt-in. It does not qualify restoring a
nonterminal journal whose filesystem generation tokens changed, actual worker
kill recovery, or production cloud/security configuration. The historical
[operator drill](operator-drill.md) remains intact.

Additional operating boundaries remain documented: Azure uncommitted blocks
are invisible and expire through provider cleanup; GCS resumable state is not
restart-persistent and may spool to disk; POSIX containment assumes a trusted
namespace and excludes privileged relocation/ACL/mount manipulation; and audit
detection of a privileged full-history rewrite requires independently retained
checkpoints. These are delivery scope limits, not uncompleted acceptance boxes.

## Tracker reconciliation

The 21 M4 delivery/hardening tickets were already closed by merged PRs but each
retained four unchecked acceptance boxes. Verification supports checking all
84 boxes, the epic's 21 children, and the roadmap's M4 entry. Seven older closed
M1 issues also retained 28 stale boxes: #17, #18, #20, #22, #23, #24 and #25.
Their accepted implementation is recorded in PRs #77/#76, #78, #75, #79, #80,
#81 and #82, the historical M1 review, current regression tests, and the
checksum-verified [M1 scale report](../m1/README.md). JetStream/DLQ acceptance
uses its separate live integration evidence; the scale report excludes it.

The repository roadmap and generated ticket mirror are reconciled to the live
tracker. No new implementation scope or production certification is implied.

## Verify retained files

From this directory, run `shasum -a 256 -c SHA256SUMS` and inspect
[validation.json](validation.json). Compressed JUnit/coverage reports preserve
their original bytes; their original and retained hashes are recorded there.
The historical operator files retain their independent
`operator-SHA256SUMS` manifest. Fresh and historical deployment evidence are
separately identified in the validation record.
