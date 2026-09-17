# REST API v1

CogniStore exposes a versioned FastAPI interface for physical objects,
authoritative catalog records, grounded Ask retrieval, policy evaluation, and
durable background actions. The checked contract is
[`openapi/v1.json`](openapi/v1.json). URI versioning and every response schema
are version 1; additions within v1 remain backward compatible.

## Run the service

Install the package, configure a persistent catalog and tier drivers, and start
the standalone server:

```bash
export COGNISTORE_DRIVERS=./drivers.yaml
export COGNISTORE_CATALOG_DB=./catalog.sqlite3
export COGNISTORE_NATS_URL=nats://127.0.0.1:4222
cognistore-api --host 127.0.0.1 --port 8080
```

The standalone process builds metadata-only Ask retrieval. Deployments that
configure Tantivy, pgvector, or answer generation should construct
`CogniStoreGateway` with a fully assembled `AskService` and pass it to
`create_app`. Configure JWT/OIDC authentication with
`COGNISTORE_AUTH_ISSUER` and `COGNISTORE_AUTH_AUDIENCE`, or pass
`authentication=JWTAuthConfig(...)` to `create_app`. Every `/v1` request then
requires a valid bearer access token before request body handling. Set
`COGNISTORE_AUTHORIZATION_POLICY` or `--authorization-policy` to a JSON role
binding file; authenticated deployments without a policy deny protected
operations. Embedded applications pass `authorization=RBACAuthorizer(...)`.
Unconfigured local deployments retain anonymous access; partial authentication
configuration and an API authorization policy without JWT fail startup.
See [authentication](authentication.md) for discovery, key
rotation, service clients, SDK headers, and deployment guidance.

A deployment can pass `authorization_hook` to `create_app` without changing a
route. It runs after authentication and can inspect `request.state.principal`.
It may add restrictions after the built-in permission checks, but it cannot
grant missing permissions. The [authorization guide](authorization.md) defines
every endpoint's permissions, role combinations, current-policy revalidation,
and trusted local process boundary. RBAC does not provide tenant isolation.

Every application created by `create_app` also serves the same-origin content
discovery interface at `/ui/`; those static routes are deliberately excluded
from the versioned OpenAPI document. The
[content-search sample](content_search_sample.md) composes Tantivy, pgvector,
and offline providers around this interface for a complete runnable workflow.

Queued actions require a worker connected to the same NATS stream and catalog,
with the same authorization policy for authenticated jobs.
Without one, accepted jobs remain durably visible in `queued` state.

Interactive documentation is available at `/docs`; the runtime OpenAPI JSON is
at `/openapi.json`. `/healthz` reports process-level API availability. Backend
health remains observable through its own service probes. Health, documentation,
metrics, and static UI routes remain public; the UI does not supply an OIDC
login flow.

## Endpoint summary

| Method and path | Behavior |
| --- | --- |
| `PUT /v1/objects/{tier}/{bucket}/{key}` | Stream and store up to 16 MiB of raw request bytes with any media type, then publish the placement through `CatalogStore`. |
| `HEAD /v1/objects/{tier}/{bucket}/{key}` | Return size, media type, and generation headers. |
| `GET /v1/objects/{tier}/{bucket}/{key}` | Stream generation-bound object bytes as an attachment with `nosniff` and sandbox headers; one closed, open-ended, or suffix `Range` is normalized before reaching the driver. |
| `DELETE /v1/objects/{tier}/{bucket}/{key}` | Delete physical bytes and the authoritative logical record. |
| `GET /v1/catalog/objects` | Return a bounded keyset page filtered by bucket, prefix, and optional tier. |
| `GET /v1/catalog/objects/{bucket}/{key}` | Return one detached authoritative catalog snapshot. |
| `POST /v1/ask` | Run the version 1 retrieval/Ask contract and retain provider diagnostics, score components, and citations. An optional mode can request metadata, keyword, vector, or full hybrid retrieval. |
| `POST /v1/catalog/importance` | Set or clear attributed importance, audit the change, and return a policy reevaluation. |
| `POST /v1/policies/evaluate` | Evaluate a policy against one current catalog record without moving it. |
| `POST /v1/policies/preview` | Preview a decision with structured reasons, guardrails, and proposed placement changes. |
| `GET /v1/policy-decisions` | Page retained decisions by object, job, or correlation ID, with execution evidence. |
| `GET /v1/policy-decisions/{decision_id}` | Inspect a retained decision and its linked execution outcome. |
| `POST /v1/actions/catalog-scans` | Queue a catalog scan and return `202 Accepted`. |
| `POST /v1/actions/policy-runs` | Queue a policy run and return `202 Accepted`. |
| `GET /v1/jobs/{job_id}` | Read durable queued, running, retrying, succeeded, or failed status. |

The object service coordinates only through `StorageDriver` and `CatalogStore`.
An upload records size, placement, an opaque generation token, and MIME when
supplied; extraction, checksums, embeddings, and keyword indexing still belong
to a catalog scan. There is no cross-backend transaction between object bytes
and the catalog, so a failed catalog publication is reported as a backend
failure and a later scan is the repair path.

Raw object bodies are consumed incrementally and rejected with 413 as soon as
the 16 MiB limit is exceeded, including chunked requests without
`Content-Length`. JSON bodies for Ask, policy, and action endpoints are bounded
to 256 KiB before parsing and use the same 413 behavior for declared or chunked
oversize requests. Policy configuration additionally limits each pattern to
1,024 characters, each MIME prefix to 255 characters, each embedding-rule name
to 256 characters, each embedding prototype query to 16,384 characters, and
all policy strings together to 64 KiB of UTF-8. Embedding rules are available only to the
`content` policy, use unique names and allowed destination tiers, and accept
cosine thresholds from -1 through 1.

