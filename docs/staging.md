# Isolated M5 staging deployment

Ticket [#161](https://github.com/melliott18/CogniStore/issues/161) deploys the
selected [pilot specification](production_pilot.md), `m5-pilot-v1` revision 1.
**Current status: deployment preparation; no environment has been provisioned
or qualified by this change.** The actual account/cluster, accepted owners,
spend limits, registry image, OIDC/proxy, secret delivery and alert destination
have not been supplied. Every live acceptance criterion remains open.

**Current owner constraint: no spending.** Purchases, paid services and cloud
provisioning are not authorized. The AWS deployment below remains an unaccepted
proposal, and its provisioning steps must not be executed under this constraint.
No-spend local checks can prepare the implementation but do not qualify this
AWS environment or remove its original live acceptance gates.

The [refreshed #160 candidate](evidence/m5/ticket-160-refresh-2026-09-29/README.md)
was assembled from clean main source `04ee37a499d58695645d739971fadc72ab5af459`
and has [retained binary archives](evidence/m5/ticket-160-refresh-2026-09-29/retention.json).
It remains unqualified, with no registry manifest digest and unresolved image
findings, and predates the [upload fix](evidence/m5/ticket-163-upload-fix/README.md).
Rebuild with subsequent runtime fixes and complete the image-security disposition
before qualifying staging. Ticket closure is not artifact or environment
qualification. Continue local work while hosted CI is suspended; record
`skipped: user instruction; known GitHub billing/spending restriction`.
Do not dispatch, rerun or poll hosted workflows.

## Selected configuration

[release/staging](../release/staging) contains deliberately incomplete operator
inputs. Replace all `REPLACE_*` values in a restricted copy outside Git.

| File | Purpose |
| --- | --- |
| `values.yaml` | Production application Helm profile: one API and worker on the same Linux amd64 node, two in-flight jobs, 2 CPU/2 GiB requests and 4 CPU/4 GiB limits, no scheduler/HPA/KEDA, verified TLS and OIDC, explicit workload identity and restricted networking. |
| `resources.yaml` | Restricted namespace, encrypted ext4 gp3 storage class, retained 200 GiB RWO hot PVC, namespace default deny and migration bootstrap egress. Requires standard EBS CSI; this is not an EKS Auto Mode profile. |
| `drivers.yaml` | Shared POSIX hot root and native AWS S3 warm tier with SSE-KMS, 8 MiB multipart settings, no bucket creation or static keys. No embedding/provider configuration. |
| `authorization.json`, `tenants.json` | Exact issuer/subject bindings for separate reader, writer, operator, auditor, admin and smoke identities in each synthetic tenant. Smoke has writer/operator/policy-manager roles, not blanket admin. Replace every subject with its issuer-assigned value. |
| `nats-values.yaml`, `jetstream-*.json` | Single persistent broker, TLS-first connections, restricted app/operator identities and isolated stream/consumer names. See [broker preparation](../release/staging/nats.md) for exact chart, runtime defaults and storage limits. |
| `environment.json` | Account/cluster/environment identity, accepted operations/security/recovery/cost owners, positive daily/total USD bounds and expiry, review references and restricted evidence destinations. References alone are not proof that evidence has been collected. |
| `image-link.json` | Links the candidate manifest hash, source and Docker configuration digest to the transferred registry manifest digest and retained transfer evidence. Never substitute an image ID for the registry digest. |

The common mounts reach API, worker, migration and Helm test pods. All must
fit the selected node and use the same hot PVC; `/tmp` and `/dev/shm` each have
separate 256 MiB memory limits. Confirm at least 16 vCPU/32 GiB **allocatable**
on that node and enough headroom for overlapping rollout pods. RWO permits
same-node mounting, not multi-node writers. The PDB deliberately prevents an
uncoordinated eviction of the single API or broker; use fenced maintenance.

The generic [Terraform reference](terraform.md) cannot be applied unchanged:
it selects PostgreSQL 17/Multi-AZ, three NATS brokers, autoscaling, a different
namespace and S3-only application storage. This package does not create an
EKS cluster, PostgreSQL 16/pgvector service, S3 bucket, keys, identity provider,
authenticating proxy, secret controller or monitoring stack. Their actual
reviewed provisioning configuration and state identity must join the evidence.

## Resolve inputs and render before deployment

Use an isolated AWS account/environment in `us-east-1` with no production or
customer data. Confirm the account and cluster context against the environment
record before any mutation. Do not choose an unrelated current kubeconfig
context or an available AWS profile by convenience. Supply accepted spending
limits and an expiry before provisioning; configure billing alerts and an
operator-enforced stop at those bounds. Budget alerts do not cap charges.

1. Retrieve the trusted candidate bundle and verify it using the
   [release procedure](release_candidate.md). Retain registry transfer evidence
   matching the image configuration and layers/platform to the saved archive.
   Fill `image-link.json`; copying a new digest into this file does not verify
   provenance. Preserve unresolved release/security findings and owner decisions.
2. Copy `release/staging` to a private input directory. Resolve image, node,
   account, KMS, issuer/audience/JWKS, proxy/monitor selectors, endpoint CIDRs,
   role/tenant subjects, ownership, cost and evidence references. Keep policy
   subjects and infrastructure addresses restricted where needed. Apply the
   same replacements to every file. The fixed application release and namespace
   are both `cognistore-staging`; the NATS release is `cognistore-staging-nats`.
3. Deliver the named configuration, runtime credential, TLS and NATS Secrets
   using the approved secret-delivery system. No Secret values belong in Helm
   values, shell arguments, Git, render output or evidence. The configuration
   Secret contains drivers, authorization and tenants plus a current reviewed
   `encryption.json` covering catalog, queue, storage and runtime. The example
   attestation is not evidence. The credential Secret supplies
   `COGNISTORE_CATALOG_DB` with `sslmode=verify-full` and a verified hostname,
   and `COGNISTORE_NATS_URL` using `tls://` with the app credentials. TLS includes
   `tls.crt`, `tls.key`, and a CA bundle covering the actual API, worker, DB and
   broker hostnames. Record versions/references and certificate fingerprints,
   never key material. Refresh/rotate through the existing secret contract.
4. Restrict the AWS role trust to the exact release service account and STS
   audience. Allow only the selected bucket/prefix and required KMS operations;
   exclude key administration and independent backup deletion. Default/hook
   service accounts must have no AWS role grant. Ensure the S3 bucket exists,
   versioning and SSE-KMS are enforced, and the same logical bucket directory
   exists under the hot root with service UID 10001 ownership.
5. Provide actual enforcing CNI rules, service routing and endpoint ranges.
   Narrow CIDRs/selectors must work after Kubernetes service translation; a
   syntactically valid rule does not prove routing or enforcement. Protect
   node/metadata access and restrict who can create pods carrying trusted
   proxy/operator labels. Install the namespace/default-deny/storage/migration
   bootstrap resources before the pre-install migration hook. Do not remove
   default deny to make installation pass.

With development dependencies installed, validate the restricted copy:

```bash
python scripts/staging_preflight.py \
  --configuration /restricted/staging-inputs \
  --candidate /restricted/candidate/manifest.json \
  --output /restricted/evidence/staging-inputs.json
helm lint helm/cognistore --strict -f /restricted/staging-inputs/values.yaml
helm template cognistore-staging helm/cognistore -n cognistore-staging \
  -f /restricted/staging-inputs/values.yaml > /restricted/evidence/application-rendered.yaml
```

The shipped unresolved inputs must exit 1 from preflight. Its successful status
means only that the bounded input contract is consistent. It does not validate
arbitrary overrides or evidence authenticity, authorize changes, or mark any
live gate as passed. Hash and review the rendered chart as well as the input
files; retain the chart archive/hash, tool versions and external service
configuration. `configuration_sha256` in its report hashes the listed inputs;
record rendered hashes separately. Do not change those inputs after the smoke
without generating a new identity and repeating affected checks.

After the concrete resource plan has been reviewed and the target inputs are
known, provision the isolated dependencies, install the broker/streams, deliver
Secrets, then install the application with these exact values and explicit
context/namespace. Collect migration heads for **both** tenant schemas.
Record live pod image IDs, rollout revisions, node/mount identities and service
versions. `helm test` checks health/UI only and cannot satisfy this ticket.

## Live smoke and negative controls

[scripts/staging_smoke.py](../scripts/staging_smoke.py) is an explicit opt-in
network client. It uses two actual OIDC tokens with the synthetic smoke role
bindings, a pre-created bucket, a trusted CA and an unrelated test CA. Run from
an authorized private client path against the direct protected API; authenticate
the browser proxy separately. It disables ambient proxies and redirects and
never sends a bearer token over plaintext. Use a new output path:

```bash
python scripts/staging_smoke.py --run-staging-smoke \
  --base-url https://staging-api.example.invalid \
  --plaintext-url http://staging-api.example.invalid \
  --bucket ACTUAL_SYNTHETIC_BUCKET \
  --environment-id ACTUAL_ENVIRONMENT_ID --candidate-id ACTUAL_CANDIDATE_ID \
  --configuration-id ACTUAL_CONFIGURATION_HASH \
  --ca-file /restricted/ca.pem --unrelated-ca-file /restricted/unrelated-ca.pem \
  --tenant-a-token-file /restricted/pilot-a.token \
  --tenant-b-token-file /restricted/pilot-b.token \
  --output /restricted/evidence/staging-smoke.json
```

It checks storage bytes/ranges and same-key tenant separation on both tiers,
queued scans and policy movement through completion, credential/TLS/plaintext
negatives, and tenant API metrics denial. It creates its own unique synthetic
prefix and cleans up only its exact fixtures when safe. An uncertain job or
submission cannot be retried blindly; preserve the run identity and reconcile
its jobs/bytes before cleanup. Retained job/audit history and S3 object versions
are expected. Local mock tests qualify client behavior only; they never stand
in for live API, PostgreSQL, S3, JetStream or OIDC evidence.

This bounded smoke does not test blocked network source pods or TLS/auth directly
against every dependency. Run a separate permitted/denied connectivity matrix
from labeled and unlabeled synthetic pods: API, worker, DB, broker, S3, STS,
JWKS and monitoring. Deny unlisted destinations and unauthorized sources;
test absent/wrong DB/broker credentials, plaintext, untrusted CA and hostname
mismatch, including reconnects. Retain expected/actual outcomes and prove the
allowed control path works so DNS failure alone is not counted as policy denial.

## Platform, telemetry, recovery and cost evidence

The [M5 evidence contract](evidence/m5/README.md) applies to every record.
Keep sanitized inventory and results in Git or durable linked storage through
at least 90 days after pilot exit. Bind source, image, rendered/input hashes and
environment identities; an ignored local directory is insufficient.

| Gate | Required evidence from the actual environment |
| --- | --- |
| Inventory | Account/region/cluster/namespace, VPC/subnets/routes/security groups/CNI, node OS/kernel/allocatable capacity, case-sensitive ext4 mount/volume IDs, actual image IDs/digests, PostgreSQL 16/pgvector versions/tenant migration heads, broker limits/consumer state, bucket/key/backup IDs. Sanitize values or use restricted links. |
| At rest and recovery | Platform records for encrypted node/hot/broker/RDS volumes, S3 version encryption, snapshots and independent backups; KMS policies/rotation/key owners and recovery access. Node swap disabled; memory-backed temporary/shared memory and node encryption verified. At least 500 GiB independent backup capacity, seven daily coherent sets, fenced daily recovery boundary and a restore demonstration; application attestations alone are insufficient. |
| Identity and access | Actual issuer/audience/subject-to-role/tenant mapping, default/unmapped denial, revocation/revalidation, scoped service access, secret delivery/rotation evidence and TLS trust/hostname negatives. Private browser proxy must forward each user's access token and verify the API upstream; test session expiry/sign-out/forged headers/CSRF. Shared service bearer identity is not acceptable. |
| Private telemetry | Tenant API `/metrics` remains 404. Use private ingress/client telemetry plus verified-TLS worker `:8081/metrics`, broker monitoring and DB/storage/node/pod signals; restrict collectors and dashboards to operators. Never disable tenancy for an API scrape. Collect every 15 s; derive the selected latency/error/queue/storage/memory signals and preserve denominators. |
| Alerts | Route to the designated actual operator; retain trigger, delivery, acknowledgement and recovery timestamps. Queue warning at 5,000 pending/2 min; stop admission at 8,000 or age >300 s/5 min. Memory-volume warning60%, admission stop75%; pod memory stop80%/5 min; any exhaustion stops immediately. Also apply every other selected pilot gate. No receiver/test notification is configured by these templates. |
| Spend and isolation | Accepted daily/total USD limits/expiry, budget notifications, tagged inventory/cost observations and an accountable stop/teardown owner. Synthetic-only dataset bounds and separate staging/pilot databases, queue identities, namespaces, buckets and volumes; no shared mutable campaign dependencies. |

For recovery, fence all readers/writers (including access/audit-producing calls),
stop worker publication/consumption, capture a coherent cross-plane boundary,
then follow [operator recovery](operator_lifecycle.md#backup). Restore to a
separate isolated environment with retained keys before declaring a set usable.
Meet RPO <=24 h and RTO <=60 min under #164; a healthy pod is not recovery proof.

For teardown, first stop admissions and all API/worker access, reconcile queued
jobs, capture the final coherent recovery/evidence set, and revoke workload and
OIDC access. Inventory retained PVC/PVs/EBS snapshots, versioned S3 objects,
database snapshots, secret versions, keys, registry artifacts, endpoints and
network charges. Uninstall application and broker only in the recorded context.
The storage class uses `Retain`: namespace deletion does not prove data or cost
cleanup. The accepted owner approves deletion of the inventoried disposable
resources after retention requirements are satisfied; do not destroy backup
keys or evidence needed for recovery. Verify zero unowned residual resources
and record remaining retained-resource cost, expiry and owner. Teardown/recovery
commands must name actual resource IDs from the inventory, not guessed IDs.

See [local implementation evidence](evidence/m5/ticket-161/README.md) for the
tested revision and remaining live work.
