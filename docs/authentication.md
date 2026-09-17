# JWT and OIDC authentication

CogniStore can authenticate human and service clients with signed JWT access
tokens from an external identity provider. Configure a trusted issuer and API
audience to require authentication for every `/v1` request, including reads,
object downloads, actions, and job status. Authentication runs before request
bodies are consumed and before built-in RBAC and the optional authorization
hook.

With no authentication configuration, the API retains anonymous access for
local development. Set both issuer and audience before exposing a deployment
to clients, and configure an authorization policy to grant protected
operations. A partial authentication configuration fails startup instead of
falling back to anonymous access. `/healthz`, `/metrics`, static `/ui/` assets, `/docs`, and
OpenAPI documentation remain public; serving the UI does not authenticate its
API requests or provide a login flow.

Authentication establishes identity. [Role-based authorization](authorization.md)
uses a trusted policy file to assign permissions to exact issuer/subject pairs.
An authenticated deployment without an authorization policy denies protected
operations. Token role claims do not grant access, and neither authentication
nor RBAC partitions data by tenant.

## Configure the API

Register CogniStore as an API/resource with the identity provider and use its
exact issuer URL and audience identifier:

```bash
export COGNISTORE_DRIVERS=./drivers.yaml
export COGNISTORE_CATALOG_DB=./catalog.sqlite3
export COGNISTORE_NATS_URL=nats://127.0.0.1:4222
export COGNISTORE_AUTH_ISSUER=https://identity.example.com/realms/storage
export COGNISTORE_AUTH_AUDIENCE=cognistore-api
export COGNISTORE_AUTHORIZATION_POLICY=/etc/cognistore/authorization.json
cognistore-api --host 127.0.0.1 --port 8080
```

Serve client traffic over HTTPS, for example through a trusted TLS reverse
proxy to this loopback listener. Preserve the `Authorization` header and keep
bearer tokens out of proxy access logs. Provider client secrets belong in the
client's secret store; the API verifies public signing keys and does not need
a provider client secret.

| API flag | Environment variable | Behavior |
| --- | --- | --- |
| `--auth-issuer` | `COGNISTORE_AUTH_ISSUER` | Required trusted HTTPS issuer, matched exactly against `iss`. |
| `--auth-audience` | `COGNISTORE_AUTH_AUDIENCE` | Required API audience, matched against `aud`. |
| `--auth-jwks-uri` | `COGNISTORE_AUTH_JWKS_URI` | Optional trusted HTTPS JWKS endpoint; when omitted, obtain it through OIDC discovery. |
| `--auth-algorithm` (repeatable) | `COGNISTORE_AUTH_ALGORITHMS` (comma-separated) | Explicit signing algorithm allowlist; default `RS256`. |
| `--auth-required-claim` (repeatable) | `COGNISTORE_AUTH_REQUIRED_CLAIMS` (comma-separated) | Required claims in addition to mandatory validation; default `sub,exp,iat`. |
| `--authorization-policy` | `COGNISTORE_AUTHORIZATION_POLICY` | JSON role bindings for protected operations; see the [policy format and matrix](authorization.md). |

Explicit flags override their corresponding environment variables. Repeated
algorithm and claim flags replace the environment list rather than appending
to it.

For example, a provider that uses ES256 can be configured with
`--auth-algorithm ES256`. Enable only the algorithms used by the provider.
Supported algorithms are RS256/384/512, PS256/384/512, ES256/384/512, and EdDSA.
Unsigned tokens and symmetric HMAC algorithms are not accepted. The token's
algorithm cannot expand the configured allowlist.

Without `--auth-jwks-uri`, discovery uses the issuer's
`/.well-known/openid-configuration` document. The discovery issuer must match
the configured issuer exactly, including any trailing slash, and its
`jwks_uri` must use HTTPS. An explicit JWKS URI skips discovery and still
requires an exact token issuer match. Signing-key URLs supplied in token
headers are not trusted.

## Token requirements and key rotation

Clients send one header:

```http
Authorization: Bearer <access-token>
```

Use a JWT **access token** issued for the configured API audience. ID tokens
are for a client's sign-in session and must not be used as API credentials.
Service clients obtain access tokens using their provider's client-credentials
flow and send them through the same interface as human clients. Opaque access
tokens and token introspection are not supported.

Validation checks the signature, the explicit algorithm allowlist, exact
issuer, audience, expiry, and required claims. `iss`, `aud`, `sub`, and `exp`
remain mandatory even when the required-claim list is customized. `sub` must
be a nonempty bounded string. Time claims must be numeric dates; malformed
values fail authentication. `iat` is required by default, and future `iat` or
`nbf` values are rejected outside the configured clock-skew allowance. The
default allowance is 30 seconds. Additional required claims enforce presence;
they do not create a scope, role, or permission check.

