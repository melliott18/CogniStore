# Next-ticket execution roadmap — 2026-08-27

> **Progress update (2026-08-29):** [PR #93](https://github.com/melliott18/CogniStore/pull/93)
> completed Group 1 tickets #90, #26, and #89. Canonical full-scale run
> `full-20260827-205845` then passed from clean revision `7961c82`; its retained
> [evidence](evidence/m1/README.md) satisfies #29's technical gate. Merge the
> evidence, then close #29 and #16 before starting Group 2.

This plan turns the verification findings and the M2 issue dependencies into
an execution order. GitHub issue dependencies remain authoritative. The first
group finishes M1; the later waves deliver M2 without starting downstream work
before its storage and indexing contracts are stable.

## Group 1 — retain evidence and close the M1 exit gate

The three implementation lanes and full campaign are complete; evidence
retention and tracker closure remain:

| Lane | Ticket | Deliverable | Result |
| --- | --- | --- | --- |
| A | [#90](https://github.com/melliott18/CogniStore/issues/90) | Collision-safe default test collection and CI enforcement | Complete |
| B | [#26](https://github.com/melliott18/CogniStore/issues/26) | Secret-safe CLI and truthful recovery-preview, exit, and verbosity contracts | Complete |
| C | [#89](https://github.com/melliott18/CogniStore/issues/89) | Fenced, audited stale scheduled-run recovery | Complete |
| Evidence | [#29](https://github.com/melliott18/CogniStore/issues/29) | Clean-revision one-million-object POSIX/S3 campaign | Passed; [report prepared for retention](evidence/m1/README.md) |

Complete Group 1 in the planned order: retain the full evidence on `main`, close
#29 with the immutable report link, then close #16 and the M1 milestone.

## Group 2 — establish the M2 data foundation

### Wave 1: catalog contract

Implement [#30](https://github.com/melliott18/CogniStore/issues/30), the
Postgres/pgvector DAL and migration path. It is the critical path for most of
M2. Freeze the domain interfaces, transaction boundaries, schema ownership,
and SQLite migration contract before downstream persistence work merges.

Required gate:

- clean install, upgrade, downgrade, and failed migration tests;
- catalog parity against current behavior;
- concurrent placement invariant tests; and
- no database-specific SQL outside the DAL.

### Wave 2: audit and MIME in parallel

After #30, run these in parallel:

- [#31](https://github.com/melliott18/CogniStore/issues/31) — append-oriented,
  redacted audit events for moves, policies, retries, failures, and manual
  actions;
- [#32](https://github.com/melliott18/CogniStore/issues/32) — libmagic-based
  MIME detection with a safe filename fallback and persisted provenance.

#31 is not a dependency of the retrieval path, so it should continue in its
own lane without delaying #32. It still must finish before M2 closes.

## Group 3 — build canonical content

### Wave 3: extraction

Implement [#33](https://github.com/melliott18/CogniStore/issues/33) after #32.
Choose and document the PDF/DOCX parser runtime, enforce file/time/output
limits, and make per-object failures isolated and versioned.

### Wave 4: identity, chunks, and CAS

Implement [#34](https://github.com/melliott18/CogniStore/issues/34) after #30
and #33. This is the canonical content-identity boundary and must resolve
BUG-2026-004: scans may no longer publish a first-megabyte sample under the
unqualified `sha256` name or replace unrelated metadata.

Required gate:

- full-object SHA-256 is streamed with bounded memory;
- chunk boundaries and metadata are deterministic and versioned;
- object-to-chunk/CAS mappings are transactional; and
- empty, one-byte, multi-chunk, large-object, and metadata-merge regressions
  pass on every supported catalog backend.

## Group 4 — fan out the search indexes

After #34, implement three independent lanes in parallel:

- [#35](https://github.com/melliott18/CogniStore/issues/35) — safe
  deduplication references and deletion semantics;
- [#36](https://github.com/melliott18/CogniStore/issues/36) — embeddings and
  pgvector query support;
- [#37](https://github.com/melliott18/CogniStore/issues/37) — a rebuildable
  keyword index.

Do not let deduplication delete canonical bytes until reference updates,
placement changes, and rollback behavior are proven atomic. Embedding and
keyword indexes must record their model/index versions and be rebuildable from
canonical catalog content.

## Group 5 — compose retrieval and public contracts

The remaining dependency chain is sequential at its core:

1. [#38](https://github.com/melliott18/CogniStore/issues/38) after #36 and #37:
   blend metadata, vector, and keyword retrieval with source-backed results.
2. [#39](https://github.com/melliott18/CogniStore/issues/39) after #30 and #38:
   expose the versioned REST/OpenAPI contract.
3. After #39, run
   [#40](https://github.com/melliott18/CogniStore/issues/40) (typed SDK) and
   [#42](https://github.com/melliott18/CogniStore/issues/42) (MIME/embedding
   policy features) in parallel. #42 also requires #32 and #36.
4. [#41](https://github.com/melliott18/CogniStore/issues/41) follows #39 and
   #40, delivering the search UI and end-to-end sample corpus.

## Execution and dependency view

```text
M1:  #90 ─┐
      #26 ─┼─> #29 full evidence ─> close #16  [closure in progress]
      #89 ─┘

M2:  #30 ─┬─> #31
           └─> #32 ─> #33 ─> #34 ─┬─> #35
                    #30 ────────────┼─> #36 ─┐
                                    └─> #37 ─┴─> #38 ─> #39 ─┬─> #40 ─> #41
                                                           └─> #42
```

The M1 arrows show the final closure order, not formal dependency links in #29.
The M2 arrows reflect the dependencies recorded in the issue bodies.

#31 remains an M2 completion requirement even though no later ticket depends
on it. #42 also depends directly on #32 and #36, as recorded in its issue.

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

## Risks to resolve early

- #30 must decide migration tooling, connection/transaction ownership, and the
  SQLite compatibility period before #31 or #32 persist new fields.
- #33 must choose the initial parser runtime and licensed deterministic
  fixtures before implementation fans out.
- #36 and #37 must choose model and keyword-index adapters that can be rebuilt
  and tested without coupling core domain code to one provider.
- #35 must define reference-count and deletion ordering before any storage
  reclamation is enabled.
- [#91](https://github.com/melliott18/CogniStore/issues/91) tracks production
  hardening for POSIX containment under concurrent symlink swaps; M2 code must
  not expand the current trust assumption.
