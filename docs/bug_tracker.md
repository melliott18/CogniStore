# CogniStore Bug Tracker

Use this document to capture and track bugs across the project. Prefer one concise entry per bug. Link to PRs, commits, failing tests, logs, and repro datasets when possible.

## Conventions

- Status: `open`, `triage`, `in-progress`, `blocked`, `fixed`, `deferred`, `won't-fix`
- Severity:
  - `S0 (Critical)`: data loss/corruption, security, blocks releases
  - `S1 (High)`: major feature broken, no workaround
  - `S2 (Medium)`: functional bug with workaround, degraded behavior
  - `S3 (Low)`: cosmetic, minor, docs
- IDs: `BUG-YYYY-NNN` (increment NNN per year)
- Linkage: reference related issues, tests, PRs, commits, and affected drivers/policies

## Template

Copy/paste and fill for each new bug:

- [ ] BUG-YYYY-NNN: <short title>
  - Status: open
  - Severity: S2 (Medium)
  - Affects: <version/commit>, components: <e.g., posix-driver, mover, policy-runner>
  - Environment: <OS, Python version, backends/tier config>
  - Reporter: <name or handle>
  - Owner: <assignee>
  - Created: <YYYY-MM-DD>
  - Updated: <YYYY-MM-DD>
  - Repro steps:
    1. <step>
    2. <step>
  - Expected: <describe>
  - Actual: <describe>
  - Logs/Output: <links or summary>
  - Minimal test case: <path to test or gist>
  - Notes/Workaround: <optional>
  - Links: <PRs, commits, related bugs>

---

## Triage Queue

<!-- New bugs go here until triaged. Move to Open once validated and scoped. -->

- [ ] BUG-2025-001: <title>
  - Status: triage
  - Severity: S2
  - Affects: <commit>
  - Reporter: <name>
  - Created: 2025-09-28
  - Notes: <initial hypothesis>

## Open

<!-- Validated bugs pending assignment or ready to pick up. -->

- [ ] BUG-2025-XXX: <title>

## In Progress

<!-- Assigned and actively being worked. Include branch/PR links. -->

- [ ] BUG-2025-XXX: <title>
  - Owner: <name>
  - PR: <link>

## Blocked

<!-- Waiting on dependency or external factor. Note the blocker and ETA. -->

- [ ] BUG-2025-XXX: <title>
  - Blocked by: <dependency/PR/infra>

## Fixed (Changelog)

<!-- When closing a bug, move the checklist item here and add the commit/PR. -->

- [x] BUG-2025-000: <example fixed bug>
  - Fixed in: <commit/PR>
  - Verified by: <test/link>

## Deferred / Won't Fix

<!-- Record rationale to avoid rediscovery. -->

- [ ] BUG-2025-XXX: <title>
  - Rationale: <why>

## Test Debt / Follow-ups

- [ ] Add regression test for <area>
- [ ] Improve logging around <component>
- [ ] Add property/fuzz tests for <module>
