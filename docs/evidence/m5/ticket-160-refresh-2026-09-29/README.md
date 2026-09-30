# Ticket #160 candidate and image refresh — 2026-09-29

**Status: assembled-unqualified.** This refresh freezes current main and retains
new image analysis. #160 was already closed on September 21; the requested
refresh is complete without changing the release qualification decision.
No security exception, deployment or pilot approval follows from ticket closure.

## Candidate identity

The [unaltered assembly manifest](manifest.json) records clean source
`04ee37a499d58695645d739971fadc72ab5af459`, tree
`40f1d9eaa454f434111d5ca3f7619881fb624e69`, equal to `origin/main` at assembly.
Candidate **`0.1.1rc1-04ee37a499d5-6e818b49e7e9`** was built without
`--allow-unmerged`. Its Linux amd64 image configuration digest is
`sha256:6e818b49e7e92735efb278dac5a029e4f299a9def255076e45a7d775f94d143d`;
this is not a registry manifest digest. Its saved image archive SHA-256 is
`cbbd1adcac7da58d4a638d7af8f21dc725394a99c0e1d53f49ccf5c6b7dd2203`.

The build and scan ran on September 29 America/Los_Angeles, September 30 UTC.
Exact UTC timestamps, commands, hashes and limitations are retained in
[validation.json](validation.json) and the underlying reports.
The manifest is the sealed assembly-time record: its pending image-scan line
is supplemented by this assessment, not rewritten after the fact. Later
commits containing evidence do not change the tested source identity.

## Local checks

| Check | Result and retained evidence |
| --- | --- |
| Installed runtime smoke | [48/48 checks](reports/smoke.json); 17 API requests, native libmagic, migration head `0014_legal_holds`, actual POSIX policy move and metadata-only Ask |
| Installed #156/#157 regressions | [287 passed, 8 skipped](reports/regressions.xml.gz); skips require a native case-insensitive volume |
| Later scanner, worker, telemetry and coherent-restore regressions | [179 passed, 12 PostgreSQL cases deselected](supplemental/supplemental-validation.json); all nine selected modules represented |
| Static security check | [Bandit passed](reports/bandit.json.gz) at the configured medium-or-higher severity/confidence threshold |
| Frozen top-level Python dependencies | [No known vulnerabilities reported](reports/pip-audit.json) for 57 distributions; pip check passed |
| Native and vendored image packages | [Findings remain unresolved](image-analysis/security-disposition.md); counts and scope below |
| Hosted CI | skipped: user instruction; known GitHub billing/spending restriction |

Smoke uses the saved runtime image. Regressions use the qualification image
`sha256:cf7a9febb5380fb48f0e4a0ae74cf81c0b3c7fd68de35cd661cee5e071a00ab0`,
which installs the same frozen runtime wheel closure and separate frozen test
tools; imports resolve to installed code. Supplemental tests ran with networking
disabled and synthetic local fixtures. These selected checks are not a full
supported-runtime matrix or production qualification. No live PostgreSQL,
S3, JetStream, TLS/OIDC, browser, production filesystem or load campaign ran.

## Image analysis and changes since the historical candidate

Trivy **0.74.0**, verified against its official release checksum, scanned the
exact saved archive using all severities, including unfixed findings, with no
ignore list. The database was updated at `2026-09-30T01:15:45.849462612Z`,
downloaded immediately before scanning, and unchanged during the offline scan.
[Scanner provenance](image-analysis/trivy-scan-provenance.json) binds its hash,
database hash, image identity, command and timestamps. Exit zero means the scan
completed, not that the image has no findings.

