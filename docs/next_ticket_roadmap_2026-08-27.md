# Next-ticket execution roadmap — 2026-08-27

> **Progress update (2026-09-01):** Groups 1 through 4 are complete, and Group
> 5 has reached its final delivery ticket. Hybrid retrieval landed through
> [PR #113](https://github.com/melliott18/CogniStore/pull/113) (#38), the REST
> contract through [PR #114](https://github.com/melliott18/CogniStore/pull/114)
> plus CI follow-up [PR #115](https://github.com/melliott18/CogniStore/pull/115)
> (#39), the typed SDK through
> [PR #116](https://github.com/melliott18/CogniStore/pull/116) (#40), and the
> content-search UI/sample through
> [PR #117](https://github.com/melliott18/CogniStore/pull/117) (#41).
> [#42](https://github.com/melliott18/CogniStore/issues/42) is the active and
> final M2 delivery ticket.

This plan turns the verification findings and the M2 issue dependencies into
an execution order. GitHub issue dependencies remain authoritative. Group 1
finished M1, Group 2 established the M2 foundation, and the remaining waves
deliver M2 from those stable storage and indexing contracts.

## Group 1 — M1 exit gate (complete)

The three implementation lanes, full campaign, evidence retention, and tracker
closure are complete:

| Lane | Ticket | Deliverable | Result |
| --- | --- | --- | --- |
| A | [#90](https://github.com/melliott18/CogniStore/issues/90) | Collision-safe default test collection and CI enforcement | Complete |
| B | [#26](https://github.com/melliott18/CogniStore/issues/26) | Secret-safe CLI and truthful recovery-preview, exit, and verbosity contracts | Complete |
| C | [#89](https://github.com/melliott18/CogniStore/issues/89) | Fenced, audited stale scheduled-run recovery | Complete |
| Evidence | [#29](https://github.com/melliott18/CogniStore/issues/29) | Clean-revision one-million-object POSIX/S3 campaign | Complete; [report retained](evidence/m1/README.md) |

Group 1 completed in the planned order: the full evidence was retained on
`main`, #29 closed with the immutable report link, then #16 and the M1
milestone closed.

## Group 2 — establish the M2 data foundation (complete)

### Wave 1: catalog contract

[#30](https://github.com/melliott18/CogniStore/issues/30) completed the
Postgres/pgvector DAL and migration path through
[PR #97](https://github.com/melliott18/CogniStore/pull/97). It froze the domain
interfaces, transaction boundaries, schema ownership, and SQLite migration
contract needed by downstream persistence work.

Completed gate:

- clean install, upgrade, downgrade, and failed migration tests;
- catalog parity against current behavior;
- concurrent placement invariant tests; and
- no database-specific SQL outside the DAL.

### Wave 2: audit and MIME in parallel

After #30, these two lanes completed in parallel:

- [#31](https://github.com/melliott18/CogniStore/issues/31) — append-oriented,
  redacted audit events for moves, policies, retries, failures, and manual
  actions, delivered by
  [PR #98](https://github.com/melliott18/CogniStore/pull/98);
- [#32](https://github.com/melliott18/CogniStore/issues/32) — libmagic-based
  MIME detection with a safe filename fallback and persisted provenance,
  delivered by [PR #99](https://github.com/melliott18/CogniStore/pull/99).

#31 was not a dependency of the retrieval path, so it completed in its own lane
without delaying #32. Its completion remains part of the M2 exit criteria.

## Group 3 — build canonical content (complete)

### Wave 3: extraction

[#33](https://github.com/melliott18/CogniStore/issues/33) completed the bounded,
versioned PDF/DOCX extraction pipeline through
[PR #107](https://github.com/melliott18/CogniStore/pull/107). Its documented
parser adapters enforce file, execution-time, and output limits while isolating
per-object failures.

### Wave 4: identity, chunks, and CAS

[#34](https://github.com/melliott18/CogniStore/issues/34) completed the
canonical content-identity boundary through
[PR #108](https://github.com/melliott18/CogniStore/pull/108), resolving
BUG-2026-004: scans no longer publish a first-megabyte sample under the
unqualified `sha256` name or replace unrelated metadata.

Completed gate:

- full-object SHA-256 is streamed with bounded memory;
- chunk boundaries and metadata are deterministic and versioned;
- object-to-chunk/CAS mappings are transactional; and
- empty, one-byte, multi-chunk, large-object, and metadata-merge regressions
  pass on every supported catalog backend.

## Group 4 — fan out the search indexes (complete)

After #34, three independent lanes completed in parallel:

- [#35](https://github.com/melliott18/CogniStore/issues/35) — safe
  deduplication references and deletion semantics, delivered by
  [PR #111](https://github.com/melliott18/CogniStore/pull/111);
- [#36](https://github.com/melliott18/CogniStore/issues/36) — embeddings and
  pgvector query support, delivered by
  [PR #109](https://github.com/melliott18/CogniStore/pull/109);
- [#37](https://github.com/melliott18/CogniStore/issues/37) — a rebuildable
  keyword index, delivered by
  [PR #110](https://github.com/melliott18/CogniStore/pull/110).

Deduplication now maintains transactional shared-reference state and reports
grace-qualified reclamation candidates without deleting bytes. The embedding
and keyword indexes record their model/index versions and are rebuildable from
canonical catalog content.

## Group 5 — compose retrieval and public contracts (active)

The retrieval and public-contract chain is complete through its user-facing
workflow:

1. [#38](https://github.com/melliott18/CogniStore/issues/38) delivered hybrid,
   source-backed retrieval through
   [PR #113](https://github.com/melliott18/CogniStore/pull/113).
2. [#39](https://github.com/melliott18/CogniStore/issues/39) delivered the
   versioned REST/OpenAPI contract through
   [PR #114](https://github.com/melliott18/CogniStore/pull/114), with latest
   mypy compatibility restored by
   [PR #115](https://github.com/melliott18/CogniStore/pull/115).
3. [#40](https://github.com/melliott18/CogniStore/issues/40) delivered the typed
   SDK through [PR #116](https://github.com/melliott18/CogniStore/pull/116).
4. [#41](https://github.com/melliott18/CogniStore/issues/41) delivered the
   content-search UI and end-to-end sample corpus through
   [PR #117](https://github.com/melliott18/CogniStore/pull/117).
5. **Active delivery ticket:**
   [#42](https://github.com/melliott18/CogniStore/issues/42) now feeds MIME and
   embedding-derived features into placement policies. Its #32, #36, and #39
   dependencies are complete.

## Execution and dependency view

```text
M1:  #90 ─┐
      #26 ─┼─> #29 full evidence ─> close #16  [complete 2026-08-29]
      #89 ─┘

M2:  [done] #30 ─┬─> [done] #31
                 └─> [done] #32 ─> [done] #33 ─> [done] #34 ─┬─> [done] #35
                           #30 ──────────────────────────┼─> [done] #36 ─┐
                                                       └─> [done] #37 ─┴─> [done] #38 ─> [done] #39 ─┬─> [done] #40 ─> [done] #41
                                                                                                     └─> [active] #42
```

The M1 arrows show the completed final closure order, not formal dependency
links in #29. The M2 arrows reflect the dependencies recorded in the issue
bodies.

#31 is complete and remains an M2 completion requirement even though no later
ticket depends on it. #42 is the final active M2 ticket; all of its #32, #36,
and #39 dependencies are complete.

## Cross-wave quality gates

Every ticket should preserve these gates:

- Python 3.10–3.14, Ruff, mypy, branch coverage, package, dependency, static
  security, and secret scans remain green.
- The default full pytest invocation added by #90 stays green; service tests
  may skip only when their documented isolated dependency is absent.
- Database and index changes include clean install, upgrade, rollback, partial
  failure, concurrency, and rebuild coverage appropriate to their state.
- Extraction, hashing, chunking, and embedding remain bounded in memory and
  isolate corrupt or unsupported objects.
- Persisted schemas, parser/chunker/model versions, and public API contracts
  are explicitly versioned before downstream consumers merge.
- Security-sensitive details are redacted before logs, CLI output, audit
  persistence, reports, or API responses.

## Resolved risks and remaining constraint

- #30 fixed migration tooling, connection/transaction ownership, and the
  SQLite compatibility contract; completed follow-up
  [#101](https://github.com/melliott18/CogniStore/issues/101) made the remaining
  lifecycle and concurrency guarantees required regressions.
- #33 selected documented parser adapters and licensed deterministic fixtures.
- #36 and #37 selected rebuildable, provider-isolated embedding and keyword
  adapters with versioned index state.
- #35 defined transactional reference-count and deletion ordering. Physical
  reclamation remains disabled; the current interface only reports candidates.
- [#91](https://github.com/melliott18/CogniStore/issues/91) tracks production
  hardening for POSIX containment under concurrent symlink swaps; M2 code must
  not expand the current trust assumption.
