# Frozen candidate image assessment — 2026-10-02

**Not approved for release.** The unsuppressed Trivy 0.75.0 report records 296 binary-package/advisory occurrences and 139 normalized source-package/advisory records: 5 critical, 22 high, 57 medium, 53 low and 2 unknown. All findings remain retained; no owner exception or blanket waiver is granted.

The exact runtime configuration digest is `sha256:62cc2900a1091daa005ae12d70f4e15884f4b04392403ddaaa06e21cdd0e50b6`, source `b7e2b3f6fb3f3fefe4b0e9d2f155204ee2bb71b6`. Scanner provenance binds the saved image archive, scanner binary, database digest, full commands and timestamps. Database intelligence was updated 2026-10-02T12:48:00.080865328Z and downloaded before this offline scan. Exit zero confirms scan execution, not zero vulnerabilities.

Normalization uses the full Debian package inventory to map each binary package to its source name/version, then groups by ecosystem, source package/version and advisory ID. Python entries group by package/version/advisory. CVE/GHSA aliases are not silently combined. `security-summary.json` retains every normalized record with affected binaries, severity, status, available fix version and references; `trivy-report.json` retains the original 296 occurrences. A reported status of `fixed` means the scanner knows a patched version; it does not mean this installed package is patched.

## Critical findings: applicability evidence, not accepted exceptions

| Advisory | Component | Evidence and remaining review |
| --- | --- | --- |
| CVE-2025-7458 | SQLite 3.40.1-2+deb12u2 | Scanner advisory describes an arbitrary-SQL precondition. SQLite remains installed and used by local paths even though the proposed service catalog is PostgreSQL. Establish reachability and update or obtain a bounded, named review decision. |
| CVE-2026-13221 | Perl 5.36.0-7+deb12u3 | Advisory concerns oversized regex alternation and incorrect matches. Source search found no direct Perl invocation in CogniStore; that observation does not prove every deployed execution path unreachable. |
| CVE-2026-42496 | Perl Archive::Tar | The retained image cannot load Archive::Tar; the installed perl-base package inventory omits it. This is evidence for a component-not-present disposition, pending reviewed acceptance. |
| CVE-2026-8376 | Perl regex compiler | Advisory specifies 32-bit builds. The actual image interpreter is ELF64 x86-64. This is evidence for a platform-not-affected disposition, pending reviewed acceptance. |
| CVE-2023-45853 | MiniZip under zlib | Installed zlib1g lists libz and documentation, with no MiniZip component. Assess the scanner source-package association and any separately bundled copies before accepting non-applicability. |

`component-observations.json` retains read-only commands against the exact runtime image, including ELF identity, dpkg package file lists and the failed Archive::Tar import. No exploit payloads or production systems were used. These checks refine triage; they neither suppress raw findings nor authorize risk acceptance. The remaining 22 high findings also need component-specific treatment, not just review of the critical count.

All 107 native inventory rows are byte-identical to the September 29 image. Six top-level Python distributions changed in the successor. Scanner version/database and coverage changed, so the increase from 133 to 139 normalized records is not evidence of native package regression or remediation. A passing top-level pip-audit does not clear vendored installer or operating-system findings.

## Release decision

Retain this candidate as unqualified until applicable findings are remediated or explicitly reviewed with owner, justification, scope and expiry, and the other owner/configuration/registry/environment gates are satisfied. A dependency/base-image fix changes candidate bytes and requires a new freeze and affected reruns. Historical image scans and skipped CI records remain unchanged. This record does not grant a security exception or production/pilot approval.
