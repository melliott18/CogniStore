# Python SDK

The synchronous, typed Python SDK exposes the complete CogniStore REST API v1
workflow without importing server, catalog, or storage-driver internals. Request and
response bodies are validated models, while object bodies and HTTP metadata use
dedicated typed response objects.

## Install

CogniStore supports CPython 3.10 through 3.14. From a clean checkout, install the
package and its SDK in a new virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
```

A locally built release wheel can be installed into the clean environment with
`python -m pip install /path/to/cognistore-<version>-py3-none-any.whl`. The SDK and
its HTTP dependency are included in the default installation; no SDK-specific extra
is required.

The REST service must be running separately. See the [REST API v1 guide](rest_api.md)
for server and worker configuration.

## Minimal query

`CogniStoreClient` is a context manager. Closing it releases its connection pool:

```python
from cognistore.sdk import AskRequest, CogniStoreClient

with CogniStoreClient("http://127.0.0.1:8080") as client:
    response = client.ask(
        AskRequest(
            text="Which documents describe storage lifecycle policy?",
            retrieval_mode="metadata+keyword+vector",
            limit=3,
        )
    )

print(response.model_dump_json(indent=2))
```

The same program is shipped in the wheel and mirrored at
`examples/python_sdk_query.py`. Start the API, then run either the installed
module or the checkout wrapper:

```bash
python -m cognistore.sdk.examples.minimal_query
python examples/python_sdk_query.py
```

Ask can return an empty `results` list when no indexed content matches. Optional
keyword, vector, and generation providers report their state in the successful
typed response; provider degradation is not a transport error.
`AskRequest.retrieval_mode` can select `metadata`, `metadata+keyword`,
`metadata+vector`, or the default `metadata+keyword+vector`. Providers outside
the selected mode report `not_requested` and are not invoked. When left at its
default, the SDK omits this newly added field on the wire so older strict v1
servers retain their equivalent hybrid default; setting it explicitly sends it.

## Configuration and timeouts

For simple use, pass the v1 server's origin as a string. The client appends `/v1`
for versioned operations, so do not include `/v1` in `base_url`:

```python
from cognistore.sdk import CogniStoreClient

client = CogniStoreClient(
    "https://cognistore.example.com",
    timeout=30.0,
    default_headers={"Authorization": "Bearer <token>"},
)
```

Keep credentials in an environment variable or secret store rather than source
code. `default_headers` applies to every request and can also carry deployment-
specific authentication headers. The SDK creates and owns its HTTP connection pool
unless an advanced caller injects `http_client`; an injected client remains owned
by the caller.

`timeout` is the HTTP request timeout in seconds. A request deadline raises
`RequestTimeoutError`, which is distinct from both a server-generated `APIError`
and other `TransportError` failures. Choose a timeout that covers the longest
synchronous operation; queued catalog scans and policy runs should use job polling
instead of an unusually long HTTP timeout.

`ClientConfig` keeps reusable connection and polling settings together:

```python
import os

from cognistore.sdk import ClientConfig, CogniStoreClient

config = ClientConfig(
    base_url=os.environ.get("COGNISTORE_URL", "http://127.0.0.1:8080"),
    timeout=30.0,
    default_headers={
        "Authorization": f"Bearer {os.environ['COGNISTORE_TOKEN']}"
    },
    poll_interval=1.0,
    max_poll_interval=30.0,
)

with CogniStoreClient(config) as client:
    health = client.get_health()
```

`poll_interval` sets the delay between job-status checks and `max_poll_interval`
caps configured and server-requested polling delays. Constructor overrides apply to
one client; `wait_for_job` arguments can override the timeout and interval for one
job.

## Method coverage

Every operation in the checked [OpenAPI v1 contract](openapi/v1.json) has a typed
method. The two convenience methods do not add server endpoints.

| Client method | REST operation | Typed result |
| --- | --- | --- |
| `get_health()` | `GET /healthz` | `HealthResponse` |
| `put_object(tier, bucket, key, content, ...)` | `PUT /v1/objects/{tier}/{bucket}/{key}` | `ObjectResource` |
| `head_object(tier, bucket, key)` | `HEAD /v1/objects/{tier}/{bucket}/{key}` | `HeadObjectResponse` |
| `get_object(tier, bucket, key, ...)` | `GET /v1/objects/{tier}/{bucket}/{key}` | `ObjectDownload` |
| `delete_object(tier, bucket, key)` | `DELETE /v1/objects/{tier}/{bucket}/{key}` | `DeleteObjectResponse` |
| `list_catalog_objects(bucket, ...)` | `GET /v1/catalog/objects` | `CatalogObjectPage` |
| `iter_catalog_objects(bucket, ...)` | Repeated catalog-page requests | Iterator of `CatalogObject` |
| `get_catalog_object(bucket, key)` | `GET /v1/catalog/objects/{bucket}/{key}` | `CatalogObject` |
| `ask(request)` | `POST /v1/ask` | `AskResponse` |
| `evaluate_policy(request)` | `POST /v1/policies/evaluate` | `PolicyEvaluationResponse` |
| `submit_catalog_scan(request)` | `POST /v1/actions/catalog-scans` | `JobStatus` |
| `submit_policy_run(request)` | `POST /v1/actions/policy-runs` | `JobStatus` |
| `get_job_status(job_id)` | `GET /v1/jobs/{job_id}` | `JobStatus` |
| `wait_for_job(job, ...)` | Repeated job-status requests | Terminal `JobStatus` |

`put_object` accepts bytes and exposes the API's `overwrite` and `content_type`
options. `get_object` accepts one HTTP byte range such as `bytes=0-1023` and returns
the bytes together with typed content length, content type, entity tag, range, and
request-ID metadata. Request models such as `AskRequest`,
`PolicyEvaluationRequest`, `CatalogScanRequest`, and `PolicyRunRequest` are exported
from `cognistore.sdk` with their response models.

## Pagination

Catalog pages use opaque, filter-bound keyset cursors. Pass a returned cursor back
unchanged with exactly the same bucket, prefix, and tier filters:

```python
from cognistore.sdk import CogniStoreClient

