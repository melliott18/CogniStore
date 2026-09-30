# CogniStore repository instructions

## Branch naming

- Never create branches with the `codex/` prefix.
- Use the repository's documented type prefixes: `feature/`, `fix/`,
  `hotfix/`, `docs/`, or `chore/`.
- Use lowercase, hyphen-separated topics after the prefix. Bug fixes should
  include the bug or ticket identifier when one exists.

## Integration branch

- Create short-lived branches from the latest `origin/main` and target `main`
  with pull requests.
- The former `dev` integration branch is retired. Do not recreate it, base new
  work on it, or open pull requests against it.

<a id="validation-policy-hosted-ci-suspended"></a>

## Validation policy: hosted CI resumption

- On 2026-09-30, the user explicitly requested making the repository public
  to resume GitHub-hosted CI. Once repository visibility is public, resume
  applicable hosted checks, including dispatch, observation and necessary
  reruns. Until then, the 2026-09-21 suspension remains in effect.
- This authorization does not establish that the account billing/spending
  restriction is resolved or that any hosted check passed. Use standard
  GitHub-hosted runners; paid services, spending increases and cloud
  provisioning still require separate authorization.
- Run appropriate local checks for the change, using
  [CONTRIBUTING.md](CONTRIBUTING.md). Record the revision, commands, results,
  and material validation gaps. Preserve historical hosted results recorded as
  `skipped: user instruction; known GitHub billing/spending restriction`,
  never relabeling them as passing. Record fresh hosted run identities and
  actual outcomes; missing or unexecuted checks are not passes. Existing
  failed hosted runs are not new test failures.
- Continue authorized implementation, local validation, commits, PR work,
  merges, and ticket updates within the user's requested scope.
  This policy does not grant new merge authority, change branch protection,
  or satisfy explicit release-qualification criteria. Record any remaining
  qualification gap once and continue independent work.
- Keep workflow definitions and security/regression checks intact. The
  [CI runbook](docs/ci_runbook.md) defines recovery and qualification after
  publication. If jobs remain blocked before execution, record the cause
  once and continue independent work without repeated blind reruns.
- Pass this policy to delegated agents and tasks, including agents working in
  older worktrees that may not contain these instructions yet.

## Defensive work and task context

- Describe security work accurately as authorized development and defensive
  verification of CogniStore. In task handoffs, identify the owned repository,
  affected component, intended fix, and permitted test environment.
- Prefer isolated local tests with temporary storage, synthetic identities,
  and fixture credentials for reproductions and regression checks. Testing
  external or production systems requires authorization for those systems.
- Preserve technical findings, reproduction evidence, and regression tests.
  For routine planning and status requests, summarize the scope, finding,
  fix, and validation result and link to the detailed evidence; load further
  details when needed for the actual work.
- If an OpenAI safeguard blocks a step, report the exact error and stop that
  step. Do not retry in a loop or change wording/models to evade the block.
  Continue independent permitted work when possible. Do not submit feedback
  or support reports unless the user requests it. Clear scope cannot guarantee
  that automated safeguards will avoid false positives.
