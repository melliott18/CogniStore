# Contributing to CogniStore

Welcome! This guide helps you get set up and contribute effectively.

## Quick start

1) Clone and create a virtualenv
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
2) Run tests to verify your env
```bash
pytest -vv -rA
```
3) Start a branch
```bash
git switch -c feature/<topic>
```
4) Make changes + tests, then open a PR to `dev`.

## Workflows and guidelines

- Git workflows: see `docs/git_workflows.md`
- Bug tracker: see `docs/bug_tracker.md`
- Roadmap: see `docs/roadmap.md`

## Commit style

Use Conventional Commits, e.g.:
- `feat(policy): add content-aware rules`
- `fix(cli): require --base to disambiguate`

## Testing

- Use `pytest -vv -rA` (configured via `pytest.ini`)
- In VS Code, prefer the task “Run unit tests” (sets PYTHONPATH and verbosity)

## PR checklist

- [ ] Clear title and description (what/why)
- [ ] Tests added/updated and passing
- [ ] Docs updated (README or docs/*)
- [ ] Small, focused commits (squash fixups before merge)
- [ ] Targets `dev` (unless hotfix to `main`)

Thanks for contributing!

---

## End-to-end contribution flow

This is the full, repeatable sequence—from creating your branch to merging and cleaning up.

1) Sync your local `dev`
```bash
git checkout dev
git fetch origin
git pull --ff-only
```

2) Create a feature branch from `dev`
```bash
git switch -c feature/<topic>
```

3) Develop with tests
```bash
# write code + tests
pytest -vv -rA
```
Commit using Conventional Commits:
```bash
git add -p
git commit -m "feat(policy): add content-aware rules"
```

4) Keep your branch up to date (rebase on latest `dev`)
```bash
git fetch origin
git rebase origin/dev
# resolve conflicts if any, run tests again
pytest -vv -rA
```

5) Push and open a PR targeting `dev`
```bash
git push -u origin feature/<topic>
# open PR on GitHub → base: dev, compare: feature/<topic>
```
In the PR description, include what/why, test coverage, and docs changes.

6) Address review feedback
```bash
# make changes
git commit -m "fix: address review feedback on policy runner"
# or use fixup commits and autosquash before merge
git commit --fixup <reviewed-commit-sha>
git rebase -i --autosquash origin/dev
```

7) Finalize and merge
- Ensure CI/tests are green.
- Prefer "Squash and merge" (clean history) or "Rebase and merge" if preserving commits matters.
- The squash commit message should follow Conventional Commits.

8) Delete the feature branch (cleanup)
```bash
# after merge
git checkout dev && git pull --ff-only
git branch -d feature/<topic>
git push origin --delete feature/<topic>
```

9) (Optional) Release
- Follow `docs/git_workflows.md` release steps to promote `dev` → `main` and tag.

Notes:
- For urgent fixes to `main`, use the hotfix flow described in `docs/git_workflows.md` and back-merge to `dev` after.