with CogniStoreClient("http://127.0.0.1:8080") as client:
    page = client.list_catalog_objects(
        bucket="research",
        prefix="papers/",
        tier="warm",
        limit=100,
    )
    for item in page.items:
        print(item.key)

    if page.page.next_cursor is not None:
        next_page = client.list_catalog_objects(
            bucket="research",
            prefix="papers/",
            tier="warm",
            limit=100,
            cursor=page.page.next_cursor,
        )
```

Do not decode, construct, persist as an offset, or reuse a cursor with changed
filters. For ordinary iteration, let the SDK carry cursors and preserve filters:

```python
with CogniStoreClient("http://127.0.0.1:8080") as client:
    for item in client.iter_catalog_objects(
        bucket="research",
        prefix="papers/",
        tier="warm",
        limit=100,
    ):
        print(item.key)
```

Ask is a bounded ranked top-k query, not a paginated collection.

## Typed errors

SDK failures are separated by where they occurred:

| Exception | Meaning |
| --- | --- |
| `APIError` and its subclasses | The server returned any non-success HTTP response. |
| `RequestTimeoutError` | The HTTP request exceeded its configured deadline. |
| `TransportError` | The server could not be reached or another HTTP transport failure occurred. |
| `ResponseContractError` | A successful response was malformed, incompatible, or not a supported v1 payload. |
| `PollingTimeoutError` | `wait_for_job` did not observe a terminal state before its deadline. |
| `JobFailedError` | A polled job reached `failed` while `raise_on_failure=True`. |

An `APIError` retains `.status_code`, `.code`, `.request_id`, `.retryable`,
`.details`, and `.retry_after`. Log the request ID for server-side correlation and
use `retryable` plus `retry_after` to inform retry policy; do not retry every 4xx
response automatically. Known status codes use `AuthenticationError`,
`PermissionDeniedError`, `NotFoundError`, `ConflictError`,
`PayloadTooLargeError`, `RangeNotSatisfiableError`, `ValidationError`,
`ServiceUnavailableError`, or `ServerError`; catching their `APIError` base also
covers unknown codes and bodyless error responses.

```python
from cognistore.sdk import APIError, CogniStoreClient

try:
    with CogniStoreClient("http://127.0.0.1:8080") as client:
        item = client.get_catalog_object("research", "missing.txt")
except APIError as exc:
    print(
        f"CogniStore {exc.code} (HTTP {exc.status_code}, request {exc.request_id})"
    )
    if exc.retryable:
        print(f"retry after {exc.retry_after!r} seconds")
```

Constructing an invalid typed request can raise Pydantic's `ValidationError` before
an HTTP request is sent.

## Asynchronous actions and polling

Catalog scans and policy runs return immediately in a non-terminal state. Submit a
typed request, then pass either its `JobStatus`, job UUID, or UUID string to
`wait_for_job`:

```python
from cognistore.sdk import CatalogScanRequest, CogniStoreClient

with CogniStoreClient("http://127.0.0.1:8080") as client:
    submitted = client.submit_catalog_scan(
        CatalogScanRequest(tier="hot", bucket="research", prefix="papers/")
    )
    completed = client.wait_for_job(
        submitted,
        timeout=300.0,
        poll_interval=1.0,
    )

print(completed.status)
```

`wait_for_job` returns only `succeeded` by default and raises `JobFailedError` for
`failed`. Pass `raise_on_failure=False` to receive and inspect a failed terminal
`JobStatus` instead. The timeout is a total polling deadline measured independently
of wall-clock changes. Timing out does not cancel the durable server job; retain the
job ID and resume with `get_job_status` or another `wait_for_job` call.
During status polling, retryable `APIError` responses are retried and a numeric
`Retry-After` is honored up to `max_poll_interval`.

Action submission has no client idempotency key in API v1. If the submission's HTTP
exchange fails after the server may have accepted it, do not automatically repeat
the `submit_*` call: doing so can create a duplicate job. Reconcile the first
submission through application records or operator audit evidence before retrying.

## API v1 compatibility and deprecation policy

The SDK targets REST API v1 and validates `api_version` and `schema_version` values
where the contract provides them. SDK and server package versions do not need to
match exactly:

- Backward-compatible server additions, including new optional response fields,
  remain within `/v1`. The SDK tolerates unknown additive response fields.
- A server response with an unsupported API or schema major version raises
  `ResponseContractError` instead of silently producing a partially typed result.
- A breaking HTTP contract change requires a new URL major such as `/v2` and a new
  documented SDK compatibility line. The v1 methods continue targeting `/v1`.
- New SDK methods, optional arguments, and model fields may be added in a minor
  release. Patch releases contain compatible fixes.
- A Python SDK symbol or behavior scheduled for removal first emits
  `DeprecationWarning`, identifies its replacement in documentation and release
  notes, and remains available for at least the next minor release. During the
  pre-1.0 series, removal may then occur in a minor release; after 1.0, removal is
  reserved for a major SDK release.

Pin an SDK version when reproducibility is required, surface deprecation warnings in
CI, and upgrade before deploying against a new REST API major.
