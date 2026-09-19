# Role-based authorization

CogniStore checks explicit permissions at the REST API, gateway, and worker
boundaries. JWT authentication establishes an issuer-scoped identity; a trusted
local policy file assigns that identity roles. An authenticated API with no
authorization policy denies protected operations. Roles in token claims,
request bodies, headers, or job metadata do not grant access.

Permissions apply within the identity's assigned tenant when a
[tenant membership policy](tenancy.md) is configured. Roles do not grant
cross-tenant access. Without tenant configuration, permissions apply to the
legacy `default` tenant. External policy engines are not supported.

## Roles and permissions

Role names and permission names are exact and case-sensitive. Multiple assigned
roles contribute the union of their permissions; an operation requiring more
than one permission needs all of them.

| Role | Permissions | Intended access |
| --- | --- | --- |
| `reader` | `read`, `legal_hold_inspect` | Object bytes and metadata, search, job status, and hold inspection. |
| `writer` | `read`, `write`, `legal_hold_inspect` | Read, upload, and delete objects subject to active holds. |
| `policy_manager` | `read`, `policy`, `legal_hold_inspect` | Evaluate and preview placement policies. |
| `operator` | `read`, `movement`, `administration`, `legal_hold_inspect` | Catalog scans and movement authority. |
| `auditor` | `audit`, `legal_hold_inspect` | Retained policy decisions, audit history, integrity verification, evidence export, and hold history. |
| `hold_manager` | `legal_hold_inspect`, `legal_hold_manage` | Inspect and place legal holds. |
| `hold_releaser` | `legal_hold_inspect`, `legal_hold_release` | Inspect and explicitly release legal holds. |
| `admin` | All permissions listed above | Every permission, subject to active holds. |

For example, `policy_manager` plus `operator` can run policies that move data;
neither role alone can run them. `writer` plus `policy_manager` can change
importance because that operation also evaluates caller-supplied policy
configuration. `auditor` does not grant object download or general job-status
access; linked execution evidence is part of the retained decision response.

## Endpoint and operation matrix

Every `/v1` operation has an explicit mapping. Authorization runs before
resource lookup, storage access, retrieval-provider calls, or job submission.
The gateway enforces the same mapping for service callers using an authorizer.
No permission depends on a control being hidden in the UI.

| Method and path | Gateway operation | Required permissions |
| --- | --- | --- |
| `PUT /v1/objects/{tier}/{bucket}/{key}` | `put_object` | `write` |
| `HEAD /v1/objects/{tier}/{bucket}/{key}` | `stat_object` | `read` |
| `GET /v1/objects/{tier}/{bucket}/{key}` | `open_object` | `read` |
| `DELETE /v1/objects/{tier}/{bucket}/{key}` | `delete_object` | `write` |
| `GET /v1/catalog/objects` | `list_catalog_objects` | `read` |
| `GET /v1/catalog/objects/{bucket}/{key}` | `get_catalog_object` | `read` |
| `POST /v1/ask` | `ask` | `read` |
| `POST /v1/catalog/importance` | `set_importance` | `write`, `policy` |
| `POST /v1/policies/evaluate` | `evaluate_policy` | `policy` |
| `POST /v1/policies/preview` | `preview_policy` | `policy` |
| `GET /v1/policy-decisions` | `list_policy_decisions` | `audit` |
| `GET /v1/policy-decisions/{decision_id}` | `get_policy_decision` | `audit` |
| `GET /v1/audit/events` | `list_audit_events` | `audit` |
| `GET /v1/audit/events/{event_id}` | `get_audit_event` | `audit` |
| `GET /v1/audit/export` | `export_audit_events` | `audit` |
| `POST /v1/audit/verify` | `verify_audit_integrity` | `audit` |
| `POST /v1/actions/catalog-scans` | `submit_catalog_scan` | `administration` |
| `POST /v1/actions/policy-runs` | `submit_policy_run` | `policy`, `movement` |
| `GET /v1/jobs/{job_id}` | `get_job` | `read` |
| `GET /v1/legal-holds` | `list_legal_holds` | `legal_hold_inspect` |
| `POST /v1/legal-holds` | `place_legal_hold` | `legal_hold_manage` |
| `POST /v1/legal-holds/{hold_id}/release` | `release_legal_hold` | `legal_hold_release` |

