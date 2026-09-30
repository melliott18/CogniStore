# Freeze and reuse a release candidate

[Ticket #160](https://github.com/melliott18/CogniStore/issues/160) provides the
candidate assembly path for [M5](https://github.com/melliott18/CogniStore/issues/155).
The selected workload is [m5-pilot-v1, revision 1](production_pilot.md): Linux
amd64, Debian bookworm, CPython 3.12, native libmagic, POSIX hot storage, native
AWS S3 warm storage, PostgreSQL and NATS JetStream. The package version for
this candidate series is `0.1.1rc1`.

**Status: locally rebuilt candidate; release qualification incomplete.** The
[2026-09-29 evidence refresh](evidence/m5/ticket-160-refresh-2026-09-29/README.md)
records the clean-main candidate, installed-artifact checks and current image
analysis. It supersedes the earlier branch candidate for current local analysis;
the [historical evidence](evidence/m5/ticket-160/README.md) remains preserved.
#160 is administratively closed, while the candidate remains
`assembled-unqualified`.

Named owner acceptance of the pilot specification remains pending, and the
[#158 hosted CI record](evidence/ci-158/README.md) records jobs blocked before
runner assignment by an account billing/spending-limit restriction. Those are
release blockers. A successful local build, branch check, smoke test, or ticket
closure does not satisfy either gate. This procedure grants no deployment,
registry publication, customer traffic, or pilot-entry authorization.

**Hosted CI is on hold under the current user instruction.** Do not dispatch,
rerun, or poll hosted workflows while the billing/spending-limit restriction
and that instruction remain in force. Continue local validation and record
hosted gates as unavailable/unmet. The future workflow procedure below applies
only after the user explicitly lifts this hold.
Hosted CI: `skipped: user instruction; known GitHub billing/spending restriction`.

## Selected product behavior

The runtime uses the shipped `cognistore-api` composition and **metadata-only
Ask**. It has no configured keyword, vector, embedding or answer-generation
provider, model, model credentials, or outbound model traffic. Omit the root
`embedding` configuration as well as Ask providers: adding the former can
activate model work independently of the API. The base package may contain
libraries for unselected features; installation alone does not select a
provider or qualify its behavior.

Metadata results, no-match queries, citations and downloads are in scope.
Requested but absent providers must be reported as unavailable; no generated
answer or semantic-search quality is promised. The `/ui/` Ask view can show
metadata results; its keyword/vector Search tabs may show empty results and
unavailable-provider diagnostics. The UI needs the per-user authenticating
proxy specified in the pilot; serving its HTML is not browser acceptance.
The [content-search sample](content_search_sample.md) and its offline models
are development demonstrations, not the candidate's provider composition.

No optional runtime extras are selected. Azure Blob and the `azure` extra,
GCS, and the `embeddings` extra are excluded from this pilot. GCS has zero
spooling allowance because it is excluded; selecting it later requires
encrypted full-object spool capacity for concurrency/retries and recovery
testing. No scheduler, PII detection, automatic repair, or production learned
policy is selected. See [REST provider behavior](rest_api.md#retrieval-selection)
and the full [pilot boundaries](production_pilot.md#feature-and-boundary-decisions).

## What is frozen

Treat a candidate as an immutable bundle, rather than a version number or a
Docker tag. Keep these identities and files together:

| Input or output | Required identity and retained evidence |
| --- | --- |
| Source | Full Git commit and tree IDs; clean-worktree/main-lineage checks; package version; exact build instructions and tooling versions. The main revision `bbfb5efb63b09693fab2158514716aaa1a146d19` contains both #156/#157 fixes; the candidate must retain both. |
| Package and dependencies | Built CogniStore wheel and its SHA-256; all selected binary dependency wheels; exact version/hash requirements lock; installed Python component inventory. No dependency resolution is permitted during reuse. |
| Runtime | Target OS/architecture/Python, digest-pinned base-image reference, built image ID, saved image archive and SHA-256, and installed native package inventory including libmagic. |
| Database | Packaged migration revisions and SHA-256 of migration files. Database service version and applied schema heads are separately recorded with #161. |
| Configuration and providers | Sanitized pilot profile and SHA-256, specification revision/hash, selected extras, provider/model identities or explicit absence. No credentials or personal/customer content. |
| Validation | Commands, tool versions, timestamps, exit status, installed-artifact smoke, fix regressions, dependency/static/native-image scans, findings/disposition and applicable hosted checks. Failed, skipped or missing gates remain visible. |

A Docker image ID (`sha256:…`) identifies the local image configuration. It is
**not a registry manifest digest** and must not be copied into Helm as if it
were one. The saved image archive freezes the built filesystem, including
native packages obtained during assembly. A subsequent registry transfer must
record its immutable repository manifest digest, verify platform and image
configuration/layers against the retained image, and link the digest to this
candidate. Before a registry digest exists, the image archive and local image
ID support local validation only; the registry-image acceptance requirement
remains open.

The sanitized profile is a scope identity, not a deployable Helm values file.
#161 supplies the rendered chart, drivers, authorization/tenant policy,
encrypted mounts, service/issuer/resource identities and secret-version
references. Hash the exact sanitized configuration used in qualification and
bind restricted details through access-controlled references. The
[staging preparation guide](staging.md) supplies those input templates, offline
identity checks and the opt-in real-service smoke; its local tests do not
establish deployment or qualification. A matching pilot
profile cannot substitute for matching deployment configuration.

## Assembly and validation

Use the [release assembly script](../scripts/release_candidate.py) and
[release Dockerfile](../release/Dockerfile) rather than the development Compose
build. The assembly process resolves dependencies once into binary wheels,
records their versions and hashes, and installs from that frozen wheelhouse
with package-index access disabled and hash verification required. Missing
compatible wheels must fail assembly; do not silently build new runtime
dependencies during qualification.

The shared native stage applies available Debian updates to `openssl`,
`libssl3`, and `tzdata` before installing `libmagic1`. It uses the pinned
bookworm base and signed distribution repositories, fails on an incomplete
package-index refresh, and retains the resulting versions in the candidate's
native inventory and image archive. This resolves these packages during
assembly only; an updated assembly creates a new candidate requiring its own
tests and image scan. These updates do not remediate pip's vendored components
or establish that other image findings are resolved.

Build the actual release candidate from a clean, recorded `main` commit after
the implementation has merged and the owner has accepted the specification.
Fetch `origin/main`, select the exact commit, verify it includes both fixes,
and ensure the source and output directories are separate. A local branch
development build is useful for checking the tooling, but must be labeled
ineligible for release qualification; it cannot be promoted by editing its
manifest. Reassemble on the accepted main revision and rerun the gates.

The build host needs Git, Python 3.12 and Docker with Linux amd64 execution;
cross-platform Docker emulation may be needed on an arm64 host. The release
base is
`python:3.12.14-slim-bookworm@sha256:1aaa65a85fda306ffb8b910824d4e93bdce61e212c7e87168123ea3073b41a1a`.
Use an absolute **new** output directory outside the checkout:

```bash
git fetch origin main
git status --short
git rev-parse HEAD origin/main
python scripts/release_candidate.py build --output /absolute/new/candidate
python scripts/release_candidate.py verify --bundle /absolute/new/candidate
```

The default build requires a clean checkout at the exact fetched `origin/main`
commit. `--allow-unmerged` allows a clean branch commit for development
validation and marks that build ineligible; it never allows dirty source.
The script does not accept or approve the owner specification on the operator's
behalf. The emitted status is always `assembled-unqualified`, with remaining
blockers recorded even when the build's local checks pass.

The output retains `manifest.json`, `SHA256SUMS`, `source.tar`, `image.tar`,
`pilot.json`, `Dockerfile`, `python-inventory.json`, `native-inventory.tsv`,
`wheelhouse/`, `runtime.lock`, `runtime.txt`, `dependencies.txt`, `bootstrap.lock`,
`build-wheelhouse/`, `build.lock`,
`tools-wheelhouse/`, `tools.lock` and `reports/`. The manifest identifies the
sanitized [pilot profile](../release/pilot.json), source/migrations, providers,
runtime, artifacts and commands. Build/test tools have separate locks and
inventories; their versions must not silently alter the recorded runtime
dependency set. Preserve the entire directory when transferring it.

The runtime lock includes pip. `bootstrap.lock` selects that same hashed wheel
to update the base interpreter's installer; the application virtualenv uses the
full runtime lock. `dependencies.txt` is the frozen third-party audit input,
including pip but excluding the unpublished CogniStore package, whose source
is checked by Bandit. This prevents the base image's bundled installer from
silently falling outside dependency auditing.

`reports/smoke.json` records the installed package smoke;
`reports/regressions.xml` and its log retain regression outcomes.
`reports/pip-audit.json` and `reports/bandit.json`, with their logs, retain
dependency/static security results. Native vulnerability scan/disposition,
registry digest, accepted target configuration/services, owner review and
required hosted evidence remain independent gates. Component inventory alone
does not satisfy a native vulnerability scan.

Run the installed-artifact smoke from outside the source checkout, so imports
come from the installed wheel. Record the actual package and image identities
that ran. Local isolated storage/query/action tests demonstrate only their
reported fixture boundaries; SQLite, test transports, emulators or development
security settings do not establish production PostgreSQL, S3, JetStream, TLS,
OIDC, browser, or two-tenant acceptance. The selected end-to-end environment
and workflows still require #161–#165 evidence on this exact candidate.

Run the #156 namespace/legal-hold and #157 concurrent PUT/DELETE regressions
locally. All applicable hosted gates from the [CI runbook](ci_runbook.md)
remain required and unmet while the current hosted hold applies. A normal
CI build that resolves dependencies afresh is useful source-matrix evidence
but does not prove the frozen candidate's dependency set. Candidate-specific
validation must install the retained wheel/lock or execute the retained image;
record the association rather than treating a same-version rebuild as equal.
Keep supported-runtime matrix results distinct from the selected Python 3.12
Linux amd64 runtime.

Audit the frozen runtime dependencies, scan the exact candidate's source and
image/native packages, and retain scanner versions, database timestamps and
full sanitized findings. A scanner error or missing report is an incomplete
gate, not zero findings. Every finding needs severity, affected component,
exploitability assessment, fix or bounded exception, named owner and review
date/expiry. Unresolved release blockers prevent qualification. Changing a
component to fix a finding produces a new candidate and affected reruns.

### Hosted assembly

The [Release candidate assembly](../.github/workflows/release-candidate.yml)
workflow is manual-only. It is retained for future use after merge and after
the user explicitly lifts the hosted CI hold; do not dispatch it as part of
current local work. Future authorized dispatch uses `main` with no inputs;
other branches are rejected. There is no automatic pull-request or push
assembly trigger. It records the latest same-SHA
main `push`/manual runs of `ci.yml`, `kubernetes.yml` and `terraform.yml`,
including run/attempt IDs, URLs and conclusions. Main assembly fails its
hosted gate when any required run is missing, pending or failed; it still
attempts to retain the built bundle and evidence. Once hosted work is explicitly
authorized again, the three prerequisites must pass on the selected main
revision using the [CI runbook](ci_runbook.md) before assembly can pass its
hosted gate. Repeated assembly resolves a new bundle and creates a new candidate.

Local builders may attach existing retained evidence with
`--hosted-evidence /absolute/hosted-ci.json` using the workflow's JSON format;
this option does not authorize a hosted request. That file records `source_sha`, repository,
assembly run URL, and the three `gates` with their workflow name, status and
run identities. A hand-edited success label is not hosted qualification:
review retained real-run evidence and preserve its artifacts. Omitting the
file leaves hosted evidence missing. Current unavailable hosted gates must not
be relabeled as passed. Even a passing source-revision matrix is
labeled as independently resolved environments; it does not become an
exact-dependency candidate test through this attachment.

The artifact name includes the source SHA and run attempt, and retention is
90 days from upload. Since the evidence policy requires retention through at
least 90 days **after pilot exit**, export the artifact to durable storage and
record checksums in the M5 index before it expires. Hosted assembly has no
registry publication or deployment step and cannot clear owner, native-security
or selected-environment qualification gates.

## Reuse and promotion

1. Retrieve the complete retained bundle, verify its checksums against the
   trusted manifest/evidence record, and verify the recorded source, platform,
   configuration and provider identities. A checksum carried only with an
   untrusted replacement bundle does not establish provenance.
2. Load the saved image archive and compare Docker's image ID with the
   recorded ID. Run that exact image for candidate image checks. Do not run
   `docker compose build`, rebuild the release Dockerfile, pull a mutable tag,
   or resolve dependencies for an already frozen qualification campaign.
3. When a wheel installation is required, use a clean matching runtime and
   the bundle's complete wheelhouse and hash lock with `--no-index`,
   `--require-hashes` and `--only-binary=:all:`. Verify the installed inventory
   and `pip check` result. Preserve the Python/native runtime identities too;
   the wheel alone does not freeze them.
4. Before promotion, attach immutable links/checksums for all required reports,
   the registry digest (when published under separate authorization), accepted
   configuration and the named review decision. Retain the same image and
   configuration through #162 security, #163 UAT, #164 recovery and #165 load
   gates. #166 records the independent pilot-entry decision.

For example, after verification, an installed-wheel check in a fresh matching
Python environment uses only the retained artifacts:

```bash
python -m venv /absolute/new/installed-check
/absolute/new/installed-check/bin/python -m pip install \
  --no-index --find-links=/absolute/new/candidate/wheelhouse \
  --require-hashes --only-binary=:all: \
  -r /absolute/new/candidate/runtime.lock
/absolute/new/installed-check/bin/python -m pip check
```

The package root and every transitive dependency must remain covered by the
lock. Do not replace this install with `pip install .`, an editable checkout,
or a package-index download. Record the installed-check interpreter/native
runtime and execute the smoke outside the source directory.

The release Dockerfile is a recipe to assemble a new candidate. A pinned base
image plus an online native-package install is not a promise of bit-identical
future rebuilds. Native package inventories explain what was built; the saved
image archive is what preserves it. Byte changes from any rebuild require a
new bundle and validation even if its package version or source SHA matches.

## Change control

Every change records old/new identities, reason, accountable reviewer, affected
gates, required reruns and acceptance. Keep previous failed/superseded evidence.
Never pool runs across revisions, silently substitute artifacts, lower a failed
threshold retrospectively, or edit a passing report to describe a new input.

| Change | Required action before evidence can be reused |
| --- | --- |
| Application/build script/workflow code, packaged assets or package version | New source identity and candidate bundle; repeat build/install, fix regressions, security and applicable hosted gates. Reevaluate affected functional, recovery and performance evidence. |
| Python dependency version/hash, optional extra, resolver output or build tool that changes an artifact | New lock/inventory and candidate; clean install and relevant regressions/integrations plus security scans. Treat unchanged source with changed wheels as a different candidate. |
| Base image, native library/package, interpreter patch, OS/architecture or rebuilt image bytes | New image/inventory and candidate; installed-image smoke, native/security checks and affected runtime, extraction, recovery and load gates. An unchanged tag is irrelevant. |
| Migration code/revision, database engine/extension version, applied schema state | New migration/environment identity; schema upgrade/rollback and coherent restore qualification, relevant catalog/isolation/concurrency tests and review of data compatibility. Application migration changes also require a new bundle. |
| Provider/model, model revision, keyword/vector configuration or root embedding block | Revise the pilot specification and provider identity; assemble the actual tenant-aware production providers and repeat retrieval/answer quality, security, privacy, resource and recovery gates. Installing an extra or sample model does not qualify it. |
| Drivers, placement/hold/tenant/auth policies, topology, queue/retry limits, resource/memory/spool bounds, storage filesystem/mounts, network/TLS or target environment | New sanitized/restricted configuration and environment identities; reviewer maps affected #161–#165 gates. Repeat isolation, hold, integrity and access controls for security/storage changes; repeat load/recovery when capacity or topology changes. |
| Secret/certificate/key rotation with unchanged code and access policy | Record old/new secret-version references and certificate/CA identities without secret values. Repeat TLS/auth reconnect, grant-revocation and retained-key restore checks as applicable; update configuration evidence. Policy, issuer, audience, permissions or crypto changes require the broader security/configuration requalification above. |
| Documentation-only correction outside packaged/build/configuration inputs | Record source and review scope. Evidence may remain associated with the unchanged artifact only when its hashes and all qualified inputs are demonstrably identical. README changes can change the wheel metadata and therefore require artifact comparison. |
| New vulnerability intelligence or expired evidence/exception/attestation with identical artifacts | Rescan/review the exact retained candidate and renew applicable evidence; a new unresolved blocker stops promotion even when artifact hashes match. |

## Durable handoff and outstanding gates

Use the [M5 evidence index](evidence/m5/README.md#ticket-160-release-candidate-assembly)
to record manifest, bundle and report checksums, immutable storage locations,
source/image/configuration/environment identities and explicit limitations.
The [current refresh record](evidence/m5/ticket-160-refresh-2026-09-29/README.md)
provides the current candidate identities, security disposition and artifact
handoff; historical reports retain their original source/image scope.
Retain all needed artifacts for at least 90 days after the pilot exit decision,
longer for unresolved findings. Export expiring CI artifacts to approved durable
storage before expiry. An ignored local directory or an expired link is not
release evidence.

The clean-main rebuild resolves the former branch-source limitation. Release
acceptance still requires an immutable registry image digest, accepted
specification/configuration, all required exact-artifact workflow/regression
evidence, passing applicable hosted checks, and reviewed security findings with
no unresolved release blockers. Administrative ticket closure does not satisfy
those requirements. Environment, audit, UAT, recovery, load and pilot-entry
gates remain separate.
