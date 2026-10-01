# Hosted CI requalification — 2026-10-01

The owner requested a review of tickets closed with skipped hosted CI and a fresh
run of the required tests. This campaign targets current `main`, source
`fae9f64811c477703ac25e04ab938be038677d16`, using the unchanged workflows at that
revision. All three runs were explicitly dispatched once, attempt 1, on standard
GitHub-hosted Ubuntu runners. No production services or cloud resources were
provisioned. Historical skipped records remain historical.

## Scope and commands

The issue inventory contained 81 issues. [ticket-review.json](ticket-review.json)
records the title, prior state, result and disposition of each reviewed ticket. [scope.json](scope.json) records the 35
closed issues with direct skipped/blocked hosted-CI language and the expanded
48-ticket review: M3's eleven children are covered by its closeout evidence, and
M5 fixes #156/#157 are included with the other closed M5 work. M2 had historical
passing hosted qualification; its tests still execute in the shared CI suite.

```sh
gh workflow run ci.yml --repo melliott18/CogniStore --ref main
gh workflow run kubernetes.yml --repo melliott18/CogniStore --ref main
gh workflow run terraform.yml --repo melliott18/CogniStore --ref main
```

Run metadata confirms all three selected runs use the same SHA. Their workflow
files are authoritative for exact test commands and dependency installation.

