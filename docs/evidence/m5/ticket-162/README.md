# Ticket #162 local system audit

**Conclusion: local audit and remediation implemented; final release audit
signoff is incomplete.** This record does not approve a candidate, environment,
deployment or pilot entry, and must not close #162.

Audit scope is `m5-pilot-v1`, revision 1, based on
`8d3d50bb649cea617fe030859c51b196457e4648`. The
[versioned matrix](../../../../release/audit-matrix.json) maps all 14 scoped
trust boundaries/mutation families to reviewed code, regression suites and
required staging checks. The [runbook](../../../system_audit.md) defines
reproduction, evidence verification and scoped re-audit rules.

| Evidence | Finding / result |
| --- | --- |
| [Security boundary review](security-review.md) | JWT/RBAC, tenant search/citations, trusted broker identities, queued grant changes, audit/checkpoints and secrets reviewed; 1,151 passes and six explicit skips across two runs. Live control gaps remain open. |
| [Mutation review](mutation-review.md) | Both known defects reproduced on complete affected baseline and verified fixed on real case-insensitive POSIX. New scan/API race reproduced and remediated under [#175](https://github.com/melliott18/CogniStore/issues/175). |
| [Independent scanner fix review](scanner-fix-review.md) | Lock order, holds, tenant scope, dry-run and PostgreSQL owning-session publication reviewed; 112 passes, 12 PostgreSQL skips in this separate reviewer run. |
| [Artifact/configuration review](artifact-config-review.md) | Entry points, privileged state ownership, dependencies and production assumptions inventoried. Local dependency/build checks passed; inherited 69 image scanner findings remain unresolved. |
| [Validation manifest](validation.json) | Exact tested source/configuration hashes, commands, outcomes, runtime, checksums and remaining gaps. Counts from overlapping suites must not be added together. |

Local full-suite outcome: **5,756 passed, 315 skipped, 86.82% combined
statement/branch coverage**. After integrating remote `main` at `45cc884`, the
overlapping worker/queue/telemetry and scan/API scope passed **206 tests with
12 PostgreSQL skips**. Final audit-tooling tests passed **31/31**. The
unconfigured services profile accurately records **10 skips / incomplete**;
its successful verification confirms the report's consistency, not service
qualification. One of those entries represents a whole module skipped during
collection, not a count of executed MinIO tests. The initial conservative
misclassification is retained alongside the corrected result.

Ruff, mypy and configured Bandit thresholds passed after integration. Build and
Twine checks passed on the scan-fix source before the upstream integration.
An initial Bandit finding in the new report parser was corrected with bounded
UTF-8 XML and DTD/entity rejection; both the failed check and retest are retained.
The dependency definitions are unchanged; the independent local dependency
audit in the artifact review passed. These checks do not resolve the frozen
image's native/vendored findings. Runtime inventories are local and unfrozen.

The full suite ran before the later upstream worker/telemetry integration;
the exact source identities and narrower post-integration retest are preserved
in `validation.json`. It is not represented as a final merged-commit full-suite
run. All application files were stable during the full invocation; final
audit-tooling changes have their separate regression report.

## Findings and release disposition

| Finding | Severity / nominated owner | Disposition |
| --- | --- | --- |
| [#175: scan observation can overwrite a completed API mutation](https://github.com/melliott18/CogniStore/issues/175) | P1 / Mitchell Elliott (proposed accountable owner) | Fixed in `30b175d`; before/after regressions and independent review retained. A newly assembled production candidate must rerun affected scan/PUT/DELETE/hold/repair/cleanup rows. The old #160 image does not contain this change. |
| [ART-01](artifact-config-review.md#findings-and-disposition): image findings | Inherited release blocker, including critical/high / proposed release-security owner Mitchell Elliott | No waiver or remediation claimed. Existing #160 security disposition and exact-image retest are required. |
| [SEC-162-01–05](security-review.md#findings-and-required-dispositions), [CFG-01–02](artifact-config-review.md#findings-and-disposition) | Qualification blockers / proposed deployment-security-recovery owner Mitchell Elliott | Real proxy/OIDC, broker ACL/network, checkpoint custody, selected catalog/backend controls, secret/CA/key rotation, accepted owners and live environment remain unqualified. Existing #159/#161/#163/#164/#166 own those gates. |

Proposed ownership is not accepted operational duty. Local PostgreSQL16 and
POSIX fixtures establish the specifically retained component outcomes, not
Linux ext4/native AWS S3, production TLS, installed candidate identity or
actual API/worker deployment qualification. There is no signed audit decision.

The former #160 development candidate is source
`e51cb39cc84b819ecbed43b56513888a956812cf`, image configuration digest
`sha256:1f065e90bbcb256970782f16622d931b84e83ffe91d5e00321bd8b8f8bb33a05`.
It is unqualified and has no recorded registry manifest digest. Current code
changes invalidate its coverage for the affected rows. No replacement
candidate or live environment was assembled by this task. Shipped staging
inputs continue to fail preflight as expected.

Operating limits remain the selected one-node, fixed API/worker replica
placement, trusted POSIX namespace writers, one PostgreSQL primary and one
persistent trusted-producer JetStream server, metadata-only Ask, synthetic
data, reviewed mutations and disabled scheduling. Optional providers,
unselected clouds, real customer data and multi-node POSIX are not qualified.

## Safeguard interruption

After producing the scanner changes and the new regression report, the
mutation-audit agent ended with this exact error:

> This content was flagged for possible cybersecurity risk. If this seems wrong, try rephrasing your request. To get authorized for security work, join the Trusted Access for Cyber program: https://chatgpt.com/cyber

The blocked subtask was not retried or rephrased. Its completed artifacts and
the result of an already-started broader test run were preserved; independent
review, audit-tooling work and ordinary local validation continued. This
interruption is recorded separately from pytest failures and
from the remaining production qualification gates.

Hosted CI: **skipped: user instruction; known GitHub billing/spending
restriction**. No hosted workflow was dispatched, rerun, watched or polled.
Normal pushes/PRs may trigger workflows automatically; they are not passing
evidence. See [SHA256SUMS](SHA256SUMS) for retained artifact checksums.
