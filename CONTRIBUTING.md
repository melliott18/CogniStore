# Contributing to CogniStore

Welcome! This guide helps you get set up and contribute effectively.

## Quick start

CogniStore supports CPython 3.10 through 3.14.

1) Clone, create a virtualenv, and install the editable package with all
development tools:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

2) Run the local quality suite:

```bash
python -m ruff check .
python -m mypy cognistore
python -m pytest --cov=cognistore --cov-report=term-missing --cov-report=xml
python -m bandit -c pyproject.toml -r cognistore -ll -ii
python -m pip_audit .
python -m build
python -m twine check dist/*
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

- Use `python -m pytest` (configured in `pyproject.toml`).
- Run unit and conformance tests with
  `python -m pytest tests/unit tests/conformance`.
- Run integration tests with `python -m pytest tests/integration`. NATS and
  MinIO cases require the isolated services and environment variables described
  in their respective documentation; local filesystem integration tests do not.
- Coverage is measured against `cognistore` and must remain at or above 80%.
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
python -m ruff check .
python -m mypy cognistore
python -m pytest --cov=cognistore --cov-report=term-missing
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
python -m pytest --cov=cognistore --cov-report=term-missing
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
