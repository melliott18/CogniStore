# Ticket #157 API PUT/DELETE ordering evidence

[Ticket #157](https://github.com/melliott18/CogniStore/issues/157) is a P1
production-pilot release gate under
[M5 epic #155](https://github.com/melliott18/CogniStore/issues/155).
The original assessment reproduced a stale DELETE erasing the catalog record
of a successful replacement PUT at revision
`2cce6ff4d43fd287ad008197f1e17f168c3580c4`, using authenticated REST, SQLite, and
real POSIX storage. The replacement bytes remained on storage; the observed
failure was catalog inconsistency and loss of API accessibility.

## Fix contract

Participating API PUT and DELETE requests take an exclusive, fail-fast fence
for tenant/bucket/key before accessing the backend and retain it through
catalog finalization. Tier is excluded from the identity. An overlapping
request returns `409 resource_conflict`, `retryable: true`, and `Retry-After: 1`
without changing bytes or placement. The legal-hold guard remains outside the
object fence, and mutation audit records retain started and terminal outcomes.
Requests may wait at that outer guard before reaching the object fence. The
Windows SQLite guard retains its conservative exclusive CRT lock fallback, so
it can serialize even unrelated keys before object-fence contention is
evaluated. Fail-fast and independent-key qualification targets POSIX SQLite
and PostgreSQL; no Windows concurrency qualification is claimed here.

SQLite fences use stable hashed lock sidecars and coordinate separate processes
that share the catalog and lock namespace. PostgreSQL uses session advisory
locks on the legal-hold guard's existing connection. In-memory coverage is
limited to the same shared catalog instance. Old workers must be quiesced
before deployment; direct backend writers, scans, and moves are outside this
API PUT/DELETE guarantee.

PostgreSQL catalog upsert/deletion finalizes in a short transaction on the
session that owns the object's advisory lock. Session loss before finalization
fails publication rather than reconnecting or using another unlocked
connection. No transaction spans backend I/O. Backend exclusion still requires
the lock session to remain live throughout storage work: session loss releases
locks and may admit another request without cancelling an in-progress storage
call or rolling back bytes. Such partial completion requires inspection and
reconciliation of current state; this mechanism does not make storage and
catalog operations atomic.

Storage and catalog publication remain separate operations. A catalog
finalization exception returns a retryable `503 backend_unavailable` without
rolling back backend changes. A PUT retry with `overwrite=true` (the API
default) overwrites and republishes current state; `overwrite=false` cannot
repair publication while previously written bytes remain. A DELETE retry can
finish removal of a residual catalog row when bytes
are already absent; if the first finalization committed before raising, the
retry returns 404. Retrying is not replay: a DELETE retry can delete a later
replacement. The complete response/state contract is in
[REST API v1](../../rest_api.md#object-mutation-ordering-and-retries), with
[deployment constraints](../../postgres_catalog.md#api-object-mutation-fences).

## Qualification status

- Fix revision: `a7d61e276df9dbbe21ad55eb9d4248d38046902c`, based on
  `2cce6ff4d43fd287ad008197f1e17f168c3580c4` (`origin/main`).
- Validation date: 2026-09-20 UTC.
- Environment: macOS 14.8.7 arm64, CPython 3.12.2, SQLite 3.45.1,
  SQLAlchemy 2.0.54, pytest 9.1.1, real POSIX storage in temporary directories.
  PostgreSQL 16.15 ran in an isolated local
  `pgvector/pgvector:0.8.6-pg16-bookworm` container; each integration fixture
  created and dropped its own database.
- Final relevant suite: **1,232 passed, 2 skipped**, 569.63 seconds, at the fix
  revision above. Both skips are range-write conformance cases for backends
  that do not advertise range writes; all PostgreSQL and new #157 cases ran.
- Ruff, mypy (163 source files), Bandit at the configured `-ll -ii` threshold,
  dependency audit, source/wheel build, and Twine package checks passed.
- Hosted CI status: **not established by this record**.

Retained evidence: [validation metadata and SHA-256 hashes](157/validation.json),
[JUnit results](157/focused-tests.xml.gz), [test log](157/focused-tests.log.gz),
[exploratory failure details](157/exploratory-failures.log.gz), and the
[validation log directory](157/). The record qualifies the tested fix; it does
not claim a merged release or a completed M5 release gate.

The original checkout's Anaconda virtualenv could not run pytest because its
native readline import crashed, and it lacked the API dependencies. Validation
used a fresh worktree-local virtualenv installed with `pip install -e '.[dev,azure]'`.

## Reproduction commands

With the isolated test PostgreSQL DSN in `COGNISTORE_TEST_POSTGRES_DSN`:

```sh
.venv/bin/python -m pytest \
  tests/unit/test_api*.py tests/unit/test_rest_api.py \
  tests/unit/test_object_mutation_fence.py tests/unit/test_catalog*.py \
  tests/unit/test_sqlite_catalog.py tests/unit/test_legal_hold*.py \
  tests/unit/test_storage*.py tests/unit/test_posix*.py tests/unit/test_tenant*.py \
  tests/conformance tests/integration/test_*postgres*.py \
  tests/integration/test_legal_hold*.py tests/integration/test_mover_catalog.py \
  tests/integration/test_posix_publication_recovery.py tests/integration/test_tenant_api.py \
  --junitxml=/tmp/cognistore-157-focused.xml --tb=short
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy cognistore
.venv/bin/python -m bandit -c pyproject.toml -r cognistore -ll -ii
.venv/bin/python -m pip_audit .
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
```

## Integration with current main

Before merging PR #170, revision `ce7733d30fc8b8a59f2c1bd723c6af51c024789a`
integrated main `1bcc5bd6f32323de1796a211079ee554858e5172`, including #156's
tenant-namespace protection and #158/#159's CI and pilot documentation. The
roadmap resolution retains both evidence links and the M5 plan; the API import
resolution retains both safety handlers. No changes from either fix were dropped.

The combined namespace, API mutation, legal-hold, storage-audit, and PostgreSQL
tenant/mutation regression selection passed **389 tests, with no skips or
failures**, in 70.30 seconds. Fresh Ruff, mypy (164 source files), OpenAPI
contract, and diff checks passed. See
[merge-validation metadata and command](157/merge-validation.json),
[JUnit results](157/merge-tests.xml.gz), and [test log](157/merge-tests.log.gz).
This scoped integration run does not change the full-suite or hosted-CI
limitations below. The PR records the selected merge head and its hosted
startup failure caused by the account billing/spending-limit block under #158.

## Broader-suite limitation

An exploratory `pytest --cov=cognistore --cov-report=term-missing` run started
before the final PostgreSQL fixes and was interrupted at 54% after reporting
2,883 passed, 7 failed, and 114 skipped (751.98 seconds). It is **not** a clean
full-suite or final-revision coverage result. One failure was the subsequently
fixed uncertain advisory-lock acquisition cleanup; the final relevant suite
reruns that regression. The other six failures were in document extraction:

- `test_parser_failure_suppresses_logging_python_and_native_output`
- `test_file_limit_accepts_exact_boundary_and_rejects_one_extra_byte`
- `test_output_limit_accepts_exact_boundary_and_rejects_one_byte_less`
- `test_non_finite_metadata_is_rejected_as_parser_error`
- `test_abnormal_worker_exit_is_recorded`
- `test_scan_streams_a_complete_document_larger_than_the_mime_sample`

The parser-output, non-finite-metadata, and abnormal-exit assertions explicitly
reported `timeout`; the remaining three reported unexpected failed status.
The parser-output timeout reproduced on the unchanged baseline in the same
runtime (1 failed, 4.27 seconds), while its isolated worktree rerun passed
(1 passed, 3.95 seconds). This demonstrates a timing-sensitive baseline failure
for that test; it does not establish the cause or baseline status of all six.
These document tests were not changed for #157. Full-suite and coverage
qualification remain outstanding.

## Acceptance mapping

| Acceptance criterion | Evidence to record | Result |
| --- | --- | --- |
| Explicit ordering contract across backend and catalog publication | `CatalogStore.object_mutation`, gateway PUT/DELETE, REST response contract | Implemented and reviewed |
| Exact stale DELETE/replacement PUT seam with real SQLite/POSIX | `test_api_object_mutations.py::test_independent_gateways_serialize_backend_and_catalog_mutations[DELETE]` | Passed: overlapping PUT409, DELETE204, replacement retry201 and readable |
| Opposite order and retry outcomes | Both orderings and pre/post-commit finalization failure cases in `test_api_object_mutations.py` | Passed; false conditional deletion404 and generation mismatch409 retain catalog state |
| Distinct gateways/catalogs and separate workers | Spawned authenticated REST workers in `test_api_object_mutations.py` and `test_postgres_object_mutations.py` | Both orderings passed for SQLite/POSIX and PostgreSQL/POSIX |
| PostgreSQL session loss before finalization | `test_object_mutation_fence_postgres.py::test_postgres_lost_delete_lock_cannot_finalize_over_replacement` | Passed: replacement PUT201, stale DELETE503, replacement catalog/GET/bytes retained |
| Legal holds, audit, independent keys and tenants | Existing legal-hold/storage-audit suites; `test_object_mutation_fence.py` and PostgreSQL counterpart; authenticated audit assertions in REST regressions | New focused regressions passed; full relevant selection recorded above |
| Relevant API/catalog/storage/database checks | Commands above, archived results, environment and source revision | 1,232 passed, 2 capability skips; static/security/package checks passed |

## Scope of evidence

Only the database/storage combinations actually executed are qualified here.
External NATS and cloud-storage integrations were not configured. SQLite/POSIX race evidence alone does not qualify
every cloud backend or database combination. The broader PUT/DELETE/move/scan
concurrency matrix, performance qualification, and recovery drills remain
separate readiness work.
