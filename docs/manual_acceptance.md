# Manual end-to-end acceptance

Ticket [#163](https://github.com/melliott18/CogniStore/issues/163) checks whether
testers can complete the selected CLI, API, SDK and browser workflows and
understand their failures. The versioned
[case matrix](manual_acceptance_matrix.json) contains 16 scenarios and 80
separately recorded interface/identity cases. A narrow observation does not
pass a case whose other expected assertions have not been exercised.

**Current scope: local synthetic rehearsal only.** No live staging or cloud
resources are authorized for this implementation. The accepted owners/specification
review in [#159](production_pilot.md) and actual staging in [#161](staging.md)
remain pending. Local SQLite/POSIX, fixture JWTs, an in-process queue substitute,
or a browser opened on localhost cannot qualify PostgreSQL, S3, JetStream,
production OIDC/proxy, provider quality, encryption or a release candidate.
Keep each such gap explicit; do not close the staging acceptance criteria on
the strength of this runbook or its local checks.

The selected baseline is `m5-pilot-v1` revision 1: **metadata-only Ask**, with
no keyword, vector, embedding or answer provider. No root `embedding` block
is permitted for that composition. MA-13 tests that exact availability
contract. Semantic retrieval/generation quality is excluded by the selected
specification; sample models cannot establish production quality. A future
provider-enabled scope needs an accepted specification and new quality cases.

Hosted CI is
`skipped: user instruction; known GitHub billing/spending restriction`.
Do not dispatch, rerun or poll hosted workflows. Use appropriate local checks
from [CONTRIBUTING.md](../CONTRIBUTING.md).

## Run identity and evidence

Before testing, create a new run with these facts and retain their sanitized
evidence references and SHA-256 digests:

| Field | Required content |
| --- | --- |
| Run | Unique run ID, tester alias, UTC start/end, local-rehearsal or staging-acceptance label. |
| Software | Source revision plus working-tree diff hash when uncommitted, package/dependency manifest, runtime versions; immutable registry image digest for staging. A source commit alone is not an image identity. |
| Configuration | Specification revision and acceptance reference, exact driver/auth/tenant/provider/repair-registry hashes, input and rendered configuration hashes, fixture seed and corpus manifest digest. Secret versions may be restricted references; retain no secret values. |
| Environment | Explicit local fixture path/runtime or accepted staging identity and endpoint; backend/catalog/broker/proxy/provider types. Record every substitution and unavailable dependency. |
| Case result | Case ID, repetition, surface, tenant, role, tester, UTC observation, exact input/command or browser steps, expected result, actual result, pass/fail/not-run status, finding reference, sanitized evidence and digest. |

The [evidence preparation tool](../scripts/manual_acceptance.py) validates
the case inventory and retained evidence. For example, from the checkout:

```bash
python scripts/manual_acceptance.py corpus --output /tmp/ma-163-corpus
python scripts/manual_acceptance.py template \
  --output /tmp/ma-163-record.json --corpus /tmp/ma-163-corpus \
  --tester local-tester --source-sha "$(git rev-parse HEAD)" \
  --candidate-id local-source --configuration-id local-fixture \
  --environment-id localhost-rehearsal --environment-kind local
python scripts/manual_acceptance.py validate /tmp/ma-163-record.json \
  --output /tmp/ma-163-validation.json
```

Use a fresh output path per run. The template is intentionally not complete;
replace descriptive local identity labels with retained configuration/candidate
identities before acceptance. `--require-complete` must fail for unperformed
cases or missing accepted staging/specification evidence. For full API upload
boundary fixtures, generate another corpus with `--limit-bytes 16777216`;
the smaller default supports bounded local extraction checks only.

Attach evidence as `{path, sha256, kind, sanitized, durable_uri}`, with paths
relative to the record directory, `sanitized: true`, and kind `log`,
`screenshot` or `review`. Browser passes require both screenshot and log plus
the browser name/version and `interaction: "actual-browser"`. Keep attempt 1
in the case record and each later repetition in `additional_attempts`, with
its own status, inputs, actual result, UTC execution time and evidence. Use
`blocked` with the observed subset when a case remains incomplete.

A structurally valid report is not proof
that its observations occurred. Review screenshots/logs and the actual state.
Never fill `actual` from the expected result or convert `not_run` into pass.

Repeat **every positive workflow at least three times per applicable interface
and tenant**. The matrix records `minimum_repetitions`; use fresh scoped keys
for each repeat and retain each attempt. Exercise each negative and role denial
at least once per applicable tenant. A combined case passes only when every
assertion passes; record partial results as incomplete with the observations
that did succeed. Do not pool revisions after a fix or silently drop failures.

Retain response status, stable error code, request/job/correlation/decision IDs,
byte counts and SHA-256, before/after placement and relevant audit checkpoints.
API reads can append access/audit events: preview immutability concerns object
bytes, catalog placement and move state, not an unchanged audit event count.
Capture terminal state and storage effects separately; `202` or a job ID never
proves completion. After uncertain submission, reconcile jobs before retrying:
REST action submissions have no caller idempotency key.

Browser evidence must come from actual interaction: record browser/version,
tester actions and screenshots of results, empty states, confirmations, failures
and terminal job views. HTML shell responses, DOM unit tests and curl requests
are supporting evidence only. Crop/redact tokens, Authorization headers,
session cookies, account details and personal information before retention.
Do not retain raw HAR exports or console dumps containing credentials.

Publish reviewed evidence in [the M5 evidence index](evidence/m5/README.md) or
durable linked storage with hashes; ignored `/tmp` files alone are not retained
evidence. Follow the M5 retention contract. Keep original failure and rerun
records, as well as the final outcome.

## Isolated fixtures and identities

Use project-authored synthetic data only, with a fixed seed and a manifest of
relative name, MIME, size, full SHA-256 and expected extraction result. The
corpus helper copies the licensed
[document fixtures](../tests/fixtures/documents/README.md) and creates bounded
synthetic byte/limit fixtures. Preserve its manifest; never assume a filename
or arbitrary padding makes a valid document.

| Fixture | Purpose and expected result |
| --- | --- |
| Same logical `manual-report` key in both tenants, with distinct tenant marker bytes | Detect cross-tenant byte/citation leaks, including known paths. Keep full expected bytes in the manifest. |
| Tenant-only key and unique nonmatching marker | Test a foreign-only direct path and no-match discovery without conflating it with the intentional same-key local copy. |
| Opaque binary including NUL and all byte values; zero-byte object | Exact download, HEAD, SHA-256, range and attachment checks; zero-byte range is unsatisfiable. |
| Valid `sample.pdf` and `sample.docx` | Match the exact normalized text and core metadata in the fixture README. |
| `unsupported.txt`, `encrypted.pdf`, `corrupt.pdf`, `corrupt.docx`, image-only PDF | Stable failure boundaries; image-only PDF may succeed with empty text. |
| Fresh move, held move and ambiguous duplicate fixtures | Exercise preview, safe resume and quarantine without modifying real user objects. |
| Exact-limit and one-byte-over inputs, bounded output-limit and worker-failure fixtures | Separate API limits from extraction limits and show continued processing of the next valid object. |
| Synthetic `example.invalid` email marker and fake hyphenated SSN in a separate PII fixture | Verify detection privacy and unknown/fail-closed behavior without introducing real PII. |

The small manual corpus does not claim the 100,000-object workload, size/MIME
distribution or performance gates from #165. In the selected deployment,
objects above 16 MiB are outside admitted scope even for CLI/backend ingestion.
Test extraction's default 25 MiB limit only in a separate local boundary fixture,
or use explicitly reduced, recorded extraction limits. Do not upload a 25 MiB
object to staging to bypass the admitted-size boundary.

Prepare separate issuer-assigned identities in **each** named tenant:

| Alias / matrix role | Binding and allowed use |
| --- | --- |
| `reader` | Only reader; object/catalog reads, Ask, jobs and hold inspection. |
| `writer` | Only writer; reader operations plus upload/delete subject to holds. |
| `operator` | Only operator; reads, administration/scans and repair/movement authority. It lacks policy permission. |
| `auditor` | Only auditor; audit/retained decisions/hold history, without object or job reads. |
| `admin` | Separate all-permissions identity; still confined to its tenant and holds. |
| `smoke` | Explicit writer + operator + policy_manager union from the staging smoke bindings, for positive policy execution. This must not replace the single-role denial tests. |
| `revoked-operator` | A disposable initially permitted operator, revoked after submission and before worker dispatch. |
| `invalid-identities` | Missing/expired/wrong-audience/wrong-issuer/bad-signature tokens, valid unmapped subjects and removed tenant grants. |
| `trusted-operator` | Local OS/process authority for disposable fixtures, not an HTTP role or demonstration of RBAC. |
| `reviewer` | Evidence reviewer, with no implied production approval authority. |

Store tokens privately outside the evidence bundle. Record sanitized aliases,
issuer/audience configuration digests and exact binding references rather than
raw subjects/tokens. Claims or `X-Tenant-ID`/identity headers must never grant
roles or select tenant storage. The proxy must forward each individual user's
valid token. Do not use a shared privileged service identity to make browser
tests pass.

**CLI fixture isolation is mandatory.** Ordinary CLI put/get/scan/move commands
do not select named API tenants. Give CLI a separate disposable **default-tenant**
catalog and disjoint POSIX hot/warm roots, with no pilot data/credentials.
Production qualification would require its separately authorized S3 fixture;
this local implementation uses POSIX for both tiers. CLI consistency `--tenant`
is a scope label, not an API catalog-partition selector. Never point these
commands at `pilot-a`/`pilot-b` storage. Named-tenant repair uses registered
scoped admin reports or a reviewed harness explicitly binding tenant catalogs
and drivers, as required by the [pilot workflows](production_pilot.md#required-user-and-operator-workflows).

## Local rehearsal commands

Use a development environment installed according to CONTRIBUTING. In a fresh
temporary directory, the local helper seeds independent named tenants and
starts the loopback-only API/UI:

```bash
COGNISTORE_SECURITY_PROFILE=development PYTHONPATH=. \
  python scripts/manual_acceptance_local.py serve \
  --root /tmp/cognistore-163-local --port 8763
```

Run the client exercise from a second terminal:

```bash
COGNISTORE_SECURITY_PROFILE=development PYTHONPATH=. \
  python scripts/manual_acceptance_local.py exercise \
  --root /tmp/cognistore-163-local --port 8763
```

The helper's private token file must never join the evidence bundle. Its
`/local-session/pilot-a-reader` (or other seeded identity) route is an
**unauthenticated local fixture selector**, not a production login or proxy.
Open that route in an actual browser, then `/ui/` and `/ui/admin/`. Switch to a
fresh session for the other identity. No upstream identity account, credentials
or billing resource is required. The local queue is unavailable; observe and
retain its honest failure state. This helper cannot pass the durable-job,
production proxy or staging-only cases.

For the independent CLI fixture, use explicit fixture paths, inspect CLI help
and capture JSON stdout plus sanitized stderr separately. For example, with a
pre-created fixture bucket and two disjoint POSIX tiers:

```bash
COGNISTORE_SECURITY_PROFILE=development python -m cognistore.cli \
  --base "$CLI_HOT_ROOT" --catalog-db "$CLI_CATALOG" \
  put documents acceptance/bytes.bin "$SEED_FILE" --json
COGNISTORE_SECURITY_PROFILE=development python -m cognistore.cli \
  --base "$CLI_HOT_ROOT" --catalog-db "$CLI_CATALOG" \
  get documents acceptance/bytes.bin "$DOWNLOAD_FILE" --json
COGNISTORE_SECURITY_PROFILE=development python -m cognistore.cli \
  --drivers "$CLI_DRIVERS" --catalog-db "$CLI_CATALOG" \
  catalog-scan hot documents --prefix acceptance/ --sync --json
```

Set the variables to the separate fixture, not the API helper's tenant roots.
Compare source/download bytes and SHA-256. Direct CLI upload alone does not
replace scan verification. Use [durable move commands](cli.md#durable-manual-move-recovery)
with a fixed unique move ID for hot→warm→hot; retain `move-status` transition
history and destination checksums. CLI anonymous job producers cannot submit
protected named-tenant work; use authenticated API/SDK actions there.

API/SDK input examples for a **recorded disposable prefix** are:

```json
{"tier":"hot","bucket":"documents","prefix":"acceptance/run-001/"}
```

for `POST /v1/actions/catalog-scans`, and:

```json
{"bucket":"documents","prefix":"acceptance/run-001/","config":{"policy":"simple","threshold":1,"allowed_tiers":["hot","warm"]}}
```

for `POST /v1/actions/policy-runs`. A nonempty object larger than one byte starts
in hot and should move to warm with this rule. Never substitute an empty prefix
unless the entire disposable bucket is the intended reviewed scope.

The equivalent SDK methods are `submit_catalog_scan(CatalogScanRequest(...))`,
`submit_policy_run(PolicyRunRequest(...))` and `wait_for_job(...)`. Retain the
initial job and terminal response, then inspect bytes/catalog/decisions. Use
`put_object`, `head_object`, `get_object(byte_range="bytes=1-7")`,
`iter_catalog_objects` and `delete_object` for byte cases. API requests use the
[REST endpoints](rest_api.md#endpoint-summary). SDK errors must preserve the
documented status/code/request ID; a caught exception alone is not evidence of
the intended denial.

## Scenario procedure and expected results

Run the specific rows in the JSON matrix. The following steps define each
case's assertions and evidence. Start with clean controls, then introduce a
single negative condition so a broken baseline cannot masquerade as successful
access denial or fault handling.

| ID | Procedure | Expected result / required evidence |
| --- | --- | --- |
| MA-01 | Upload manifest bytes through each interface. HEAD and download immediately, scan, compare catalog/physical placement. For API/SDK, page at limit 1 with unchanged cursors until complete; delete a dedicated unheld key, then read it again. | Full bytes/SHA-256 and size match; no skipped/duplicate pagination records. PUT is 201, DELETE 204, deleted GET/catalog is 404. CLI completes its independent fixture workflow; never label it named-tenant authentication evidence. |
| MA-02 | Request `bytes=1-7`, `bytes=7-`, `bytes=-7`, then malformed, multiple and out-of-bounds ranges, plus a range on the zero-byte object. | Successful ranges are exact slices with 206, correct Content-Range and Content-Length. Invalid/unsatisfiable ranges return 416 and `bytes */SIZE`. Retain full headers and byte comparisons. |
| MA-03 | Scan only the recorded prefix with CLI sync in the legacy fixture and queued API/SDK in each tenant. Inspect private synthetic extraction evidence and public catalog projections separately. | Supported documents match expected normalized text/checksums; unsupported objects do not stop later valid files. Public responses omit internal extraction text/properties. Scope and sibling-tenant controls remain intact. |
| MA-04 | Follow accepted scans/runs through terminal state, restart API/worker with retained state, and reread the same IDs. In a separate controlled fixture induce retry, exhausted failure and one DLQ redrive; revoke a submitting role and tenant grant before worker dispatch. | IDs/history survive restart; success matches effects. Failed/revoked work has no unauthorized effects. Redrive retains original principal and rechecks current grants. Record attempts and DLQ evidence; no looped resubmission after uncertainty. Local no-queue failures do not pass durable-job cases. |
| MA-05 | Snapshot full source/destination bytes, placement and move state. Preview/evaluate simple policy and content policy with required unavailable features. Review reasons, then execute the explicit simple rule over the recorded prefix. Repeat in both directions using a threshold above the known object size for warm→hot. | Previews do not move or create a move. Required unavailable signals fail closed (`stay`/`required_features_unavailable`). Confirmed successful jobs have correct destination SHA-256 and placement, generation-safe source cleanup and retained execution decisions; job success alone is insufficient. |
| MA-06 | In a disposable fixture prepare one eligible nonterminal move and one unexplained duplicate/mismatch. Complete consistency scan, preview repair, explicitly enable only reviewed repair, inspect action counts and perform a new scan. Exercise API/SDK registered report preview/confirmation, stale token and foreign report ID. | `summary.complete` is true; findings are distinct from command success. Preview leaves source state intact. Safe repair resumes the original ID only after checksum/generation checks. Ambiguous/held data is quarantined for review without hidden movement/deletion. Stale/mismatched confirmation fails; no arbitrary path input is accepted. |
| MA-07 | Execute every applicable allow/deny operation in the role table below, both with existing and nonexistent known resource IDs. Inspect evidence as the separate auditor. | Allowed control succeeds; forbidden operation returns generic 403 before lookup and changes no bytes/jobs/placement. Token role claims and edited UI controls do not grant permission. |
| MA-08 | With valid same-role identities, switch tenants and request known peer-only object, job, hold, decision, audit-event and repair IDs; reuse a peer cursor; ask for peer-only metadata/citations. Compare each tenant's deliberately shared logical key. Attempt default-namespace aliases and caller-supplied tenant fields/headers. | No foreign results or mutations. Shared-key reads return only the caller's bytes. Permitted direct foreign-resource lookup is indistinguishable from absent; unauthorized operations still return 403. Foreign cursors must not reveal data (reject or return only the caller's permitted scope). Captured citations/downloads stay tenant-bound. |
| MA-09 | Repeat with missing, expired, wrong-audience, wrong-issuer and bad-signature tokens; valid unmapped identities and removed tenant grants. In an actual qualified browser proxy, test expiry, sign-out, forged forwarding/tenant headers and cross-site mutation with an authenticated session. | Invalid authentication is 401 with a safe challenge; no-default/unmapped access is denied. No leaked state or side effects. Proxy preserves individual actor/tenant, clears expired session data and prevents cross-site mutation. Local selector-cookie behavior cannot qualify this proxy. |
| MA-10 | Place exact, prefix and bucket holds via the distinct admin/hold authority. Attempt overwrite, delete, policy move, CLI move and eligible repair; try release with writer/operator. Read as reader; then explicitly release with authority and reason, retaining history. | Reads remain available; destructive effects are blocked, including for admin and stability overrides. Unauthorized release is 403. Authorized release preserves attributed history and only removes that hold; overlapping active holds continue to protect objects. |
| MA-11 | In a real browser on `/ui/`, use a relevant metadata query, inspect result/citation and download. Search a unique no-match marker, use keyword/vector tabs, and observe a bounded API failure then retry. Repeat in both tenants. | Ask metadata results identify the correct object and download exact bytes. Empty results differ from errors; absent providers are explicit. Search tabs may filter metadata results to empty and must retain unavailable diagnostics. No generated answer is expected. |
| MA-12 | On `/ui/admin/`, connect each single-role identity and inspect available views. As admin preview a scoped scan/policy/repair, cancel the dialog, then repeat and type `CONFIRM`. Inspect terminal jobs/effects, audit decisions/checkpoints, repair counts, empty lists, unavailable dependency and failed-refresh stale state. Disconnect/switch identity. | Cancel causes no submission or mutation. Confirmation shows tenant/bucket/prefix/config; 202 remains queued. Allowed views work; forbidden views/requests stay denied. Errors and stale data are visibly distinguished from success, tokens/previews/data clear on disconnect/expiry, and audit links match actual actions. |
| MA-13 | Through API, SDK and actual browser request metadata, default hybrid, and `synthesize=true` with relevant and no-match queries; inspect configured provider inventory and outbound-call evidence. | Metadata citations/downloads are grounded in permitted objects. Absent requested keyword/vector/generation providers report unavailability; unrequested providers are `not_requested`. No answer or embedding/model call occurs. This proves selected composition behavior only. |
| MA-14 | Exercise all corpus negative formats, exact/over API byte and JSON limits, documented extraction input/output limits, image-only empty text, deterministic timeout and worker-failure injection, followed by a valid document. | Stable per-object failures match [extraction codes](document_extraction.md#stable-failure-codes); source bytes remain intact and unrelated work continues. API object cap is 16 MiB, JSON cap 256 KiB; oversize is 413 with no oversized catalog publication. Reduced extraction-limit fixtures are labeled and do not claim production default-boundary testing. |
| MA-15 | In a separate isolated fixture enable regex PII, scan supported synthetic PII and clean documents; inject extraction/detector failure. Inspect persisted classification and provider inputs. Re-scan a previously indexed fixture and inspect retained old index/source objects. | Text and document properties are withheld on every PII-enabled scan, including failures; classifications have no matched values/offsets. No new text is supplied to content search/embedding/Ask. Unknown/stale required classification fails closed. Old indexes, backups and source bytes are not automatically erased. This is neither a PII-free guarantee nor production PII/search qualification. |
| MA-16 | Review every case and all repetitions. Link each failure to a finding, fix revision and affected rerun. Reconcile manifests/placement/holds/jobs/audit, review retained artifacts, and record the owner decision. | No release-blocking finding or missing required observation is silently accepted. Pending specification/staging/browser/provider prerequisites remain open. A local rehearsal report says incomplete for staging acceptance even when its local assertions pass. |

### Single-role checks for MA-07 and MA-12

Run each row in both tenants with the single-role principal; do not reuse admin
for a single-role positive. Use new disposable keys for permitted writes.

| Role | Positive controls | Required denials |
| --- | --- | --- |
| Reader | Object/catalog read, Ask, own job status, hold inspection | PUT/DELETE, scan, policy preview/run, repair, audit export. |
| Writer | Reader controls plus unheld PUT/DELETE | Scan, policy preview/run, repair, audit export, hold placement/release. |
| Operator | Read, scan, storage view, registered repair preview/execution | PUT/DELETE, policy preview/run without policy_manager, audit export, hold release. |
| Auditor | Retained decisions, audit query/export/verify, hold history | Object download/Ask, job read, upload/delete, scan, policy preview/run, repair. |

Use the [authorization contract](authorization.md#endpoint-and-operation-matrix)
for exact endpoints. Audit export requires complete pagination plus an
independent retained checkpoint. Reader/operator jobs must be compared with
their tenant's known IDs; absent queue support leaves positive job controls
unperformed rather than automatically passing them.

### Provider scope changes

If a later accepted scope requires production providers, revise MA-13 before
running it. Freeze actual provider/model/index/version/configuration and
tenant binding; use a held-out, human-reviewed synthetic query set with relevant,
no-match and deliberately unavailable/timeout cases. Record expected source
IDs, grounded citations, exact downloads, answer support/abstention, latency
and failure clarity for every case, with predefined quality thresholds and
reviewer judgements. Exercise both tenants and unauthorized known-source IDs.
Provider diagnostics and deterministic sample queries alone cannot certify
semantic relevance or generation quality. Retain the old metadata-only report
as history, not a substitute for the new provider campaign.

## Findings, reruns and cleanup

For any byte, tenant, hold or audit failure, stop that fixture's mutable work
and preserve evidence and state. Record a stable finding ID, severity,
reproduction/case/repetition, expected/actual result, source/config/environment,
sanitized artifacts, linked issue/fix, owner and affected rerun set. Do not
edit a failed observation into a pass. Fix release blockers, create a new run
bound to the fix revision, and rerun every affected interface/tenant/role plus
relevant regressions. Keep unresolved findings and unused scope explicit.

Before cleanup, reconcile all submitted jobs and active holds, save manifests,
placement, reports, audit checkpoints and terminal outcomes. Stop the local
server and remove only its inventoried disposable fixture paths after copying
sanitized evidence. Never delete arbitrary buckets, a guessed catalog path,
unreconciled jobs or evidence as part of a retry. Actual cloud cleanup remains
outside the current local-only authorization.
