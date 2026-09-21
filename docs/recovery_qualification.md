# M5 recovery qualification campaign

Ticket [#164](https://github.com/melliott18/CogniStore/issues/164) qualifies
operator recovery for the selected [pilot specification](production_pilot.md),
`m5-pilot-v1`, revision 1. **Status: campaign preparation; no live recovery,
rotation, restore, or rollback qualification is claimed.** The specification's
named owner acceptance under #159 and the actual isolated deployment under
[#161](staging.md) remain missing. The retained #160 branch artifact is also
unqualified. A locally passing evidence checker or historical SQLite drill does
not meet those prerequisites or this ticket's acceptance criteria.

This procedure applies only to the owned CogniStore application in the recorded,
authorized isolated staging environment, using synthetic identities and data.
Do not inject faults into production, another namespace, shared dependencies,
or an unrecorded account. Local tests use temporary storage and fixture
credentials. No cloud fault, deployment, or key change is performed by the
offline tooling accompanying this guide.

## Prerequisites and fixed scope

Before scheduling any live drill, the accepted recovery and security owners
must review the concrete environment inventory, fault/restore targets, timing,
stop conditions, and rollback plan. Retain their decision with the exact
specification, source, image, and configuration identities. Resolve #159/#161
and release blockers; do not treat issue closure as acceptance evidence.

The fixed topology is PostgreSQL **16** with pgvector, one API and one worker
on the same Linux amd64 node and encrypted case-sensitive ext4 RWO hot volume,
native versioned SSE-KMS S3 warm storage, and one persistent verified-TLS
JetStream server with separate main/DLQ streams and durable consumers. Both
synthetic tenants, `pilot-a` and `pilot-b`, participate in every applicable
trial. Worker concurrency remains two. Scheduler, HPA, and KEDA remain
disabled; record and test rejection of scheduled envelopes. No scheduler
database or keyword index belongs in this recovery set because neither is
enabled. Enabling either requires a revised scope and fresh qualification.

1. Freeze the actual #160 candidate and #161 environment, including PostgreSQL
   and pgvector versions, both tenant migration heads, image/registry digest,
   kernel/mount/volume identity, S3 bucket/versioning/encryption configuration,
   broker limits/ACK/retry settings, TLS endpoints, secret versions, and current
   reviewed encryption attestations. Retain dependency and tool versions.
2. Establish external authenticated probes, private worker/broker/database/
   storage telemetry, and synchronized UTC clocks. Record the clock source and
   observed offsets. Probe each tenant's real operation, not just `/healthz`.
   Confirm the actual alert recipient, delivery, acknowledgement, and recovery
   notification path before injecting a fault.
3. Generate a bounded versioned synthetic corpus and SHA-256 manifest. Include
   identical logical keys with different bytes in both tenants, both tiers,
   ordinary and held objects, overwritten versions, deleted-object history,
   queued scans, and hot-to-warm and warm-to-hot policy movements. Include
   multipart-sized objects within the selected 16 MiB object limit. Retain
   expected content, size, placement, version/generation, hold, audit, and job
   state per tenant; a count-only inventory cannot prove integrity.
4. Exercise healthy positive and denied controls before every trial. A tenant
   can read its expected bytes, submit a protected job, and observe completion;
   the other tenant cannot disclose or mutate its object/job/hold. Default and
   unmapped identities remain denied. These controls distinguish the intended
   fault from an already broken environment.
5. Take and verify a coherent recovery set using [the backup procedure](operator_lifecycle.md#backup).
   Retain seven daily sets outside application deletion credentials, with
   at least 500 GiB independent encrypted backup capacity. Keep the latest
   verified boundary no more than 24 hours old, and take another before the
   upgrade. Target at most five minutes of admission downtime per backup and
   include actual downtime in availability evidence.

Use authenticated API/SDK actions for named-tenant jobs. Ordinary CLI producers
do not attach the submitting identity required by protected workers. CLI
consistency `--tenant` is a scope label, not a catalog partition selector. For
full reconciliation use scoped admin APIs or an operator-reviewed harness that
explicitly binds `catalog.for_tenant` and tenant-scoped storage drivers for
**each** tenant. Retain that harness's source/hash and commands; never point
ordinary CLI writers at the named tenants' catalog/storage and label the result
tenant coverage. See [tenant ownership](tenancy.md) and
[operator migration boundaries](operator_migrations.md).

## Trial inventory and independence

Use a separate trial ID, synthetic prefix, timestamped baseline and evidence
directory for every row repetition. Restore healthy controls and reconcile the
previous trial before starting the next; three retries of the same failed
attempt are not three independent trials. Preserve failed trials, their fixes,
and fresh reruns. Do not pool different candidate/configuration revisions or
average a failure into a passing result.

| Cohort | Minimum independent trials | Required fault or change |
| --- | ---: | --- |
| Worker transfer interruption | 3 | Kill the recorded worker during proven active transfer; cover both movement directions across the cohort. |
| Worker catalog-publication interruption | 3 | Kill at a proven transfer-verification/catalog-publication boundary; retain observed journal phase and commit ambiguity. |
| PostgreSQL disconnection | 3 | Interrupt only the selected application's catalog connection during active work, including the publication boundary. |
| JetStream disconnection | 3 | Interrupt producer/consumer connectivity and reconnect while retaining persistent main/DLQ streams. |
| POSIX storage unavailability | 3 | Isolate the disposable selected hot mount/access during work using the reviewed platform mechanism. |
| S3 storage disconnection | 3 | Interrupt selected worker/API storage access during work, including a multipart transfer. |
| Lost submission response and retry | 3 | Lose the action response after it may have been accepted; reconcile before any resubmission. |
| Retry exhaustion, DLQ and redrive | 1 per tenant | Exhaust a bounded transient failure, retain the DLQ record, repair its cause, and redrive with original identity. |
| Runtime credential rotation | 1 complete campaign | Rotate each selected API/service, database, broker, and storage workload credential mechanism under work; prove old rejection and replacement operation. |
| TLS certificate/CA rotation | 1 complete campaign | Rotate overlapping trust and leaf certificates on every applicable connection and exercise reconnects. |
| RBAC grant revocation | 1 per tenant | Remove a queued submitter's required permission and verify delivery/retry/redrive revalidation. |
| Tenant grant revocation | 1 per tenant | Remove or change queued submitter membership and verify rejection against the saved tenant. |
| Full restore with pending work | 1 | Restore every plane with queued jobs and a nonterminal move; prove generation-change handling. |
| Full restore using retained old keys | 1 | Restore a distinct pre-rotation set using the retained original encryption-key versions. |
| Full restore after upgrade/rollback | 1 | Restore a distinct coherent set as part of the rehearsed candidate upgrade and rollback. |

The generated template groups the two tenants' credential, CA and grant
checks into one campaign record per change type. Record each tenant's results
inside that run's raw evidence. DLQ/redrive subcases can join broker-disconnect
or lost-response runs when their separately identified observations cover each
tenant; they do not replace the three independent trials of those faults.

The three full restores must use distinct recovery-set and destination
identities. A single restore labeled with all three scenarios is insufficient.
Each must include both tenants and pass RPO/RTO independently. Credentials and
CA trust are separate from encryption keys; rotating a database password does
not exercise retained KMS-key recovery.

## Common fault procedure and stop conditions

1. Record the trial's healthy baseline, accepted action requests/job IDs, move
   IDs, current journal states, object/hold manifests, independent audit
   checkpoints, and main/DLQ/consumer state. Start workload and external probes;
   record the precise fault target, injection and removal commands, and operator.
2. Introduce only the reviewed fault. For phase-specific interruptions, retain
   candidate-matching trace, debugger, or test-harness evidence proving the
   worker reached that phase before termination. A random kill with no phase
   evidence cannot pass the phase-specific row. Record the instrumentation and
   its effect; do not replace the frozen binary with a modified build and claim
   exact-candidate qualification.
3. Preserve logs, broker diagnostics, journals, state transitions and external
   failures while the fault remains active. Confirm bounded retries,
   backpressure, admission rejection and alerts where applicable. Do not purge
   streams, manually ACK unfinished work, clear leases, edit a journal, or
   delete a retained source to make the trial appear recovered.
4. Stop admissions and externally fence old workers before repair/restart.
   Verify their processes/containers cannot resume storage writes, including
   automatic restarts; lease expiry alone is insufficient. Remove the injected
   fault and prove the correct connection/control path works again. Restore
   valid TLS/credentials rather than weakening verification.
5. Inspect source and destination bytes and generations, current catalog
   placement, journal history, job identity, attempts, ACKs and any DLQ entry.
   Before verified destination publication, source bytes must remain available.
   After uncertain publication, determine the committed state before cleanup.
   Expect at-least-once delivery; require one durable logical job identity and
   zero duplicate destructive effects, rather than assuming one delivery.
6. Allow bounded recovery only after the old owner is fenced and current
   permissions, holds, checksums and generation preconditions are satisfied.
   Use [reviewed move recovery](operator_incidents.md#interrupted-move-and-consistency-repair).
   Ambiguous/mismatched objects must quarantine with an explicit reason and
   preserved copies. Resolve the quarantine through reviewed reconciliation;
   it is safe failure evidence, not proof that the workload has recovered.
7. Complete [pre-resume reconciliation](#pre-resume-reconciliation), record the
   measured outage and owner decision, then restore the healthy baseline for
   the next independent trial. A new authorized job in both tenants must reach
   its expected terminal outcome and storage effect.

Fence admissions and mutating work immediately on a checksum, tenant, hold,
audit, TLS/encryption or credential-control failure, unexpected sensitive
data, exhausted storage, or unavailable/unaccepted operator. Apply all pilot
queue/memory/error/probe stop thresholds. Preserve the failing state and
recovery evidence, record a blocker, fix the cause and repeat the affected
cohort. The accepted owner authorizes resumption only after the cause, fix and
reconciliation are established. No unresolved integrity or recovery blocker
can be waived by a healthy pod or checker exit status.

## Dependency failures, uncertain submission, and DLQ

For database failures, capture the request and transaction/journal boundary:
a disconnected client cannot infer whether publication committed. Check both
tenant schemas after reconnect and correlate catalog/audit state with retained
bytes before advancing a move. For broker failures, retain sequence and
consumer positions, publication acknowledgements or their absence, retry
counts, reconnect/TLS results and actual terminal work. Persistent stream
restoration is not needed for a connectivity-only fault; preserve its state.
For POSIX/S3 failures, compare partial/staged writes, metadata/sidecars, S3
multipart/version state and source hashes. Remove only known drill-owned
temporary state after reconciliation; never recursively clean a namespace to
recover capacity or conceal extra copies.

REST actions do **not** support a caller-provided idempotency key. Losing an
HTTP response, receiving a transport error, or failing a broker acknowledgement
does not prove that no job exists. In each lost-response trial:

1. Record a unique synthetic request fixture, tenant/principal, correlation ID,
   exact submission window and sanitized request hash before sending once.
2. Interrupt the response path after possible acceptance. Retain client,
   proxy/API, catalog job-status/audit and broker evidence. Reconcile accepted
   IDs through operator-authorized tenant-bound inspection and use
   `GET /v1/jobs/{job_id}` when the ID is known; there is no assumed public
   job-list endpoint. Preserve any job recorded before publication too.
3. Follow every matching job to a known outcome and inspect its actual effects.
   If acceptance remains uncertain, keep it pending for investigation and do
   not submit a replacement. Only resubmit after recording proof that the
   original did not accept executable work, or after a reviewed reconciliation
   establishes what a new action may safely do. Count any additional logical
   request and its effects explicitly; an automatic HTTP retry is not dedupe.

In the separate trusted CLI fixture, a supported stable `--job-id` and broker
deduplication window may provide retry behavior; neither applies by implication
to the protected REST actions. Keep that fixture outside pilot tenant state.

For each tenant's DLQ case, induce a reversible transient dependency failure
long enough to exhaust the **frozen deployed** retry budget. Preserve the
original logical job ID, principal/tenant, attempt timestamps, diagnostic
chunks/checksums, dead-letter ID, failure classification and audit links.
Repair the cause, inspect the retained record, then follow the
[redrive procedure](operator_incidents.md#dead-letter-diagnosis-and-redrive)
with the same DLQ ID and real broker configuration. Its dry run does not check
existence or replay safety. Verify redrive intent/completion, the new transport
identity and retained logical identity, then observe actual terminal execution
and effects. Retrying a completed redrive must return the existing outcome.
Preserve malformed/integrity failures for review; changing the payload or its
identity to bypass rejection is not redrive.

## Credential, trust, and grant changes under work

Inventory the actual secret provider, version pins, workload identity sessions,
cache TTLs, consumers and reconnect paths. Record references/fingerprints only,
never tokens, passwords, private keys, DSNs containing credentials, or rendered
Secrets. Follow [runtime secrets](secrets.md#caching-rotation-and-outages),
[security rotation](operator_security.md#planned-secrets-signing-keys-and-certificate-rotation)
and [encryption rotation](encryption.md#key-ownership-and-rotation).

For each credential mechanism, submit and keep work active in both tenants,
install a complete valid replacement through the approved secret-delivery
mechanism, and restart/refresh every affected client according to its contract.
Unpinned storage references may refresh after cache expiry; existing streams
keep their original backend. Static configuration, pinned versions and TLS
contexts require recreation. Worker `SIGHUP` reloads tier limits only. Retire
the old credential after the planned overlap and active transfers complete;
prove its rejection at the authority, replacement success, old/new-object
checksums, job completion and continued denial of unauthorized access. For
AWS workload identity, test the actual role/session issuance and expiration
mechanism selected in #161; do not introduce static S3 keys for the drill.

For TLS, install overlapping old/new CA trust in all clients, restart them,
rotate the server leaf/key, exercise every configured hostname and NATS
discovered/reconnect endpoint, then remove old trust and restart clients again.
Test the API upstream/ingress, PostgreSQL, broker, and applicable storage and
identity paths. Record successful new handshakes plus rejected plaintext,
untrusted CA and wrong-hostname controls with a healthy allowed control.
For externally managed provider roots that cannot be rotated by the operator,
record that boundary and the provider's supported trust-update rehearsal;
do not claim an operator leaf rotation was performed there. Retain any
unexercised selected path as a qualification gap.

Rotate OIDC signing keys by publishing the new key before issuing tokens,
retaining old public keys through token lifetimes/cache windows, and verifying
new-key refresh and negative controls. Account for the configured JWKS TTL
and unknown-key refresh interval. There is no per-token revocation lookup.

Exercise RBAC and tenant revocation as separate scenarios for each tenant:

1. Pause claims without changing the queued job and submit protected work as
   a currently permitted synthetic identity. Retain the original principal,
   tenant, job ID and unchanged source/hold state.
2. Atomically replace the relevant policy on every API and worker, removing
   the required RBAC grant or removing/changing the tenant binding. Record
   policy hashes and deployment timestamps; policy reload failure must deny.
3. Release the original job for delivery/retry/redrive. Verify authorization
   rejection before handler/storage effects and retained identity. RBAC
   denial can record terminal failure/DLQ. Tenant mismatch may reject before
   tenant-scoped status can update; reconcile restricted worker/DLQ evidence
   rather than requiring a fabricated succeeded/failed API status.
4. Verify subsequent API denial, no cross-tenant disclosure, and no bytes,
   placement or hold change from rejected work. Work already authorized before
   revocation may finish; label that timing separately rather than counting it
   as a revalidation failure. Keep a permitted control identity working.
5. Do not restore the grant merely to make redrive succeed. Record the intended
   denied terminal/disposition result; only an explicit reviewed policy change
   may reauthorize later work. Token expiration or issuer credential revocation
   alone does not cancel queued jobs because workers retain normalized identity,
   not bearer tokens.

## Three fenced full restores

Perform the following complete sequence independently for the pending-work,
retained-key, and upgrade/rollback scenarios. Use new isolated catalog,
storage, and broker destinations each time. A backup listing or successful
`pg_restore` is not a completed restore.

1. **Capture one boundary.** Stop every API request including reads that write
   access/audit state, CLI/admin/catalog writers, scan/policy producers, worker
   consumption/publication and external namespace writers. Prove the old
   processes cannot restart. Record fence start/completion, outstanding request
   and move IDs, and the coherent recovery-point UTC timestamp. For the
   pending-work set deliberately retain a known nonterminal move and queued
   jobs after stopping the owner; draining all work defeats that scenario.
2. **Retain all planes together.** With the fence held, retain the whole
   PostgreSQL database/all tenant schemas, migration heads/roles/extensions,
   POSIX payloads/staging/sidecars and ownership, S3 object-version inventory
   and selected versions, main/DLQ snapshots with stream/consumer state,
   logical job IDs, placement and hold history, audit records with independent
   checkpoints, configuration/secret-version references, encryption-key
   references and recovery permissions. Hash the archives and manifests;
   record capture times and verified inventory under one recovery-set ID.
   Preserve seven daily sets and prove workload identities cannot delete them.
3. **Select the recoverable set.** Declare incident time and identify the
   latest verified coherent set that can actually restore. Reject incomplete,
   mismatched, expired or unreadable components. Record later acknowledged
   changes individually, with timestamps and intended replay or loss
   disposition. A newer database dump cannot advance the recovery boundary
   independently of objects, queue, holds and audit state.
4. **Restore while fenced.** Follow [operator restore](operator_lifecycle.md#restore)
   into empty, isolated encrypted resources with the compatible saved binary,
   all tenant schemas and pgvector. Restore storage bytes/versions/sidecars and
   both broker streams/consumers before clients can create empty streams.
   Prevent writable application startup from auto-migrating the restored
   catalog. Check backup hashes, ownership, migration heads, stream sequence
   bounds/configuration/consumer positions and selected object versions before
   running the application. Record scheduler-disabled configuration and
   absence of unintended publishers.
5. **Exercise retained keys.** In that scenario, rotate the selected encryption
   key/version after taking the pre-rotation set, retain original access, and
   restore that set under its original key versions using the recovery
   identity. Prove new writes use the new configuration and pre-rotation
   objects/backups remain readable. Include POSIX/catalog/broker volume or
   snapshot keys and S3/backup KMS dependencies actually used by the set.
   Record real key references and decrypt/read evidence; merely listing a
   retained key is insufficient. Prove a fixture recovery identity without
   required key access is denied while the authorized recovery identity can
   restore; preserve the usable key and backup throughout this negative
   control. Do not disable/delete keys still needed by
   any retained version or set.
6. **Reconcile changed generations.** Record original and restored backend
   generations for the pending move. A POSIX restore changes inode/device/
   change-time evidence; S3 restoration may create a new version. Identical
   SHA-256 bytes do not authorize rewriting a journal generation token.
   Demonstrate safe quarantine or blocked cleanup for an actual mismatch;
   verify both copies, source preservation and the explicit reason. Keep the
   old worker fenced. The storage/catalog owners must review a concrete
   reconciliation using verified bytes, placement, holds and journals.
   Preserve old evidence and run fresh validation after the repair. If the
   supported procedure cannot safely resolve it, record a blocker, implement
   and test the fix, then repeat the restore; do not silently drop the job or
   count unresolved required work as restored.
7. **Verify before reopening.** Complete every check below, allow only the
   bounded recovery worker activity needed to finish reviewed pending work,
   and preserve its audit/job effects. Verify a new authorized job and exact
   object round-trip for each tenant. Record final reconciliation completion,
   owner authorization, and the time the service can safely accept work.

### Pre-resume reconciliation

Ordinary admissions and unrestricted writers remain fenced until the following
matrix is complete for **both** tenants. Controlled recovery writes must be
named in the reviewed plan and recorded. Read-only inspections must use an
appropriate catalog/backup interface; normal HTTP reads may write audit/access
state and cannot run unnoticed inside the original snapshot fence.

| Plane | Evidence required before resume |
| --- | --- |
| Object integrity | Full expected/actual inventory, byte counts and SHA-256 hashes across both tiers, exact logical keys and S3 versions, POSIX metadata/sidecars, no unexplained missing/extra objects or source loss. Check every campaign object, not only a smoke sample. |
| Catalog and movement | Both tenant owners/schemas/migration heads, placements and content manifests, pending/terminal journals, source/destination generations, resolved reviewed quarantines, no duplicate destructive effects. |
| Governance | Held-object identity and hold history intact; attempted held overwrite/delete denied; budget/access history and policy state agree with the selected boundary and recorded replay. |
| Audit | Complete ordered exports for both tenants verify against independently retained checkpoints; recovery/redrive/repair records join to the original event history. Self-consistency without an independent anchor is insufficient. |
| Queue | Saved/restored main and DLQ inventory, sequence/configuration and durable consumer positions, every accepted logical job matched to outcome/disposition, retries and ACKs accounted for, original principal/tenant retained, no lost acknowledged job outside accepted RPO. |
| Security and topology | Matching current tenant/RBAC policies, default/unmapped denial, known foreign object/job/hold denial, valid TLS including reconnect, current encryption evidence, recovery-key availability, one selected worker with concurrency two, scheduler/HPA/KEDA disabled. |
| End-to-end readiness | Actual permitted read/write and protected job complete in both tenants, expected denied controls hold, private telemetry/alerts operate, cause/fix and all replay/loss/quarantine dispositions reviewed. |

Record expected and actual values, checker/harness revision, start/end time,
operator and raw evidence references for each check. A boolean assertion
without observations is insufficient. Preserve failed and subsequent fresh
reports; an earlier consistency report is historical evidence, not validation
of the restored namespace.

### Timing and loss calculations

Record timezone-qualified UTC timestamps for the coherent recovery point,
backup fence/capture/verification, first external failed probe or declared
interruption, fault injection/removal, restore start/end, reconciliation
start/end, owner approval and safe service resumption. Retain the actual probe
stream and step timing. Use the earlier of first external failure and declared
interruption as incident/outage start when both exist; do not start the clock
at the later beginning of an operator command.

For **each** full restore:

- `RPO seconds = incident time - latest verified coherently recoverable boundary`
  must be nonnegative and **<= 86,400**.
- `RTO seconds = safe service resumption time - incident time` must be
  nonnegative and **<= 3,600**. Resumption is after both-tenant reconciliation
  and the required owner decision, not database startup or a healthy pod.
- Enumerate acknowledged changes after the boundary, with acknowledgement
  timestamps, counts, identities, verified replay outcome or explicit accepted
  loss disposition. Verify zero unexplained loss; retained-source replay time
  needed for safe service counts in RTO.

If the failed probes predate the selected recoverable boundary, explain and
select a defensible coherent point rather than accepting negative RPO. A
missing timestamp, unknown clock offset, incomplete check, or unverified
backup is an unresolved result. Report each trial independently; averaging
three RTOs or measuring a database-only recovery does not satisfy the gate.
Retain outage timing and reconciliation for fault/rotation/upgrade trials too.

## Upgrade and rollback rehearsal

Retain old and candidate image digests, schema/migration inventories for both
tenants, values/secret references, job-envelope versions, and the pre-upgrade
coherent recovery set. Review the [supported upgrade procedure](operator_lifecycle.md#upgrade)
and actual migration code. Rehearse on a full isolated restore with pending
work, recording the exact migration commands and their start/end times.
Do not discover downgrade compatibility on the original environment.

1. Prove old producers are fenced for incompatible changes, then apply the
   candidate deliberately to every tenant schema. Record any new accepted
   work and resulting data/configuration/envelope changes. Verify both tenants'
   bytes, holds, audit continuity, pending moves/jobs and security controls.
2. Choose the rollback path from **tested compatibility evidence**. If the
   previous binary is compatible with the current schema, configuration and
   pending envelopes, restore its retained application/configuration release
   and rerun complete reconciliation. Retain the evidence for that claim;
   successful process startup alone is insufficient.
3. If compatibility is unknown or a downgrade would discard state, keep
   writers fenced and restore the **whole pre-upgrade coherent set** with its
   matching binary. This is the third independent full-restore trial. Account
   for every accepted post-boundary write/job and its replay/loss disposition.
   Even if compatible application rollback succeeds, perform this independent
   full restore to qualify the fallback required by this campaign.
4. Measure the actual upgrade/rollback interruption, check changed-generation
   move handling, complete the pre-resume matrix, and obtain the accepted
   owner's decision. Preserve the original failed state and known-good set.

A Helm rollback changes release resources; it does not restore database data,
externally managed Secrets, object versions, queue state, or encryption-key
access. Never mix an old catalog with newer object/queue state, bypass
data-preservation downgrade guards, rewrite generations, or delete
infrastructure as rollback. An unresolved compatibility or recovery result
blocks qualification until fixed and rerun.

## Evidence retention and qualification decision

Use the [M5 evidence contract](evidence/m5/README.md). Bind each trial and
recovery set to the exact specification commit/acceptance, source commit/tree,
artifact/registry image digest, candidate manifest hash, input/rendered
configuration hashes, environment/account/cluster/namespace and concrete
resource identities. Record operator/reviewer, tool/harness revision,
timestamps, fault target, expected/actual results, command exit statuses,
RPO/RTO inputs, failures, fixes, independent reruns and unresolved gaps.

Keep a sanitized durable manifest plus immutable sanitized raw logs, probe
samples, metric exports, requests/responses, job/journal history, object/hash
inventories, broker state, policy/secret-version references and independent
audit checkpoints. Use repository evidence or durable restricted artifact
storage with checksums, retention owner and at least 90 days after pilot exit.
Test reviewer access to linked evidence. A machine-local ignored directory,
an expiring link, or an unbacked `passed` field is insufficient. Keep secrets
and key material in their approved recovery system; evidence holds references
and fingerprints, not credentials. Preserve raw observations after
sanitization without erasing failure times or inconsistent values.

The offline evidence tool checks the bounded record contract and campaign
completeness; it cannot attest that a fault ran, a hash was computed honestly,
an external link is durable, or a signer approved the result. A named accepted
owner reviews the raw evidence and records a dated qualification decision.
Any missing prerequisite, unmet independent trial, failed reconciliation or
target, unexplained loss/quarantine, or untested control remains a blocker.
Local tooling tests qualify the tooling only.

Hosted CI is **skipped: user instruction; known GitHub billing/spending
restriction**. Do not dispatch, rerun, watch or poll hosted workflows. This
standing suspension does not satisfy any explicit release-qualification gate;
retain that gap once and continue authorized local preparation and validation.

## Offline evidence checker

Create a new bundle directory and generate its unobserved record template. The
checker uses only Python's standard library and never contacts a service:

```bash
mkdir recovery-164
python scripts/recovery_qualification.py init --output recovery-164/campaign.json
# Populate observations and add sanitized evidence files under recovery-164/.
python scripts/recovery_qualification.py check \
  --campaign recovery-164/campaign.json --output recovery-164/report.json
```

`init` records no successful observations. `check` returns **1** for incomplete
or invalid evidence and **0** for `complete-for-review`. Both commands refuse
an existing output path, preserving prior records. Store subsequent reports at
new paths. The report always says `production_qualified: false`,
`resume_authorized: false`, and `faults_injected: false`. It is an offline
intake check for the named owner's review; it neither performs a recovery nor
authorizes service resumption or issue closure.

Populate the generated fields as follows. JSON duplicate keys and nonfinite
numbers are rejected. A missing/false observation or skipped trial cannot pass.

| Field | Required content |
| --- | --- |
| `identity` | Exact source commit (40 lowercase hex digits), registry manifest `image_digest` (`sha256:` plus 64 hex digits), candidate manifest/configuration manifest/specification SHA-256, and environment ID. Copy this identity into every trial; changed source/configuration/environment starts a new campaign. |
| `identity_artifacts` | Map each of `candidate_manifest_sha256`, `configuration_sha256`, and `specification_sha256` to an artifact ID whose verified bytes have that hash. Preserve the actual frozen files. The configuration manifest inventories rendered inputs and their hashes; review its consistency with #160/#161. |
| `prerequisites` | Each named prerequisite has `observed: true` and evidence IDs for accepted specification, frozen candidate, verified live staging and a complete catalog-partition inventory. The checker cannot verify signatures or the truth of those records. |
| `tenants` | The complete retained partition inventory, including both pilot tenants and any default/former tenant partitions present in the recovery boundary. Membership alone is not the partition inventory. Extend every restore's `tenants` map to exactly match it. |
| `artifacts` | Object keyed by evidence ID, each containing a bundle-relative `path` and lowercase SHA-256. Files must exist inside the bundle; absolute/traversing/escaping paths and duplicate file paths or content hashes fail. Hashes are streamed, so archive size does not determine memory use. |
| `runs` | The generated 28 records, each with distinct ID, operator, exact identity, UTC `started_at`/`completed_at`, `result: passed`, its own `evidence` IDs and every generated `checks` observation true. Each trial's raw files must contain its own timestamps/identity and may not be reused as another trial's evidence. Shared frozen inputs belong in identity/prerequisite references. Repeated trials of the same kind must have non-overlapping intervals. |
| `tenant_evidence` / `connection_evidence` | Every run maps each pilot tenant to supporting artifact IDs from its own `evidence`. Credential rotation separately names API/OIDC, PostgreSQL, JetStream and S3 workload identity; CA rotation names API/proxy, PostgreSQL, JetStream, S3 and OIDC. Populate the generated connection keys; other runs use an empty connection map. A single report may contain multiple clearly identified subcases. |
| `restore` | Distinct recovery-set and isolated target IDs, coherent `boundary_at`, earliest `incident_at`, final `reconciled_at`, and `writers_resumed_at`. UTC timestamps use `YYYY-MM-DDTHH:MM:SS[.fraction]Z`. The checker calculates RPO/RTO; callers cannot supply a passing duration instead of timestamps. |
| Restore tenant counts | Equal integer expected/verified counts for objects, holds, audit events and pending jobs, with zero missing/extra objects or hash mismatches. Both pilot tenants need nonempty object/hold/audit fixtures; pending and upgrade/rollback restores also require nonterminal jobs in both. Additional inventoried partitions may have zero expected counts. Raw inventories must substantiate every count and disposition. |
| Restore loss accounting | Nonnegative integer `acknowledged_changes_lost` and a `loss_manifest` artifact ID also included in that trial's evidence. Even a zero-loss manifest must identify the trial and reconcile acknowledgements/replay. Human review verifies timestamps, contents and accepted loss against the recovery boundary. |

The checker rejects unknown scenario kinds, insufficient valid repetitions,
mismatched identities, absent/tampered/reused artifacts, invalid/overlapping
trial windows, unobserved checks, missing tenant partitions, invalid counters,
reused restore sets/targets, premature resumption, and any individual restore
above 86,400 seconds RPO or 3,600 seconds RTO. It requires reconciliation before
safe resumption and both timestamps inside the measured trial. The truth and sufficiency of per-service CA
and credential subcases, per-tenant revocation and DLQ subcases, full object
manifests, compatibility results and evidence durability still require raw
record review; a grouped boolean does not prove them automatically.

Do not edit a failed trial into success or delete its raw evidence. Retain the
failed campaign/report and linked finding/fix disposition with the fresh
campaign; failed entries intentionally keep their containing campaign
incomplete. A candidate change requires fresh candidate-bound qualification.
Copy durable restricted evidence into a reviewer-authorized bundle for offline
checking, then retain it with its access/retention controls. The checker does
not fetch URLs or assert that files left on the local machine are durable.
Do not include credentials in input JSON: reports omit input values, but the
input bundle itself must already be sanitized.

The [local development evidence](evidence/m5/ticket-164/README.md) records
checker regressions and isolated recovery tests. Those results demonstrate
application/tool behavior in fixtures, not execution of the live campaign above.
