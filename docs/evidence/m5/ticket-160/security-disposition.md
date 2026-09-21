# Ticket #160 initial image security findings and disposition

**Decision: unresolved release blocker; no accepted exceptions.** This record
preserves the initial local image scan and its findings. It is not a passing
security gate, exploitability conclusion, named owner signoff or authorization
to deploy. A package fix, later image or later scan requires its own source/image
binding and verification; it does not change this initial result.

## Initial scan identity and limitations

The [initial assembly manifest](initial-manifest.json) binds this source and
image to the retained initial candidate artifacts.

- Source: `aeefe370fb46b192076ee11c257242f611c0c39f` (clean local branch build;
  not a qualified main release).
- Local image: `cognistore:rc-aeefe370fb46`, Linux amd64.
- Image ID: `sha256:521ff48ac9d3834f12ab7c7213479a5d5e454fe5fa67f03189433c765c2d5b9d`.
  This is a Docker configuration digest, not a registry manifest digest.
- Approximate invocation time: `2026-09-21T10:59Z`, from the execution record;
  the SARIF itself contains no invocation timestamp or image identity.
- Docker Scout version: `1.0.9`, recorded in SARIF. The retained log advertises
  version `1.24.0` as available; this establishes that the installed scanner was
  behind its advertised release, not that the advertised version was independently
  verified as current.
- Exit code: **2**, with **18 vulnerable package identities and 71 findings**.
  The log reports reuse of a cached SBOM with 219 indexed package entries.
- The SARIF contains no vulnerability-database revision/update time. Neither
  freshness nor complete vulnerability coverage can be established from this
  record. Preserve these findings, then require an up-to-date scanner/database
  record for the exact replacement image before security acceptance.

The recorded command was:

```bash
docker scout cves local://cognistore:rc-aeefe370fb46 \
  --platform linux/amd64 --format sarif \
  --output /Users/mitchell/.codex/artifacts/cognistore-160/native-scan.sarif \
  --exit-code
```

Standard output/error were retained in `native-scan.log`. Analysis below is
based on this saved report; no additional scan, hosted CI request or external
advisory verification was performed to prepare it. The compressed source report
and log below replace dependence on those ignored local paths.

## Severity and package inventory

Counts use each result's referenced rule `properties.cvssV3_severity`, preserving
`UNSPECIFIED` rather than interpreting it as low or safe. There are 71 results,
71 rule records and 71 distinct CVE IDs. The table counts a CVE once, not once
per file location. Scout's Debian identities are source-package groupings and
may correspond to several installed binary packages; all rows except pip have
`pkg:deb/debian/` identities for Debian 12/bookworm. Pip is `pkg:pypi/pip`.

Every row remains **unresolved**. All **65 Debian findings** report
`fixed_version: not fixed`; the six pip findings name fixed versions. That field
means the report does not supply a fixed version for the matched distribution;
it does not prove there is no upstream fix, that a fix is available from a
trusted compatible repository, or that the vulnerability is exploitable here.

| Reported package/version | Critical | High | Medium | Low | Unspecified | Total | Reported fixed version(s) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `apt@2.6.1` | 0 | 0 | 0 | 1 | 0 | 1 | not fixed |
| `coreutils@9.1-1` | 0 | 0 | 0 | 4 | 0 | 4 | not fixed |
| `diffutils@1:3.8-4` | 0 | 0 | 0 | 1 | 0 | 1 | not fixed |
| `gcc-12@12.2.0-14+deb12u1` | 0 | 0 | 0 | 1 | 0 | 1 | not fixed |
| `glibc@2.36-9+deb12u14` | 0 | 0 | 0 | 7 | 0 | 7 | not fixed |
| `gnupg2@2.2.40-1.1+deb12u2` | 0 | 0 | 0 | 1 | 0 | 1 | not fixed |
| `gnutls28@3.7.9-2+deb12u7` | 0 | 0 | 0 | 1 | 0 | 1 | not fixed |
| `krb5@1.20.1-2+deb12u5` | 0 | 0 | 0 | 4 | 0 | 4 | not fixed |
| `libgcrypt20@1.10.1-3+deb12u1` | 0 | 0 | 0 | 2 | 0 | 2 | not fixed |
| `openssl@3.0.20-1~deb12u2` | 1 | 3 | 1 | 2 | 0 | 7 | not fixed |
| `perl@5.36.0-7+deb12u3` | 2 | 3 | 4 | 3 | 3 | 15 | not fixed |
| `shadow@1:4.13+dfsg1-1+deb12u2` | 0 | 0 | 0 | 1 | 0 | 1 | not fixed |
| `sqlite3@3.40.1-2+deb12u2` | 0 | 0 | 0 | 4 | 0 | 4 | not fixed |
| `systemd@252.39-1~deb12u2` | 0 | 0 | 0 | 4 | 0 | 4 | not fixed |
| `tar@1.34+dfsg-1.2+deb12u1` | 0 | 0 | 1 | 1 | 0 | 2 | not fixed |
| `util-linux@2.38.1-5+deb12u3` | 0 | 4 | 1 | 2 | 2 | 9 | not fixed |
| `zlib@1:1.2.13.dfsg-1` | 0 | 1 | 0 | 0 | 0 | 1 | not fixed |
| `pip@25.0.1` | 0 | 0 | 5 | 1 | 0 | 6 | 25.3; 26.0; 26.1; 26.1.2; 26.2.0 |
| **Total** | **3** | **11** | **12** | **40** | **5** | **71** | 6 findings have a reported fixed version |

