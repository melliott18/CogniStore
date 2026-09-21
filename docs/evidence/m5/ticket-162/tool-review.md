# Ticket #162 audit-tool review

**Result: the two identified local-runner defects are addressed; no additional
material actionable defect found in this bounded re-review.** This reviews
local regression evidence tooling, not production or candidate signoff.

Initial review: 2026-09-21T22:15:53.320356+00:00 at checkout base
`5b6a3c23e1fdf6ab930d1a93629f81ff5cb758a2`. Follow-up review: 2026-09-21T22:26:04.338975+00:00
of the seven-line change in `b575dc96360d0a006fd385b117f3da5e3862134d`.
Current reviewed bytes:

| Reviewed file | SHA-256 |
| --- | --- |
| `scripts/system_audit.py` | `1d2016ab536a6b3c7313ad56f95f31fa82cb0586e135da62f280c6470118613f` |
| `tests/unit/test_system_audit.py` | `5688d00b7ae4ab6ae6fe90c96d39f3b00ac99f25d8a8a9af91237e53d78fa81c` |
| `release/audit-matrix.json` | `fddd9a972a573f61416897e0cbcc12f176659d9b9c7137b7a16378d9d6c4c6ad` |
| `docs/system_audit.md` | `2c2e2396363355be638928ef7a0f80beaf2b4a932d2213e8bb49407646d12ca8` |

The earlier review identified inherited `PYTEST_ADDOPTS` deselection producing a
false local pass, and incomplete verification of report identity metadata.
The revised runner clears ambient pytest selectors/plugins, disables plugin
autoload, binds checkout imports, and validates report scope/schema, source
commit ancestry, runtime identity, interval and test invocation. The regression
suite includes a child pytest case proving that ambient selection cannot hide
a required failing case, plus changed metadata/artifact controls. JSON duplicate
keys and nonfinite tokens are rejected. XML parsing is limited to bounded UTF-8
input without DTD/entity declarations or NULs before parsing.

The follow-up correctly recognizes pytest module collection skips only when
`classname` is empty, a skipped outcome exists, and the module name exactly
matches an expected file. These cases remain `incomplete`, never `passed`;
unrelated cases retain their previous rejection. The added regression exercises
this module-skip representation. No further defect found in that change.

The runbook distinguishes verification of an intact failed/incomplete report
from a test pass, records Helm/NATS chart prerequisites and applicable native
filesystem limitations, and reserves final acceptance for exact-artifact,
configuration, environment and reviewer evidence. The runner always records
`production_signoff: false`; source/checksum correspondence does not authenticate
a reviewer or prevent wholesale record forgery. Preserve independent reviewed
records, and retain external tool/dependency/chart identities for any qualified
campaign; the source snapshot does not freeze that external runtime.

This final review was read-only apart from this evidence note. No mutation/fault
experiment or product test was run during the re-review. The parent task reports
31 tooling tests passed and the unconfigured services profile retained ten
skips as incomplete evidence; its retained validation records are the execution
evidence. No tests were rerun for this follow-up review.
The earlier findings were communicated and corrected before this review; they
are tooling defects, not new production-runtime findings.

Hosted CI: `skipped: user instruction; known GitHub billing/spending restriction`.
