# Ticket #160 local candidate evidence

**Status: implementation validated locally; release qualification incomplete.**
No release, registry publication, deployment or pilot entry is approved.

The final development bundle was assembled from clean committed source
`e51cb39cc84b819ecbed43b56513888a956812cf` (tree `ab158ab74f7dabf5095352b35ec34c832db92ae6`).
It includes both fixes and the current hosted-CI suspension guidance.
Its candidate ID is `0.1.1rc1-e51cb39cc84b-1f065e90bbcb`; its Docker configuration digest is
`sha256:1f065e90bbcb256970782f16622d931b84e83ffe91d5e00321bd8b8f8bb33a05`. The [manifest](manifest.json)
binds source, wheel/dependency hashes, migrations, selected configuration,
provider/model absence and image archive. This is an unmerged branch candidate;
a registry manifest digest is not yet recorded. Later evidence-only commits do
not change that source claim.

| Check | Result |
| --- | --- |
| Tooling unit tests | 44 passed |
| Installed amd64 image smoke | 48 checks passed; 17 API requests and one actual POSIX policy move |
| #156/#157 installed-wheel regressions | 287 passed; 8 native case-insensitive-volume cases skipped |
| Ruff, pip check, Bandit | Passed |
| Frozen top-level Python dependency audit | No known vulnerabilities reported; includes locked pip 26.2.1 |
| Image scan | 69 reported findings; unresolved, including 3 critical and 14 high |
| Hosted CI | skipped: user instruction; known GitHub billing/spending restriction |

[validation.json](validation.json) gives commands, identities, boundaries and
outstanding gates. [smoke.json](smoke.json), [JUnit](regressions.xml.gz),
[tooling JUnit](tooling-tests.xml.gz), [dependency audit](pip-audit.json) and
[Bandit](bandit.json.gz) retain the underlying local results. Linux amd64 runs
used Docker on macOS arm64; smoke uses synthetic POSIX/SQLite fixtures and an
in-process ASGI transport. It does not establish AWS S3, PostgreSQL, JetStream,
TLS/OIDC, browser, production filesystem, recovery or performance acceptance.

The [security disposition](security-disposition.md) retains final, intermediate
and initial image scan findings without waivers. The six initial pip-specific
findings disappear after freezing/updating both pip copies; four findings in
vendored installer code remain in addition to 65 Debian findings. A passing
top-level dependency audit does not override the image scan. Scanner age and
unrecorded vulnerability-database freshness also remain limitations.

[SHA256SUMS](SHA256SUMS) checks all retained evidence here.
[bundle-SHA256SUMS](bundle-SHA256SUMS) checks the complete generated bundle,
including its image and binary wheels; those large binaries are **not** in this
directory. The local bundle location is recorded in validation.json for this
workspace only. Durable remote binary retention remains an explicit unmet
handoff gate; these reports are not a substitute for storing the full bundle.
The source revision is retained in Git and in the generated source archive.

The [initial manifest](initial-manifest.json), `initial/` reports and failed
first wheel-lock build are retained. The lock generator was corrected to
ignore nested vendored distribution metadata, with a regression test. The
[intermediate manifest](intermediate-manifest.json) records the pip-fixed build
before the supported-matrix hosted-evidence guard correction. Its scan has the
same unresolved 69-finding outcome; counts across runs must not be pooled.

Owner/specification acceptance, actual clean-main freeze, registry identity,
#161 rendered configuration and selected service workflows, required hosted
gates, reviewed security disposition, durable binary retention, and later
#162–#166 production qualification remain outstanding. This evidence does not
close #160. Reproduction and change control are in the
[release guide](../../../release_candidate.md).
