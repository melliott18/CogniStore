# Operator handbook

Use this handbook to install, change, recover, and troubleshoot the shipped
CogniStore application. The procedures describe the `0.1.0` package and the
configuration in this checkout; record the source revision or image digest as
well as the package version because multiple revisions share that version.

## Choose a procedure

| Task or symptom | Procedure | Completion evidence |
| --- | --- | --- |
| First installation | [Clean local walkthrough](operator_lifecycle.md#install), then [reference architectures](reference_architectures.md) | Installed CLI, indexed object, byte-identical download, complete clean scan |
| Change application version | [Upgrade](operator_lifecycle.md#upgrade) and [rollback](operator_lifecycle.md#rollback) | All catalog partitions at the intended schema; smoke operation and jobs succeed |
| Protect or recover state | [Backup](operator_lifecycle.md#backup) and [restore](operator_lifecycle.md#restore) | Coherent restore set, verified object hashes, fresh consistency scan, preserved job identities |
| Switch catalog, storage, or deployment | [Migration guides](operator_migrations.md) | Cutover inventory and checksums reconcile; old writers fenced |
| API failure, stalled work, or alert | [Incident decision tree](operator_incidents.md) and [SLO alert runbooks](operational_slos.md#runbooks) | Dependency recovered, original operation resolved, alert recovers with telemetry present |
| Dead letter or incomplete move | [Queue and repair procedures](operator_incidents.md) | Reviewed redrive or repair, original job inspected, new clean scan |
| Authentication, key, or tenant incident | [Security runbook](operator_security.md) | Access restored or revoked as intended; protected operation checked and audit evidence retained |

The [recorded operator drills](evidence/m4/README.md) include a clean virtualenv
installation, POSIX/SQLite backup and restoration, and an injected move-repair
incident. They are local functional evidence. Production PostgreSQL, broker,
cloud storage, and cluster recovery must be rehearsed in the deployment's own
isolated recovery environment. Existing [Kubernetes qualification](kubernetes.md#automated-qualification)
and [scale/recovery qualification](scale_qualification.md) cover their separate
contracts.

## Prepare before an incident

Assign named on-call owners for API/platform, catalog/indexing, storage/workers,
and security; the [SLO model](slo_model.md) identifies accountable groups. Record
the deployment namespace, tenant inventory, configured tier endpoints, catalog
partitions, queue stream/subject/consumer and DLQ, scheduler location, and backup
locations. Store credentials in the approved secret system, with a tested
recovery identity and retained encryption-key versions.

Set recovery point and recovery time objectives for this deployment. Measure
them with a restore drill: the recovery point is the newest coherent restored
state, and recovery time ends only after application/data validation. Replicas
and versioning do not establish those objectives. Rehearse before a migration
and after changing storage, encryption, queue topology, or tenant policy.

Each incident/change record should retain UTC start/end times, source revision
or image digest, sanitized configuration identity, tenant/scope, job and
correlation IDs, observed symptoms, exact actions, validation results, unresolved
findings, and the recovery decision. Restrict consistency reports and exports as
data-bearing artifacts. Use [structured telemetry](observability.md) and
[operational audit events](audit_events.md); exclude tokens and resolved secrets.

## Command conventions

Commands run from the repository root unless stated otherwise. Quoted uppercase
variables denote operator-supplied paths or identifiers. `--no-config` disables
the user CLI profile, but **does not ignore environment variables**; use a clean
shell or explicitly select the intended settings. The local tutorial uses the
`development` profile and synthetic data. Production requires the
[encryption](encryption.md), [authentication](authentication.md), and
[tenant isolation](tenancy.md) configuration.

CLI exit status zero means a report was produced, not necessarily that the
system is healthy. Check consistency `summary.complete` and
`summary.consistent`, repair actions/blockers, and terminal job state. Preview
commands can validate configuration without proving a subsequent mutation is
safe; the writable operation rechecks live state. See the complete
[CLI contract](cli.md#dry-run-contract).