JWKS keys are cached for 300 seconds by default. A token with an unknown `kid`
can trigger an early refresh so that new signing keys work without a restart.
Repeated unknown-key refresh attempts are limited by a 30-second interval to
bound requests to the identity provider; an unknown key received during that
interval is rejected and can be retried after the interval. Unknown keys, invalid signatures, and
failed validation never fall back to accepting an unverified token.

If discovery or JWKS retrieval is unavailable, an already cached key can only
be used while the cache is fresh. Once it expires, authentication fails closed
until keys can be refreshed. Publish new keys before issuing tokens with them
and retain old keys through the lifetime of previously issued tokens. Account
for the cache TTL when planning key retirement or emergency revocation; there
is no per-token revocation lookup.

## Embedded applications

Pass authentication alongside a configured gateway:

```python
from cognistore.api import create_app
from cognistore.auth import current_principal
from cognistore.auth.authorization import RBACAuthorizer
from cognistore.auth.jwt import JWTAuthConfig

authentication = JWTAuthConfig(
    issuer="https://identity.example.com/realms/storage",
    audience="cognistore-api",
    algorithms=("RS256",),
    required_claims=("sub", "exp", "iat"),
    leeway_seconds=30,
    cache_ttl_seconds=300,
    refresh_interval_seconds=30,
    timeout_seconds=5,
)

app = create_app(
    gateway,
    authentication=authentication,
    authorization=RBACAuthorizer(policy_path="authorization.json"),
)
```

`create_app` also accepts an existing `JWTAuthenticator`. Request handlers and
an optional `authorization_hook` can inspect `request.state.principal`;
`current_principal()` exposes the same operation-scoped identity to service
code. Anonymous operation has no principal. The immutable `Principal` holds
only `issuer`, `subject`, and optional `client_id`; `client_id` or `azp` provides
client attribution when present. The issuer-scoped `subject` remains the
identity for both human and service clients. The optional hook imposes an
additional restriction after built-in authorization; it cannot grant missing
RBAC permissions.

## Python SDK

Obtain and refresh tokens with the identity provider's supported client
library, then supply the token as a default header:

```python
import os

from cognistore.sdk import CogniStoreClient

with CogniStoreClient(
    "https://cognistore.example.com",
    default_headers={
        "Authorization": f"Bearer {os.environ['COGNISTORE_TOKEN']}"
    },
) as client:
    page = client.list_catalog_objects("demo-bucket")
```

The SDK sends the supplied token; it does not perform sign-in, acquire tokens,
or refresh them. Keep token values out of source code and diagnostics. Recreate
the client with a fresh header when replacing an expired token. See the
[SDK guide](python_sdk.md) for configuration and typed API errors.

## Errors

Authentication failures use the normal versioned API error envelope and
request ID. They do not disclose tokens, claim contents, signing keys, or
provider failure details.

| Condition | HTTP status | Error code | Challenge |
| --- | --- | --- | --- |
| Missing credentials | `401` | `authentication_required` | `WWW-Authenticate: Bearer` |
| Malformed, invalid, expired, wrong-audience, or unknown-key token; required keys unavailable | `401` | `invalid_token` | `WWW-Authenticate: Bearer error="invalid_token"` |
| Authenticated principal lacks a required permission, or current authorization policy cannot be loaded | `403` | `forbidden` | None |

An authenticated principal lacking a required RBAC permission receives a
generic `403` without resource-existence details. A deployment's authorization
hook can also deny an otherwise permitted request. When investigating a
`401`, check the client token's intended audience and lifetime, the exact
configured issuer, provider key rotation, and API reachability to the trusted
discovery/JWKS endpoint without copying tokens into tickets or logs.

## Jobs and audit attribution

Authenticated API submissions add the normalized identity to reserved job
metadata `cognistore.principal`. Workers restore this identity for execution,
including retry and failure attribution. The job persists only the issuer,
subject, and optional client ID, never the access token, signature, raw claims,
or provider credentials. Background execution does not retain a token to
revalidate after its expiry. Workers instead reread the current authorization
policy and recheck required permissions on every delivery, including retries
and redrives. An authenticated job is denied if its worker lacks a policy.

Audit events use actor type `authenticated` and a stable
`principal:sha256:<digest>` actor ID derived from an unambiguous encoding of
the issuer and subject. `Principal.actor_id` provides the exact query value.
Identical subject strings from different issuers have distinct actor IDs.
Client-supplied attribution fields do not replace the verified audit actor.
See [operational audit events](audit_events.md) for retention and querying.

Job metadata is an internal propagation contract, not signed authentication
proof. Protect NATS with appropriate network access and publisher credentials:
any trusted publisher able to submit job envelopes can supply that metadata.
Restrict publication to trusted producers. Direct CLI and scheduler jobs do
not carry authenticated principals, so workers with an authorization policy
reject them. Their existing local behavior remains available only in trusted,
unconfigured deployments. See [worker revalidation](authorization.md#worker-revalidation) and
[background workers](background_workers.md) for deployment configuration.
