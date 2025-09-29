# Git workflows for CogniStore

This guide describes how we use Git to build, review, and ship CogniStore. It’s designed to be fast for a solo dev and scalable for future contributors.

## Goals
- Keep `main` always releasable
- Do day-to-day work on short-lived branches
- Make PRs easy to review, test, and revert
- Ensure tests/docs are updated before merge

## Branch model

- `main` (default): stable, tagged releases. CI must be green.
- `dev` (integration): active development; feature PRs target `dev` by default.
- `feature/<short-topic>`: short-lived branches for a change (e.g., `feature/content-policy`, `feature/s3-driver`).
- `fix/<short-bug-id>`: bugfix branches (e.g., `fix/BUG-2025-001-posix-path`).
- `hotfix/<short>`: urgent fix branched from `main`, merged back to `main` then `dev`.

## Daily workflow

1) Create a feature branch from the latest `dev`:
```bash
git checkout dev
git pull --ff-only
git switch -c feature/content-policy
```
2) Commit small, focused changes with clear messages (see Commit style below).
3) Keep up to date by rebasing on `dev`:
```bash
git fetch origin
git rebase origin/dev
```
4) Push and open a PR against `dev`.

## Commit style (Conventional Commits)

Use Conventional Commits for clarity and automated changelogs:
- `feat:` new feature
- `fix:` bug fix
- `docs:` documentation
- `refactor:` code change w/o behavior change
- `test:` tests only
- `chore:` build/tools/infra

Examples:
```
feat(policy): add content-aware policy with mime and glob rules
fix(cli): require --base to disambiguate subcommands
```
Use `fixup!` commits during review and autosquash before merge:
```bash
git commit --fixup <SHA>
git rebase -i --autosquash origin/dev
```

## Pull requests

- Target branch: `dev` (except hotfixes to `main`)
- Keep PRs small and focused; include:
  - Rationale and scope in the description
  - Tests for new behavior and edge cases
  - Docs updated (README or docs/*)
- Make CI green:
  - Run tests locally before pushing: `pytest -vv -rA`
  - In VS Code, use the task “Run unit tests” (sets PYTHONPATH and verbosity)
- Prefer rebase to keep history linear; avoid merge commits in feature branches
- After approval, squash-merge or rebase-merge to keep history clean

## Releases

1) From `dev` → `main` via a release PR (or fast-forward if identical)
2) Tag an annotated version (Semantic Versioning):
```bash
git checkout main
git pull --ff-only
# bump version in project metadata (pyproject.toml/setup files) if applicable
git commit -m "chore(release): v0.2.0"
git tag -a v0.2.0 -m "CogniStore v0.2.0"
git push origin main --tags
```
3) Create a GitHub Release with highlights (link to merged PRs)

## Hotfixes

1) Branch from `main`:
```bash
git checkout main && git pull --ff-only
git switch -c hotfix/posix-path
```
2) Implement fix + tests, open PR to `main`
3) After merge, back-merge to `dev`:
```bash
git checkout dev && git pull --ff-only
git merge --ff-or-rebase origin/main
```

## Syncing forks / keeping branches updated

- Sync your feature branch regularly:
```bash
git fetch origin
git rebase origin/dev
```
- Resolve conflicts locally and force-push the feature branch if needed:
```bash
git push --force-with-lease
```
(Avoid force-pushing `dev` or `main`.)

## Testing and documentation

- Tests: `pytest -vv -rA` (configured in `pytest.ini`)
- VS Code task: “Run unit tests” runs pytest with verbosity and sets `PYTHONPATH`
- Docs: update `README.md` and `docs/*` for user-facing changes

## Code review checklist (for PR authors)

- [ ] PR targets correct branch (`dev` or `main` for hotfix)
- [ ] Clear title and description (what/why)
- [ ] Unit/integration tests updated and passing
- [ ] Docs updated (usage, flags, examples)
- [ ] Small, reviewable commits (squash fixups before merge)

## Handling conflicts

- Prefer rebase over merge in feature branches:
```bash
git fetch origin
git rebase origin/dev
```
- Use `--force-with-lease` when updating the remote feature branch
- If conflict is large, consider splitting the PR into smaller chunks

## Do’s and don’ts

- Do: keep branches short-lived; rebase early and often
- Do: write meaningful commit messages; update tests and docs
- Don’t: commit virtualenvs, secrets, large binaries
- Don’t: force-push protected branches (`main`, `dev`)

## Quick reference

```bash
# Start a feature
git switch -c feature/<topic>

# Run tests
pytest -vv -rA

# Rebase on latest dev
git fetch origin
git rebase origin/dev

# Push and open PR
git push -u origin feature/<topic>

# After approval: squash-merge or rebase-merge to dev

# Release to main
# (bump version), tag, push tags
```

---

For more contributor guidance or repo conventions, see also:
- `README.md` (usage and CLI examples)
- `docs/bug_tracker.md` (bug tracker)
- `docs/roadmap.md` (roadmap)
