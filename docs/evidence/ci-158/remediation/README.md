# #158 CI remediation

Base: `fae9f64811c477703ac25e04ab938be038677d16`. On 2026-10-01 the owner
explicitly authorized resuming hosted CI for #158 and merging validated fixes.
The earlier failed campaign remains recorded in PR #186; these changes do not
relabel those failures or establish #160 frozen-candidate qualification.

## Repairs

- Capture the public OTLP exporter constructor's injected `session` in the
  redirect regression, rather than reading the removed private `_session`.
  Exercise 301, 302, 303, 307 and 308, checking exactly one HTTPS payload send.
- Annotate database scalar results according to the schema. SQLAlchemy 2.1.1
  and mypy 2.3.1 reproduce all 17 original errors; no type checks are disabled.
- Build the isolated MinIO fixture from upstream release
  `RELEASE.2025-10-15T17-29-55Z`, commit
  `9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a`. The source archive checksum and
  Go build image digest are fixed in Dockerfile. Upstream Quay/Docker Hub
  images are unavailable and the previous binary download returns HTTP 410.
  CI, Compose and kind use the same non-root Dockerfile target; kind loads
  the built image explicitly. This is an isolated test service, not a
  production MinIO deployment or security qualification.
- Disposition 129 historical Gitleaks findings: the original 126 plus three
  SHA-256 provenance digests introduced by PR #186. `secret-review.json`
  records each exact fingerprint, classification and source-line checksum.
  Source context was read from every named Git object. `.gitleaksignore`
  adds only commit/path/rule/line fingerprints, without excluding files,
  rules, commits as a whole, or future changes at the same locations.
- Make all deployment workflows run on every PR/main push, including docs
  changes, so all 15 check contexts can be required consistently. The
  reviewed protection request pins checks to the GitHub Actions app,
  requires up-to-date branches and PRs, and enforces checks for admins.
  Repository settings must be applied and read back separately.

## Local validation before hosted runs

Fresh Python 3.13 environment, current project development/Azure dependencies:

- `python -m mypy`: 165 source files pass.
- `python -m ruff check .`: pass.
- `python -m cognistore.api.openapi --check docs/openapi/v1.json`: pass.
- `python -m pytest tests/unit/test_encryption_policy.py tests/unit/test_observability.py tests/unit/test_helm_chart.py`:
  65 passed; 23 Helm contracts skipped because Helm is absent locally.
- `docker build --target minio --tag cognistore-minio:qualification .`:
  pass; Linux arm64 fixture starts as UID 10001 and health check passes.
- `python -m pytest tests/integration/test_s3_minio.py` against that disposable
  loopback fixture: 27 passed, one unsupported-range-write skip.
- `gitleaks git --log-opts='--all' --redact`: 159 commits scanned, no findings
  after exact historical dispositions (Gitleaks 8.30.1).
- `docker compose config -q` and `git diff --check`: pass.

The pre-existing local Python 3.12.7 environment crashed importing macOS
`readline` before pytest collection. Tests use a fresh Python 3.13 environment;
this host failure is not an application-test failure. Full-suite and hosted
results, exact final revisions and read-back protection evidence are recorded
in the qualification follow-up after execution. Local passes alone do not
satisfy hosted current-main qualification, live-cloud or frozen-candidate gates.

## Follow-up validation and enforcement

Main protection was applied on 2026-10-01 and read back independently:
`protection-readback.json` confirms all 15 GitHub Actions contexts,
strict/up-to-date checks, administrator enforcement and PR requirements.
Legacy branch protection supplies enforcement; separate rulesets remain empty.
A synthetic new token at a historically exempted location is still detected
(`secret-negative-control.json`).

The full local default suite at `d7873e9` passed: 6,488 passed, 322 skipped,
zero failures/errors, 89.53% line coverage and 78.70% branch coverage. See
`local-default-summary.json` for exact skip reasons and command; compressed
JUnit/coverage and targeted reports are retained alongside it. Counts from
these overlapping suites must not be summed.

The first PR campaign found a stale MinIO tag in CI's non-root assertion after
all images built successfully. The assertion now uses the same tag as Compose;
the non-root check also passes locally. Its original failure is retained in
`pr-d7873e9-container-failure.log.gz`. This failed attempt is not a qualified
revision, regardless of the other passing jobs.

The isolated local Compose integration profile passed (412 passed, 62 skipped;
synthetic PostgreSQL/NATS/MinIO/Azurite services, no live-cloud credentials).
`docker/verify_shutdown.sh` also passed: the interrupted-move journal survived
worker shutdown and restart. Both disposable Compose projects were removed
with their temporary volumes after validation. Their logs are retained here.
The first hosted default-suite artifact independently records 6,508 passed,
302 skipped, zero failures/errors at `d7873e9`.

The Python matrix timeout is increased from 20 to 30 minutes to accommodate
its existing 13–15 minute unit/conformance suite, the new MinIO source build,
and the previously blocked service integrations. Test commands, coverage
thresholds and checks remain unchanged. Kubernetes acceptance at `d7873e9`
passed install/upgrade/rollback/persistence/autoscaling; its run is
https://github.com/melliott18/CogniStore/actions/runs/36903004174.
