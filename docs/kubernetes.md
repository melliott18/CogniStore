# Kubernetes deployment and autoscaling

The chart in [`helm/cognistore`](../helm/cognistore) deploys the REST API and
browser UI, background workers, an optional recurring scheduler, and catalog
migration and smoke-test jobs. The UI is served by the API at `/ui/`, so API
replicas scale both interfaces together. The chart uses the repository's
runtime image and defaults to the `production` security profile.

PostgreSQL, NATS, object storage, the ingress controller, and autoscaling
controllers are operator-managed dependencies. Their data and lifecycle are
independent of the application release. The chart does not provision a cloud
account or install a database operator.

## Deployment boundary

Use PostgreSQL with pgvector 0.8.0 or newer for the catalog, persistent NATS
JetStream, and the same storage endpoints and credentials for every API and
worker replica. Give each release its own queue stream, subject, and durable
consumer unless the releases intentionally share one worker pool. Every worker
in a pool must agree on queue topology and acknowledgement settings.

The normal scalable deployment leaves the scheduler disabled. Workers reject
scheduled envelopes in that mode; ordinary REST action jobs use the shared
PostgreSQL catalog's durable claims and move journal. Scaling replicas does not
turn JetStream's at-least-once delivery into an exactly-once transport. Stable
job identities, duplicate detection, transaction fences, and verified move
transitions protect side effects through retries. Retain a durable manual
move's caller-provided idempotency key when retrying it. REST API v1 action
submission has no caller-provided idempotency key: retain the returned job ID
for status checks, and reconcile an uncertain submission before submitting it
again. See the [SDK's asynchronous job workflow](python_sdk.md) for this
boundary.

Recurring schedule coordination is still a SQLite file. Enabling the scheduler
requires a persistent volume shared by the scheduler and all its workers on
one node. This deliberately limits scheduled-worker scaling to that node; a
network filesystem does not make SQLite a distributed coordinator. Never
replace that shared file with a separate file per worker. See
[background workers](background_workers.md) for schedule leases and fenced
recovery after a lost worker.

The embedded Tantivy index also supports only one writer per local path. The
chart does not create a shared keyword index for API replicas. Use the existing
[keyword index](keyword_search.md) rebuild and ownership contract when adding
an operator-managed search process. PostgreSQL remains authoritative; index
directories are not a replacement for catalog backups.

To enable recurring schedules, set `scheduler.enabled: true`, supply
`schedules.yaml` in the configuration Secret, and select an encrypted RWO block
volume through `scheduler.storage`. The chart pins workers to the scheduler's
node and replaces the scheduler without overlapping publishers. These are
deployment constraints, not high availability for the scheduler database.

For protected schedules, set `scheduler.principalIssuer` and
`scheduler.principalSubject` to a dedicated service identity granted the needed
roles and tenant membership. The scheduler asserts this operator-configured
identity; it does not obtain or verify a JWT. Restrict configuration and NATS
publishing access accordingly. One scheduler identity resolves to one tenant.
Changing it affects new occurrences; pending occurrences retain their original
principal and tenant. Drain old anonymous schedules before enabling protected
workers. See the [scheduler identity contract](background_workers.md#protected-schedules).

## Production prerequisites

Prepare these resources in the release namespace before installing:

| Resource | Required content or behavior |
| --- | --- |
| Configuration Secret | Trusted `drivers.yaml`, current `encryption.json`, `authorization.json`, and `tenants.json`; `schedules.yaml` when schedules are enabled. The production chart enables tenant isolation. |
| Credentials Secret | Environment variables used by the driver configuration, `COGNISTORE_CATALOG_DB`, `COGNISTORE_NATS_URL`, and any password or provider credential variables. Keep credentials out of values files and chart output. |
| TLS Secret | `tls.crt`, `tls.key`, and `ca.crt` for the API and worker health listeners and trusted outbound services. Certificates must cover the service names clients actually use. |
| PostgreSQL | Dedicated database, pgvector, verified server TLS, encrypted durable storage and backups; a role permitted to apply catalog migrations and tenant schema creation. |
| NATS | JetStream file storage and backups, verified client TLS with `handshake_first: true`, and trusted publisher/consumer access. Protect monitoring separately. |
| Kubernetes | A NetworkPolicy-enforcing CNI, suitable storage class, and enough allocatable resources for the maximum replica count. |

Keep issuer, audience, authorization bindings, and tenant bindings consistent
between API and worker configuration. Every protected job revalidates current
permissions and tenant membership before work. Follow
[authentication](authentication.md), [authorization](authorization.md), and
[tenant ownership](tenancy.md) when preparing these files. TLS does not grant
application permissions.

Production also requires current encryption evidence for `catalog`, `queue`,
and `runtime`, plus `storage` for POSIX tiers or custom cloud endpoints. The
runtime evidence must include scheduler state, local files, temporary storage,
and the node's swap policy. The chart cannot prove storage encryption or renew
attestations; follow the [encryption deployment guide](encryption.md). Keep the
`development` profile confined to disposable clusters with synthetic data.

## Install a release

Use Kubernetes 1.28 or newer and Helm 3 or newer. Provision the resources above through
your secret-management workflow. For example, with reviewed local files and an
existing namespace:

```bash
kubectl -n cognistore create secret generic cognistore-config \
  --from-file=drivers.yaml --from-file=encryption.json \
  --from-file=authorization.json --from-file=tenants.json
kubectl -n cognistore create secret generic cognistore-credentials \
  --from-env-file=runtime.env
kubectl -n cognistore create secret generic cognistore-tls \
  --from-file=tls.crt --from-file=tls.key --from-file=ca.crt
```

Keep `runtime.env` outside the repository. The application NATS URL must use
`tls://`; the PostgreSQL DSN must use verified TLS. Configuration files are
mounted under `/etc/cognistore/config` and certificates under
`/etc/cognistore/tls`. The chart sets `PGSSLROOTCERT` and
`COGNISTORE_TLS_CA_FILE` to `/etc/cognistore/tls/ca.crt`. Confirm any additional
paths referenced by driver or credential configuration have corresponding
`extraVolumes` and `extraVolumeMounts`.

Create a release values file containing only nonsecret configuration:

```yaml
image:
  repository: registry.example.com/cognistore
  digest: sha256:REPLACE_WITH_TESTED_IMAGE_DIGEST
config:
  existingSecret: cognistore-config
  revision: "1"
credentials:
  existingSecret: cognistore-credentials
tls:
  existingSecret: cognistore-tls
auth:
  issuer: https://identity.example.com
  audience: cognistore-api
```

Add reviewed `networkPolicy.ingress` and `networkPolicy.egress` rules for the
destinations described below before installation. Defaults deny application
ingress and data egress, with a separate DNS exception; a values file containing
only the example above intentionally cannot reach its database. Complete
`networking.k8s.io/v1` rule objects are accepted so selectors, namespaces,
ports, and CIDRs can match the actual topology. Enable the ingress only after
setting its class, host, certificate Secret, and controller-specific verified
HTTPS upstream configuration.

Allow the release's smoke-test pod to reach the API on port 8080 in both
directions of policy enforcement. It has
`app.kubernetes.io/component: smoke`; the API has
`app.kubernetes.io/component: api`, and both carry the release's
`app.kubernetes.io/instance` label. The migration hook also needs database
egress under any namespace-wide default-deny policy, including during a first
install before ordinary chart resources exist.

Render and inspect the exact release before installing:

```bash
helm lint helm/cognistore -f release-values.yaml
helm template cognistore helm/cognistore -n cognistore \
  -f release-values.yaml > rendered.yaml
helm upgrade --install cognistore helm/cognistore -n cognistore \
  -f release-values.yaml --wait --timeout 10m
helm test cognistore -n cognistore --logs
```

Set an immutable image digest for production and grant image-pull access where
needed. Defaults reference a local image name; the chart does not publish that
image to a registry. Changes to externally managed Secrets are not part of the
chart checksum. Increment `config.revision` and upgrade to restart clients
after a credential, certificate, or configuration rotation. Distribute
overlapping CA trust before rotating certificates.

## Security and network controls

Application workloads run as non-root, disallow privilege escalation, drop
Linux capabilities, use the runtime-default seccomp profile, and use a
read-only root filesystem. Writable runtime paths have explicit volume mounts.
The service account does not need Kubernetes API access for normal work.
Configure workload identity deliberately if a driver requires a projected
service-account token.

Startup and liveness probes use `/healthz`; readiness uses `/readyz` to keep
unready API and worker pods out of traffic. Production probes use HTTPS.
Readiness is an operational check, not an authenticated data API.
The scheduler uses an exec probe against its last completed publication cycle.
A failed completed cycle stays live but becomes unready; a stale cycle fails
both checks. Tune `scheduler.readinessMaxAge` (30 seconds by default) and
`scheduler.livenessMaxAge` (300 seconds) to cover the measured worst-case
publication cycle before enabling a large schedule set.

Keep the API service private until the ingress is configured for TLS to both
the client and the API upstream. Forward bearer authorization headers without
logging their contents. Protect `/metrics`, `/docs`, and OpenAPI at the ingress
as appropriate; these are operator interfaces, and `/healthz`, `/readyz`, and the static
UI are public application routes. Loading the UI does not sign a user in.

Network policy must allow only the intended ingress clients, DNS, PostgreSQL,
NATS, object storage, identity-provider discovery/JWKS, and any selected model
or telemetry endpoints. Replace example selectors and CIDRs with actual
deployment destinations. Kubernetes NetworkPolicy enforcement depends on the
installed network implementation; verify blocked and permitted traffic in the
target cluster. See the [Kubernetes networking reference](https://kubernetes.io/docs/concepts/cluster-administration/networking/).

## Autoscaling signals

API autoscaling uses a Kubernetes HPA. CPU utilization needs a metrics server
and CPU resource requests; tune the request and target against representative
traffic. A request-rate metric needs a custom-metrics adapter that maps one
rate series to each API pod. The chart cannot install or infer that adapter.
See the [Kubernetes HPA reference](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/).

Both autoscalers are opt-in. After configuring the metrics sources, set
`api.autoscaling.enabled: true` and/or `worker.autoscaling.enabled: true`.
The default API metric is the per-pod
`cognistore_http_requests_per_second`, with a target of 20 requests per second
and a range of 2–8 replicas. Supply alternative `api.autoscaling.metrics` to
use CPU:

```yaml
api:
  autoscaling:
    enabled: true
    metrics:
      - type: Resource
        resource:
          name: cpu
          target:
            type: Utilization
            averageUtilization: 70
```

For a trusted single-tenant API scrape, the request-rate input can be derived
from `cognistore_http_requests_total` with `rate(...[2m])`, summed by pod and
namespace, excluding `/healthz`, `/readyz`, and `/metrics` so probes do not drive scaling. Prometheus must
attach those Kubernetes identity labels. Tenant-enabled APIs intentionally
return 404 for `/metrics`; use trusted per-pod ingress telemetry or CPU scaling
in that mode. Never expose aggregate telemetry to tenants to obtain a scaling
signal. See [observability](observability.md) for the counter's exact labels.

Workers use a KEDA `nats-jetstream` ScaledObject. Its account, stream, and
consumer must match the worker's durable consumer. `lagThreshold` is the target
queue lag per replica, not a maximum job execution time. Keep at least one
worker to bootstrap and validate the consumer. KEDA reads NATS's monitoring
endpoint; it does not use the application's NATS client credentials. Restrict
that endpoint to trusted operators and the KEDA controller, and configure TLS
trust for KEDA separately from the application pods. See the
[KEDA JetStream scaler reference](https://keda.sh/docs/2.18/scalers/nats-jetstream/).

Set `worker.autoscaling.natsMonitoringEndpoint` to the monitoring `host:port`
and keep `useHttps: true` for production. The default target is eight queued
jobs per replica, with 1–8 replicas and 300 seconds of scale-down stabilization.
These are starting values, not a throughput guarantee. Tune them alongside
`worker.maxInFlight`, measured job duration, dependency connection limits, and
cluster capacity. Keep enough spare node capacity for the configured maximum.

Use one autoscaling controller per workload. Do not attach an independent HPA
to a worker Deployment already managed by KEDA. Scale-down stabilization and
pod termination grace must cover normal draining. The worker stops accepting
new jobs on termination and keeps lease heartbeats alive while an active
thread reaches its side-effect boundary. A forced pod kill can still interrupt
an unusually long transfer; size the grace period using measured job times
and verify recovery under the actual storage backend.

## Migrations, upgrades, and rollback

Treat application and database versions as one compatibility decision. The
migration job runs the image's packaged Alembic migrations before new
application workloads start. PostgreSQL advisory locks serialize concurrent
migration opens. Include every tenant schema in migrations and backups;
upgrading only the default catalog is insufficient for a tenant deployment.

Before an upgrade, retain the previous image digest and values, back up the
catalog and JetStream state, and rehearse recovery. Preserve scheduler SQLite
state when scheduling is enabled. Review changed job-envelope versions and
drain older consumers before publishing a version they cannot read. Stop
producers and fence writers for a schema change that is not compatible with
the running image; a Helm hook does not pause old pods.

Before disabling the scheduler or moving to independent workers, stop schedule
publication and drain every scheduled occurrence. Workers started with
`--disable-scheduled-jobs` reject scheduled deliveries; they cannot safely resume
them without the original shared coordination file. Keep the stream identity
and scheduler PVC unchanged through upgrades and compatible rollbacks.

A Helm rollback changes Kubernetes resources; it does not undo a completed
database migration. Only roll back the image when it supports the current
schema. Otherwise stop producers and writers, restore or deliberately migrate
the catalog using the database runbook, and restore compatible queue/scheduler
state before resuming. Never downgrade automatically in a Helm rollback hook:
some catalog downgrades discard newer state or are deliberately rejected.
See [PostgreSQL catalog operations](postgres_catalog.md) and
[Helm hook lifecycle](https://helm.sh/docs/topics/charts_hooks/).

Retain dependency volumes and backups through the rollback window. A
chart-created scheduler PVC carries Helm's keep policy; after uninstall,
explicitly reuse it with `scheduler.storage.existingClaim` when restoring the
same schedule history. Uninstalling
an application release is not a database reset. Snapshot catalog, queue,
scheduler state, and storage consistently rather than combining unrelated
restore points. After recovery, inspect move journals and scheduled runs before
resuming publication; recover quarantined scheduled runs only after externally
fencing the previous worker.

## Automated qualification

From the repository root, run:

```bash
./scripts/kubernetes/verify.sh
```

Install Docker, kind, kubectl, Helm, and Python 3 first. The script uses a private
kubeconfig, refuses to reuse an existing cluster name, writes diagnostics to
`test-results/kubernetes`, and deletes its cluster on exit. Set
`COGNISTORE_KUBE_KEEP_CLUSTER=1` to retain a failed cluster for investigation or
`COGNISTORE_KUBE_SKIP_BUILD=1` to reuse a locally built
`cognistore:helm-acceptance` image. These options do not select an existing
production context.

The qualification harness creates a disposable kind cluster, builds and loads
the runtime image, provisions persistent PostgreSQL, NATS, and object-storage
fixtures, and installs the chart with the explicit development profile. It
checks API/UI health, catalog and job state across upgrade and rollback, and
actual replica growth under request and queue load. The queue campaign checks
one started and one successful audit event per submitted job. Template checks
alone cannot establish those runtime properties.

The synthetic cluster uses development transport settings. Passing it validates
the chart's installation and lifecycle mechanics; validate production
certificates, authentication, tenant authorization, encryption evidence,
NetworkPolicy enforcement, and the chosen storage provider in the target
environment as well. Helm test is a small in-cluster smoke check and does not
replace the load and recovery campaign.

The smoke-test pod remains after success so `helm test --logs` can retrieve its
output. The next test replaces it; delete it explicitly when retiring a release.

For a release drill, record the deployed image digest, chart version, values
revision, current Alembic head for every tenant, worker consumer configuration,
HPA/KEDA conditions, and the qualification output. During load, observe:

```bash
kubectl -n cognistore get deployment,hpa,scaledobject
kubectl -n cognistore get pods
kubectl -n cognistore get events --sort-by=.lastTimestamp
```

Unavailable HPA metrics point to the metrics API/adapter and pod-label mapping.
KEDA scaler errors point to monitoring endpoint reachability, TLS trust, or a
wrong account/stream/consumer. Pending pods require capacity or storage-affinity
investigation. A rollout blocked at migration requires catalog credentials,
schema compatibility, and network access to be resolved before retrying.
