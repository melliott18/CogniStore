# Hosted CI qualification attempt — #158

**Update, 2026-10-01:** hosted execution resumed. The [fresh campaign](2026-10-01/README.md) records actual runtime/deployment results and ticket dispositions. The failed account-startup attempt below is preserved as historical evidence.

**Status: blocked; hosted qualification is incomplete.** On 2026-09-20 UTC,
all three workflows were freshly dispatched on `main` revision
[`2cce6ff4d43fd287ad008197f1e17f168c3580c4`](https://github.com/melliott18/CogniStore/commit/2cce6ff4d43fd287ad008197f1e17f168c3580c4).
All 15 jobs failed before runner assignment or step execution. GitHub retained
zero artifacts for these runs. No application test, coverage, package,
security, Helm, Terraform, or Kubernetes pass is claimed.

| Workflow / attempt 1 | Jobs | Observed result |
| --- | ---: | --- |
| [CI — 35497918083](https://github.com/melliott18/CogniStore/actions/runs/35497918083) | 13 | Account billing/spending-limit block; includes all five Python versions |
| [Kubernetes acceptance — 35497919250](https://github.com/melliott18/CogniStore/actions/runs/35497919250) | 1 | Same account block |
| [Terraform reference — 35497920338](https://github.com/melliott18/CogniStore/actions/runs/35497920338) | 1 | Same account block |

Each check annotation reports that recent account payments failed or the
spending limit needs to be increased. The repository/account owner,
[@melliott18](https://github.com/melliott18), must resolve the actual restriction
in GitHub Billing & plans. Billing, spending, account plan, repository visibility,
and check-enforcement settings were not changed during this work.

## Retained evidence and provenance

[hosted-attempts.json](hosted-attempts.json) retains selected GitHub REST run
and job metadata, every job's complete annotation response, artifact inventories,
the commands that dispatched the workflows, and settings-review responses.
The snapshot records capture time in UTC, source revision, workflow/configuration
SHA-256 hashes, run IDs/attempts, timestamps, URLs and conclusions. The REST
jobs endpoint was queried for the recorded attempt, with `per_page=100`, and
its total was checked against the returned job count. Artifact inventories
also report a total of zero.

The `started_at` fields are GitHub job bookkeeping timestamps; they do not
demonstrate runner execution. Every retained job has `steps: []`, `runner_id: 0`
and an empty runner name. There are no built image, installed dependency,
runtime configuration or executed environment identities to record. The
source hashes identify requested inputs only.

This record contains no local application-suite results and does not reuse
historical M4 passes. The workflow/reporting changes on the #158 branch were
not part of these main-revision dispatches. Their local static validation is
recorded separately in [change-validation.json](change-validation.json).

## Acceptance state

| Ticket criterion | State / next evidence needed |
| --- | --- |
| Fresh hosted checks actually start and pass on recorded `main` | **Blocked.** The three fresh runs above all failed before execution; rerun after account remediation. |
| CPython 3.10–3.14, default collection, native libmagic, service integrations and ≥80% coverage | **Unqualified.** Existing gates remain intact. Retain each runtime's JUnit/coverage and review explicit skip/xfail reasons after execution. |
| Deployment workflows execute with accurate scope | **Blocked.** Both were explicitly dispatched despite path filters. A future kind pass qualifies the development cluster campaign; a Terraform pass qualifies mocked plans and Helm rendering. Neither establishes live-cloud production qualification. |
| Required checks, failure visibility, ownership and outage runbook reviewed | **Reviewed with an enforcement limitation.** See below and the [runbook](../../ci_runbook.md). Final-candidate requalification remains in [#160](https://github.com/melliott18/CogniStore/issues/160). |

The settings review found Actions enabled, all actions allowed, read-only
default workflow permissions, and workflow approval of pull requests disabled.
The branch-protection and repository-rulesets APIs each returned HTTP 403:
`Upgrade to GitHub Pro or make this repository public to enable this feature.`
The response is retained verbatim; no required-check enforcement is claimed.
An account-plan or visibility change is an owner decision. Until resolved,
reviewers must explicitly inspect all relevant checks on the selected revision.

Failure visibility was confirmed through failed run/job conclusions and all
15 check annotations, including failures that have no step logs or artifacts.
The [runbook](../../ci_runbook.md) identifies the owner, triage procedure,
expected check names, three-workflow recovery campaign, evidence retention,
and the final-candidate repeat. GCS emulator, service/emulator integrations,
mocked infrastructure, development kind, opt-in live-cloud tests and skipped
coverage must remain separately identified in the eventual successful record.

## Resume and verify

After the owner resolves the account block, follow the
[requalification procedure](../../ci_runbook.md#requalify-the-current-main-revision),
record the then-current `main` revision, and retain fresh evidence for all three
workflows. Keep this failed attempt as incident history. Do not close #158 or
claim the candidate qualified until its acceptance evidence exists.

From this directory, verify the retained files with:

```bash
shasum -a 256 -c SHA256SUMS
```

Future successful artifacts expire after the workflow's configured retention
period; archive sanitized reports and logs into durable storage before expiry.
Ignored local paths and expired artifact links cannot satisfy the evidence policy.