## Priority remediation and applicability review

These are investigation and remediation candidates derived from the retained
advisory text, not verified installed fixes or accepted risk exceptions.

| Priority/package | Report evidence and next required disposition |
| --- | --- |
| Critical/high OpenSSL | `CVE-2026-75803` describes authentication-tag verification bypass through a particular empty-ciphertext `EVP_Cipher()` path. High `CVE-2026-54874`, `CVE-2026-63072` and `CVE-2026-63076` concern DTLS buffering, CMS decryption and CMP verification respectively. Embedded advisory references identify upstream **OpenSSL 3.0.22** fixes, but Scout reports no fixed bookworm package. Verify a compatible vendor patch/base-image update and actual linked libraries; then rebuild and rescan. No claim is made that normal TLS alone reaches these specific paths. |
| Critical/high Perl | Critical `CVE-2026-12087` concerns a short-source `pack_ip_mreq_source()` heap read; critical `CVE-2026-13221` concerns very large regexp alternations. High `CVE-2026-48962`, `CVE-2026-48959` and `CVE-2026-57432` concern glob evaluation, ZIP processing and pack/unpack respectively. The report lists upstream fixes and newer Debian-series versions, with no fixed bookworm version. Establish installed binary/module presence, vendor applicability and reachable use; no assumed irrelevance or package-removal waiver. |
| High util-linux | `CVE-2026-78409`, `CVE-2026-78410`, `CVE-2026-78408` and `CVE-2026-76642` involve privileged mount/nsenter paths and configuration preconditions. Embedded references name util-linux 2.42.3-1 outside the selected bookworm match. Validate exact binaries, versions, privileges/capabilities and mount policy before assessing reachability. Running the application as non-root alone is not a reviewed exception. |
| High zlib | `CVE-2026-85091` describes non-blocking gzip write buffer handling. Its advisory describes versions 1.3.1.2–1.3.2 while the detected Debian package is 1.2.13. This is a candidate version-range mismatch requiring vendor/source confirmation; keep it unresolved rather than declaring a false positive from prose alone. |
| Pip fixes available in the report | Six findings affect pip 25.0.1. The highest listed fix is **26.2.0** (`CVE-2026-13346`); the others list 25.3, 26.0, 26.1 or 26.1.2. Freeze a reviewed pip artifact and update every affected system/virtualenv copy, not just the application dependencies, then rerun install/inventory/image checks on the new image. The report's package-index/installer findings also require build-environment review. |

There is a second explicit applicability inconsistency: the embedded text for
Perl `CVE-2026-13221` says the issue was introduced in 5.37.10, whereas Scout
matched Debian Perl 5.36.0. Debian patch history and exact module/binary presence
must be checked before accepting a false-positive disposition. Similarly, some
advisory prose/vendor assessments differ from Scout's severity label; this
record retains the scanner label and does not silently downgrade it.

No finding is waived based solely on synthetic data, a non-root user, omitted
model providers, an upstream “minor issue” note, or lack of a reported fix.
The accountable security/release owner is **pending acceptance** under #159.
That owner must record for every finding or justified group: applicability and
reachability evidence, chosen fix or bounded exception, compensating controls,
reviewer/date, expiry/recheck criteria and affected requalification. Currently
there are **no accepted exceptions and no completed remediation** in this
initial scan record. The image remains blocked from security acceptance.

## Complete initial finding catalogue

The following grouping accounts for all 71 retained CVE results. Each item has
the same unresolved disposition above; full locations, descriptions, scores,
package URLs and advisory references remain in the compressed SARIF.

