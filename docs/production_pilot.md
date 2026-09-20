# First production deployment and pilot specification

**Specification:** `m5-pilot-v1`, revision 1, 2026-09-20.

**Tracking:** [#159](https://github.com/melliott18/CogniStore/issues/159),
under [M5 #155](https://github.com/melliott18/CogniStore/issues/155).

**Status:** proposed engineering baseline; owner acceptance and qualification
are pending. This document does not approve deployment or pilot entry.

This selects one production-configured, synthetic-data deployment for
qualification. Its targets are engineering choices, not inferred customer
requirements, achieved results, or a customer SLA. M1–M4 remain completed
delivery history; their [closeout evidence](evidence/m4/README.md) does not
qualify this candidate. The [roadmap](roadmap.md) gives the remaining sequence.

## Ownership, review, and version control

Mitchell Elliott ([melliott18](https://github.com/melliott18)) is the **proposed**
accountable owner below, based on repository maintainership. Nomination does
not imply accepted operational duties. Before #159 is accepted, record his
explicit acceptance or replacement names in this table and link the review of
the exact specification commit. Do not infer approval from an unchecked ticket,
a generated mirror, automated review, or this document's presence on a branch.

| Responsibility | Proposed named owner | Acceptance / review record |
| --- | --- | --- |
| Deployment, database, broker, storage, backups, on-call and recovery | Mitchell Elliott | Pending |
| Security, tenant policy, encryption, data admission and audit review | Mitchell Elliott | Pending |
| Workload, UAT, SLO evaluation and pilot expand/fix/stop decision | Mitchell Elliott | Pending |

One person may accept these roles for this internal pilot; no independent
security review or staffed backup operator is implied. If the accepted operator
is unavailable, pause admissions and mutable work. A replacement must explicitly
accept the handoff. #166 records the restricted contact/escalation route and
staffed hours before entry; never store personal contact details or credentials
here. Real alert receipt and acknowledgement are qualification gates.

Review the topology, workload, numerical gates and exclusions **before** #160
freezes the candidate and before #163–#165 collect qualification results.
Acceptance must identify the specification revision, Git commit, reviewer,
UTC date, decision, and any resolved objections. A change to backend, filesystem,
providers, tenant policy, concurrency, data bounds, target or measurement cohort
increments this revision and records affected requalification. Never relax a
threshold after a failed run and count that run as passing.

## Selected deployment

| Dimension | Decision for this pilot |
| --- | --- |
| Deployment | Production Helm in one isolated AWS `us-east-1` environment; separate staging and pilot namespaces, catalog databases, queue identities, volumes and buckets. No shared mutable dependencies with another campaign. |
| OS/filesystem | Linux amd64 nodes; Debian bookworm application runtime with CPython 3.12 and native libmagic. Case-sensitive ext4 on encrypted block volumes, trusted service-owned directories, no external namespace writers. Exact kernel, image and filesystem/mount identities are frozen in #160/#161. |
| Application placement | One API/UI replica and one worker replica pinned to the same selected node. Both mount the **same** encrypted 200 GiB RWO PVC at `/var/lib/cognistore/hot`. Migration/smoke and temporary rolling-update pods must be able to mount it on that node. RWO is not ReadWriteOncePod. |
| Storage tiers | `hot`: POSIX root above. `warm`: native AWS S3, versioning and SSE-KMS, verified HTTPS, workload identity, `auto_create_bucket: false`. One pre-created bucket per environment; the same logical bucket name exists under the POSIX root. Moves keep bucket/key and change backend. Two S3 configurations pointing at the same namespace are not distinct tiers. |
| Catalog | One external PostgreSQL 16 primary, pgvector >=0.8.0, encrypted 100 GiB durable capacity and backups, verified TLS. All tenant schemas and control-plane state use this database. No SQLite catalog, no catalog read replicas, and no HA claim. |
| Broker | One external NATS JetStream server with encrypted 20 GiB file storage and backups; verified TLS including reconnects, `handshake_first: true`, restricted publishers/consumers. Separate main/DLQ streams and durable consumers per environment; freeze stream limits and ACK/retry settings. At-least-once delivery is retained. |
| Tenant model | Two synthetic tenants, `pilot-a` and `pilot-b`, with exact trusted issuer/subject mappings, separate catalog schemas and storage prefixes. Default/unmapped identities are denied pilot access. Identical logical keys in both tenants are deliberate isolation fixtures. |
| Identity / network | Operator-managed OIDC issuer and audience, explicit reader/writer/operator/auditor/admin bindings. Private access, TLS at ingress and verified API upstream, enforcing CNI NetworkPolicy, restricted DB/broker/storage/identity/telemetry egress. An authenticating reverse proxy must forward the individual browser user's valid access token to `/v1`, with no shared service identity. No anonymous `/v1` access. |
| Scheduler / scaling | `scheduler.enabled: false`; scheduled envelopes must be rejected. HPA and KEDA disabled; fixed replicas and worker `maxInFlight: 2`. No multi-node POSIX access, recurring schedules, or distributed SQLite coordination. Node loss needs fenced recovery and volume reattachment. |
| Runtime resources | API and worker each request 2 vCPU/2 GiB, limit 4 vCPU/4 GiB. Selected node has at least 16 vCPU/32 GiB allocatable to permit rollout overlap and OS headroom. DB and broker each have at least 2 vCPU/4 GiB. #161 records actual allocations and placement. |

Use the [Helm production prerequisites](kubernetes.md#production-prerequisites),
[reference architectures](reference_architectures.md),
[POSIX containment assumptions](posix_containment.md), and
[encryption contract](encryption.md). These selections are inputs to #161,
not an executable values file: that ticket supplies real resource identities,
mounts, credentials, trusted issuer, routing and immutable version pins. The
default chart alone does not provision them. All selected service patches and
dependency/image digests must pass #158/#160; versions here select a family,
not permission to deploy an unreviewed old image.

## Feature and boundary decisions

| Boundary | In scope and required behavior | Explicit exclusion |
| --- | --- | --- |
| Default API and providers | Use the shipped `cognistore-api` composition: metadata-only `AskService`, `/ui/` discovery and admin. Verify metadata results, relevant/no-match queries, citations, downloads and explicit unavailable-provider diagnostics. | Full semantic Ask, keyword/vector retrieval and answer synthesis; the sample's offline providers do not qualify production quality. |
| Production provider composition | No embedding or answer model provider required; omit the root `embedding` block as well as Ask providers, so policy does not activate a model independently. No provider credentials or outbound model traffic. Enabling providers later requires a custom assembled gateway/AskService, tenant-aware providers, a new spec and fresh quality/security gates. | Merely installing pgvector or a Python extra does not enable retrieval or generation. |
| Keyword ownership | No Tantivy writer or index path in this deployment. PostgreSQL/catalog state remains authoritative. | Shared multi-writer Tantivy. Future keyword adoption needs one writer per tenant-local path, explicit reader refresh and a rebuild/recovery owner. |
| Documents / PII | Synthetic PDF/DOCX extraction and opaque binary storage. PII detection disabled; no personal/customer/confidential content is admitted. Synthetic negative fixtures may test boundaries in isolated staging. | PII-enabled content search: those scans withhold extracted text/properties from persistence, do not feed content search/embeddings/Ask, and do not erase old indexes or source bytes. PII detection is not a data-admission guarantee. |
| Policy / governance | Deterministic size/content rules using available features, preview then explicit execution, legal holds, consistency/repair review and audit export/checkpoints. Fail closed for required unavailable signals. | Learned/LLM production decisions, automatic repair or orphan cleanup without review, OCR and regulatory certification. |
| Dependencies | Base runtime includes native libmagic and selected S3 client dependencies. Pin resolved packages and retain SBOM/install manifest. | Azure extra and Azure backend are not installed/qualified; GCS is not selected; embeddings extra is not needed. Their implemented driver contracts remain historical delivery, not this pilot's qualification. |
| Encryption evidence | Current reviewed `catalog`, `queue`, `runtime` **and `storage`** attestations; POSIX requires storage evidence. Include disks, snapshots, backups, temporary/shared memory and swap policy. Disable node swap. Exercise CA/key rotation and retained-key restores. | Attestation validation alone is not proof of physical encryption. Example owners/expiry/evidence in `examples/encryption.production.json` are not usable attestations. |

See [REST composition and limits](rest_api.md), [PII/search behavior](pii_detection.md),
[keyword recovery](keyword_search.md#rebuild-and-recovery), and
[extraction limits](document_extraction.md). The content UI `/ui/` has no OIDC
login flow and does not itself attach an Authorization header; its browser
workflows require the per-user authenticating proxy selected above. #161 must
configure it, and #162/#163 must test issuer/audience/tenant binding, session
expiry, sign-out, forged headers and cross-site request protection. It must
not turn every browser request into one privileged service principal. Failure
to qualify this proxy blocks browser discovery acceptance; switching to API-only
discovery requires a reviewed scope revision. The separate
[admin UI](admin_ui.md) accepts an in-memory bearer token; never retain tokens
in screenshots. The Ask view must show available metadata results; the Search
view's keyword/vector tabs may filter them out and must show empty results and
unavailable-provider diagnostics. No generated answer is expected. A rendered
HTML page is not search acceptance.

### Temporary storage and memory budget

The chart's `/tmp` is a **256 MiB memory-backed** `emptyDir` per pod. Retain that
bound, allow at most four simultaneous object uploads and two in-flight worker
jobs, and measure `/tmp`, shared memory and process RSS separately. API upload
handling may retain the whole body plus a copy; streaming storage is not proof
of bounded API memory. The selected 16 MiB maximum gives 64 MiB of concurrent
upload payload, before copies and parser/driver overhead. No qualifying pod may
OOM or exhaust temporary space.

Keep S3 multipart threshold and chunk size at 8 MiB for this profile. Provide a
separate 256 MiB memory-backed `/dev/shm` mount for parser shared segments in
#161; count both memory volumes against pod limits. With two jobs, reserve at
least two 25 MiB parser inputs plus two 4 MiB outputs and adapter/process
overhead. These are sizing inputs to measurement, not peak-memory guarantees.
Warn at 60% and stop new admissions at 75% of either memory-volume limit or
80% of pod memory for five minutes; any exhaustion stops the run immediately.

GCS spooling allowance is **zero because GCS is excluded**, not because it never
uses disk. Selecting it would require encrypted disk sized for concurrent full
object spools, retries and free-space margin, plus restart-recovery qualification.
Objects above 16 MiB are excluded even for direct CLI/backend ingestion; REST's
16 MiB and JSON's 256 KiB limits remain unchanged. Oversize rejection is tested.

## Workload and growth assumptions

Corpus generator, seed and SHA-256 manifest must be versioned with #163/#165.
Use these exact size bins for the 100,000-object qualification dataset, split
equally between tenants and initially placed in `hot`:

| Object size | Share by count | Count |
| --- | ---: | ---: |
| 4 KiB | 60% | 60,000 |
| 64 KiB | 30% | 30,000 |
| 1 MiB | 9% | 9,000 |
| 16 MiB | 1% | 1,000 |

The logical payload is 28,426,240,000 bytes (about 26.474 GiB); metadata, sidecars,
versions, replicas and backups are additional. Use 80% opaque synthetic objects,
10% valid PDF and 10% valid DOCX by count. Large valid documents must stay under
the parser's 4 MiB normalized-output limit; separate corrupt, encrypted,
unsupported and output-limit fixtures test explicit failures without counting
as successful extraction. Retain size and MIME histograms, including their joint
distribution, rather than assuming file padding produces valid documents.

| Load | Required pattern |
| --- | --- |
| Nominal foreground | 10 requests/s offered, 16 outstanding requests maximum, at most four PUTs simultaneously. Mix: 40% GET/HEAD (half each), 20% catalog reads, 15% metadata Ask, 15% PUT (two-thirds replacement, one-third create), 5% DELETE, 5% policy preview. Creates and deletes each account for 5% of total traffic. Use the same size mix for reads and writes. |
| Hot spots | 80% of requests target 20% of each tenant's keys. Run same-key PUT/DELETE and hold conflicts in a separately labeled correctness cohort with explicit expected conflict outcomes. |
| Background | Two in-flight jobs maximum. Submit one scan per tenant every five minutes over a rotating <=100-object prefix; one policy pass per tenant per hour over <=1,000 objects. Each direction must move at least 10,000 objects in the soak, with the size mix above. Select explicit placement rules for those prefixes in each direction; the default size threshold alone would move only large objects. Poll every accepted job to terminal state. |
| Burst / saturation | 30 requests/s offered, at most 32 outstanding, for 15 minutes; still at most four PUTs. Separately drive the queue to its admission limit and confirm visible rejection and recovery. Do not include deliberately overloaded/fault cohorts in nominal percentiles. |
| Capacity headroom | Repeat a two-hour nominal-load run at 200,000 objects / about 52.948 GiB logical payload, including a hot-to-warm-to-hot movement pass; extend until the pass finishes. This exceeds twice the pilot starting volume and tests twice the pilot object cap. |
| Pilot growth | Start at 50,000 objects / about 13.237 GiB; plan <=2% net object/byte growth per day. Hard cap 100,000 live objects and 30 GiB logical payload across both tenants; either cap stops admission. |

Foreground creates/deletes must balance during the soak: use a recorded bounded
key pool and replace/delete eligible unheld objects to keep the corpus at the
declared scale. Scans accumulate per-object results for a prefix; keep their
prefixes bounded instead of submitting a whole-corpus scan. For the pilot,
impose the growth/cap externally through the
controlled harness and operator admissions; the application does not enforce
this specification automatically. Quotas on source versions/backups also count:
provision 200 GiB hot capacity, budget at most 200 GiB of S3 versions/payload,
and at least 500 GiB independent encrypted backup capacity. Measure actual usage
daily; changing retention or deleting recovery points to meet a cap is forbidden.
No data can be the only copy of information someone needs.

## Required user and operator workflows

#163 must record expected/actual outcomes, tester, timestamps, candidate/config
identity and sanitized logs/screenshots for each tenant workflow in **both** tenants.
Repeat every positive workflow at least three times per applicable interface;
exercise each applicable role denial and each negative case at least once per
tenant. All required cases must pass; skip is not pass.

1. API and SDK upload, HEAD, exact-byte SHA-256 download, closed/open/suffix range
   reads, delete, pagination, metadata discovery and no-match results. Use an
   actual browser for Ask citations/downloads, unavailable-provider messages,
   empty/error states and admin policy/job/audit flows.
2. Trusted operator CLI upload/download/scan and hot↔warm moves with durable
   move IDs in a **separate disposable default-tenant fixture**, with its own
   catalog, POSIX root and S3 bucket. It has no pilot state/credentials. The
   ordinary CLI has no tenant selector for these commands; it cannot stand in
   for named-tenant operations or HTTP JWT enforcement. Use authenticated
   API/SDK actions for pilot-a/pilot-b scans and policy movement. CLI
   consistency/repair acceptance also uses only the isolated legacy fixture:
   its `--tenant` scope label does not select an API tenant partition. Named
   tenant reconciliation uses scoped admin APIs or a reviewed harness explicitly
   binding `catalog.for_tenant` and tenant-scoped storage drivers. Never point
   ordinary CLI writers at the named tenants' storage/catalog.
3. Queued API/SDK scans and policy passes from 202 through terminal outcomes and
   actual storage effects. For uncertain submissions reconcile returned/recorded
   jobs before resubmission: REST actions have no caller-provided idempotency key.
   Check retry, DLQ/redrive and worker permission revalidation.
4. Preview policy/repair without object or placement mutation; inspect reasons,
   execute only reviewed synthetic changes, verify holds and audit continuity,
   then inspect state in the browser. Access/audit logging is still allowed.
5. Reader/writer/operator/auditor and separate admin principals; expired and
   wrong-audience JWTs, revoked roles/tenant grants, known foreign object/job/hold
   IDs, cursors, citations and default-tenant namespace aliases. No cross-tenant
   disclosure/mutation and no held-object deletion/overwrite are allowed.
6. Supported extraction, unsupported/corrupt/encrypted documents, API/JSON size
   boundaries and missing providers. Verify honest failure/unknown states and
   continued processing of unrelated objects. No synthetic model output counts
   as semantic search or answer-quality evidence.

## Numerical qualification gates

Run on the exact #160 candidate and #161 production configuration. Allow a
30-minute warm-up, then **72 continuous measured hours** at nominal load with
background work, at least 1,000,000 eligible foreground attempts, at least
10,000 samples per operation class and 1,000 per object-size read/write bin.
If a minimum is unmet, extend the campaign; do not pool different revisions.
Run burst, capacity and fault campaigns separately with labeled boundaries.
Pilot results use the same availability, latency, successful movement/scan,
resource, queue and coverage formulas over the declared active windows. The
dedicated burst, doubled-capacity, movement-backlog and recovery campaigns are
qualification entry gates; they need not be repeated during the pilot unless
a material change requires requalification.

| Gate | Pass/fail target and measurement |
| --- | --- |
| Integrity / isolation | Zero checksum mismatches, unexplained missing/extra objects, lost acknowledged state outside the accepted RPO, hold bypasses, cross-tenant leaks or duplicate destructive effects. Full manifest/catalog/hold reconciliation before/after soak, capacity and every recovery drill. |
| External availability | >=99.5% expected successful foreground responses / **all offered eligible attempts**, measured at the client, including connection failures, timeouts, unexpected 4xx and rejected requests. Count abandoned load-generator requests as bad. Deliberate negative/conflict/fault tests are separate cohorts. Maintenance inside measured hours counts as downtime. |
| Foreground latency | Client end-to-end p95 <=500 ms and p99 <=2 s for HEAD, catalog reads and policy preview; metadata Ask p95 <=2 s / p99 <=5 s; PUT/GET <=1 MiB p95 <=2 s / p99 <=5 s, >1 MiB p95 <=5 s / p99 <=10 s; DELETE p95 <=2 s / p99 <=5 s. Timeout 30 s counts bad; keep failures in the report. Report each operation and size bin, never only pooled quantiles. |
| Offered load / completion | Sustain 10 offered requests/s with >=9.95 expected successes/s over the measured nominal window and no rising backlog. Missed arrival slots count bad even at the concurrency ceiling. Burst must recover nominal latency and queue age within 10 minutes after load falls. |
| Movement | >=99.9% successful completed calls and >=99% of all completed calls successful within 30 s; retries/replays remain in denominators. Also require every logical move to reach verified terminal success or an explicitly expected correctness conflict, with zero unresolved moves after drain. |
| Movement throughput | Dedicated movement-only backlog for >=60 minutes in each direction, >=10,000 successful moves per direction, exact size mix. >=99% of non-overlapping five-minute windows achieve >=1 verified successful move/s. A stalled window remains eligible and fails. Report this separately from shipped overlapping-window telemetry. |
| Scan lag | >=99% of at least 1,000 scan attempts succeed within 300 s of original publication (queue/retry time included), plus all acknowledged scans reconciled to terminal outcomes. Unknown timestamps are a coverage failure. Not a search-index freshness claim. |
| Resources / capacity | At nominal and doubled corpus size: >=30% free hot/catalog/broker disk; API/worker CPU p95 <70% of limits, RSS p95 <70% of limits; no OOM, disk/temp exhaustion or monotonic leak. After drain, RSS and temporary usage <=110% of post-warm-up baseline. |
| Queue / notification | Freeze 10,000-message main-stream cap; warn at >=5,000 pending for 2 min, stop new submissions at >=8,000 or oldest pending age >300 s for 5 min. Any admission rejection alerts immediately. Every induced alert must reach the accepted operator within 5 min, be acknowledged within 15 min, include a working runbook, and send recovery notification within 5 min of clearing. |
| Evidence coverage | Client results and private telemetry cover >=99.9% of the measured period, with no unexplained gap >60 s. Empty denominators, missing samples, unknown queue age and missing environment identities are inconclusive and block acceptance. |

Use nearest-rank quantiles over **all eligible offered attempts**, timed from
scheduled arrival through full response completion, including client wait.
Retain actual elapsed durations and status for every observed response. For
the gate calculation, score every unexpected failure, 30-second timeout or
abandoned/unsent arrival as at least 30 seconds; fast failures cannot improve
the latency result. Keep the independent availability denominator unchanged.
Use exact counts for ratios, with numerator/denominator and time boundaries in
each report. Capture
CPU, RSS, disk, connections, temporary usage and backlog at least every 15 s;
collect byte throughput and actual infrastructure consumption for capacity
planning. This profile chooses no financial-savings claim or cost SLO.

The shipped [SLO model](slo_model.md) retains its definitions and rolling
30-day window. Its API metric counts completed non-5xx responses, including
4xx, and cannot see requests that never reach the API. Report it separately
(target 99.9%); neither the 72-hour run nor 14-day pilot establishes 30-day
attainment. Tenant API `/metrics` returns 404: #161/#165 must provide private
ingress/client, worker and dependency telemetry without disabling tenancy.
The shipped queue alert's `>10000` threshold is insufficient for a 10,000 cap;
the lower thresholds above and real delivery routes must be deployed and tested
before entry. They are requirements for #165, not changes made by this document.

## Recovery objectives and stop/rollback policy

**RPO <=24 hours; RTO <=60 minutes.** RPO is incident time minus the latest
verified, coherently recoverable cross-plane boundary, not the age of a database
dump. RTO starts at the first failed external probe/declared interruption and
ends only after both tenants pass byte/placement/hold/audit/job reconciliation
and the service can safely accept work. Retained acknowledged changes within
the RPO may require replay from the synthetic source manifest; report their
count and timestamps explicitly. No silent loss is acceptable.

Take a fenced coherent recovery set at least every 24 hours and before upgrades;
retain at least seven daily sets outside the application's deletion credentials.
Use [the cross-plane backup procedure](operator_lifecycle.md#backup): include
all tenant schemas, POSIX bytes/sidecars, S3 versions, KMS access, main/DLQ stream
and consumer state, config/secret-version references and independent audit
checkpoints. Stop **all** API/CLI/worker access that can persist audit or access
state, not just PUT/DELETE. Target <=5 minutes of admission downtime per backup;
include it in measured availability. No scheduler/index backup is needed for
the excluded components. A stale or unverified backup stops admission.

#164 must complete at least three independent full restores, one containing
pending nonterminal work, one using retained pre-rotation keys, and one after
an upgrade/rollback. Each must meet RPO/RTO without averaging. Also repeat
worker-kill, database/broker/storage disconnect and lost-response/retry cases
three times each, and credential/CA rotation plus grant revocation once each.
Prove restored-generation mismatches quarantine/reconcile work before destructive
cleanup. Roll back only to a tested compatible application/schema/config set;
if incompatible, fence writers and restore the coherent recovery set. Never
use infrastructure destruction as rollback.

Stop admissions and fence mutating work immediately for any integrity, tenant,
hold or audit failure; unexpected sensitive data; encryption/TLS control failure;
expired attestations; exhausted storage; or an unaccepted/unavailable operator.
Also stop for a hard data cap, queue/temp/memory threshold above, >1% unexpected
foreground failures over 5 minutes with >=100 attempts, three consecutive
30-second failed external probes, or telemetry missing for >5 minutes. Preserve
evidence and state, notify the owner, and follow the
[incident runbooks](operator_incidents.md). Only the named accepted owner can
authorize resume after cause/fix, integrity reconciliation and affected gates
pass again. A software rollback does not itself establish data safety.

## Pilot entry, duration, and exit

The cohort is **at most five internal testers**, using the two synthetic tenants;
the accepted owner records the roster privately before entry. Each role must
be exercised even if a tester holds separate test identities. No external
customer, real PII, important production data, or unsupervised public access.

Entry requires accepted owner/spec review; fixed #156/#157 with regressions;
passing hosted matrix #158; frozen candidate #160; verified staging #161;
passing signed audit #162, UAT #163, recovery #164 and load/alerts #165; current
backups/attestations; and a named #166 go/no-go decision linking those artifacts.
Administrative issue closure, a local ignored report or historical M4 evidence
cannot substitute. All blockers must be resolved; material changes requalify.

Run **14 consecutive calendar days**, including at least ten staffed eight-hour
shifts (80 active hours). Maintain the nominal synthetic foreground and
background pattern throughout staffed windows, alongside human tasks: at least
2,880,000 offered foreground attempts, 10,000 per operation class, 1,000 per
read/write size bin and 1,000 scan attempts. Count missed arrival slots as bad;
do not reduce load when the service fails. The owner
declares staffed UTC windows before the run; collect service/SLO evidence for
all those windows, including failures and pauses. Outside those windows stop
admissions and drain/pause mutations. Daily reviews cover integrity, growth,
backup age, alerts/incidents and operator effort; review SLOs at days 7 and 14.
Stopping does not reset history or turn a failed day into an excluded window.

Exit requires the ongoing pilot gates identified above, no unresolved blocker,
100% daily manifest/placement/hold/audit reconciliations, and at least 30 recorded
user tasks across upload/retrieve, discovery/download, policy/job and admin
workflows. At least 90% must complete without maintainer intervention. Retain
task failures, completion times and operator minutes per day; report no fewer
than three testers' feedback on usefulness and failure clarity. If fewer than
three testers enroll, the usefulness gate is incomplete rather than assumed.

The accepted owner records **expand / fix and repeat / stop**, with measured
results and prioritized linked defects/needs. Expansion needs a new scope and
capacity/security review; a stopped/failed pilot does not satisfy #155. The exit
updates the roadmap, operator guidance and regenerated ticket mirror in #166.

## Evidence handoff

Use [the M5 evidence index](evidence/m5/README.md). Every acceptance record must
bind this specification revision to source commit, immutable image, dependency
manifest, provider configuration, rendered chart/config hashes, environment
identity, workload seed, UTC duration/sample counts, commands, expected/actual
results and limitations. Store sanitized evidence in the repository or durable
linked artifact storage with checksums; local ignored paths alone are inadequate.
Keep credentials, tokens, secret values, personal data and customer content out
of this specification and public evidence. Restricted resource identities and
secret-version references may live in access-controlled manifests whose stable
reference and digest are recorded here.