| Workflow | Run | Outcome |
| --- | --- | --- |
| CI | [36898446320](https://github.com/melliott18/CogniStore/actions/runs/36898446320) | Failed: telemetry test, mypy, MinIO startup/build, secret-scan false positives |
| Kubernetes acceptance | [36898449966](https://github.com/melliott18/CogniStore/actions/runs/36898449966) | Failed: MinIO image pull HTTP 401; deployment rollout timed out |
| Terraform reference | [36898454581](https://github.com/melliott18/CogniStore/actions/runs/36898454581) | Passed: six mocked plans and production Helm handoff |

## Test outcomes

**Overall: hosted execution is restored, qualification is not complete.** Five
of the 15 expected jobs passed; ten failed. There are no cancelled or pending
jobs. Both CI and Kubernetes fail, while Terraform passes. Open #158 must
remain open for the failures and missing integration/deployment evidence.

| Matrix job | Unit/conformance result | Combined line/branch coverage | Integration result |
| --- | --- | --- | --- |
| Python 3.10 | 6,325 passed / 1 failed / 11 skipped | 86.31% | Not run: MinIO startup unauthorized |
| Python 3.11 | 6,325 passed / 1 failed / 11 skipped | 86.31% | Not run: MinIO startup unauthorized |
| Python 3.12 | 6,325 passed / 1 failed / 11 skipped | 86.32% | Not run: MinIO startup unauthorized |
| Python 3.13 | 6,325 passed / 1 failed / 11 skipped | 86.32% | Not run: MinIO startup unauthorized |
| Python 3.14 | 6,325 passed / 1 failed / 11 skipped | 86.31% | Not run: MinIO startup unauthorized |

Coverage exceeds the existing 80% threshold in every matrix job, but does not
fill the missing integration coverage. Overlapping tests across jobs are not
summed into a unique-test claim.

The default suite completed with **6,503 passed, one failed, and 302 skipped**.
The sole failure is the OpenTelemetry redirect regression described below.
Default-suite skips include unconfigured PostgreSQL (192), Azurite (27), live
Azure (27), GCS emulator (22), and NATS (10); missing optional `httptools` (10);
native case-insensitive storage (8); and six further documented capability,
collection, live-GCS, MinIO, and local Helm-chart conditions. The retained
JUnit summary records every reason. No timing/heartbeat failure occurred in
this fresh default run.

The separate GCS job completed **20 passed, two skipped, one strict expected
failure**. The expected failure is fake-gcs-server ignoring a media-GET
generation precondition; skips cover unsupported range writes and absent live
GCS credentials. Packaging, all 37 operator-drill checks, alert validation,
Python dependency audits, and Bandit passed. These Python audits do not replace
the historical frozen-image vulnerability analysis.

## Confirmed findings

- **Telemetry regression test:**
  `tests/unit/test_encryption_policy.py::test_telemetry_redirect_cannot_forward_payload_to_plaintext`
  raises `AttributeError: OTLPSpanExporter has no attribute _session`. The test
  relies on a removed private exporter attribute; it fails before verifying the
  redirect assertion. This is not evidence of an observed plaintext payload
  leak, and the protection is not established by this failing test.
- **Type checking:** `python -m mypy` reports 17 errors in six files, all missing
  annotations in database/migration code. Ruff passes; the subsequent OpenAPI
  check is skipped after mypy fails. These are executed failures, not an account
  startup restriction.
- **MinIO image availability:** the pinned Quay manifest returns HTTP 401
  `UNAUTHORIZED`. Container builds fail; Compose integration, shutdown checks,
  and reduced movement qualification do not execute. The subsequent missing
  movement JSON export is a consequence of the absent campaign, not a second
  movement failure.
- **Kubernetes acceptance:** all 23 Helm security/migration contract tests pass.
  The clean kind cluster cannot pull the same MinIO image (`ErrImagePull` /
  `ImagePullBackOff`), and the 300-second deployment rollout wait expires.
  Application install, upgrade, rollback, persistence, and autoscaling trials
  do not execute. Cluster events are retained; cleanup deletes the test cluster.
- **Secret scanning:** Gitleaks 8.24.3 reports 126 `generic-api-key` findings.
  Source-context review classifies 119 as SHA-256 evidence digests, four as Go
  module checksums, one as the Python base image's public GPG fingerprint, one
  as a loopback Azurite development-only fixture key, and one as the literal
  `held-move` idempotency identifier. See the sanitized finding inventory. This
  is an executed failing gate requiring precise false-positive disposition;
  it is not recorded as a pass. The full-history dispatch scans fetched branch
  history as well: 57 findings are on commits not ancestral to this `main` SHA.
  No ignore rules or scan scope were changed during this review.
- **Merge enforcement:** the branch-protection API returns HTTP 404, `Branch not
  protected`; both repository rulesets and effective `main` rules return empty
  arrays. No required-check enforcement is configured in these inspected
  surfaces. No settings were changed.

## Ticket dispositions

Closed implementation tickets are not bulk-reopened for shared CI infrastructure
or tooling failures. Fresh failing gates and unexecuted coverage remain tracked
under open [#158](https://github.com/melliott18/CogniStore/issues/158); this review
does not mark them accepted. The scoped owner-directed closures of #160, #162,
and #163 are preserved. Their separate candidate/staging qualifications remain
outstanding. The historical frozen candidate is not the source of this run.

| Tickets | Applicable checks and interpretation |
| --- | --- |
| #72 | Terraform workflow passed all six mocked cases and Helm handoff; remains closed. No real-cloud qualification is claimed. |
| #55 | GCS emulator conformance passed, subject to its recorded expected failure and live-cloud skips; remains closed. |
| #66 | Prometheus/operational alert configuration and synthetic breach/recovery fixtures passed; remains closed. Actual operator notification and live soak remain #165 scope. |
| #68, #73 | Installed-package operator backup/restore and repair drill passed all 37 recorded checks; remain closed. This does not replace selected-environment recovery under #164. |
| #71 | 23 chart contracts passed; cluster acceptance blocked by confirmed MinIO HTTP 401. Remains closed for original delivery; fresh qualification failure stays tracked under #158. |
| #60, #65 | The telemetry redirect regression fails on the removed private exporter attribute; retain the delivery closures while tracking test/compatibility remediation under #158. |
| #17, #18, #20, #22, #23, #24, #25 | Unit/conformance results and service-startup outcomes recorded above; MinIO and reduced movement coverage cannot be claimed from the blocked container job. |
| #43–#53 | M3 unit regressions and shared runtime gates run; PostgreSQL integration depends on successful service startup. |
| #54, #56–#59, #61–#64, #67, #69, #70, #91 | M4 unit/conformance regressions run; backend-dependent acceptance must distinguish executed tests from service blocks. |
| #156, #157, #175 | Scoped fix regressions run within the shared suites; platform-specific and service-dependent skips are preserved. Linux CI does not establish native case-insensitive filesystem execution. |
| #160, #162, #163 | Runtime tests, packaging/security and evidence-tooling tests contribute scoped regression evidence; they do not qualify the separately frozen candidate, replace live staging/UAT, or dispose of image scan findings. |
| #12, #14, #15 | Delivery closeout remains recorded; shared hosted qualification is not green. Open #158 and #155 retain the unresolved release gates. |

## Evidence retention and limitations

Run metadata, complete hosted logs (secret-scan output redacted by Gitleaks), artifact inventories, and selected
text reports are retained beside this report. XML/log/SARIF and the large Terraform JSON-lines report are compressed
without changing their content. Large package binaries are excluded; original artifact IDs, sizes, digests and expiry metadata
remain in each artifact inventory. SHA256SUMS covers the retained files.

Hosted dependency resolution is recorded in uploaded requirements reports. The
runs use the current source, not the historical closed-ticket merge revisions
or the frozen #160 image. Emulator/mocked/synthetic results are not live-cloud,
production, long-duration load, or pilot qualification. No application code,
workflow gates, branch settings, or release artifacts were changed.

The aggregate CI log download failed with a transport stream cancellation.
All 13 complete job logs were retrieved separately through the Actions jobs
API instead; [log-collection.json](log-collection.json) records that fallback.
Kubernetes and Terraform aggregate logs downloaded successfully.

## Documentation validation

`git diff --check` passed. All 13 retained XML reports parse; all 16 JSON
files and the Terraform JSON-lines report parse; all 35 gzip files decompress;
local Markdown links resolve; all run SHAs/attempts and 15 final job states
match the report. The checksum inventory covers 84 files.

A local Gitleaks scan of the new documentation directory reports three
`generic-api-key` matches in `operator-drill.source-sha256.json` (lines 13,
36, 101). Each is verified by hashing the corresponding file at the tested
source commit. They are evidence-digest false positives; no ignore rules were
added, and the scanner exit status remains 1. This local documentation scan
does not replace the 126-finding hosted history scan.
