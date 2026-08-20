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

- [ ] BUG-2026-003: POSIX publication and deletion lack durability barriers
  - Status: open
  - Severity: S1 (High)
  - Affects: `993fbb8`; components: POSIX driver, mover cleanup
  - Environment: confirmed by code audit; power-loss qualification pending
  - Reporter: M1 implementation audit
  - Owner: unassigned
  - Created: 2026-08-20
  - Updated: 2026-08-20
  - Repro steps:
    1. Publish a staged destination through `os.replace` or `os.link`.
    2. Observe that neither the file nor destination parent is synced.
    3. Delete the source without syncing its parent.
  - Expected: a move is reported complete only after the destination and
    namespace changes are durably ordered before source removal.
  - Actual: verification can read page-cache data that is not durable across a
    sudden power loss, after which the source has already been removed.
  - Minimal test case: add a fault-injected durability contract and record a
    filesystem/power-loss qualification under ticket #29.
  - Notes/Workaround: no general application-level workaround; use a tested
    durable filesystem profile and do not claim power-loss safety yet.
  - Links: `cognistore/drivers/posix_driver.py`; related tickets #22 and #29

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

- [ ] BUG-2026-005: Manual CLI moves cannot resume their durable move job
  - Status: open
  - Severity: S1 (High)
  - Affects: `993fbb8`; components: CLI, move-job recovery
  - Environment: reproduced with SQLite and two POSIX tiers
  - Reporter: M1 implementation audit
  - Owner: unassigned
  - Created: 2026-08-20
  - Updated: 2026-08-20
  - Repro steps:
    1. Interrupt a manual move after destination publication.
    2. Repeat the same CLI command with the same catalog.
  - Expected: the CLI resumes or exposes the existing durable job.
  - Actual: every invocation generates a new idempotency key and preflight
    exits on the existing destination, leaving the original job incomplete.
  - Minimal test case: add CLI crash/resume coverage with a caller-supplied key.
  - Notes/Workaround: recover through the Python mover API using the original
    idempotency key; the CLI currently has no supported recovery command.
  - Links: `cognistore/cli/cognistore_cli.py`; fold into ticket #26

- [ ] BUG-2026-006: One failed recovery job prevents later jobs from resuming
  - Status: open
  - Severity: S1 (High)
  - Affects: `993fbb8`; components: mover recovery, worker handlers
  - Environment: reproduced with two PREPARED POSIX move jobs
  - Reporter: M1 implementation audit
  - Owner: unassigned
  - Created: 2026-08-20
  - Updated: 2026-08-20
  - Repro steps:
    1. Create an earlier PREPARED job whose source is missing.
    2. Create a later healthy PREPARED job.
    3. Run incomplete-job recovery.
  - Expected: the first failure is recorded and recovery continues independently.
  - Actual: `FileNotFoundError` aborts the recovery batch and the healthy job
    remains unmoved on every retry.
  - Minimal test case: cover mixed failing/healthy recovery batches in mover
    and worker handler tests.
  - Notes/Workaround: repair or terminalize the first blocking job manually.
  - Links: `cognistore/core/mover.py`, `cognistore/jobs/handlers.py`

- [ ] BUG-2026-007: Catalog scans can corrupt placement while moves are active
  - Status: open
  - Severity: S1 (High)
  - Affects: `993fbb8`; components: scanner, catalog, move state machine
  - Environment: reproduced with SQLite and POSIX hot/warm tiers
  - Reporter: M1 implementation audit
  - Owner: unassigned
  - Created: 2026-08-20
  - Updated: 2026-08-20
  - Repro steps:
    1. Pause a move in CLEANUP, scan the source tier, then resume; or pause it
       in TRANSFERRED, corrupt and scan the destination, then resume.
    2. Inspect job state, physical objects, and catalog placement.
  - Expected: scans reconcile with active move generations and cannot publish
    unverified or stale placement.
  - Actual: a source scan can leave the catalog on a deleted source after a
    completed move; a destination scan can leave it on corrupt destination
    bytes after the move fails and retains the valid source.
  - Minimal test case: add concurrent scan/move tests at every state transition.
  - Notes/Workaround: do not scan source or destination scopes while moves for
    those objects are active.
  - Links: `cognistore/core/scanner.py`, `cognistore/core/mover.py`

- [ ] BUG-2026-008: SQLite prefix matching expands wildcards and ignores ASCII case
  - Status: open
  - Severity: S1 (High)
  - Affects: `993fbb8`; components: SQLite catalog, scoped policy runs
  - Environment: reproduced with SQLite on macOS/Python 3.12
  - Reporter: M1 implementation audit
  - Owner: unassigned
  - Created: 2026-08-20
  - Updated: 2026-08-20
  - Repro steps:
    1. Add keys `a_one`, `abone`, `a%literal`, and `axliteral`.
    2. List with literal prefix `a_` or `a%`.
    3. Compare with in-memory `Catalog.list` and test `Foo` versus `foo`.
  - Expected: prefix semantics are literal, case-sensitive `str.startswith`.
  - Actual: both wildcard prefixes return all four keys and ASCII case is
    insensitive, so policy scope can include unintended objects.
  - Minimal test case: add catalog parity tests for `%`, `_`, escape characters,
    and case.
  - Notes/Workaround: avoid wildcard characters and case-distinct prefixes;
    this does not fully restore cross-catalog parity.
  - Links: `cognistore/core/sqlite_catalog.py`

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
