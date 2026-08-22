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
