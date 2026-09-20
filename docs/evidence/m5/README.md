# M5 release-readiness evidence

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

## Native negative control

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

## Reproduction and verification

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
