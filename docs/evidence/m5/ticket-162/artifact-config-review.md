# Ticket #162 artifact, configuration and deployment boundary review

**Conclusion: source/configuration review completed; candidate and deployment
security acceptance remain blocked.** This is an authorized defensive review of
the owned CogniStore repository, using local source, synthetic inputs and saved
candidate reports. No production system, cloud resource, actual credential,
customer data or browser identity provider was exercised. It is not a named
operator/security-owner signoff.

## Review identities and evidence

- Reviewed source: `8d3d50bb649cea617fe030859c51b196457e4648` on
  `chore/162-system-safety-audit`, 2026-09-21 UTC. The file-level review inventory
  in [artifact-inventory.json](artifact-inventory.json) identifies the exact
  source/configuration bytes, independently of later evidence-only changes.
- Selected specification: `m5-pilot-v1`, revision 1. Its acceptance remains
  pending in [production_pilot.md](../../../production_pilot.md).
- Retained candidate source: `e51cb39cc84b819ecbed43b56513888a956812cf`;
  Linux amd64 image configuration digest:
  `sha256:1f065e90bbcb256970782f16622d931b84e83ffe91d5e00321bd8b8f8bb33a05`.
  This is **not** a registry manifest digest. The candidate identity is in the
  [#160 manifest](../ticket-160/manifest.json), and its security findings remain
  in the [#160 disposition](../ticket-160/security-disposition.md).
- `git diff e51cb39cc84b819ecbed43b56513888a956812cf HEAD -- cognistore pyproject.toml release/Dockerfile`
  was empty at the reviewed source. This establishes those source inputs were
  unchanged; it does not establish that a new build, runtime or deployment has
  identical bytes or has passed qualification.
- All **65** entries in the retained #160 `SHA256SUMS` match their repository
  files. This verifies consistency with the retained checksums, not independent
  artifact provenance. The complete archived image/wheelhouse was not retrieved
  or revalidated by this review.
- Actual registry digest, account/cluster, rendered deployment identity, live
  environment identity and accepted responsible owners: **not supplied**.
  [artifact-unresolved-preflight.json](artifact-unresolved-preflight.json)
  binds the shipped configuration hashes and retained candidate-manifest hash.

The fresh local quality run used macOS 14.8.7 ARM64 / CPython 3.12.7, not the
selected Debian bookworm / CPython 3.12 Linux amd64 runtime. Commands, tool
versions, timestamps, exit codes and log checksums are retained in
[artifact-validation.json](artifact-validation.json).

| Local check | Result and scope |
| --- | --- |
| `python -m bandit -c pyproject.toml -r cognistore -ll -ii` | Exit 0; no findings meeting both configured severity/confidence thresholds. Raw metrics retain 140 low-severity and 7 medium-severity/low-confidence observations; this is not a claim of zero scanner observations. |
| `python -m pip_audit . --format json` | Exit 0; no known vulnerabilities in the independently resolved source-project dependency set. This is not an audit of every native/vendored package or the frozen candidate closure. |
| `python -m pip check` | Exit 0 in the local development environment. |
| `python -m build --outdir /tmp/cognistore-162-review-dist` | Exit 0; wheel and sdist built. Their hashes are recorded in the inventory; these temporary local outputs are not release artifacts. |
| `python -m twine check /tmp/cognistore-162-review-dist/*` | Exit 0 for both outputs. |
| Wheel archive inspection | All three configured console entry points, 14 migration revisions with migration runtime/template files, and seven static UI assets are present. |
| `PYTHONPATH=. /tmp/cognistore-73-validation/bin/python scripts/staging_preflight.py --configuration release/staging --candidate docs/evidence/m5/ticket-160/manifest.json --output docs/evidence/m5/ticket-162/artifact-unresolved-preflight.json` | Expected exit 1 / `incomplete`; the shipped placeholders cannot qualify as resolved deployment inputs. This check used CPython 3.12.2. |
| Fresh native-image scan | Not performed. Retained findings and scanner freshness gaps remain open; local Python audit does not override them. |
| Hosted CI | skipped: user instruction; known GitHub billing/spending restriction |

## Entrypoints, authority and state ownership

| Boundary / reviewed implementation | Selected configuration and required operating boundary |
| --- | --- |
| API/UI: `cognistore/api/server.py:main`, `cognistore/api/app.py:create_app`, `helm/cognistore/templates/deployments.yaml` | Chart explicitly runs `cognistore-api`, supplies JWT issuer/audience, RBAC/tenant policies and TLS from Secrets. The request middleware validates bearer tokens and resolves exact issuer/subject membership for `/v1`. `/ui/` is static content, not a login/session implementation. A separately operated proxy must forward each browser user's token and verify API upstream TLS. Health/UI smoke cannot prove browser authorization. |
| Worker: chart command `cognistore --no-config ... worker`, production authorization/tenant policy arguments | API publications carry trusted internal identity assertions; broker publish privileges therefore grant privileged submission capability. The selected worker rejects scheduled envelopes, runs two jobs concurrently and must revalidate current roles/tenants at execution. Broker credentials must not be exposed to ordinary callers. |
| Operator CLI and sample: `pyproject.toml:[project.scripts]`, pilot workflow 2 | The CLI is a trusted local entry point with direct catalog/storage access; it is not HTTP JWT enforcement and ordinary write commands are not named-tenant selection. Its `--tenant` reconciliation label does not choose an API tenant partition. Use only the separate disposable default-tenant fixture. `cognistore-sample` is a development composition and does not qualify selected production providers. |
| Catalog and migration: `cognistore/api/server.py`, chart migration hook | One external PostgreSQL 16 primary owns catalog, moves, holds and audit state; tenant schemas must have their actual migration heads recorded. Pre-install hook account has no AWS role grant. The same-node hot mount is available to API, worker and hooks; live mount/ownership evidence remains required. |
| Storage: `release/staging/drivers.yaml`, `release/staging/resources.yaml` | One trusted service-owned, case-sensitive ext4 hot namespace on an encrypted 200 GiB RWO volume; native S3 warm namespace with versioning, SSE-KMS, verified HTTPS, workload identity and no bucket creation. RWO is not a multi-node filesystem or ReadWriteOncePod. Catalog/storage generation fencing does not authorize external namespace writers. |
| Broker: `release/staging/nats-values.yaml`, `jetstream-*.json`, `cognistore/jobs/nats_queue.py:NatsJetStreamQueue.connect` | Single file-backed broker with TLS-first connections/reconnects, distinct application/operator identities and explicit subject restrictions. Main stream is capped at 10,000 messages and 1 GiB with new-message rejection; delivery remains at-least-once. The DLQ has 30-day age retention and no independent byte cap, so broker disk admission/alert limits and reconciliation remain necessary. No live permission, ACK/retry or reconnect behavior was established here. |
| Providers: `cognistore/api/gateway.py:CogniStoreGateway.__init__`, `cognistore/policy_feature_runtime.py:load_policy_feature_loader` | Default `AskService(catalog)` supplies metadata-only Ask. The omitted root embedding block prevents independently activating policy models. No Tantivy writer, keyword/vector retrieval, answer generation, model credential or model egress is selected. Installing packages or pgvector is not provider composition. |
| Deployment privileges/secrets: `helm/cognistore/templates/_helpers.tpl`, `release/staging/values.yaml` | Non-root UID/GID 10001, dropped capabilities, read-only root, RuntimeDefault seccomp; bounded memory-backed `/tmp` and `/dev/shm`. Explicit STS projection, regional endpoints and disabled IMDS fallback accompany the exact role. Runtime DB/broker credentials use a separate Secret; actual secret versions, IAM trust/policy, certificate ownership, rotation and node swap/encryption require operator evidence. |
| Encryption/network/telemetry: `cognistore/encryption.py`, chart NetworkPolicy, `docs/staging.md` | Production runtime requires verified TLS and current attestations for relevant components. Attestation owner/evidence strings are inputs, not proof of disk/backup encryption. Selected private proxy/monitor access and explicit egress need an enforcing CNI and allowed/denied connectivity evidence. Tenant API metrics remain disabled; trusted private worker/ingress/DB/broker telemetry requires separate collector access. |

## Findings and disposition

The following items are inherited blockers or unfulfilled qualification gates,
not newly reproduced application vulnerabilities. Proposed owner means a
responsibility nomination; no acceptance is inferred from authorship or
maintainership. No exception, downgrade or release approval is granted here.

| ID / severity | Trigger and evidence | Proposed owner / disposition |
| --- | --- | --- |
| ART-01 / release blocker, inherited critical/high findings | Promoting retained #160 image while its [security summary](../ticket-160/security-summary.json) has **69 unresolved reported IDs: 3 critical, 14 high, 8 medium, 39 low, 5 unspecified**. Docker Scout 1.0.9 had an advertised update and the retained report supplies no vulnerability-database freshness identity. Counts may include CVE/GHSA aliases and do not establish exploitability. Native packages and pip-vendored setuptools/msgpack findings remain outside the clean top-level Python audit. | Mitchell Elliott, proposed security/release owner; tracked by [#160](https://github.com/melliott18/CogniStore/issues/160). Block candidate/security acceptance pending current scanner/vendor applicability review, explicit bounded owner disposition where permitted, remediated image and exact-image retest. Do not silently patch vendored code, replace the image, or assume non-root execution waives a finding. |
| CFG-01 / qualification blocker | Treating resolved template strings or `staging_preflight` consistency success as actual environment security proof. The shipped inputs still fail preflight. No registry transfer, live rendered hash, platform encryption/backup evidence, actual OIDC/proxy behavior, dependency TLS negatives/reconnects or enforcing network matrix exists in this record. | Mitchell Elliott, proposed deployment/security owner; existing [#161](https://github.com/melliott18/CogniStore/issues/161) and final [#162](https://github.com/melliott18/CogniStore/issues/162) gates remain pending. Test the selected candidate/configuration in the authorized isolated environment after inputs are supplied. |
| CFG-02 / qualification blocker | Claiming audit or pilot acceptance without accepted owner/specification review and real browser/session/operator evidence. The pilot owner table remains pending; UI token forwarding/session expiry/sign-out/forged-header/CSRF behavior belongs to an external proxy. An API token test or successful static-page response does not exercise this path. | Mitchell Elliott, proposed accountable owner; [#159](https://github.com/melliott18/CogniStore/issues/159), [#161](https://github.com/melliott18/CogniStore/issues/161), [#163](https://github.com/melliott18/CogniStore/issues/163) and [#166](https://github.com/melliott18/CogniStore/issues/166). Retain explicit owner acceptance and live browser/operational evidence before signoff. |

Hosted qualification remains a separate unmet release requirement under the
standing suspension. It is not a new software failure. None of the positive
local results closes ART-01, CFG-01 or CFG-02.

## Unsupported substitutions and qualification limits

The selected profile excludes SQLite production catalogs, catalog read replicas,
multi-node POSIX access, external namespace writers, automatic scale-out,
recurring schedules/distributed SQLite coordination, shared multi-writer
Tantivy, GCS/Azure, PII-enabled search, model providers and customer data.
The generic Terraform/Compose profiles select materially different topology
and security; they cannot substitute for this profile. The generic API can be
assembled without authentication; such a composition is outside the selected
mandatory-auth Helm deployment and cannot qualify it.

No Linux/ext4 native execution, PostgreSQL/S3/JetStream multireplica concurrency,
TLS/OIDC/CNI enforcement, browser session controls, restore/key rotation, live
load/alerts or real operator acknowledgement was performed by this review.
Other #162 evidence records cover local application tests; the production
campaigns remain dependent on the exact candidate and environment. Any change
to code, dependency/image bytes, provider selection, trusted policies, topology,
resource limits or environment requires the scoped re-audit described in
[release_candidate.md](../../../release_candidate.md#change-control).

## Requirements for consuming audit evidence

Keep offline input checks and live-qualified results distinct. A robust audit
gate must require the complete versioned control set, exact source/image/config/
environment binding, nonempty sanitized evidence with validated checksums,
explicit expected/actual outcomes and real review acceptance. Unknown, missing,
inconclusive, expired, failed or skipped required controls must block signoff.
Reject duplicate JSON keys, malformed identities, nonfinite values and empty
denominators; arbitrary evidence reference strings must not become passing
evidence. Hashes establish correspondence, not authentic reviewer identity or
truth of manually supplied claims. Preserve those trust limits in any automated
validator's output.
