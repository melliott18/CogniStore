# Git workflows for CogniStore

This guide describes how we build, review, and ship CogniStore. The repository
uses a single integration branch so contributors cannot accidentally start from
an older line of development.

## Goals

- Keep `main` tested and releasable.
- Do all work on short-lived branches.
- Make pull requests easy to review, test, and revert.
- Keep tests and documentation synchronized with behavior.

## Branch model

- `main`: the default, integration, and release branch. All pull requests
  target `main`. Follow the standing
  [hosted CI suspension](../AGENTS.md#validation-policy-hosted-ci-suspended):
  use applicable local validation and disclose unavailable hosted evidence.
  Existing merge authorization and enforced branch rules still apply. After
  explicit resumption, applicable hosted checks must pass before merge.
- `feature/<short-topic>`: new capabilities.
- `fix/<short-bug-id>`: normal bug fixes. Include the bug or ticket identifier
  when one exists.
- `hotfix/<short-topic>`: urgent production fixes, still reviewed against
  `main` with an expedited path.
- `docs/<short-topic>` and `chore/<short-topic>`: documentation and maintenance
  changes.

The former `dev` integration branch is retired. Do not recreate it, base new
work on it, or open pull requests against it.

## Daily workflow

1. Update local `main` and create a short-lived branch:

```bash
git switch main
git fetch origin
git pull --ff-only
git switch -c feature/content-policy
```

2. Commit small, focused changes with clear messages.
3. Keep the branch current by rebasing on `origin/main`:

```bash
git fetch origin
git rebase origin/main
```

4. Push the branch and open a pull request with `main` as the base.

## Commit style

Use Conventional Commits:

- `feat:` new feature
- `fix:` bug fix
- `docs:` documentation
- `refactor:` behavior-preserving code change
- `test:` tests only
- `chore:` build, tooling, or infrastructure

Examples:

```text
feat(policy): add content-aware placement rules
fix(cli): require --base to disambiguate commands
```

Use fixup commits during review and autosquash before merge when appropriate:

```bash
git commit --fixup <SHA>
git rebase -i --autosquash origin/main
```

## Pull requests

- Target `main` for every change, including hotfixes.
- Keep the scope focused and explain the rationale and impact.
- Add tests for new behavior and edge cases.
- Update user-facing and operator documentation where relevant.
- Run the local gates in `CONTRIBUTING.md` before pushing.
- Prefer squash merge for a single logical change or rebase merge when the
  individual commits are intentionally preserved.
- Delete the short-lived branch after merge.

## Releases

Releases are cut from a tested commit on `main`; there is no `dev`-to-`main`
promotion step.

1. Land any version and release-note change through a pull request to `main`.
2. Update local `main` and create an annotated Semantic Versioning tag:

```bash
git switch main
git pull --ff-only
git tag -a v0.2.0 -m "CogniStore v0.2.0"
git push origin v0.2.0
```

3. Create a GitHub Release with highlights and links to the included pull
   requests.

## Hotfixes

1. Branch from the latest `main`:

```bash
git switch main
git pull --ff-only
git switch -c hotfix/posix-path
```

2. Implement the smallest safe fix with regression coverage.
3. Open an expedited pull request to `main` and merge only after applicable
   checks pass, following the
   [hosted CI suspension](../AGENTS.md#validation-policy-hosted-ci-suspended)
   while it is in effect. Record local evidence and the hosted gap; do not
   dispatch or wait for hosted workflows during the suspension.
4. Tag a patch release if the fix needs an immediate distribution.

## Keeping branches updated

Rebase short-lived branches on `origin/main`:

```bash
git fetch origin
git rebase origin/main
git push --force-with-lease
```

Use `--force-with-lease` only for your short-lived branch. Never force-push
`main`.

## Testing and documentation

- Run `python -m pytest` for the complete default suite. The repository's
  import configuration keeps duplicate test basenames collision-safe.
- Run Ruff, mypy, coverage, package, and security gates documented in
  `CONTRIBUTING.md`.
- Update `README.md` and `docs/*` for user-visible changes.

## Pull-request checklist

- [ ] Base branch is `main`.
- [ ] Title and description explain what changed and why.
- [ ] Unit and integration tests are updated and passing.
- [ ] Documentation is updated where needed.
- [ ] Commits are focused, with fixups squashed where appropriate.

## Legacy `dev` transition

`main` became the sole integration target and the remote `dev` branch was
retired on 2026-08-22. Existing clones should switch to `main`, update it, and
remove their local `dev` branch after preserving any unpushed work.

For more contributor guidance, see `CONTRIBUTING.md`, `README.md`,
`docs/bug_tracker.md`, and `docs/roadmap.md`.
