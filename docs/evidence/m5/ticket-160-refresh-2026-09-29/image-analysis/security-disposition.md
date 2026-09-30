# Refreshed candidate image security disposition

Local analysis on September 29, 2026 (America/Los_Angeles; recorded UTC timestamps fall on September 30). **Scan completed; findings remain an unresolved release blocker. No findings are suppressed, waived, or accepted as exceptions.** Administrative closure of ticket #160 does not qualify this image or authorize deployment.

## Exact artifact and scanner evidence

- Source: `04ee37a499d58695645d739971fadc72ab5af459`.
- Image configuration identity: `sha256:6e818b49e7e92735efb278dac5a029e4f299a9def255076e45a7d775f94d143d`; Linux amd64.
- Retained Docker archive SHA-256: `cbbd1adcac7da58d4a638d7af8f21dc725394a99c0e1d53f49ccf5c6b7dd2203`.
- Scanner: **Trivy 0.74.0**, the latest official release returned by the release API at preparation time; released August 14, 2026. The downloaded macOS ARM64 archive matched the official checksum list and GitHub release-asset digest. The binary, original archive, checksum list, release metadata, and signature bundle are retained under `../tools/`; signature verification was not performed. [Official release](https://github.com/aquasecurity/trivy/releases/tag/v0.74.0), [official installation guidance](https://trivy.dev/docs/latest/getting-started/installation/).
- Vulnerability database schema `2` from `ghcr.io/aquasecurity/trivy-db:2`: updated `2026-09-30T01:15:45.849462612Z`, downloaded `2026-09-30T02:42:05.58934Z`, next update `2026-10-01T01:15:45.849462202Z`.
- Database SHA-256: `dd0b4e5115b6e70e010bd6eeb3e746ef47ce77507602c829ff164658b3b68c53`. Content and metadata were unchanged across the offline scan. The exact database cache is retained under `../tools/trivy-cache/`. [Official database guidance](https://trivy.dev/docs/latest/configuration/db/).
- Scan window: `2026-09-30T02:44:53.917426+00:00` through `2026-09-30T02:44:54.662602+00:00`. Scan exit `0`; CycloneDX conversion exit `0`. Findings exit code was deliberately configured as zero; zero confirms execution, not a clean image.
- `trivy-scan-provenance.json` retains complete argument arrays, timestamps, archive/source/image binding, database metadata and digests, scanner provenance digest, and result hashes. `trivy-report.json` is the unsuppressed raw result; `trivy-sbom.cdx.json` is generated from the same report inventory, without a second image scan.

## Findings and counting rules

Raw Trivy findings are package/advisory occurrences, so a Debian source-package advisory can appear on several installed binary packages. The normalized count deduplicates by **ecosystem, Debian source package (or Python package name), source package version, and advisory ID**. It does not merge different CVE/GHSA identifiers automatically and is not a count of independently exploitable flaws.

| Severity | Raw occurrences | Normalized source-package/advisory pairs |
| --- | ---: | ---: |
| CRITICAL | 5 | 5 |
| HIGH | 59 | 19 |
| MEDIUM | 105 | 50 |
| LOW | 114 | 56 |
| UNKNOWN | 3 | 3 |
| Total | 286 | 133 |

There are 133 distinct advisory identifiers and 54 affected binary/Python package names. Advisory data sources are Debian Security Tracker (283 raw occurrences) and GitHub Security Advisories (3). The image inventory includes 107 Debian packages and 95 Python package records; Python records include duplicate installation locations and embedded vendored inventories.

Raw statuses: 213 `affected`, 43 `fix_deferred`, 14 `will_not_fix`, and 16 `fixed`. In this report, `fixed` means an upstream/package fix version is available while the installed version still matches the advisory; it does **not** mean this image has been fixed. A missing fixed version, a vendor deferral, or a vendor decision not to fix does not constitute an accepted exception.

## Comparison with the previous Scout report

The committed prior summary used Docker Scout 1.0.9 against source `e51cb39cc84b819ecbed43b56513888a956812cf`, image `sha256:1f065e90bbcb256970782f16622d931b84e83ffe91d5e00321bd8b8f8bb33a05`. It reported 69 identifiers: 3 critical, 14 high, 8 medium, 39 low, and 5 unspecified. Its vulnerability database freshness was not recorded.

**All 107 native package/version/architecture inventory rows are identical between the prior and current images.** The increased native finding count therefore cannot be attributed to native package changes or interpreted as regression/remediation from an OS upgrade. Scanner coverage, binary-versus-source package grouping, advisory freshness, and severity selection differ. Runtime Python dependency changes are recorded separately by the parent candidate analysis.

The normalized comparison has 65 shared source-package/advisory pairs, 68 currently reported pairs absent from the prior summary, and four prior-only identifiers:

| Prior-only identifier | Package | Interpretation |
| --- | --- | --- |
| CVE-2010-0928 | OpenSSL | Not reported by current scanner; installed native version unchanged; no remediation claim. |
| CVE-2025-52099 | SQLite | Not reported by current scanner; installed native version unchanged; no remediation claim. |
| CVE-2025-45582 | tar | Not reported by current scanner; installed native version unchanged; no remediation claim. |
| CVE-2026-57585 | msgpack | Its GHSA alias remains HIGH; this is identifier grouping, not remediation. |

The [upstream MessagePack advisory](https://github.com/msgpack/msgpack-python/security/advisories/GHSA-6v7p-g79w-8964) explicitly associates CVE-2026-57585 with GHSA-6v7p-g79w-8964. Trivy retains the GHSA at installed version 1.1.2 with reported fixed version 1.2.1.

OpenSSL illustrates classification changes: installed 3.0.20-1~deb12u2 is unchanged, while CVE-2026-75803 moves from prior CRITICAL to current LOW and now has scanner-reported fixed version 3.0.22-1~deb12u1. Several other OpenSSL severities also differ. Those changes do not fix the installed libraries. All 14 shared severity/fixed-version differences are retained in `trivy-security-summary.json`.

## Priority triage

All normalized critical and high findings are listed below. “Not supplied” means Trivy did not identify a fixed version for the installed distribution; it is not a statement that no fix can exist elsewhere. Debian links identify the relevant vendor tracker; only the vendor references discussed under applicability were separately read during this run.

| Source/Python package and installed version | Advisory | Severity | Scanner status | Scanner-reported fixed version |
| --- | --- | --- | --- | --- |
| perl `5.36.0-7+deb12u3` | [CVE-2026-13221](https://security-tracker.debian.org/tracker/CVE-2026-13221) | CRITICAL | `affected` | Not supplied |
| perl `5.36.0-7+deb12u3` | [CVE-2026-42496](https://security-tracker.debian.org/tracker/CVE-2026-42496) | CRITICAL | `fix_deferred` | Not supplied |
| perl `5.36.0-7+deb12u3` | [CVE-2026-8376](https://security-tracker.debian.org/tracker/CVE-2026-8376) | CRITICAL | `affected` | Not supplied |
| sqlite3 `3.40.1-2+deb12u2` | [CVE-2025-7458](https://security-tracker.debian.org/tracker/CVE-2025-7458) | CRITICAL | `affected` | Not supplied |
| zlib `1:1.2.13.dfsg-1` | [CVE-2023-45853](https://security-tracker.debian.org/tracker/CVE-2023-45853) | CRITICAL | `will_not_fix` | Not supplied |
| acl `2.3.1-3` | [CVE-2026-54369](https://security-tracker.debian.org/tracker/CVE-2026-54369) | HIGH | `fix_deferred` | Not supplied |
| gzip `1.12-1` | [CVE-2026-41992](https://security-tracker.debian.org/tracker/CVE-2026-41992) | HIGH | `fix_deferred` | Not supplied |
| msgpack `1.1.2` | [GHSA-6v7p-g79w-8964](https://github.com/advisories/GHSA-6v7p-g79w-8964) | HIGH | `fixed` | 1.2.1 |
| ncurses `6.4-4` | [CVE-2025-69720](https://security-tracker.debian.org/tracker/CVE-2025-69720) | HIGH | `affected` | Not supplied |
| openssl `3.0.20-1~deb12u2` | [CVE-2026-84782](https://security-tracker.debian.org/tracker/CVE-2026-84782) | HIGH | `affected` | Not supplied |
| perl `5.36.0-7+deb12u3` | [CVE-2026-42497](https://security-tracker.debian.org/tracker/CVE-2026-42497) | HIGH | `fix_deferred` | Not supplied |
| perl `5.36.0-7+deb12u3` | [CVE-2026-48962](https://security-tracker.debian.org/tracker/CVE-2026-48962) | HIGH | `affected` | Not supplied |
| perl `5.36.0-7+deb12u3` | [CVE-2026-57432](https://security-tracker.debian.org/tracker/CVE-2026-57432) | HIGH | `affected` | Not supplied |
| perl `5.36.0-7+deb12u3` | [CVE-2026-57433](https://security-tracker.debian.org/tracker/CVE-2026-57433) | HIGH | `affected` | Not supplied |
| perl `5.36.0-7+deb12u3` | [CVE-2026-9538](https://security-tracker.debian.org/tracker/CVE-2026-9538) | HIGH | `fix_deferred` | Not supplied |
| setuptools `70.3.0` | [CVE-2025-47273](https://avd.aquasec.com/nvd/cve-2025-47273) | HIGH | `fixed` | 78.1.1 |
| sqlite3 `3.40.1-2+deb12u2` | [CVE-2026-11822](https://security-tracker.debian.org/tracker/CVE-2026-11822) | HIGH | `fix_deferred` | Not supplied |
| sqlite3 `3.40.1-2+deb12u2` | [CVE-2026-11824](https://security-tracker.debian.org/tracker/CVE-2026-11824) | HIGH | `fix_deferred` | Not supplied |
| systemd `252.39-1~deb12u2` | [CVE-2026-16742](https://security-tracker.debian.org/tracker/CVE-2026-16742) | HIGH | `fix_deferred` | Not supplied |
| util-linux `2.38.1-5+deb12u3` | [CVE-2026-53613](https://security-tracker.debian.org/tracker/CVE-2026-53613) | HIGH | `affected` | Not supplied |
| util-linux `2.38.1-5+deb12u3` | [CVE-2026-76642](https://security-tracker.debian.org/tracker/CVE-2026-76642) | HIGH | `affected` | Not supplied |
| util-linux `2.38.1-5+deb12u3` | [CVE-2026-78408](https://security-tracker.debian.org/tracker/CVE-2026-78408) | HIGH | `affected` | Not supplied |
| util-linux `2.38.1-5+deb12u3` | [CVE-2026-78409](https://security-tracker.debian.org/tracker/CVE-2026-78409) | HIGH | `affected` | Not supplied |
| util-linux `2.38.1-5+deb12u3` | [CVE-2026-78410](https://security-tracker.debian.org/tracker/CVE-2026-78410) | HIGH | `affected` | Not supplied |

Available-version leads also include OpenSSL/libssl3 3.0.22-1~deb12u1 for six normalized advisories, tzdata 2026c-0+deb12u1 for DLA-4792-1, and setuptools 83.0.0 for CVE-2026-59890. These are scanner-reported remediation leads, not approved dependency edits. Updating the pinned base/native packages or installer vendoring would create a new candidate requiring fresh tests and scans.

### Applicability evidence requiring review

- **SQLite CVE-2025-7458:** [Debian](https://security-tracker.debian.org/tracker/CVE-2025-7458) marks the installed 3.40.1-2+deb12u2 source package vulnerable and describes the arbitrary-SQL precondition. Debian’s minor-issue/no-DSA policy differs from Trivy’s imported NVD CRITICAL severity. Neither policy nor a currently excluded SQLite deployment substitutes for a reviewed reachability decision.
- **Perl CVE-2026-8376:** [Debian’s description](https://security-tracker.debian.org/tracker/CVE-2026-8376) limits this overflow to 32-bit Perl builds. Read-only image archive evidence identifies `/usr/bin/perl` as ELF class 2 (64-bit), machine 62 (x86-64). This is a platform-applicability discrepancy for owner review; the raw CRITICAL finding remains retained. Evidence is in `component-observations.json`.
- **Vendored setuptools/msgpack:** Both system and application pip `vendor.txt` files actually name msgpack 1.1.2 and setuptools 70.3.0. This confirms the recorded versions, not full affected-component reachability. Trivy identifies those packages from embedded SBOM data and warns that third-party SBOMs may cause inaccurate detection.
- **Setuptools CVE-2025-47273:** The [upstream advisory](https://github.com/pypa/setuptools/security/advisories/GHSA-5rjg-fvgr-3xxf) identifies `PackageIndex` download code, with a fix in 78.1.1. No `setuptools/package_index.py` entry was observed in the image archive layers. The vendored subset needs an explicit applicability review before any disposition; the HIGH finding is unchanged.
- **Setuptools CVE-2026-59890:** The [upstream advisory](https://github.com/pypa/setuptools/security/advisories/GHSA-h35f-9h28-mq5c) concerns source-distribution building and Unicode filename normalization, especially on macOS APFS/HFS+. This artifact is a Linux installed-wheel runtime. That workload distinction warrants review and does not authorize a waiver.

No package-version mismatch was established. Potential scope/component differences above are documented separately from package-version identity, and no accepted exceptions were created.

## Per-package raw counts

The JSON summary retains each package’s advisory IDs, installed/fixed versions, statuses, and sources, plus all normalized advisory records and primary references.

| Binary/Python package | Installed version | Critical | High | Medium | Low | Unknown | Total |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| apt | `2.6.1` | 0 | 0 | 0 | 1 | 0 | 1 |
| bash | `5.2.15-2+b13` | 0 | 0 | 0 | 1 | 0 | 1 |
| bsdutils | `1:2.38.1-5+deb12u3` | 0 | 5 | 4 | 2 | 0 | 11 |
| coreutils | `9.1-1` | 0 | 0 | 0 | 5 | 0 | 5 |
| diffutils | `1:3.8-4` | 0 | 0 | 0 | 1 | 0 | 1 |
| gcc-12-base | `12.2.0-14+deb12u1` | 0 | 0 | 0 | 1 | 0 | 1 |
| gpgv | `2.2.40-1.1+deb12u2` | 0 | 0 | 2 | 2 | 0 | 4 |
| gzip | `1.12-1` | 0 | 1 | 1 | 0 | 0 | 2 |
| libacl1 | `2.3.1-3` | 0 | 1 | 1 | 0 | 0 | 2 |
| libapt-pkg6.0 | `2.6.1` | 0 | 0 | 0 | 1 | 0 | 1 |
| libattr1 | `1:2.5.1-4` | 0 | 0 | 1 | 0 | 0 | 1 |
| libblkid1 | `2.38.1-5+deb12u3` | 0 | 5 | 4 | 2 | 0 | 11 |
| libbz2-1.0 | `1.0.8-5+b1` | 0 | 0 | 1 | 0 | 0 | 1 |
| libc-bin | `2.36-9+deb12u14` | 0 | 0 | 15 | 8 | 0 | 23 |
| libc6 | `2.36-9+deb12u14` | 0 | 0 | 15 | 8 | 0 | 23 |
| libgcc-s1 | `12.2.0-14+deb12u1` | 0 | 0 | 0 | 1 | 0 | 1 |
| libgcrypt20 | `1.10.1-3+deb12u1` | 0 | 0 | 0 | 2 | 0 | 2 |
| libgnutls30 | `3.7.9-2+deb12u7` | 0 | 0 | 0 | 1 | 0 | 1 |
| libgssapi-krb5-2 | `1.20.1-2+deb12u5` | 0 | 0 | 0 | 4 | 0 | 4 |
| libk5crypto3 | `1.20.1-2+deb12u5` | 0 | 0 | 0 | 4 | 0 | 4 |
| libkrb5-3 | `1.20.1-2+deb12u5` | 0 | 0 | 0 | 4 | 0 | 4 |
| libkrb5support0 | `1.20.1-2+deb12u5` | 0 | 0 | 0 | 4 | 0 | 4 |
| libmount1 | `2.38.1-5+deb12u3` | 0 | 5 | 4 | 2 | 0 | 11 |
| libncursesw6 | `6.4-4` | 0 | 1 | 1 | 1 | 0 | 3 |
| libp11-kit0 | `0.24.1-2` | 0 | 0 | 2 | 0 | 0 | 2 |
| libpam-modules | `1.5.2-6+deb12u2` | 0 | 0 | 2 | 0 | 0 | 2 |
| libpam-modules-bin | `1.5.2-6+deb12u2` | 0 | 0 | 2 | 0 | 0 | 2 |
| libpam-runtime | `1.5.2-6+deb12u2` | 0 | 0 | 2 | 0 | 0 | 2 |
| libpam0g | `1.5.2-6+deb12u2` | 0 | 0 | 2 | 0 | 0 | 2 |
| libpcre2-8-0 | `10.42-1+deb12u1` | 0 | 0 | 0 | 0 | 1 | 1 |
| libsmartcols1 | `2.38.1-5+deb12u3` | 0 | 5 | 4 | 2 | 0 | 11 |
| libsqlite3-0 | `3.40.1-2+deb12u2` | 1 | 2 | 3 | 3 | 0 | 9 |
| libssl3 | `3.0.20-1~deb12u2` | 0 | 1 | 2 | 10 | 0 | 13 |
| libstdc++6 | `12.2.0-14+deb12u1` | 0 | 0 | 0 | 1 | 0 | 1 |
| libsystemd0 | `252.39-1~deb12u2` | 0 | 1 | 1 | 5 | 0 | 7 |
| libtasn1-6 | `4.19.0-2+deb12u1` | 0 | 0 | 0 | 1 | 0 | 1 |
| libtinfo6 | `6.4-4` | 0 | 1 | 1 | 1 | 0 | 3 |
| libudev1 | `252.39-1~deb12u2` | 0 | 1 | 1 | 5 | 0 | 7 |
| libuuid1 | `2.38.1-5+deb12u3` | 0 | 5 | 4 | 2 | 0 | 11 |
| login | `1:4.13+dfsg1-1+deb12u2` | 0 | 0 | 0 | 3 | 0 | 3 |
| mount | `2.38.1-5+deb12u3` | 0 | 5 | 4 | 2 | 0 | 11 |
| ncurses-base | `6.4-4` | 0 | 1 | 1 | 1 | 0 | 3 |
| ncurses-bin | `6.4-4` | 0 | 1 | 1 | 1 | 0 | 3 |
| openssl | `3.0.20-1~deb12u2` | 0 | 1 | 2 | 10 | 0 | 13 |
| passwd | `1:4.13+dfsg1-1+deb12u2` | 0 | 0 | 0 | 3 | 0 | 3 |
| perl-base | `5.36.0-7+deb12u3` | 3 | 5 | 8 | 2 | 1 | 19 |
| sysvinit-utils | `3.06-4` | 0 | 0 | 0 | 1 | 0 | 1 |
| tar | `1.34+dfsg-1.2+deb12u1` | 0 | 0 | 3 | 2 | 0 | 5 |
| tzdata | `2026b-0+deb12u1` | 0 | 0 | 0 | 0 | 1 | 1 |
| util-linux | `2.38.1-5+deb12u3` | 0 | 5 | 4 | 2 | 0 | 11 |
| util-linux-extra | `2.38.1-5+deb12u3` | 0 | 5 | 4 | 2 | 0 | 11 |
| zlib1g | `1:1.2.13.dfsg-1` | 1 | 0 | 2 | 0 | 0 | 3 |
| msgpack | `1.1.2` | 0 | 1 | 0 | 0 | 0 | 1 |
| setuptools | `70.3.0` | 0 | 1 | 1 | 0 | 0 | 2 |

## Validation boundary and next decision

No image packages, dependency locks, application source, or workflow definitions were changed by this scan. No ignore rules, severity filters, “ignore unfixed” behavior, or accepted exceptions were applied. Secret/misconfiguration scans, exploit testing, production endpoint tests, and a production release approval are outside this result.

Optional web-tool reads for several Debian pages returned exactly `Failed to fetch restricted URL`; those reads were stopped without retries. Their raw advisory URLs and database findings remain available. This limits independently confirmed vendor triage, not successful scanner execution.

Before release qualification, the accountable owner must review the affected components and remediation leads, replace/rebuild the candidate where needed, and approve any bounded exception with evidence. No high/critical finding is automatically dismissed by an upstream severity difference, a missing current scanner match, or the platform observations above.

Hosted CI: **skipped: user instruction; known GitHub billing/spending restriction**. Local scan success does not satisfy hosted or production qualification gates. Ticket #160 was already administratively closed; this refresh makes no ticket-state change and does not imply the outstanding release blockers are resolved.