| Reported severity | Reported package/version | CVE IDs |
| --- | --- | --- |
| CRITICAL | `openssl@3.0.20-1~deb12u2` | CVE-2026-75803 |
| CRITICAL | `perl@5.36.0-7+deb12u3` | CVE-2026-12087, CVE-2026-13221 |
| HIGH | `openssl@3.0.20-1~deb12u2` | CVE-2026-54874, CVE-2026-63072, CVE-2026-63076 |
| HIGH | `perl@5.36.0-7+deb12u3` | CVE-2026-48962, CVE-2026-48959, CVE-2026-57432 |
| HIGH | `util-linux@2.38.1-5+deb12u3` | CVE-2026-78409, CVE-2026-78410, CVE-2026-78408, CVE-2026-76642 |
| HIGH | `zlib@1:1.2.13.dfsg-1` | CVE-2026-85091 |
| MEDIUM | `openssl@3.0.20-1~deb12u2` | CVE-2026-63074 |
| MEDIUM | `perl@5.36.0-7+deb12u3` | CVE-2026-19487, CVE-2025-15649, CVE-2026-15534, CVE-2026-7010 |
| MEDIUM | `tar@1.34+dfsg-1.2+deb12u1` | CVE-2025-45582 |
| MEDIUM | `util-linux@2.38.1-5+deb12u3` | CVE-2026-13595 |
| MEDIUM | `pip@25.0.1` | CVE-2026-8643, CVE-2026-3219, CVE-2026-6357, CVE-2026-13346, CVE-2025-8869 |
| LOW | `apt@2.6.1` | CVE-2011-3374 |
| LOW | `coreutils@9.1-1` | CVE-2017-18018, CVE-2025-5278, CVE-2026-56391, CVE-2026-56392 |
| LOW | `diffutils@1:3.8-4` | CVE-2026-53910 |
| LOW | `gcc-12@12.2.0-14+deb12u1` | CVE-2022-27943 |
| LOW | `glibc@2.36-9+deb12u14` | CVE-2010-4756, CVE-2018-20796, CVE-2019-1010022, CVE-2019-1010023, CVE-2019-1010024, CVE-2019-1010025, CVE-2019-9192 |
| LOW | `gnupg2@2.2.40-1.1+deb12u2` | CVE-2022-3219 |
| LOW | `gnutls28@3.7.9-2+deb12u7` | CVE-2011-3389 |
| LOW | `krb5@1.20.1-2+deb12u5` | CVE-2018-5709, CVE-2024-26458, CVE-2024-26461, CVE-2026-11850 |
| LOW | `libgcrypt20@1.10.1-3+deb12u1` | CVE-2018-6829, CVE-2024-2236 |
| LOW | `openssl@3.0.20-1~deb12u2` | CVE-2010-0928, CVE-2025-27587 |
| LOW | `perl@5.36.0-7+deb12u3` | CVE-2011-4116, CVE-2023-31486, CVE-2026-48961 |
| LOW | `shadow@1:4.13+dfsg1-1+deb12u2` | CVE-2007-5686 |
| LOW | `sqlite3@3.40.1-2+deb12u2` | CVE-2021-45346, CVE-2025-29088, CVE-2025-52099, CVE-2025-70873 |
| LOW | `systemd@252.39-1~deb12u2` | CVE-2013-4392, CVE-2023-31437, CVE-2023-31438, CVE-2023-31439 |
| LOW | `tar@1.34+dfsg-1.2+deb12u1` | CVE-2005-2541 |
| LOW | `util-linux@2.38.1-5+deb12u3` | CVE-2022-0563, CVE-2025-14104 |
| LOW | `pip@25.0.1` | CVE-2026-1703 |
| UNSPECIFIED | `perl@5.36.0-7+deb12u3` | CVE-2026-57433, CVE-2026-7017, CVE-2026-82560 |
| UNSPECIFIED | `util-linux@2.38.1-5+deb12u3` | CVE-2026-53613, CVE-2026-53615 |

## Retained artifacts

The files below are gzip-compressed byte-for-byte copies, with deterministic
gzip timestamps. Their decompressed SHA-256 identities are:

- SARIF: `2425398b2f310dcb37e7ca885fe7ec45d2284f126fe360878550492fa43ced77`.
- Log: `3f18473b8e55d31ad4ad179a04728a53a6fbb9b80692ed5e6ddd6fdf70591b0e`.

| Compressed artifact | SHA-256 |
| --- | --- |
| [initial-native-scan.sarif.gz](initial-native-scan.sarif.gz) | `06e99f41b02ac6524e863aa216dde4bf0b76fbfeabff7bfae396a0d96477a98a` |
| [initial-native-scan.log.gz](initial-native-scan.log.gz) | `da9d20e26e61539394c9d740f8547648174bcade18fa15329e66c53d5e2ece6c` |

A later scan should be appended with its own image/source/scanner identities,
counts and disposition. Mark this record superseded for candidate selection
only after retaining the replacement evidence; never delete the initial
failure or apply its findings to a different image without verification.
