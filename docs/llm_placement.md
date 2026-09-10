# LLM-assisted placement

`--policy llm` requests a schema-validated placement proposal from a configured
model service. The runner then applies the usual destination, importance,
residency, cooldown, and execution safeguards. A model cannot relax those
controls or execute tools. It sees only object size, current tier, and eligible
destination tiers, never bucket names, object keys, payload bytes, extracted
content, or credentials.

When no provider is configured, or a response is malformed, late, unavailable,
or proposes an ineligible move, the object stays in its current tier. There is
no threshold fallback. `--llm-threshold` and the corresponding API/job field
remain accepted for existing callers but are ignored. The Python
`ThresholdProvider` remains available only when explicitly supplied for
compatibility and replay of historical snapshots.

## Configure a model service

The built-in `HTTPPlacementProvider` calls a deployment-controlled service that
implements the wire contract below. It has no model-vendor or SDK dependency.
Configure the CLI, API process, or background worker that actually evaluates
the policy; queued jobs do not carry endpoint settings or secrets.

| Environment variable | Meaning | Default |
| --- | --- | --- |
| `COGNISTORE_LLM_ENDPOINT` | Full service URL; HTTPS required except loopback HTTP. | Unconfigured; stay. |
| `COGNISTORE_LLM_MODEL` | Model or deployment identifier; required with an endpoint. | None. |
| `COGNISTORE_LLM_MODEL_VERSION` | Model/deployment version recorded in audit metadata. | `1` |
| `COGNISTORE_LLM_API_KEY` | Optional secret sent as `Authorization: Bearer …`. | No authorization header. |
| `COGNISTORE_LLM_TIMEOUT_SECONDS` | Total wall-clock inference budget, including retries; greater than 0 and at most 60. | `10` |
| `COGNISTORE_LLM_MAX_ATTEMPTS` | Maximum attempts, from 1 through 5, within that budget. | `2` |

Inject the API key through your deployment's secret mechanism. Endpoint URLs
cannot contain user/password credentials or fragments. Redirects, transport
retries, and environment proxy settings are disabled. Configuration errors use
fixed messages without printing environment values.

For example, with a service running on loopback and an existing catalog:

```sh
export COGNISTORE_LLM_ENDPOINT=http://127.0.0.1:8080/placement
export COGNISTORE_LLM_MODEL=storage-placement
export COGNISTORE_LLM_MODEL_VERSION=2026-09-10
export COGNISTORE_LLM_TIMEOUT_SECONDS=5
export COGNISTORE_LLM_MAX_ATTEMPTS=2

python -m cognistore.cli --drivers drivers.yaml --catalog-db "$CAT_DB" \
  policy-run demo-bucket --policy llm --allowed-tiers hot,warm --dry-run --json
```

A dry-run may call the configured model to produce a proposal, but performs no
storage or catalog writes and does not persist audit events. Its output reports
the final guarded decision. Writable runs retain the redacted inference evidence
with policy audit/snapshot records. Use `--sync` for an explicit inline writable
run; otherwise the configured worker evaluates the queued job.

## HTTP wire contract

The adapter sends one POST request with JSON content type, an optional bearer
authorization header, and this body:

```json
{
  "model": "storage-placement",
  "prompt": "Versioned placement instructions followed by eligible input JSON",
  "schema": {"type": "object", "...": "complete decision schema supplied at runtime"}
}
```

The service returns the decision directly in its successful response body, not
inside a vendor response envelope or Markdown code block:

```json
{"action":"move","dst_tier":"warm","reason":"Size supports placement in warm storage"}
```

All three fields are required; extra fields and duplicate keys are rejected.
`action` is exactly `move` or `stay`. A move names an eligible destination other
than the current tier. A stay requires JSON `null` as `dst_tier`. `reason` must
be a nonblank string of at most 512 characters. The entire response must be
valid UTF-8 and at most 16,384 bytes. Compressed responses are rejected so their
expansion cannot bypass the response limit. Non-finite numbers, wrong types,
invalid JSON, and invalid destinations produce a safe stay.

HTTP statuses 429, 500, 502, and 503 and connection failures are explicitly
retryable when time remains. Timeouts, including HTTP 408/504, immediately select
a safe stay. Other HTTP/transport failures and invalid responses are not retried.
Retry backoff starts at 50 milliseconds and doubles between attempts, all within
the total inference deadline. A provider result arriving after the deadline
cannot cause a move. Underlying
calls that cannot be canceled retain a bounded worker slot until they finish;
exhausted capacity also produces a safe stay.

## Python adapters and tests

An SDK or internal service can implement `PlacementLLMProvider`, with public
`provider_id`, `model`, and `version` strings and
`complete(prompt, schema, timeout_seconds) -> str`. `CallablePlacementProvider`
adapts the same three-argument callback; its optional `model_version` identifies
the model revision separately from the adapter's `version`. For example, this fully offline adapter
demonstrates the boundary:

```python
import json

from cognistore.core.placement_llm import CallablePlacementProvider
from cognistore.core.policy_factory import build_policy


def complete(prompt: str, schema: dict, timeout_seconds: float) -> str:
    # A real binding passes these arguments to its configured model service.
    return json.dumps({"action": "stay", "dst_tier": None, "reason": "offline example"})


provider = CallablePlacementProvider(
    complete, provider_id="internal-sdk", model="placement-model", version="1",
    model_version="2026-09-10",
)
policy = build_policy("llm", threshold=1048576, allowed_tiers=["hot", "warm"],
                      llm_provider=provider)
decision = policy.evaluate(current_tier="hot", size=1024)
```

Adapters must respect the supplied remaining timeout and may raise
`TransientPlacementError` only for a known retryable failure. Raw provider
exceptions are excluded from user-facing reasons and audit evidence. The
orchestrator owns retries and strict response validation. `FakePlacementProvider`
is also available for deterministic fixtures. The default test suite uses
fakes and HTTP mock transports; it requires no credentials or external service.

Audit evidence includes provider/model/version, prompt and schema versions,
redacted prompt and response, validation/fallback codes, attempted calls, timing,
and the parsed proposal. The runner records the final decision after guardrails.
Retries retain the first successful logical-decision evidence. Distinct fallback
evidence links to it with `inference_retry_of`; repeated identical redacted
failures are deduplicated.
Payloads and keys never enter prompts, and endpoint/authentication data is not
part of audit identity. The HTTP adapter also removes exact configured API-key
values echoed by a service before responses enter inference or audit evidence.
LLM snapshots preserve evidence but are marked
unsupported for deterministic replay: a model identity does not freeze external
service behavior. See [policy datasets](policy_datasets.md) and
[placement controls](placement_controls.md).