[Legal hold](legal_holds.md) placement and release additionally require an
authenticated principal, including in an otherwise anonymous local deployment.
Neither ordinary write/movement authority nor a stability override releases a
hold. A hold's scope and history remain in the actor's tenant partition.

| Worker operation | Required permissions | Reason |
| --- | --- | --- |
| `catalog.scan` | `administration` | Scans storage and updates the catalog and indexes. |
| `policy.run` | `policy`, `movement` | Recovers existing moves, plans placement, and executes moves. |

There is no standalone REST movement endpoint. Policy evaluation and preview
do not execute moves. Read operations can retain access telemetry without
requiring write permission. Unknown protected operations and unknown worker
job types have no implicit permission grant.

`/healthz`, `/metrics`, `/`, static `/ui/` assets, `/docs`, `/redoc`, and OpenAPI
documentation remain public infrastructure or presentation surfaces. Restrict
infrastructure access through deployment networking as appropriate. The UI's
API calls still pass through the same `/v1` permission checks.
Tenant-enabled deployments return 404 for the aggregate `/metrics` endpoint;
shared operational telemetry must use private operator infrastructure.

## Configure a policy

Create a JSON file managed by trusted deployment operators:

```json
{
  "bindings": [
    {
      "issuer": "https://identity.example.com/realms/storage",
      "subject": "report-reader",
      "roles": ["reader"]
    },
    {
      "issuer": "https://identity.example.com/realms/storage",
      "subject": "placement-service",
      "roles": ["policy_manager", "operator"]
    },
    {
      "issuer": "https://identity.example.com/realms/storage",
      "subject": "compliance-reviewer",
      "roles": ["auditor"]
    }
  ]
}
```

Bindings match the exact `(issuer, subject)` pair. Identical subjects from
different issuers are different identities. `client_id` and `azp` are audit
attribution, not role selectors. Use the issuer-assigned subject of a service
account just as you would a human subject. An empty `bindings` array grants
nobody access.

Configure the API with both JWT authentication and the policy path:

```bash
export COGNISTORE_AUTH_ISSUER=https://identity.example.com/realms/storage
export COGNISTORE_AUTH_AUDIENCE=cognistore-api
export COGNISTORE_AUTHORIZATION_POLICY=/etc/cognistore/authorization.json
export COGNISTORE_DRIVERS=/etc/cognistore/drivers.yaml
export COGNISTORE_CATALOG_DB=/var/lib/cognistore/catalog.sqlite3

cognistore-api --host 127.0.0.1 --port 8080
```

`--authorization-policy PATH` overrides `COGNISTORE_AUTHORIZATION_POLICY`.
Supplying an API authorization policy without JWT authentication is a startup
error. A partial JWT configuration also fails startup. With JWT authentication
but no policy, protected requests fail authorization instead of accepting every
valid identity.

The policy file is reread for every authorization check. Replace it atomically
when changing roles, and keep its write permissions restricted to trusted
operators. Invalid JSON, malformed bindings, unknown roles, or an unreadable
file fail closed; a previously valid policy is not a fallback. Removing a
binding takes effect at subsequent checks, including queued jobs and retries.
Role changes do not retroactively cancel an operation that has already passed
its authorization boundary.

## Embedded services

```python
from cognistore.api import create_app
from cognistore.auth.authorization import RBACAuthorizer
from cognistore.auth.jwt import JWTAuthConfig

authorization = RBACAuthorizer(policy_path="authorization.json")
app = create_app(
    gateway,
    authentication=JWTAuthConfig(
        issuer="https://identity.example.com/realms/storage",
        audience="cognistore-api",
    ),
    authorization=authorization,
)
```

`CogniStoreGateway` also accepts `authorization=authorization` for direct
service use. Request-scoped authorization accompanies the principal into
service calls and thread workers. A custom `authorization_hook` can impose an
additional restriction; it cannot replace or bypass the built-in permission
checks. Hook success never grants a missing RBAC permission.

## Worker revalidation

Run every consumer of authenticated jobs with the same authorization policy:

```bash
export COGNISTORE_AUTHORIZATION_POLICY=/etc/cognistore/authorization.json
python -m cognistore.cli \
  --drivers /etc/cognistore/drivers.yaml \
  --catalog-db /var/lib/cognistore/catalog.sqlite3 \
  --nats-url nats://127.0.0.1:4222 \
  worker
```