The [raw report](image-analysis/trivy-report.json.gz) contains **286**
package/advisory records: 5 critical, 59 high, 105 medium, 114 low and 3 unknown.
After grouping Debian binary packages by source package/advisory, there are
**133** normalized records: **5 critical, 19 high, 50 medium, 56 low and
3 unknown**. These are also 133 distinct advisory IDs in this report. The
[security disposition](image-analysis/security-disposition.md) retains package
versions, vendor statuses, available fixes and historical comparison; the
[SBOM](image-analysis/trivy-sbom.cdx.json.gz) comes from the same scan inventory.
None is waived. A passing top-level Python audit does not clear bundled
installer libraries or native packages. Five optional vendor-page reads returned
`Failed to fetch restricted URL` and were stopped; the
[access record](image-analysis/vendor-review-access.json) retains that review gap.
Archive-relative tool paths in the disposition refer to the full analysis asset;
large raw JSON files are stored as `.gz` copies in this repository.

All **107 native package/version/architecture rows are unchanged** from the old
candidate, even after refreshing the native build stage without cache. The old
Docker Scout report had 69 advisory IDs; 65 IDs are shared, 68 are newly reported
and 4 old IDs are absent under their previous identifiers. Scanner, database,
identifier and severity differences prevent attributing that change to fixes.
One prior-only CVE is an alias for a still-reported GHSA. Absence is not evidence
of remediation or accepted risk.

[Seventeen top-level Python package changes](dependency-delta.json) include
OpenTelemetry, SQLAlchemy, Starlette and their changed dependency closure.
The current source also includes the later scanner coordination and telemetry
changes. The selected provider profile remains metadata-only, with no production
keyword, vector or answer model and no Azure/embedding extras.
[Configuration hashes](configuration-inventory.json) bind the current pilot
specification, selection and staging templates. The specification bytes changed
since the old candidate although its revision label remains 1; the profile hash
is unchanged. Staging templates still contain placeholders and are not an
accepted deployed environment.

## Retention, reproduction and remaining gates

[retention.json](retention.json) records the authenticated draft GitHub release
and uploaded asset identities, byte counts and SHA-256 verification. The retained
archives together contain the exact saved image, all frozen wheelhouses, source archive,
reports, scanner and database. This supplies remote binary retention for this
refresh; the record is an unpublished draft prerelease, with no registry push.
Keep its assets through at least 90 days after pilot exit and longer while
findings remain unresolved. Hashes identify immutable bytes; GitHub draft asset
storage itself is mutable and must not be silently replaced. Retrieve both
archives and their checksum file with authenticated GitHub CLI access:

```bash
gh release download candidate-0.1.1rc1-20260929-04ee37a \
  --repo melliott18/CogniStore --dir retained-candidate
cd retained-candidate
shasum -a 256 -c SHA256SUMS
tar -xzf cognistore-candidate-0.1.1rc1-04ee37a499d5-6e818b49e7e9.tar.gz
tar -xzf cognistore-analysis-20260929-04ee37a.tar.gz
cd candidate-main
shasum -a 256 -c SHA256SUMS
```

[SHA256SUMS](SHA256SUMS) checks all repository evidence in this directory.
[bundle-SHA256SUMS](bundle-SHA256SUMS) checks the complete original candidate
bundle after retrieving and extracting the remote archive. Raw `.gz` reports
here decompress to the exact bytes retained in that bundle or supplemental
analysis. See the [release guide](../../../release_candidate.md) for rebuild
and change-control rules: a rebuild or dependency/configuration change produces
a new candidate and invalidates affected qualification evidence.

The [September 21 evidence](../ticket-160/README.md) remains byte-for-byte
unchanged, including reports referenced by downstream tickets. This refresh
supersedes its candidate only for current local analysis; it does not retarget
or upgrade historical #162–#166 campaign results.

Remaining gates are explicit: named owner/specification acceptance (#159),
rendered configuration/environment and selected service workflows (#161),
verified registry manifest identity, reviewed security acceptance/remediation
(#162), applicable hosted qualification (#158), and the selected UAT, recovery,
performance and pilot requirements (#163–#166). The existing #162 safeguard
exception is not a blanket vulnerability waiver. While this refresh was in
progress, separate PR #180 repaired #162 XML evidence at main commit
`74d286efeae39dd08f404105c3d0c724e56e7402`; that evidence/test-only change does
not alter this frozen runtime or establish a new production campaign. Candidate
assembly and this refresh are complete; release qualification remains incomplete.
