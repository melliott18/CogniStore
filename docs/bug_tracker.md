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

- [ ] BUG-2026-004: Catalog scan stores a sample digest as full SHA-256 and replaces metadata
  - Status: open
  - Severity: S1 (High)
  - Affects: `993fbb8`; components: scanner, indexer, catalogs
  - Environment: reproduced with a POSIX object larger than 1 MiB
  - Reporter: M1 implementation audit
  - Owner: unassigned
  - Created: 2026-08-20
  - Updated: 2026-08-20
  - Repro steps:
    1. Store an object larger than 1 MiB with existing catalog metadata.
    2. Run `scan_catalog` for its tier.
    3. Compare catalog `sha256` with the full-object digest and inspect metadata.
  - Expected: sampled evidence is explicitly named and existing independent
    metadata is preserved.
  - Actual: `sha256` covers only the first 1 MiB and the upsert replaces
    existing metadata.
  - Minimal test case: add large-object and metadata-merge coverage for both
    in-memory and SQLite catalogs.
  - Notes/Workaround: do not treat scan `sha256` as canonical for objects above
    1 MiB; verified move checksums are full-object digests.
  - Links: `cognistore/core/scanner.py`, `cognistore/core/indexer.py`; ticket #34

- [ ] BUG-2026-012: POSIX containment is vulnerable to concurrent symlink swaps
  - Status: open
  - Severity: S1 (High)
  - Affects: `052487d`; components: POSIX driver, mover cleanup
  - Environment: platforms with mutable POSIX directory trees
  - Reporter: M1 implementation audit; reconfirmed by M1 verification review
  - Owner: unassigned
  - Created: 2026-08-27
  - Updated: 2026-08-27
  - Repro steps:
    1. Pass initial below-root and no-symlink validation for an object path.
    2. Concurrently replace a path component with a symlink before the later
       path-based open, stat, replace, or unlink.
  - Expected: no concurrent mutation can redirect an operation outside the
    configured tier root.
  - Actual: validation and use are separate path-based operations; static
    symlink tests do not close the race.
  - Minimal test case: deterministic adversarial swaps around read, publish,
    list, stat, and conditional-delete operations.
  - Notes/Workaround: use isolated tier roots writable only by trusted
    operators until descriptor-relative, no-follow containment is implemented.
  - Links: issue #91; `cognistore/drivers/posix_driver.py`;
    `docs/m1_review_2026-08-20.md`

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

