# Ticket #160 frozen-candidate qualification — 2026-10-02

**Release qualification remains blocked. #160 is open.** This campaign rejects
the previous image on a reproduced upload defect, freezes a successor from
CI-qualified main, and tests the successor's actual installed runtime. Passing
source CI or assembly does not approve the image's security findings or the
selected production environment.

## Artifact identities

| Candidate | Exact source and runtime identity | Disposition |
| --- | --- | --- |
| September 29 | Source `04ee37a499d58695645d739971fadc72ab5af459`; image `sha256:6e818b49e7e92735efb278dac5a029e4f299a9def255076e45a7d775f94d143d` | Not promotable: three newly applied HTTP upload regressions failed. Seven controls passed; ten optional protocol cases were deselected. |
| Successor `0.1.1rc1-b7e2b3f6fb3f-62cc2900a109` | Source `b7e2b3f6fb3f3fefe4b0e9d2f155204ee2bb71b6`; tree `2744aaa0ceb2a219db479f6f86e0a993179e33ba`; image `sha256:62cc2900a1091daa005ae12d70f4e15884f4b04392403ddaaa06e21cdd0e50b6` | Assembly and recorded installed tests pass; release/security/environment gates remain outstanding. |

Both image identities are Docker configuration digests, not registry manifest
digests. The [successor manifest](manifest.json) records clean source equal to
`origin/main` at assembly, frozen wheel/lock hashes, migrations and the selected
metadata-only profile. Hosted [assembly run 37016575224](https://github.com/melliott18/CogniStore/actions/runs/37016575224)
passed on that exact source. No `--allow-unmerged` override or local rebuild was
used for this successor. Its saved image was downloaded, verified and loaded by
configuration digest. Later evidence commits do not relabel the tested source.

## Qualification evidence

| Check | Result and evidence |
| --- | --- |
| Frozen old candidate against newer upload regression | [3 failed, 7 passed](existing-candidate/upload-regressions.xml.gz): one truncated HTTP error response and two premature closes during paced uploads. [Command and test-source hash](existing-candidate/validation.json) bind the newer test module to the old installed runtime. |
| Successor installed smoke | [48 checks passed](assembly-reports/smoke.json), including native libmagic, migration head, metadata-only Ask and actual POSIX policy movement. |
| Successor original namespace/legal-hold and PUT/DELETE regressions | [287 passed, 8 skipped](assembly-reports/regressions.xml.gz); native case-insensitive storage was unavailable. |
| Upload and worker regressions in successor runtime | [49 passed, 10 deselected](installed-checks/fix-regressions.xml.gz); all ten h11 upload cases pass. Optional httptools cases are excluded. |
| Entire integration suite against successor runtime and isolated selected services | [386 passed, 35 skipped](installed-checks/integration.xml.gz). Real PostgreSQL/pgvector 16, NATS JetStream and MinIO fixtures; no production/cloud resources. |
| Fresh source-level hosted campaign | [Run identities and reviewed results](hosted-review.json): All 15 required jobs passed. CI, Kubernetes and Terraform target the exact successor source; these environments resolve dependencies independently of the frozen runtime. |
| Frozen Python dependency audit, pip check and Bandit | Passed; [dependency report](assembly-reports/pip-audit.json) and [Bandit report](assembly-reports/bandit.json.gz) retain their scope. |
| Image/native/vendored packages | [139 normalized advisory records](image-analysis/security-disposition.md), including 5 critical and 22 high; unresolved. |

Suites overlap and must not be summed. [Local report summary](local-test-summary.json)
retains exact counts and skip reasons. The integration skips cover absent Azure
extras, GCS emulator/live bucket, optional httptools and unsupported range writes.
The selected profile excludes Azure/GCS and production semantic providers; sample
search tests with fixture providers do not qualify a production Ask integration.

Hosted Python 3.10–3.14 each passed 6,341 unit/conformance cases (11 skipped)
and 412 service integrations (62 skipped), with 88.63–88.64% combined coverage
against the unchanged 80% gate. The default suite passed 6,519 cases with
302 skips; GCS emulator passed 20 with two skips and one strict expected failure.
Kubernetes used a disposable development kind cluster; Terraform passed six
mocked plan cases. [Reviewed hosted results](hosted-review.json) preserve every
run, job and reported skip reason.

The local harness derives from the retained runtime image and installs frozen
pytest tools in `/opt/test-tools`. Imports explicitly resolve to the original
`/opt/cognistore` installation. [Identity verification](installed-checks/integration-identity.json)
confirms every runtime distribution's frozen version and the module hash from
the hosted smoke report. No application source checkout is on the import path.
The first integration attempt [failed during collection](installed-checks/integration-attempt-1/integration.xml.gz)
because a test helper required an absent OpenAPI validator. The repair added
separately [hash-locked test tools](extra-test-tools.lock); runtime identity was
reverified before the successful rerun. The failed attempt is retained.

[Service identities](installed-checks/services.json) record native arm64 service
fixtures with an emulated amd64 candidate on Docker/macOS. The network was
internal, no ports were published, and data was temporary. The fixtures and
network were removed after completion. Docker service-log capture retained only
stdout, not stderr; complete pytest logs/JUnit and image identities are retained.
This is installed-client/service integration evidence, not #161 production
TLS/OIDC, secret delivery, storage encryption, network policy or alert delivery.

## Security assessment and release blockers

Trivy 0.75.0 scanned the saved image using the database updated
`2026-10-02T12:48:00.080865328Z`. [Provenance](image-analysis/trivy-scan-provenance.json)
binds the scanner, database, image archive, commands and report hashes. The
[raw report](image-analysis/trivy-report.json.gz) has 296 binary-package/advisory
occurrences; [normalized findings](image-analysis/security-summary.json.gz) have
139 source-package/advisory records: 5 critical, 22 high, 57 medium, 53 low and
2 unknown. The [CycloneDX SBOM](image-analysis/trivy-sbom.cdx.json.gz) comes from
the same inventory. Scan exit zero means execution succeeded, not a clean image.

All 107 native package rows match the September 29 inventory. Six top-level
Python versions changed, recorded in [dependency-delta.json](dependency-delta.json).
Scanner/database changes prevent attributing finding-count differences to fixes.
The [disposition](image-analysis/security-disposition.md) adds evidence about
Perl architecture, absent Archive::Tar and zlib's installed files, while leaving
raw findings intact. No named security exception or blanket waiver is approved.
Large report names mentioned there refer to uncompressed files in the full
analysis archive; repository copies use `.gz`.

The remaining release gates are concrete:

- Remediate applicable image findings or obtain reviewed, bounded dispositions
  with named owner, scope, justification and expiry. A fix that changes bytes
  requires a new candidate and affected reruns.
- Record the accepted #159 specification and named operational/security owners.
- Supply #161's actual isolated environment and rendered configuration, verify
  selected service/security workflows and bind their evidence to this image.
- Transfer the image to the selected registry and verify its immutable manifest
  digest; a local Docker configuration digest is insufficient.
- Complete applicable target-environment UAT, recovery, load and pilot gates.

[The retained evidence from September 29](../ticket-160-refresh-2026-09-29/README.md)
and earlier source campaigns keep their original outcomes. Historical hosted
skips remain skipped. Current public-repository hosted execution is now allowed
and this campaign records actual fresh outcomes; it grants no production or
pilot approval.

## Retention and change control

[retention.json](retention.json) identifies the authenticated draft release,
full candidate and qualification assets, remote asset IDs and verified digests.
The [secret-review record](expanded-secret-review.json) classifies every finding
in raw reports and decompressed logs as verified source/image provenance, a
public signing-key fingerprint or the exact loopback Azurite fixture value. The new manifest digest and two copies of the public signing fingerprint have
[exact reviewed Gitleaks fingerprints](repository-secret-review.json); no rule
or file exclusion is introduced.

The binary candidate, reports and scanner/database are retained remotely; local
ignored paths and expiring Actions artifacts are not the sole evidence.
Keep the assets through at least 90 days after pilot exit and longer for
unresolved findings. Draft storage is mutable; recorded hashes identify the
bytes and assets must not be silently replaced.

Verify this directory with `shasum -a 256 -c SHA256SUMS`.
[bundle-SHA256SUMS](bundle-SHA256SUMS) verifies the original candidate after
extracting its archive. The qualification archive has its own file inventory.
See the [release guide](../../../release_candidate.md) for reuse and invalidation
rules. This evidence update does not rebuild or modify either frozen candidate.