The CLI also accepts `worker --authorization-policy PATH`. A job carries only its
normalized submitting identity, never a bearer token or a snapshot of granted
roles. The worker checks current bindings before scheduler execution claims,
handler work, or storage side effects. `AsyncWorker` owns this dispatch check;
handlers are internal implementation functions rather than a separate
authenticated entry point. Each retry and dead-letter redrive is
checked again; granting access at submission does not guarantee access later.
JWT expiry is not replayed during execution because no token is retained.

| Worker policy | Job principal | Behavior |
| --- | --- | --- |
| Configured | Present | Resolve current bindings and require every job permission. |
| Configured | Missing | Deny. |
| Unconfigured | Present | Deny; missing worker policy does not disable authorization. |
| Unconfigured | Missing | Preserve trusted local execution. |

Authorization denials are terminal job failures and follow the durable
dead-letter flow. Redrive retains the original principal and does not bypass
revalidation. Missing or malformed identity, an unknown operation, or a failed
policy reload cannot authorize execution.

CLI job producers do not authenticate a submitting principal. Their anonymous
jobs are rejected by workers with a policy; submit protected manual work through
the authenticated REST action endpoints. The scheduler supports an
operator-configured service identity for [protected schedules](background_workers.md#protected-schedules).
That configuration is a trusted assertion, not JWT verification. Keep anonymous
scheduler/CLI workloads in a separate trusted deployment. Drain existing
anonymous scheduled runs before enabling a policy on their consumer; rejection
before scheduler coordination can leave their old scope reservations requiring
operator recovery. Do not rewrite an anonymous job's identity to get it accepted.

Reserved principal metadata is an internal propagation contract, not signed
proof. Restrict NATS publication and broker access to trusted producers that
verify client identities or assert an explicitly approved scheduler service
identity. Permission lookup cannot distinguish a forged principal supplied by
a publisher that already has that privilege.

## Trusted local process boundary

With neither authentication nor authorization configured, the API retains
anonymous access for local use, except audit history, export, and verification
endpoints, which always require an authenticated auditor. Unconfigured, anonymous workers and direct
local library/CLI operations likewise retain their existing behavior. The
operator who controls drivers, storage credentials, the catalog, or policy
files is inside the trusted process boundary.

RBAC guards the protected API, gateway operations, and worker execution; it
does not sandbox local Python code or a CLI with direct storage/database
credentials. Such code can call storage or catalog primitives directly. Use
the authenticated API for clients that must be constrained by RBAC.

## Failures and audit records

Missing or invalid bearer credentials retain the documented `401` behavior.
An authenticated principal lacking permission receives `403 forbidden` with
the message `Operation not permitted` and the usual request ID. The denied response does not disclose whether an
object, tier, decision, or job exists. Checks precede resource lookups and
backend calls, and policy-load failures cannot fall back to allowing access.

The standard gateway and worker persist `authorization.decision` events
through the existing catalog audit mechanism. Every denial and every allow,
including read operations, is recorded without sampling. Audit context records the operation,
outcome, required permissions, and hashed issuer-scoped actor identity without
raw tokens, claims, resource paths, queries, or request bodies. Existing
[audit retention and query controls](audit_events.md) apply.

An embedded authorizer without a catalog emits the same sanitized decision
as a structured log. If an attached audit catalog cannot persist an audited
allow, the operation is denied and a sanitized logging fallback records the
persistence failure. Fully unconfigured trusted local operation does not emit
RBAC decisions.

## Verification contract

Changes to endpoints or jobs must keep the permission matrix and enforcement
tests aligned. The suite should verify:

- Every protected route and gateway operation has a mapping, and each role's
  allowed and denied operations match the table, including combined roles.
- Denied requests never invoke service backends, and existing and missing
  resource identifiers produce the same authorization failure.
- Direct gateway calls and worker dispatch enforce their permission boundaries;
  an additional hook cannot bypass RBAC.
- Workers reject revoked bindings, missing policies/principals, invalid policy
  files, and unknown job operations before execution, including retry/redrive.
- File updates take effect on subsequent checks, identity contexts remain
  isolated across concurrent work, and audit events exclude sensitive
  request/resource data.
- Public infrastructure routes and fully unconfigured trusted local operation
  preserve their documented behavior.

See [authentication](authentication.md), [REST API v1](rest_api.md), and
[background workers](background_workers.md) for the surrounding contracts.