Policy evaluation is the side-effect-free dry-run contract. Its response
includes the schema-v1 MIME and embedding feature projection with explicit
`fresh`, `missing`, `stale`, or `unavailable` state and source provenance.
Configured non-fresh signals fail closed before size fallback. See the
[policy feature projection](policy_features.md) for rule order and reindexing
semantics. [Importance and minimum residency](placement_controls.md) describes
`config.movement_constraints`, trusted classification changes, and the
constraint evidence returned by evaluation.

The [placement explanations](placement_explanations.md) resources use one
decision schema for previews and retained decisions. Placement diffs describe
the proposal; execution mode, per-decision state, and durable job status remain
separate. The `/ui/` Placement view renders these resources, including retained
rule and guardrail reasons when model details are unavailable.

Public object and catalog metadata excludes storage paths, generations, parser
state, extraction text, and content-identity internals. The remaining mapping
is bounded to 16 KiB; `metadata_truncated` reports when only safe core fields
could be retained. Ask citations apply the same internal-field exclusion and
already expose their own truncation flags.

## Pagination

Catalog collection requests take `limit` from 1 through 200 and return:

```json
{
  "schema_version": 1,
  "items": [],
  "page": {
    "limit": 50,
    "next_cursor": null
  }
}
```

`next_cursor` is an opaque URL-safe keyset token. It binds the last key to the
bucket, prefix, tier, resource, and cursor schema. A token reused with different
filters returns `422 invalid_cursor`; clients must not decode or construct it.
The DAL fetches at most `limit + 1` rows and never materializes the complete
bucket for an API page.

Ask is a bounded ranked top-k operation rather than an offset page. Its result,
candidate, and passage limits are part of the Ask v1 contract.

## Retrieval selection

`POST /v1/ask` accepts an optional `retrieval_mode` with one of the response
mode values:

| Value | Providers requested |
| --- | --- |
| `metadata` | Authoritative catalog metadata only |
| `metadata+keyword` | Catalog metadata and keyword search |
| `metadata+vector` | Catalog metadata and vector similarity |
| `metadata+keyword+vector` | All retrieval providers (default) |

Catalog metadata is always the authoritative base signal. A provider outside
the requested mode is not invoked and reports `not_requested`. If a requested
optional provider is missing or unavailable, the request still returns 200,
its diagnostic explains the degradation, and `mode`/`active_signals` describe
only the providers that actually succeeded. `synthesize=true` independently
requests answer generation over the resulting cited context.

## Errors and request identity

All pre-response failures use one envelope. Backend exception messages,
payload values, and credentials are not copied into server-failure responses.

```json
{
  "schema_version": 1,
  "request_id": "79926b95-2fd4-40ad-b6b6-e78dfae5bb70",
  "error": {
    "code": "resource_not_found",
    "message": "The requested resource was not found",
    "retryable": false,
    "details": []
  }
}
```

The server preserves a bounded `X-Request-ID` containing identifier characters
or generates a UUID, then echoes it on the response. Validation errors use 422,
missing resources 404, conflicts 409, queue saturation and unavailable
backends 503 with `Retry-After`, and unexpected backend failures 500. Ask's
documented optional-provider degradation is a successful 200 response with
provider diagnostics, not a transport error.

Object uploads and JSON command bodies over their hard limits return 413.
Invalid, multiple, or unsatisfiable byte ranges return 416 with
`Content-Range: bytes */{size}`;
satisfiable ranges return 206 with exact `Content-Range` and `Content-Length`.
Configured authentication returns `401 authentication_required` with
`WWW-Authenticate: Bearer` for missing credentials, or `401 invalid_token` with
`WWW-Authenticate: Bearer error="invalid_token"` for invalid credentials or
unavailable required signing keys. Authentication failures use safe generic
messages. A missing permission returns a generic 403 before resource lookups,
without revealing whether the requested resource exists. The additional
authorization hook may also deny access, and
challenge headers such as `WWW-Authenticate` are preserved.

## Asynchronous status

Action submission creates a versioned `JobEnvelope`, records `job.queued` in
the catalog audit DAL, and publishes through `JobQueue`. The `202` response
contains the job ID, correlation ID, type, current state, timestamps, attempt,
and `status_url`; its `Location` header points to the same URL.

API jobs carry an explicit status-tracking metadata marker. Workers record
`job.started` before invoking a handler and `job.succeeded` after durable
handler/coordinator completion but before acknowledging the source delivery.
Existing retry, failure, and dead-letter evidence supplies retrying and failed
states. Status therefore survives API and worker restarts and preserves the
same audit retention policy as other operational evidence. A pruned or unknown
job returns 404.

Authenticated submissions carry only the normalized principal in reserved job
metadata, allowing workers to attribute audit events without persisting access
tokens or role snapshots. Workers check current bindings again before execution,
including every retry and redrive; revocation can therefore reject a previously
accepted job. Broker publishers are trusted to produce this internal metadata; protect
broker access accordingly. See [identity propagation](authentication.md#jobs-and-audit-attribution).

## Contract generation

FastAPI, Pydantic, and Uvicorn versions are pinned because their schema
generation is part of the public artifact. Regenerate and validate the sorted
document with:

```bash
python -m cognistore.api.openapi docs/openapi/v1.json
python -m cognistore.api.openapi --check docs/openapi/v1.json
```

The check validates OpenAPI 3.1, rejects duplicate operation IDs, and performs
an exact byte comparison. CI runs it after dependency installation.
