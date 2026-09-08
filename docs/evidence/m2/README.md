# M2 closeout evidence

M2's knowledge layer and search requirements were accepted on 2026-09-08.
All 13 delivery issues (#30–#42) and the three verification/tracking
follow-ups (#100–#102) are complete. This record supports the closeout of
[epic #13](https://github.com/melliott18/CogniStore/issues/13) and
[milestone 1](https://github.com/melliott18/CogniStore/milestone/1).

## Provenance

- Qualified source revision:
  [`2dcde3b70bf085c79d4b79ed9a93e280d6a92332`](https://github.com/melliott18/CogniStore/commit/2dcde3b70bf085c79d4b79ed9a93e280d6a92332)
- Final delivery: [PR #119](https://github.com/melliott18/CogniStore/pull/119),
  merged on 2026-09-02, completing policy features in #42.
- Qualification: [CI run 33665981824](https://github.com/melliott18/CogniStore/actions/runs/33665981824),
  the `main` push run for that exact revision; all 11 jobs succeeded.
- Run started at `2026-09-02T18:14:21Z` and completed at
  `2026-09-02T18:21:42Z`; reports retrieved on 2026-09-08.
- [Machine-readable evidence](ci-33665981824.json) records job URLs, results,
  selected log excerpts, artifact identity, report hashes, and test counts.

The retained Python 3.12 reports come from GitHub artifact `9860842926`
(`test-reports-python-3.12`). The compressed copies below decompress to the
original report bytes. Their original hashes and sizes are recorded in the
JSON, and [SHA256SUMS](SHA256SUMS) covers the retained files.

- [Unit and conformance JUnit](unit.xml.gz)
- [Integration JUnit](integration.xml.gz)
- [Coverage XML](coverage.xml.gz)

These artifacts qualify the implementation revision above. CI for the
documentation closeout is recorded separately on its pull request.

## Accepted results

| Gate | Result |
| --- | --- |
| Python 3.10–3.14 | Each version passed 1,222 unit/conformance tests and 160 integration tests, with one capability skip |
| Coverage with branch measurement enabled | 84.74% on 3.10–3.12, 84.76% on 3.13, 84.77% on 3.14; all exceed the 80% combined statement/branch gate |
| Default full suite | 1,286 passed, 81 skipped where opt-in external services were absent |
| Compose | 160 integration tests passed, one capability skip; interrupted move diagnostics survived worker restart |
| Quality and packaging | Ruff, mypy, deterministic OpenAPI, wheel/sdist metadata, and installation outside the checkout passed |
| Security | Dependency, installed-development-dependency, Bandit, and secret scans passed |

The service-enabled matrix and Compose jobs exercised isolated
PostgreSQL/pgvector, NATS, and MinIO. Their sole integration skip was the S3
range-write conformance case: that driver does not advertise range writes.
Default-suite service skips are not missing acceptance evidence; the matrix
and Compose runs cover those services. Counts across these configurations
must not be added because the default suite skips the MinIO module as a unit.

The Python 3.12 coverage XML separately reports 88.01% line coverage and
74.09% branch coverage. The 84.74% gate is their combined coverage.py measure,
not branch-only coverage.

## Acceptance mapping

Paths below identify tests at the qualified revision. The retained JUnit
reports preserve their executed names and results.

| M2 requirement | Delivery and verification evidence |
| --- | --- |
| Durable Postgres/pgvector catalog and audit history | #30, #31, #100, #101; `tests/integration/test_postgres_catalog.py` covers migrations, import, audit, lifecycle, detached snapshots, and concurrency; catalog conformance runs against memory, SQLite, and PostgreSQL |
| Metadata, vector, and keyword queries | #36–#38; `tests/integration/test_pgvector_embeddings.py`, `tests/integration/test_pgvector_embedding_benchmark.py`, `tests/unit/test_keyword_search.py`, and `tests/unit/test_ask_retrieval.py` cover model-space isolation, exact/HNSW search, rebuilds, filters, and hybrid retrieval |
| PDF/DOCX extraction, chunks, and deduplication | #32–#35; `tests/unit/test_document_extraction.py`, `tests/unit/test_content_identity.py`, and PostgreSQL shared-reference regressions cover bounded extraction, deterministic identities, and safe reference lifecycle |
| Source-backed Ask results through API, SDK, and UI | #38–#41; `tests/integration/test_content_search_sample.py` ingests the packaged corpus, extracts/indexes PDF and DOCX, reuses duplicate content, queries keyword/vector/hybrid modes, checks answers/citations, opens cited bytes, and reloads idempotently; REST, SDK, and UI contract suites cover public access |
| MIME and embedding-derived placement features | #42; `tests/unit/test_content_policy.py` verifies MIME selection, ordered named similarity rules, and safe missing/stale/unavailable behavior; `tests/integration/test_policy_features.py` verifies live pgvector reindexing and reevaluation |

The final ticket's four criteria are satisfied as follows:

1. **Policy selection:** `test_feature_projection_drives_mime_and_embedding_rules`
   verifies fresh MIME and named embedding-rule selection.
2. **Dry-run evidence:** CLI and REST tests expose feature schema, source
   digest, model-space provenance, and freshness. The policy preview reports
   features without mutating placement; see `tests/unit/test_cli_safety.py`,
   `tests/unit/test_rest_api.py`, and `tests/unit/test_policy_runner.py`.
3. **Safe missing providers:** parameterized content-policy tests cover
   missing, stale, and unavailable features. Lower-priority rules cannot
   bypass unresolved required evidence. Policy-runner tests also verify
   generation-bound source digest checks and retention after replacement.
4. **Reindexing:** `test_reindexing_changes_policy_feature_freshness_and_reevaluation`
   verifies a fresh matching decision, safe `stay` after content replacement,
   and a fresh nonmatch after reindexing, with the changed source digest and
   exact model-space provenance.

The completed delivery sequence and implementation PRs are recorded in the
[execution roadmap](../../next_ticket_roadmap_2026-08-27.md). Public workflow
and operator contracts are documented in the [content-search sample](../../content_search_sample.md),
[REST API](../../rest_api.md), [Python SDK](../../python_sdk.md), and
[policy-feature guide](../../policy_features.md).

## Scope limits

- The sample and policy integration fixtures use deterministic embedding
  providers. They establish the indexing/retrieval/policy contracts, not the
  semantic quality of a production model or hosted provider.
- UI contract tests and API integration are retained here. This CI evidence
  does not claim an automated browser-interaction run.
- Python 3.13 and 3.14 each emitted 14 warnings about unclosed SQLite
  connections while passing their tests and coverage gates; the retained JSON
  records this diagnostic limitation.
- Deduplication tracks shared references and reclamation candidates; physical
  reclamation remains disabled as documented in [content identity](../../content_identity.md).
- Policy training, behavioral signals, hysteresis, and cost/carbon optimization
  belong to M3. Production tenancy, deployment, and POSIX concurrent-path
  hardening in [#91](https://github.com/melliott18/CogniStore/issues/91) remain
  later work.
- The CI reduced movement campaign is regression evidence. The separate
  [M1 closeout report](../m1/README.md) owns the one-million-object claim.

## Verify retained reports

From this directory:

```bash
shasum -a 256 -c SHA256SUMS
python -m json.tool ci-33665981824.json >/dev/null
python - <<'PY'
import gzip
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

evidence = json.loads(Path("ci-33665981824.json").read_text())
for report in evidence["retained_files"]:
    data = gzip.decompress(Path(report["retained_path"]).read_bytes())
    assert len(data) == report["bytes"]
    assert hashlib.sha256(data).hexdigest() == report["sha256"]
    ElementTree.fromstring(data)
print("All retained reports match their original artifact bytes.")
PY
```
