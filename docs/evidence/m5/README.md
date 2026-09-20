# M5 release readiness and pilot evidence

M5 [#155](https://github.com/melliott18/CogniStore/issues/155) qualifies one
selected deployment after completed M1–M4 delivery. The versioned
[deployment/workload specification](../../production_pilot.md) selects the
scope and thresholds. This index is a handoff contract, **not passing evidence**.
No release candidate, environment, qualification result or pilot approval has
been recorded here yet. Historical [M4 evidence](../m4/README.md) remains intact.

| Evidence owner ticket | Required durable record | Current state |
| --- | --- | --- |
| [#159](https://github.com/melliott18/CogniStore/issues/159) | Exact specification revision/commit, named owner acceptance and dated review decision | Proposed specification; acceptance pending |
| [#156](https://github.com/melliott18/CogniStore/issues/156), [#157](https://github.com/melliott18/CogniStore/issues/157) | Fix commits and regression reports for namespace isolation/holds and overlapping DELETE/PUT | Not recorded |
| [#158](https://github.com/melliott18/CogniStore/issues/158), [#160](https://github.com/melliott18/CogniStore/issues/160) | Hosted runtime matrix and immutable source/image/dependency/provider/configuration manifest | Not recorded |
| [#161](https://github.com/melliott18/CogniStore/issues/161) | Isolated environment identity, production controls, versioned configuration, storage/memory/queue bounds and private telemetry | Not recorded |
| [#162](https://github.com/melliott18/CogniStore/issues/162) | Candidate/environment-bound security and consistency audit, residual risks and signed decision | Not recorded |
| [#163](https://github.com/melliott18/CogniStore/issues/163) | Per-interface/role/tenant manual acceptance matrix, actual browser evidence and corpus manifests | Not recorded |
| [#164](https://github.com/melliott18/CogniStore/issues/164) | Repeated coherent restore/fault/rotation/rollback reports, recovery-set IDs, RPO/RTO and integrity results | Not recorded |
| [#165](https://github.com/melliott18/CogniStore/issues/165) | Soak/burst/capacity reports, raw measurements, denominator coverage and real alert receipt/recovery evidence | Not recorded |
| [#166](https://github.com/melliott18/CogniStore/issues/166) | Named entry decision, daily pilot records, user tasks and explicit expand/fix/stop decision | Not recorded |

Each record must include:

- Record ID, accountable reviewer/operator, UTC timestamps and pass/fail/
  inconclusive decision; link the exact specification commit and target IDs.
- Candidate source SHA, image digest, Python/native dependency manifest and
  hashes of rendered Helm, drivers, policy and provider configuration. Record
  secret **version references**, never values, in restricted evidence.
- Environment ID, topology, OS/kernel/filesystem/mount semantics, service/image
  versions, resource limits, queue/retry configuration and encryption evidence.
- Corpus generator revision/seed and checksum manifest; size/MIME distribution,
  tenant/role cohort, offered load, concurrency, duration and eligible sample
  counts. Keep fault/negative/overload cohorts separate and explain exclusions.
- Reproducible commands, expected/actual outcomes, raw sanitized logs/metrics,
  numerators/denominators, quantiles, coverage gaps, screenshots where required,
  and SHA-256 checksums of every retained artifact.
- Findings, explicit skips and their impact, linked fixes, affected reruns and
  reviewer decisions. No empty denominator or missing evidence is a pass.

Retain artifacts in this directory or durable access-controlled CI/artifact
storage and add immutable links plus checksums to this index. Preserve failed
and superseded runs with their disposition. Check accessibility and retention
before acceptance; a local `/tmp` path, ignored `.artifacts` directory or expiring
CI link by itself is insufficient. Retain qualification and pilot evidence for
at least 90 days after the exit decision, extending retention for unresolved
findings. Do not commit credentials, bearer tokens or customer/personal content.

Owner acceptance of the specification, technical review, qualification signoff
and pilot entry approval are separate decisions. Record each explicitly; this
table never advances automatically from ticket closure or a green unit test.

## Specification authoring review — 2026-09-20

`m5-pilot-v1`, revision 1, was checked against application source at
`2cce6ff4d43fd287ad008197f1e17f168c3580c4`. An independent Codex agent reviewed
runtime boundaries and numerical consistency; the resulting revision addresses
S3 tier aliasing, the upload limit and memory budget, browser authentication,
CLI tenant boundaries, corpus arithmetic, balanced load, and distinct
qualification/pilot measurement populations. No actionable review findings
remained. This is an automated technical review, not the pending named owner's
acceptance or a production security signoff.

Local authoring validation passed:

- All nine `tests/unit/test_ticket_mirror_sync.py` tests, using
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p no:capture
  tests/unit/test_ticket_mirror_sync.py`. The existing Python 3.12 environment
  required capture/plugin isolation to avoid its native-readline startup crash;
  it also emitted a missing-Starlette warning-filter warning.
- `python scripts/sync_ticket_mirror.py --check`: mirror matches 80 live issues.
- Local Markdown target/anchor validation for the specification, roadmap and
  this index: 31 links resolved; corpus byte arithmetic independently checked.
- Whitespace checks for all changed/new Markdown files.

These documentation checks do not execute a release candidate or establish any
of the production gates above. The Git commit containing this review versions
the reviewed specification and documentation together.
