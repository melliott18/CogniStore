# Retained XML repair and ticket disposition — 2026-09-29

The [closure review](https://github.com/melliott18/CogniStore/issues/162#issuecomment-5768724604)
identified nine malformed JUnit archives. The original retention step inserted
literal `<LOCAL_HOST>` and `<WORKTREE>` placeholders into serialized XML,
including quoted attributes. Their recorded checksums matched the invalid
bytes, so checksum verification alone did not detect the format defect.

All nine archives now parse. The repair replaces only those literal tokens
with XML-escaped text and recompresses with `gzip` timestamp zero. Parsed
hostname/path values still contain the same sanitized placeholders. The
coverage archive was already valid and is unchanged. This maintenance repair
does not rerun or reinterpret the original product tests.

[xml-repair.json](xml-repair.json) records the source revision, every old/new
compressed and decompressed SHA-256, and each placeholder replacement count.
Reverse only the listed escaping to reproduce the original decompressed hash;
the original compressed artifacts remain available at the recorded Git
revision. No test names, timestamps, durations, diagnostics, failure/skip
outcomes, source identities or qualification decisions were changed.

Updated digest references are the full/merge/tooling entries in
`validation.json`, four run entries in `mutation-validation.json`, and each
service profile's decompressed `tests.xml` digest. `SHA256SUMS` covers the
repaired compressed artifacts, updated manifests and this new repair record.
The historical readiness assessment under ticket #166 still identifies the
old source/reviewed README hash; rewriting that historical binding would
misrepresent its evaluated input, so it remains unchanged.

The six expected failures in `scan-before.xml.gz` remain failures. Both service
profiles' XML still records ten skipped entries. The initial profile's
historical runner misclassification (`failed`, nine skips, one unexpected
case) remains recorded; the final profile still says `incomplete`, ten skips.
Neither was converted to a passing result.

## Validation

The new [retained-evidence regression tests](../../../../tests/unit/test_retained_audit_evidence.py)
reproduced all nine parsing failures before repair. They require valid JUnit
with the original suite and individual-case outcomes, consistent compressed
and decompressed digest references, a complete checksum inventory, reversible
placeholder-only changes, and unchanged valid coverage XML. The audit-runner
tests also run; no external service, database fault or blocked audit step is
invoked. Exact commands and results are in
[xml-repair-validation.json](xml-repair-validation.json).

For future retention, sanitize parsed XML attribute/text values and serialize
with an XML library, or XML-escape replacement values before substitution.
Parse the final decompressed bytes before sealing them. Never apply a raw
plain-text path/hostname replacement to XML and assume a matching checksum
establishes a usable report.

## Owner-directed closure and remaining release work

The owner explicitly requested repairing this evidence and closing #162 when
done. The closure records that completed maintenance and the previously
implemented local audit/remediation. It does not assert that every original
production audit criterion passed. The
[2026-09-23 safeguard disposition](https://github.com/melliott18/CogniStore/issues/162#issuecomment-5797914203)
remains an accepted temporary verification exception; the blocked step was
not repeated or rephrased.

The [M5 evidence index](../README.md) and
[pilot entry requirements](../../../production_pilot.md) continue to require
accepted owners, a candidate containing the fixes, exact registry/configuration/
environment identities, disposition of the 69 reported image findings, and
the selected native-backend/live-control audit and final conclusion. Existing
#159/#160/#161/#163/#164/#166 retain their related responsibilities. Those
unperformed checks remain unperformed; the XML repair grants no deployment,
pilot, security or release signoff and does not waive those requirements.

Hosted CI: **skipped: user instruction; known GitHub billing/spending
restriction**. Application/dependency inputs are unchanged, so the full product
suite, dependency audit and image build were not repeated for this evidence-only
repair.
