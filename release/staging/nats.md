# Isolated staging broker profile

This is preparation for ticket #161, not a deployed or qualified broker.
Use `nats/nats` chart **2.14.6**, release `cognistore-staging-nats`, namespace
`cognistore-staging`, and a separately reviewed immutable NATS image digest.
The profile has one server, one independent 20 GiB PVC, TLS-first client
connections and private HTTPS monitoring. There is no HA claim.

The PVC uses the encrypted, retained `cognistore-staging-hot` StorageClass from
`resources.yaml`; it never mounts the application's hot PVC. Check the actual
EBS volume, KMS key, ext4 filesystem, node swap policy and backup destination
before claiming encryption. The broker requests and limits 2 vCPU / 4 GiB.
Its `/tmp` is a bounded memory-backed volume; the chart's PID directory is on
the encrypted node filesystem. Preserve the append patches: replacing the
chart's volume lists through `merge` would remove its data and TLS mounts.

Replace the image placeholder and both monitoring selector placeholders.
Resolve `REPLACE_MONITOR_NAMESPACE` / `REPLACE_MONITOR_APP` identically in the
application and broker profiles. Monitoring is restricted to that namespace
**and** pod label on port 8222; there is no public Service or tenant route.
The monitoring listener is operationally sensitive and has no application JWT
authorization. Restrict who can create pods or edit labels in both namespaces.
Allow operator monitoring egress to this broker through its own policy too.

Deliver these Secrets externally, retaining only their version references:

- `cognistore-staging-nats-tls`: `tls.crt`, `tls.key`, `ca.crt`. The certificate
  must cover `cognistore-staging-nats.cognistore-staging.svc.cluster.local` and
  every additional hostname actually used by clients or monitoring. Use the
  same trusted CA in the application TLS bundle. Clients must verify the
  hostname and use `tls://`; neither `tls-skip-verify` nor plaintext is allowed.
- `cognistore-staging-nats-auth`: `app-username`, `app-password`,
  `operator-username`, `operator-password`. Generate distinct credentials using
  the accepted secret workflow. The application's NATS URL contains only the
  application identity, with URL-encoded credentials. Never deliver operator
  credentials to API or worker pods. Neither password values nor URLs containing
  them belong in deployment evidence, shell history or source control.

The app permission list follows `cognistore/jobs/nats_queue.py`: publish jobs
and immutable dead-letter diagnostics, inspect account/selected stream state,
read selected DLQ records, create/check the one durable consumer, pull deliveries,
and acknowledge/retry/heartbeat those deliveries. Requests and pulls receive
responses through `_INBOX.>`. The dedicated broker contains only this staging
environment: its trusted app processes share a broker identity and inbox space;
these permissions are not a per-browser-user or per-tenant isolation boundary.
Tenant authorization is enforced by CogniStore on enqueue and execution.

Use the separate operator identity to create both streams from the supplied JSON
files **before** application startup. App stream create/update/delete/purge,
message deletion and foreign-campaign subjects are denied. Startup therefore
fails if a required stream is absent, rather than creating an unreviewed stream.
Consumer creation is still permitted on its exact stream/name/filter subject
because the current worker always sends the server-side create action; broker
subject permissions cannot constrain its JSON body. An app compromise remains
able to affect the selected consumer. The operator identity administers this
dedicated broker and must remain on restricted operator pods labeled
`cognistore.io/queue-operator: "true"`; backup/restore workflows need independent
qualification and retained recovery credentials.

The main stream freezes 10,000 messages / 1 GiB, file storage, one replica,
discard-new and a 120-second duplicate window. The DLQ freezes one replica,
file storage, discard-old and 30-day retention. The application requires DLQ
message/byte limits of `-1`; changing those JSON fields alone prevents worker
startup. The broker has a **12 GiB total JetStream file-store limit** on its
20 GiB PVC, so the DLQ does not receive unbounded physical capacity. This is
a hard failure boundary, not a replacement for disk/queue alerts or admission
control. Stop producers and retain/backup diagnostics before space is exhausted;
never purge the DLQ simply to make a qualification run pass. Require at least
30% free disk and measure metadata/filesystem overhead as well as stream bytes.

The unmodified worker CLI freezes ACK wait 30 s, heartbeat 10 s, max ACK pending
64, unlimited broker delivery attempts, application max attempts 7, retry base
1 s / maximum 30 s / jitter 0.2. Worker `maxInFlight: 2` bounds execution.
Record actual consumer configuration and retry arguments in environment evidence.
Warn at 5,000 pending for two minutes; stop submissions at 8,000 pending or
oldest pending age over 300 s for five minutes. Admission rejection alerts
immediately. The profile does not provide admission automation, an alert receiver
or proof of notification receipt. Those are separate qualification inputs.

For offline chart verification after obtaining the pinned chart archive:

```bash
helm lint /absolute/nats-2.14.6.tgz -f release/staging/nats-values.yaml
helm template cognistore-staging-nats /absolute/nats-2.14.6.tgz \
  --namespace cognistore-staging -f release/staging/nats-values.yaml
HELM=/absolute/helm COGNISTORE_NATS_CHART=/absolute/nats-2.14.6.tgz \
  python -m pytest tests/unit/test_staging_broker.py
```

These tests validate rendered structure and exercise the application/client
request subjects against the profile permissions with an isolated fake
transport. They do not prove live NATS enforcement, certificate/reconnect
behavior, CNI enforcement, encrypted volumes, backups or actual alert delivery.
Record those real positive/negative checks separately, including wrong
credentials, untrusted certificates, denied subjects/network paths and recovery.
Coordinate any restart or voluntary eviction as an outage; the single-server
PDB intentionally permits zero voluntary disruptions. Secret rotation needs
an explicit controlled restart because the reloader is disabled.

Primary references checked for this profile: the pinned
[chart values](https://github.com/nats-io/k8s/blob/nats-2.14.6/helm/charts/nats/values.yaml),
[NATS authorization](https://docs.nats.io/learn/security/authorization) and
[JetStream API](https://docs.nats.io/reference/jetstream/api/).
