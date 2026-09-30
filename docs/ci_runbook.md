# Hosted CI recovery and qualification

<a id="current-policy-hosted-ci-suspended"></a>

## Current policy: hosted CI resumption after publication

On 2026-09-30, the user explicitly requested making the repository public
to resume GitHub-hosted CI. The procedures below become active once the
repository is public; until then, the 2026-09-21 suspension remains in effect.
Follow the [agent validation policy](../AGENTS.md#validation-policy-hosted-ci-resumption)
and retain appropriate local checks alongside the fresh hosted evidence.

Publication and authorization do not prove that the billing/spending
restriction is resolved or that any job passed. Record exact revisions, run
identities and observed results. Historical skipped/unavailable evidence stays
unchanged. If a fresh attempt still cannot start, record the cause once and
continue independent work without repeated blind reruns. Standard hosted
runners are in scope; paid services, spending increases and cloud provisioning
require separate authorization. Local results cannot be reported as a hosted
pass or production qualification.

## Recovery ownership and scope

The repository/account owner, [@melliott18](https://github.com/melliott18), owns
Actions billing, availability, and repository check enforcement. The owner or
a delegated maintainer handles incident triage, reruns, and retained evidence.
Record the named delegate in the incident issue before handing it off.

[Ticket #158](https://github.com/melliott18/CogniStore/issues/158) tracks
restoring hosted CI and requalifying the current `main` revision. Its
[evidence record](evidence/ci-158/README.md) records the actual recovery state,
run IDs, annotations, and enforcement review. An account-level Actions failure
is still a failed qualification: local results cannot establish a hosted pass.
After successful CI recovery, repeat this campaign on the final release candidate
under [ticket #160](https://github.com/melliott18/CogniStore/issues/160).

## Workflow coverage and expected checks

The workflow files are authoritative for commands, versions, job names, and
artifact retention. All three support `workflow_dispatch`.

| Workflow | Qualification scope | Expected job/check names |
| --- | --- | --- |
| [CI](../.github/workflows/ci.yml) | Every PR and push to `main`; lint/types/OpenAPI, observability, supported Python matrix with isolated services, GCS emulator, default collection, packaging/operator drill, containers/movement, security and secrets | `Lint and type check`; `Prometheus rules and operational alerts`; `Tests (Python 3.10)` through `Tests (Python 3.14)`; `GCS HTTP emulator`; `Default full test suite`; `Build and install package`; `Container images and Compose integration`; `Dependency and static security scans`; `Secret scan` |
| [Kubernetes acceptance](../.github/workflows/kubernetes.yml) | Path-filtered PRs/pushes; fresh kind installation, upgrade, rollback, persistence and autoscaling | `Install, upgrade, rollback and autoscale` |
| [Terraform reference](../.github/workflows/terraform.yml) | Path-filtered PRs/pushes; mocked plans, validation and rendered Helm handoff | `Mocked production plan and Helm handoff` |

Kubernetes and Terraform may legitimately have no automatic run after a
docs-only change. A qualification campaign explicitly dispatches both; an
absent or skipped workflow is not passing evidence. Concurrency cancels an
older run of the same workflow/ref, so do not dispatch duplicate campaigns
while the first one is running.

## Diagnose a hosted outage

1. Record the affected commit, workflow, run ID/attempt, time, conclusion, and
   whether any job steps started. For one affected run:

   ```bash
   ci_repo=melliott18/CogniStore
   ci_run_id=REPLACE_WITH_RUN_ID
   gh run view "$ci_run_id" --repo "$ci_repo" \
     --json url,headSha,event,attempt,status,conclusion,jobs
   gh run view "$ci_run_id" --repo "$ci_repo" --log-failed
   gh api --paginate "repos/$ci_repo/actions/runs/$ci_run_id/jobs?filter=latest"
   ```

   A run that fails before steps start may have no logs. Follow each job's
   `check_run_url` from the jobs response and read its `/annotations` endpoint,
   or inspect the run annotations in GitHub. Preserve the annotation text and
   any unavailable-log response instead of calling an empty log a test pass.

2. Route account payment/spending-limit annotations to @melliott18. The owner
   checks account billing/payment status and Actions budget/limits, resolves
   the actual account restriction, and records the resolution time. Repository
   YAML changes cannot remove an account billing restriction. Do not increase
   spending, change repository visibility or account plans, or weaken checks
   as an incidental troubleshooting step.

3. If the annotation instead identifies disabled Actions, action policy,
   permissions, or runner availability, the owner checks those settings and
   records the specific correction. If job steps ran, investigate the failing
   command, dependency availability, timeout, or application regression using
   logs and the workflow's existing failure diagnostics. Keep infrastructure
   startup failures distinct from executed test failures.

4. Keep the incident open while runs cannot execute. Link affected PRs and
   label any local workaround with its exact revision, environment, commands,
   and limitations. A prior exception is not continuing authorization to
   describe later unexecuted jobs as green. After remediation, perform a
   fresh campaign below rather than relying on an older revision's result.

## Requalify the current main revision

Prerequisite: repository visibility is public under the 2026-09-30 user
authorization. A fresh campaign establishes whether hosted execution is
available; publication alone does not resolve the historical account incident.
Do not execute these dispatch or watch commands before publication.

From a checkout with GitHub CLI access, capture the selected remote revision
and dispatch each workflow once:

```bash
ci_repo=melliott18/CogniStore
ci_revision=$(gh api "repos/$ci_repo/commits/main" --jq .sha)
printf '%s\n' "$ci_revision"
gh workflow run ci.yml --repo "$ci_repo" --ref main
gh workflow run kubernetes.yml --repo "$ci_repo" --ref main
gh workflow run terraform.yml --repo "$ci_repo" --ref main
gh run list --repo "$ci_repo" --branch main --commit "$ci_revision" \
  --event workflow_dispatch --limit 20 \
  --json databaseId,workflowName,headSha,event,attempt,status,conclusion,url,createdAt
```

Select the three run IDs created by these dispatches and record their
`headSha`, run attempt and URL. `main` can advance between requests: verify
that every run targets `ci_revision`. If it advances during the dispatches,
select the new current revision and repeat all three workflows together.
Never combine passes from different revisions into one qualification.

For each selected run, wait for completion and inspect the jobs, not just the
workflow badge:

```bash
ci_run_id=REPLACE_WITH_RUN_ID
gh run watch "$ci_run_id" --repo "$ci_repo" --exit-status
gh run view "$ci_run_id" --repo "$ci_repo" \
  --json url,headSha,event,attempt,status,conclusion,jobs
```

Require each listed check to execute successfully. Review test totals,
coverage, skips and expected failures; investigate unexpected changes.
Record failed attempts as well as the eventual successful attempt. If a
runtime defect requires a code change, qualify the new revision after the fix
lands on `main`. A branch-only pass can support PR review but does not satisfy
the current-main qualification.

## Review required checks and failure visibility

Read both legacy branch protection and repository rulesets; neither the
workflow file nor a green badge proves merge enforcement:

```bash
gh api "repos/$ci_repo/branches/main/protection"
gh api --paginate "repos/$ci_repo/rulesets"
gh api "repos/$ci_repo/rules/branches/main"
```

Review any applicable ruleset details and record required status contexts,
enforcement state, bypass permissions, and whether the required checks match
the actual names above. On 2026-09-20 the protection and rulesets requests
returned HTTP 403 with `Upgrade to GitHub Pro or make this repository public
to enable this feature.` The [#158 record](evidence/ci-158/README.md) preserves
the responses. This is an enforcement limitation, not proof that required
checks are configured. Recheck enforcement after the authorized publication;
that historical response does not establish current settings. Changes to
branch protection or account plans require separate authorization.

Until enforcement is available, the reviewer must explicitly confirm the
expected checks and selected revision before merge and record that manual
review. Before publication, record applicable local evidence and the known
hosted gap without dispatching or waiting for runs. After publication, retain
actual hosted outcomes; follow the user's existing merge authorization and
any enforced branch rules. Path-filtered
Kubernetes/Terraform jobs need a deliberate required-check
strategy: a blanket required context can wait indefinitely when its workflow
does not trigger. Do not declare these checks enforced without reviewing the
actual settings and trigger behavior.

For failure visibility, verify that the PR/run shows failed jobs and that
annotations explain startup failures even when step logs are unavailable.
Inspect report artifacts and diagnostics after executed failures. A missing
artifact may mean the job never reached upload; capture that fact. Workflow
uploads using `if-no-files-found: warn` can finish without evidence, so job
success alone does not establish that the reports were retained.

## Retain qualification evidence

Download reports before expiry and preserve metadata and logs with them:

```bash
ci_attempt=$(gh run view "$ci_run_id" --repo "$ci_repo" --json attempt --jq .attempt)
ci_archive="test-results/ci-qualification/$ci_revision/$ci_run_id/attempt-$ci_attempt"
mkdir -p "$ci_archive"
gh run view "$ci_run_id" --repo "$ci_repo" --attempt "$ci_attempt" \
  --json url,headSha,event,attempt,status,conclusion,jobs > "$ci_archive/run.json"
gh api --paginate "repos/$ci_repo/actions/runs/$ci_run_id/artifacts" \
  > "$ci_archive/artifacts.json"
gh run view "$ci_run_id" --repo "$ci_repo" --attempt "$ci_attempt" \
  --log > "$ci_archive/run.log"
gh run download "$ci_run_id" --repo "$ci_repo" --dir "$ci_archive/artifacts"
```

Record command failures explicitly when logs or artifacts do not exist. The
artifact list and download are scoped to the entire run, not an attempt;
archive before rerunning, retain artifact IDs and creation times, and verify
their producing jobs before attributing them to an attempt. Do not use an
older attempt's surviving artifact to fill a missing report. Keep attempts
separate if rerunning a run ID. For each campaign retain:

- The exact source SHA, workflow files, run URLs/IDs/attempts, timestamps and
  conclusions, plus check annotations for failures before job execution.
- JUnit, coverage, operator drill, movement, Kubernetes and Terraform outputs;
  summarize counts and scope without adding overlapping suites together.
- Original logs and reports, a SHA-256 manifest, commands used to capture
  them, and any redaction/compression mapping. Review for credentials before
  committing evidence or publishing an archive; do not retain kubeconfigs or
  runtime Secret exports.
- A durable repository evidence directory or access-controlled archive whose
  location and checksum manifest are linked from the qualification issue.
  GitHub artifact URLs alone are not durable evidence.

The matrix reports and distributions currently expire after 14 days;
Kubernetes and Terraform artifacts also expire after 14 days. Operator and
move drill evidence expire after 30 days. The GCS
`gcs-emulator-test-reports` and default-suite `default-full-suite-test-reports`
artifacts also retain their JUnit/runtime/dependency reports for 30 days.
Matrix report bundles include runtime, dependency, configuration and service
image information. Consult the selected revision's workflow files for exact
contents and retention; older revisions may only preserve GCS/default-suite
results in logs. After archiving, verify the checksums and record any missing
evidence before accepting the campaign.

## Qualification boundaries and closeout

The matrix runs CPython 3.10–3.14 with real isolated NATS,
PostgreSQL/pgvector, MinIO, and Azurite processes. GCS has a separate HTTP
emulator job. These validate local services and emulator behavior; they do not
establish live Azure/GCS/S3 account behavior. Live-cloud tests remain opt-in.
Record their skips explicitly, along with unsupported-capability skips and
the precise strict GCS media-GET generation-precondition xfail documented in
[the test](../tests/integration/test_gcs.py). An unexpected pass of that strict
xfail requires review; it must not be silently waived.

The default suite intentionally runs without the matrix's service environment;
its service skips are not a substitute for the service-enabled jobs. Preserve
the coverage threshold and investigate a missing required libmagic detector.
The reduced container movement campaign is not a full-scale qualification.

[Kubernetes acceptance](kubernetes.md#automated-qualification) uses a disposable
kind cluster, synthetic data and the development security profile. It qualifies
installation/lifecycle/persistence/autoscaling mechanics on that run's
revision. Production TLS/OIDC, authorization, encryption, network policy,
provider storage and capacity still require target-environment qualification.
[Terraform verification](terraform.md) uses mocked providers and no cloud
apply. Historical M4 cluster evidence and local M4 test results retain their
[original limitations](evidence/m4/README.md).

Close #158 only when the account restriction is resolved, all three current-main
workflows have executed successfully with reviewed limitations and retained
evidence, and required-check/failure-visibility review and ownership are
documented. Keep remaining account/enforcement blockers explicit. The
final-candidate repeat in #160 must record its own revision and fresh results;
this campaign does not prequalify later changes.
