# M5 release readiness and pilot evidence

M5 [#155](https://github.com/melliott18/CogniStore/issues/155) qualifies one
selected deployment after completed M1–M4 delivery. The versioned
[deployment/workload specification](../../production_pilot.md) selects the
scope and thresholds. This index is a handoff contract, **not passing evidence**.
No qualified release candidate, environment or pilot approval has been recorded.
Provisional local #160 assembly evidence is retained below. Historical
[M4 evidence](../m4/README.md) remains intact.

| Evidence owner ticket | Required durable record | Current state |
| --- | --- | --- |
| [#159](https://github.com/melliott18/CogniStore/issues/159) | Exact specification revision/commit, named owner acceptance and dated review decision | Proposed specification; acceptance pending |
| [#156](https://github.com/melliott18/CogniStore/issues/156) | Fix commit and namespace isolation/legal-hold regression reports | [Recorded below](#ticket-156-reserved-storage-namespace-aliases); local results and limitations retained |
| [#157](https://github.com/melliott18/CogniStore/issues/157) | Fix commit and overlapping DELETE/PUT regression reports | [Local fix and merge regressions retained](157-put-delete-ordering.md); hosted qualification remains separate |
| [#158](https://github.com/melliott18/CogniStore/issues/158) | Hosted runtime matrix on the exact candidate | [Hosted runs blocked before execution](../ci-158/README.md); qualification incomplete |
| [#160](https://github.com/melliott18/CogniStore/issues/160) | Immutable source/image/dependency/provider/configuration manifest and exact-artifact checks | [Assembly procedure and open gates](#ticket-160-release-candidate-assembly); no qualified candidate recorded |
| [#161](https://github.com/melliott18/CogniStore/issues/161) | Isolated environment identity, production controls, versioned configuration, storage/memory/queue bounds and private telemetry | [Preparation and local checks](ticket-161/README.md); actual deployment and all live gates pending |
| [#162](https://github.com/melliott18/CogniStore/issues/162) | Candidate/environment-bound security and consistency audit, residual risks and signed decision | Not recorded |
| [#163](https://github.com/melliott18/CogniStore/issues/163) | Per-interface/role/tenant manual acceptance matrix, actual browser evidence and corpus manifests | [Local acceptance kit and rehearsal](ticket-163/README.md); selected staging qualification remains pending |
| [#164](https://github.com/melliott18/CogniStore/issues/164) | Repeated coherent restore/fault/rotation/rollback reports, recovery-set IDs, RPO/RTO and integrity results | Not recorded |
| [#165](https://github.com/melliott18/CogniStore/issues/165) | Soak/burst/capacity reports, raw measurements, denominator coverage and real alert receipt/recovery evidence | Not recorded |
| [#166](https://github.com/melliott18/CogniStore/issues/166) | Named entry decision, daily pilot records, user tasks and explicit expand/fix/stop decision | Not recorded |

Each record must include:

- Record ID, accountable reviewer/operator, UTC timestamps and pass/fail/
  inconclusive decision; link the exact specification commit and target IDs.
- Candidate source SHA, image digest, Python/native dependency manifest and
  hashes of rendered Helm, drivers, policy and provider configuration. Record
  secret **version references**, never values, in restricted evidence.
- Environment ID, topology, OS/kernel/filesystem/mount semantics, service/image
  versions, resource limits, queue/retry configuration and encryption evidence.
- Corpus generator revision/seed and checksum manifest; size/MIME distribution,
  tenant/role cohort, offered load, concurrency, duration and eligible sample
  counts. Keep fault/negative/overload cohorts separate and explain exclusions.
- Reproducible commands, expected/actual outcomes, raw sanitized logs/metrics,
  numerators/denominators, quantiles, coverage gaps, screenshots where required,
  and SHA-256 checksums of every retained artifact.
- Findings, explicit skips and their impact, linked fixes, affected reruns and
  reviewer decisions. No empty denominator or missing evidence is a pass.

Retain artifacts in this directory or durable access-controlled CI/artifact
storage and add immutable links plus checksums to this index. Preserve failed
and superseded runs with their disposition. Check accessibility and retention
before acceptance; a local `/tmp` path, ignored `.artifacts` directory or expiring
CI link by itself is insufficient. Retain qualification and pilot evidence for
at least 90 days after the exit decision, extending retention for unresolved
findings. Do not commit credentials, bearer tokens or customer/personal content.

Owner acceptance of the specification, technical review, qualification signoff
and pilot entry approval are separate decisions. Record each explicitly; this
table never advances automatically from ticket closure or a green unit test.

## Specification authoring review — 2026-09-20

`m5-pilot-v1`, revision 1, was checked against application source at
`2cce6ff4d43fd287ad008197f1e17f168c3580c4`. An independent Codex agent reviewed
runtime boundaries and numerical consistency; the resulting revision addresses
S3 tier aliasing, the upload limit and memory budget, browser authentication,
CLI tenant boundaries, corpus arithmetic, balanced load, and distinct
qualification/pilot measurement populations. No actionable review findings
remained. This is an automated technical review, not the pending named owner's
acceptance or a production security signoff.

Local authoring validation passed:

- All nine `tests/unit/test_ticket_mirror_sync.py` tests, using
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p no:capture
  tests/unit/test_ticket_mirror_sync.py`. The existing Python 3.12 environment
  required capture/plugin isolation to avoid its native-readline startup crash;
  it also emitted a missing-Starlette warning-filter warning.
- `python scripts/sync_ticket_mirror.py --check`: mirror matches 80 live issues.
- Local Markdown target/anchor validation for the specification, roadmap and
  this index: 31 links resolved; corpus byte arithmetic independently checked.
- Whitespace checks for all changed/new Markdown files.

These documentation checks do not execute a release candidate or establish any
of the production gates above. The Git commit containing this review versions
the reviewed specification and documentation together.

## Ticket #156: reserved storage namespace aliases

[Ticket #156](https://github.com/melliott18/CogniStore/issues/156) fixes default-tenant
access to another tenant's physical objects through differently spelled reserved
directories on case-insensitive storage. The qualified fix revision is
`d8b85a83519d03d7571794c68c6bc0344b4c4dd2`, based on
`2cce6ff4d43fd287ad008197f1e17f168c3580c4`.

The shared boundary now reserves casefold/default-ignorable aliases without
rewriting ordinary names, rejects ambiguous path components, hides reserved
listing entries, and protects the POSIX staging bucket. API namespace errors
return a generic 422 validation response. The intentional compatibility change
and supported filesystem comparison contract are documented in
[tenant ownership](../../tenancy.md#persistence-and-namespaces) and
[POSIX containment](../../posix_containment.md#reserved-filename-comparisons).

[validation.json](ticket-156/validation.json) records commands, source hashes,
test totals, coverage, environment and limitations. The retained
[full-suite results](ticket-156/default.xml.gz),
[regression results](ticket-156/regression.xml.gz), and
[quality report](ticket-156/quality.json) identify the tested scope.

The full invocation recorded **5,453 passed, 291 skipped, and six failures**,
with 86.90% combined statement/branch coverage. All six failures were in
document-extraction tests using a two-second worker deadline; several explicitly
returned `timeout` instead of the expected parser outcome. The complete
**unchanged 34-test extraction module passed** on a focused rerun. Neither that
module nor the extraction implementation differs from the base revision. This
is consistent with timing sensitivity under host load, not proof of its cause.
The [rerun results](ticket-156/extraction-rerun.xml.gz) and
[rerun log](ticket-156/extraction-rerun.log.gz) preserve that distinction.
After replacing the six failed outcomes with their unchanged rerun results,
5,459 distinct tests pass and 291 remain skipped; combined coverage is 86.91%.
This is not a single completely green full-suite invocation, and the overlapping
run counts must not be added together. All 267 new namespace regressions passed
in both their focused run and the full invocation.

| Acceptance criterion | Implementation and regression evidence |
| --- | --- |
| Reserved namespace handling | `drivers/namespaces.py` pins Unicode 17 default ignorables and folds case; tenant and POSIX guards apply that comparison before delegation/traversal. Documentation explicitly excludes arbitrary filename translation, trimming, and short-name aliases. |
| Authenticated held-object attack | Sixteen JWT cases cover memory/SQLite catalogs and deterministic/native case-insensitive POSIX. Victim overwrite is denied by its hold; attacker aliases are rejected before raw backend calls; victim bytes, generation, catalog record and hold remain unchanged after PUT/GET/HEAD/DELETE attempts. |
| Every shared entry path and bucket boundary | 251 driver cases cover byte/range/stream writes, readers including conditional/lazy readers, stat, generation, durability, ordinary/conditional delete, list/page prefixes, cursor filtering, tenant buckets and POSIX staging buckets. |
| Ordinary-key compatibility | Case, Unicode spelling, nested reserved-looking components and near-matches retain their original coordinates. Cloud wrapper controls preserve literal repeated/trailing slashes. Canonical tenant prefixes are unchanged. |
| Existing regressions | The complete default suite includes tenant, legal-hold, POSIX containment and storage conformance tests. Skipped service/live-cloud cases are retained and are not counted as passes. |
| Fix revision and release evidence | The revision above includes implementation, regression tests and compatibility documentation. This evidence-only follow-up records its exact source hashes and reports. |

### Native negative control

The [negative control](ticket-156/negative-control.log.gz) loads only the original
tenant-wrapper module from the base revision into a separate Python process,
then runs the new authenticated native case-insensitive regression. The
attacker's PUT returns **201 instead of 422**, so the regression fails as
expected. Worktree source files are unchanged by this control. Its
[JUnit report](ticket-156/negative-control.xml.gz) is expected failing evidence,
not a failure of the fixed revision.

The actual native temporary volume resolved uppercase/mixed-case paths to the
victim's physical file. HFS+/ext4 ignorable behavior is covered by a deterministic
POSIX lookup fixture and conservative entry-point checks; no native HFS+/ext4 or
live-cloud exploitation is claimed. The fixture independently defines the
Unicode comparison data. Full-table review confirmed all 4,174 pinned Unicode
default-ignorable codepoints against the published source.

### Reproduction and verification

With development dependencies installed, run from the qualified checkout:

```bash
python -m pytest tests/unit/test_tenant_namespace_aliases.py tests/unit/test_tenant_namespace_api.py
python -m pytest --cov=cognistore --cov-report=term --cov-report=xml
python -m ruff check .
python -m mypy cognistore
python -m cognistore.api.openapi --check docs/openapi/v1.json
python -m bandit -q -c pyproject.toml -r cognistore -ll -ii
python -m pip_audit . --strict --desc --progress-spinner=off
python -m build
python -m twine check dist/*
```

Native case-insensitive cases explicitly skip if the temporary volume does not
resolve ASCII case aliases; the deterministic cases run on either kind of
volume. All native cases ran in this captured macOS/CPython 3.13 campaign.
External-service skips and the existing resource/dependency warnings are
retained in the full log. This record qualifies the scoped fix, not the other
M5 release gates or a new live-service/platform matrix.

From `docs/evidence/m5/ticket-156`, verify the retained reports with
`shasum -a 256 -c SHA256SUMS`.

## Ticket #160: release candidate assembly

[Local candidate evidence](ticket-160/README.md) records the exact clean branch
source, frozen artifacts and findings: 44 tooling tests, 48 installed smoke
checks and 287 regressions passed, with eight explained filesystem skips.
Bandit and the top-level dependency audit passed. The image scan retains 69
unresolved findings, including critical/high findings; no exception is approved.
The candidate remains `assembled-unqualified`, and #160 remains open.

The [release candidate guide](../../release_candidate.md) defines the frozen
bundle, exact-artifact reuse, selected metadata-only provider composition and
change-control rules. The assembly tooling is intended for package series
`0.1.1rc1`, on Linux amd64 / CPython 3.12 / Debian bookworm with native
libmagic. The selected `main` base
`bbfb5efb63b09693fab2158514716aaa1a146d19` contains both #156/#157 fixes; that
base and their historical regression reports do not qualify a new candidate.

Record the final clean-main source SHA/tree, wheel and binary dependency lock
hashes, installed Python/native inventory, migration hashes, sanitized profile
and deployment configuration hashes, explicit provider/model absences, saved
image checksum and immutable image identity together. A local Docker image ID
is a configuration digest, not a registry manifest digest; record the latter
and its verified association before claiming the image-digest acceptance gate.
Retain exact-artifact smoke/regression/security reports with the same identity,
plus applicable hosted check links and documented finding dispositions.

**Release qualification remains incomplete.** The named owner has not accepted
the pilot specification, #158 records a hosted account billing/spending-limit
block, and no accepted environment/configuration or passing candidate-specific
hosted campaign is recorded here. Local tooling tests or an unmerged branch
build are development validation only. No deployment, publication, customer
traffic, pilot entry, or completed #160 acceptance is asserted.

Current user direction prohibits further hosted CI dispatches, reruns and
polling while the billing restriction is unresolved; local work continues.
The release workflow is manual-only for future explicitly authorized use.
Hosted gates remain unavailable/unmet, and this tooling change records no new
hosted execution or passing hosted evidence.
