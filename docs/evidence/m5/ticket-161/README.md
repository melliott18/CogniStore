# Ticket #161 staging preparation evidence

**Status: preparation validated locally; actual deployment is blocked on target
inputs and all live acceptance criteria remain pending.** No cloud resources,
customer data, registry publication, operator notification or hosted CI run
was created by this implementation. Do not close #161 based on these checks.

The [staging runbook](../../../staging.md) describes the selected input bundle,
offline consistency checker, explicitly enabled real-service smoke, deployment
handoff and teardown/recovery evidence. It preserves the selected fixed-node
POSIX/S3 topology, PostgreSQL 16 requirement and one persistent TLS JetStream
server instead of silently deploying the different generic Terraform example.

Local validation: **218 tests passed**, with zero failures or skips. Repository
Ruff, mypy (164 application files), Bandit (application and new scripts),
whitespace checks and 53 local documentation targets passed.

[validation.json](validation.json) records the tested revision, commands, hashes and outcomes.
[tests.xml.gz](tests.xml.gz) and [tests.log.gz](tests.log.gz) retain the scoped local regression results.
[unresolved-preflight.json](unresolved-preflight.json) records the expected rejection of the shipped
placeholder inputs against the retained candidate manifest. This is a useful
negative control, not failed live staging acceptance. [quality.log](quality.log) retains
static checks; [SHA256SUMS](SHA256SUMS) verifies the evidence files.

Local tests render the application and pinned NATS Helm charts, exercise actual
queue/client subjects against the configured broker permission patterns, reject
incomplete/unsafe selected inputs, and test the smoke client through synthetic
HTTP transports. These do not prove live broker/CNI enforcement, TLS/OIDC,
PostgreSQL/S3 service behavior, encryption or successful alert delivery. The
application package and dependency definitions are unchanged; the full product
suite, package rebuild and dependency audit were not rerun for this tooling
and configuration change.

The default workspace Python environment had an existing native readline
startup crash and missing runtime dependencies. Validation used the separate
complete local runtime recorded in `validation.json`. Earlier focused chart
checks passed with capture/plugin isolation; the final retained integrated run
uses normal pytest in the complete runtime. A Bandit finding on comparison to
the literal `/dev/shm` mount path was a false positive: no temporary file is
created there by preflight. That one comparison has an explained B108 exclusion;
production scanners and runtime security controls are unchanged.

Required inputs are the selected AWS account/profile and cluster, accepted
owners and spend/expiry bounds, trusted candidate registry image and transfer
record, OIDC identities and browser proxy, secret/TLS delivery, restricted
network/telemetry setup and real operator notification destination. Existing
#159/#160 qualification gaps remain visible in their own evidence records.

| #161 acceptance criterion | Current result |
| --- | --- |
| Exact candidate/configuration running and resources inventoried | Pending: no environment or registry manifest identity supplied. |
| Authenticated storage/queue smoke and credential/TLS/network negatives | Pending: client validated with synthetic transports; no live service calls. |
| Platform encryption/backups and key/recovery ownership | Pending: configuration intent only; no platform evidence. |
| Tenant-safe telemetry and actual operator notification | Pending: monitoring network boundaries prepared; no collector/receiver deployment or notification. |
| Isolation, spend bounds, access and teardown | Runbook and required-input record prepared; actual resource/cost/owner evidence pending. |

Hosted CI: `skipped: user instruction; known GitHub billing/spending restriction`.
No existing hosted failures are counted as new local test failures, and local
results do not satisfy suspended release-qualification gates.
