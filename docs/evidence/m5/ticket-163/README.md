# Ticket #163: local manual acceptance preparation and rehearsal

**Local work only; staging acceptance remains incomplete.** The user explicitly
excluded real cloud resources requiring an account or billing credentials.
No cloud resources, external identity/model services, or production data were
used. Do not close #163 or claim pilot entry from this evidence.

The [runbook](../../../manual_acceptance.md) and
[matrix](../../../manual_acceptance_matrix.json) define 16 scenarios and 80
interface/role/tenant cases. The checked-in corpus, record generator and
validator make subsequent runs repeatable. The validator distinguishes a valid
partial record from completed manual acceptance, checks evidence hashes and
required repetitions, and rejects incomplete or stale post-fix reruns.

The application under test is unchanged from source
`8d3d50bb649cea617fe030859c51b196457e4648`. The new helper/tooling was developed in
`chore/163-manual-acceptance`; [validation.json](validation.json) binds the tested
working-tree files by SHA-256 and records commands and results. This is a
macOS/CPython 3.12 local source rehearsal, with SQLite and two POSIX roots,
synthetic RS256 JWTs and two tenants (`pilot-a`, `pilot-b`). It is not the frozen
Linux/PostgreSQL/S3/OIDC/TLS candidate. The source and evidence are versioned
together by the Git commits containing this directory.

Final automated validation: **529 tests passed** (349 applicable tooling/product
regressions and 180 separate broker/movement/repair tests). Ruff, mypy and
Bandit passed. The local acceptance record contains **12 passed, two failed and
66 incomplete cases**; these are a different population from automated tests.

## Retained observations

- [acceptance.json](acceptance.json) records each matrix case's actual scope,
  expected result, tester, source/configuration binding, repetitions and evidence.
  [acceptance-validation.json](acceptance-validation.json) retains the honest
  incomplete verdict. Passing a local case does not pass staging qualification.
- [local/rehearsal.json](local/rehearsal.json) contains actual loopback HTTP/API,
  SDK and separate default-tenant CLI observations. CLI movement follows durable
  move IDs to terminal state and compares checksums. API/SDK observations include
  bytes, ranges, role/token denials, holds, tenant IDs and missing providers.
  [local/cli-commands.json.gz](local/cli-commands.json.gz) retains CLI commands and
  results. Its 114 observations contain 112 successes and the two retained
  upload failures. Each new helper run retains its own immutable run directory.
- [browser/session.json](browser/session.json) records actual agent-operated
  browser actions, expected/observed states, screenshots and limitations.
  Relevant/no-match Ask queries were repeated three times for each tenant.
  Reader/auditor view restrictions, scoped policy confirmation/cancellation,
  explicit queue failure, failed job inspection and audit integrity were
  exercised. A cited download was clicked, but its saved bytes were not verified
  through the browser; API/SDK byte checks do not fill that gap.
- [corpus/manifest.json](corpus/manifest.json) binds the prepared deterministic
  corpus: duplicate bytes, distinct same-key tenant payloads, PDF/DOCX,
  corrupt/encrypted/unsupported documents, image-only PDF, synthetic PII and
  limit fixtures. The default 64 KiB limit fixtures are reduced local inputs,
  not evidence of the API's 16 MiB boundary. The live helper uses its separately
  documented byte generator and document assets; generating this corpus does
  not mean every fixture was exercised.
- [nats/metadata.json](nats/metadata.json) and
  [nats/junit.xml.gz](nats/junit.xml.gz) retain **180 passing supplemental
  automated tests** for NATS/JetStream, durable movement and consistency repair.
  NATS 2.10.26 ran from an existing pinned local image, bound only to loopback,
  with 256 MiB/one CPU and private disposable storage. Its container was stopped
  and removed. This was separate from the browser fixture and does not establish
  a successful browser/API submission through a production worker.

The final client rehearsal ran in a fresh fixture on port 8764. Browser evidence
comes from the earlier port 8763 fixture. Both use unchanged application source;
[client configuration](local/configuration.json) and
[browser configuration](browser/configuration.json) retain their separate
identities. [Runtime package versions](local/runtime-packages.json) describe the
client/server rehearsal environment. Both owned servers were stopped.

## Open finding: oversized eager HTTP upload

`LOCAL-163-001` remains open. An eager HTTPX upload of 16 MiB + 1 byte received
a connection reset rather than an inspectable HTTP 413 response. Both tenants'
observed outcomes are retained, along with the
[first failure](local/oversize-reset-first-observation.json) and
[initial rehearsal](local/rehearsal-initial.json.gz).
The [single curl diagnostic](local/oversize-curl-observation.json), using
`Expect: 100-continue`, received HTTP 413 and `payload_too_large` successfully.
That diagnostic does not erase the eager-client failure. The report keeps
upload-boundary acceptance failed until its behavior and intended client
contract are resolved and affected cases rerun. No product fix is claimed.

## Limits and reproduction

Use the commands in [validation.json](validation.json) and the runbook from the
recorded checkout with the declared dependencies installed. Review the actual
record instead of summing overlapping preliminary and final test runs.

The browser fixture has no broker: the confirmed submission correctly failed
and retained a failed job with zero worker attempts. Full queued API/SDK/browser
execution, registered repairs, all identity/tenant combinations and all
extraction/PII boundaries remain incomplete. PII was disabled, and extraction
used explicit filename MIME fallback because native libmagic was unavailable.
The browser's fixture session selector is not an OIDC/proxy implementation.
No human tester signoff, physical encryption, production provider quality,
accepted staging configuration or immutable registry image was verified.

No application or dependency definitions changed. Applicable local tooling,
UI, SDK, auth, tenant, hold, PII and storage/job regressions were run; the full
product suite, complete coverage campaign, package rebuild and dependency audit
were not repeated for this tooling/documentation change. The retained security
scanner initially flagged a fixed temporary-directory default; the helper now
requires an explicit fresh private root and tests preservation of existing data.

Hosted CI: `skipped: user instruction; known GitHub billing/spending restriction`.

Verify retained artifacts from this directory:

```bash
shasum -a 256 -c SHA256SUMS
```

The repository is the durable evidence location; restricted repository URIs in
the record refer to these versioned files, not an external artifact service.
Retain them under the [M5 evidence contract](../README.md). Private fixture
tokens, signing keys, runtime databases and object roots are excluded.
