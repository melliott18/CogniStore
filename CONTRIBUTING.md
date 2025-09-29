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
