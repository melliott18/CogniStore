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
python -m pip install -e ".[dev,azure]"
```

2) Run the local quality suite:

```bash
python -m ruff check .
python -m mypy cognistore
python -m pytest \
  --cov=cognistore --cov-report=term-missing --cov-report=xml
python -m bandit -c pyproject.toml -r cognistore -ll -ii
python -m pip_audit .
python -m build
python -m twine check dist/*
```

3) Start a branch

```bash
git switch -c feature/<topic>
```

Use a change-type prefix (`feature/`, `fix/`, `hotfix/`, `docs/`, or
`chore/`) with a lowercase, hyphen-separated topic. Do not use tool- or
author-specific prefixes such as `codex/`.

4) Make changes + tests, then open a PR to `main`.

## Workflows and guidelines

- Git workflows: see `docs/git_workflows.md`
- Bug tracker: see `docs/bug_tracker.md`
- Roadmap: see `docs/roadmap.md`
- Hosted CI failures, qualification reruns, required checks and evidence:
  see the [CI runbook](docs/ci_runbook.md).

## Commit style

Use Conventional Commits, e.g.:
- `feat(policy): add content-aware rules`
- `fix(cli): require --base to disambiguate`

## Testing

- Run the complete default suite with `python -m pytest`. The repository's
  import configuration keeps duplicate test basenames collision-safe.
- Run unit and conformance tests with
  `python -m pytest tests/unit tests/conformance`.
- Run integration tests with `python -m pytest tests/integration`. NATS and
  MinIO and Azure Blob cases require the isolated services and environment variables described
  in their respective documentation; local filesystem integration tests do not.
- To build the test image and run the complete integration suite entirely in
  containers, use `docker compose --profile integration up --build
  --abort-on-container-exit --exit-code-from integration-tests`.
- Run the deterministic reduced or full movement campaign only as documented in
  the [scale and recovery qualification guide](docs/scale_qualification.md).
  Archive its JSON evidence; a reduced run is not a one-million-object claim.
- Coverage is measured against `cognistore` and must remain at or above 80%.
- In VS Code, prefer the task “Run unit tests” (sets PYTHONPATH and verbosity)

## PR checklist

- [ ] Clear title and description (what/why)
- [ ] Tests added/updated and passing
- [ ] Docs updated (README or docs/*)
- [ ] Small, focused commits (squash fixups before merge)
- [ ] Targets `main`

Thanks for contributing!

---

## End-to-end contribution flow

This is the full, repeatable sequence—from creating your branch to merging and cleaning up.

1) Sync your local `main`
```bash
git switch main
git fetch origin
git pull --ff-only
```

2) Create a feature branch from `main`
```bash
git switch -c feature/<topic>
```

3) Develop with tests
```bash
# write code + tests
python -m ruff check .
python -m mypy cognistore
python -m pytest \
  --cov=cognistore --cov-report=term-missing
```
Commit using Conventional Commits:
```bash
git add -p
git commit -m "feat(policy): add content-aware rules"
```

4) Keep your branch up to date (rebase on latest `main`)
```bash
git fetch origin
git rebase origin/main
# resolve conflicts if any, run tests again
python -m pytest \
  --cov=cognistore --cov-report=term-missing
```

5) Push and open a PR targeting `main`
```bash
git push -u origin feature/<topic>
# open PR on GitHub → base: main, compare: feature/<topic>
```
In the PR description, include what/why, test coverage, and docs changes.

6) Address review feedback
```bash
# make changes
git commit -m "fix: address review feedback on policy runner"
# or use fixup commits and autosquash before merge
git commit --fixup <reviewed-commit-sha>
git rebase -i --autosquash origin/main
```

7) Finalize and merge
- Ensure CI/tests are green.
- Prefer "Squash and merge" (clean history) or "Rebase and merge" if preserving commits matters.
- The squash commit message should follow Conventional Commits.

8) Delete the feature branch (cleanup)
```bash
# after merge
git switch main && git pull --ff-only
git branch -d feature/<topic>
git push origin --delete feature/<topic>
```

9) (Optional) Release
- Follow `docs/git_workflows.md` to version and tag the tested commit on `main`.

Notes:
- For urgent fixes, use the expedited `hotfix/` flow in
  `docs/git_workflows.md`; hotfix pull requests also target `main`.
- The former `dev` integration branch is retired. Do not recreate it or use it
  as a base or pull-request target.