- [x] BUG-2026-009: CLI usage errors can disclose a space-separated secret
  - Status: fixed
  - Severity: S2 (Medium)
  - Updated: 2026-08-27
  - Resolution: parser diagnostics and all pre-parser selectors now operate on
    a token-preserving redacted argument view. Attached, separate, short,
    repeated, multi-token, leading-dash, and selector-like secret values cannot
    enter usage or configuration errors.
  - Verified by: `tests/unit/test_cli_output.py` and
    `tests/unit/test_cli_contract.py`.
  - Fixed in: [PR #93](https://github.com/melliott18/CogniStore/pull/93).

- [x] BUG-2026-010: Hard worker loss permanently wedges a scheduled scope
  - Status: fixed
  - Severity: S1 (High)
  - Updated: 2026-08-27
  - Resolution: operators can inspect stale occurrences read-only and apply an
    explicitly fenced, owner-matched recovery. The same job and generation
    become retryable in one audited, idempotent transaction while retaining the
    logical scope lock.
  - Verified by: `tests/unit/test_scheduler.py`,
    `tests/unit/test_schedule_recovery_cli.py`, and
    `tests/integration/test_scheduled_run_recovery.py`.
  - Fixed in: [PR #93](https://github.com/melliott18/CogniStore/pull/93).

- [x] BUG-2026-011: Default pytest invocation cannot collect the full suite
  - Status: fixed
  - Severity: S2 (Medium)
  - Updated: 2026-08-27
  - Resolution: pytest's default import mode gives duplicate-basename test
    files distinct module identities, and CI runs the exact documented
    `python -m pytest` command from a clean development install.
  - Verified by: the default-suite CI job in
    [run 33114755574](https://github.com/melliott18/CogniStore/actions/runs/33114755574).
  - Fixed in: [PR #93](https://github.com/melliott18/CogniStore/pull/93).

- [x] BUG-2026-005: Manual CLI moves cannot resume their durable move job
  - Status: fixed
  - Severity: S1 (High)
  - Updated: 2026-08-24
  - Resolution: `move --idempotency-key` now binds a caller-supplied key to a
    durable SQLite move job and resumes that exact job on replay, including
    after destination publication. `move-status`, filterable `move-list`, and
    `move-resume` expose the journal and provide an explicit recovery workflow;
    identity conflicts and terminal failures remain fail-closed.
  - Verified by: `tests/integration/test_cli_move_recovery.py`.
  - Operator guide: `docs/cli.md` (Durable manual-move recovery).

- [x] BUG-2026-003: POSIX publication and deletion lack durability barriers
  - Status: fixed
  - Severity: S1 (High)
  - Updated: 2026-08-21
  - Resolution: POSIX writes now sync file contents before publication and sync
    destination/staging namespaces afterward. Directory creation, range writes,
    deletion, missing-delete retries, and visible-destination recovery all fail
    closed around their required barriers. Darwin uses `F_FULLFSYNC`.
  - Verified by: `tests/unit/test_posix_driver.py` and
    `tests/integration/test_move_jobs.py::test_recovery_reconfirms_visible_destination_durability_before_source_cleanup`.
  - Follow-up: filesystem and sudden-power-loss qualification remains under
    ticket #29.

- [x] BUG-2026-006: One failed recovery job prevents later jobs from resuming
  - Status: fixed
  - Severity: S1 (High)
  - Updated: 2026-08-21
  - Resolution: a definitively missing PREPARED source is recorded as a terminal
    failed move. Synchronous and worker recovery continue through later jobs
    before reporting the first terminal failure.
  - Verified by:
    `tests/integration/test_move_jobs.py::test_recovery_terminalizes_missing_source_and_continues_with_later_job`
    and
    `tests/unit/test_job_handlers.py::test_policy_handler_isolates_failed_recovery_from_later_healthy_job`.

- [x] BUG-2026-007: Catalog scans can corrupt placement while moves are active
  - Status: fixed
  - Severity: S1 (High)
  - Updated: 2026-08-21
  - Resolution: generation-stable scan observations use object-specific move
    fences, atomically rechecked with catalog publication. Active moves and
    authoritative terminal outcomes reject stale or unverified placements.
  - Verified by: `tests/integration/test_scanner_move_coordination.py` for both
    in-memory and SQLite catalogs.

- [x] BUG-2026-008: SQLite prefix matching expands wildcards and ignores ASCII case
  - Status: fixed
  - Severity: S1 (High)
  - Updated: 2026-08-21
  - Resolution: SQLite prefix filtering now uses a binary, literal substring
    comparison matching the in-memory catalog's case-sensitive `str.startswith`
    semantics.
  - Verified by:
    `tests/unit/test_sqlite_catalog.py::test_list_prefix_matches_in_memory_literal_case_sensitive_semantics`.

- [x] BUG-2026-001: Concurrent source replacement can be deleted by move cleanup
  - Status: fixed
  - Severity: S0 (Critical)
  - Fixed in: `fix/critical-movement-races`
  - Resolution: move jobs persist the source generation and cleanup uses the
    driver's conditional deletion contract. A changed source remains intact,
    the catalog is reconciled to it, and the move ends with durable failure
    evidence.
  - Verified by:
    `tests/integration/test_move_jobs.py::test_cleanup_never_deletes_a_replaced_source_generation`
    and the shared conditional-delete conformance test.
  - Links: `docs/m1_review_2026-08-20.md`; related roadmap tickets #22 and #23

- [x] BUG-2026-002: Destination replacement after final verification can cause data loss
  - Status: fixed
  - Severity: S0 (Critical)
  - Fixed in: `fix/critical-movement-races`
  - Resolution: the verified destination generation is persisted and rechecked
    before conditional source deletion. A mismatch retains the source, fails
    the move with evidence, and restores the source catalog placement.
  - Verified by:
    `tests/integration/test_move_jobs.py::test_cleanup_retains_source_if_destination_changes_after_final_verification`.
  - Links: `docs/m1_review_2026-08-20.md`; related roadmap tickets #22 and #23

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
