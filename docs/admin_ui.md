# Administration UI

Open `/ui/admin/` on the CogniStore API origin to inspect storage configuration,
preview policies, submit permitted actions, follow jobs, review audit evidence,
and repair eligible incomplete moves. The packaged interface requires no
separate frontend service or JavaScript build. Content search remains at
`/ui/`.

Use the [manual acceptance matrix](manual_acceptance.md) for repeatable browser,
role, tenant, confirmation, job and audit checks with retained evidence.

## Connect and establish scope

Configure [JWT authentication](authentication.md),
[role bindings](authorization.md), and, for shared deployments,
[tenant membership](tenancy.md) on the API. Obtain an access token for the
configured API audience through your identity provider, then connect from the
administration page. The page does not implement an identity-provider login
flow. Static assets are public; administration data requires a verified
identity and server-owned permissions, including in an otherwise anonymous
local deployment.

The access token stays in page memory and is cleared from the password field
after connection. It is not saved in browser storage. Disconnect clears the
token, loaded data, and previews. An automatic session lookup on page load also
supports a trusted reverse proxy that supplies upstream authentication.

The session response identifies the current tenant and allowed operations.
The browser uses these operations to show permitted views and controls. Every
request is also authorized independently at the API and service boundaries;
editing browser controls does not grant access. A valid token with no current
role grants cannot establish an administration session.

The displayed tenant comes from server-side membership. There is no tenant
selector: headers, query strings, form values, and token role claims cannot
choose another tenant. Even administrators can inspect and act only within
their assigned tenant. API and workers must share the same trusted role and
membership policies.

| View or action | Required permissions |
| --- | --- |
| Connect to an administration session | Any current permission grant. |
| Inspect driver/tier configuration and dependency state | `administration` |
| Preview a policy for one object | `policy` |
| Submit a policy run | `policy` and `movement` |
| Submit a catalog scan | `administration` |
| List jobs or inspect a job | `read` |
| Read retained decisions or audit events; verify audit integrity | `audit` |
| List, inspect, or preview a configured repair | `administration` |
| Execute a configured repair | `administration` and `movement` |

For example, `policy_manager` can preview a policy; add `operator` to submit
a run that can move objects. An `auditor` can inspect evidence without being
granted object reads or movement authority. Roles and their exact permission
unions are defined in the [authorization guide](authorization.md).

## Storage and configuration

The **Storage** view reports configured tiers, driver capabilities, encryption
and topology information, and catalog/queue dependency state. Operational
configuration is redacted: credentials, storage paths, and trusted repair
bindings are not exposed to the browser. Shared process-wide telemetry remains
on private operator infrastructure.

Driver configuration does not prove backend availability. Drivers without a
generic safe health probe are explicitly reported as unverified. A catalog or
queue failure is shown separately from available configuration, so a partial
failure does not become an all-healthy result. The observation time identifies
when the displayed state was collected.

Driver configuration changes remain deployment-managed. Use the
[driver configuration guide](../README.md#driver-configuration-optional) and
[tier pools](tier_pools.md) to change trusted server configuration.

## Policies and actions

In **Policies & actions**, supply a bucket, object key, and policy JSON to
preview placement. For example:

```json
{
  "policy": "simple",
  "threshold": 1048576,
  "allowed_tiers": ["hot", "warm"]
}
```

The preview evaluates current catalog evidence without moving bytes or
recording a completed action. It displays current and proposed placement,
structured reasons, guardrail evidence, and execution state. Unknown or legacy
evidence remains explicitly unavailable. See
[placement explanations and diffs](placement_explanations.md) for the meaning
of each field.

A policy run applies the supplied configuration to a bucket and literal key
prefix. An empty prefix covers the whole bucket. Review the displayed tenant,
bucket, prefix, and configuration before explicitly confirming submission.
The confirmation dialog shows the request scope and payload and requires
typing `CONFIRM`. Catalog scans and repair execution use the same explicit
confirmation step.

A one-object preview does not establish the outcome for every object in a
bucket-wide run; workers reevaluate current evidence and enforce their current
permissions, legal holds, movement constraints, and other policy guards.

A catalog scan likewise identifies one tier, bucket, and literal prefix and
queues an update of that scoped catalog inventory. Submitted actions return
job and correlation identifiers. Submission means the action was queued; it
does not mean that the scan, policy run, or any proposed move completed.

## Jobs and audit evidence

The **Jobs** view lists retained creation evidence with bounded pagination.
Inspect or refresh a job to observe `queued`, `running`, `retrying`, `succeeded`, or
`failed` status, attempt count, and available failure information. Only retained
jobs in the current tenant are accessible. A missing job ID returns the same
not-found response as an ID owned by another tenant.

Use job and correlation IDs to connect actions, retained placement decisions,
and audit events. A successful policy job can include moves, stays, and
suppressed decisions. Inspect each decision's execution evidence before
concluding that an object moved.

**Audit & explanations** shows retained evidence, not reconstructed from
current settings. Filtering and pagination remain tenant-scoped. Audit reads and integrity
checks require `audit` and record their own disclosure in the tenant audit
chain; an unavailable audit store cannot produce an unaudited disclosure.
Review [audit operations](audit_events.md) for retention, exports, independent
checkpoints, and the limits of integrity verification.

## Configure and execute repairs

The **Repairs** view exposes only reports registered by a trusted operator.
Prepare a completed [consistency report](consistency_checks.md) using the same tenant,
catalog, drivers, and scope as the running deployment. Configure
`COGNISTORE_ADMIN_REPAIR_REPORTS` with the path to an operator-owned JSON file:

```json
[
  {
    "repair_id": "quarterly-storage-check",
    "tenant_id": "alpha",
    "path": "/var/lib/cognistore/reports/alpha.sqlite3",
    "binding_id": "<source-binding-id-from-the-consistency-report>"
  }
]
```

The binding must match the report's source binding. Registration is the
operator's assertion that the report belongs to the API's current catalog and
drivers; the API checks the registered binding against the report, rather than
recomputing the CLI source digest. Remove or replace registrations when the
catalog locator, driver configuration, or storage roots change, and create a
new report for the new configuration. Current repair evidence is still
verified independently before a move can resume.

Report registration does not let the browser submit paths, catalog locators,
driver configuration, or arbitrary scope. Without a registry, the repair view
has no registered reports. Each tenant sees only its own registrations.

Unavailable reports are listed as individual errors while healthy report
entries remain inspectable. Keep registry and report files writable only by
trusted operators and the API service account that records repair outcomes.

Preview a registered repair before execution. The response identifies the
tenant, bucket, literal prefix, and tiers, along with proposed actions and
counts. Execution requires the current preview token and explicit confirmation
of the same scope. Changing scope or evidence requires a new preview.

Repair resumes only eligible existing moves through the original durable move
journal and idempotency key. Execution rechecks current catalog, backend
generation, full-checksum evidence, leases, legal holds, and locality guards.
It can perform the original move's verified source cleanup. Missing data,
unexplained duplicates, changed or insufficient evidence, and ambiguous jobs
remain for operator review; a report finding alone does not authorize a move
or deletion. Repeated execution reuses the recorded repair result rather than
creating a replacement move.

Repair status distinguishes `ready`, `running`, `completed`, and
`review_required`. Review action-level outcomes and counts even when processing
finishes. Keep the original report for historical evidence and run a new
consistency scan to assess storage after repair.

## Loading, failure, and freshness

Each permitted view loads independently and has a refresh control. An empty
result is distinguished from a failed request; failures include a retry path.
The latest job list is polled every ten seconds while it contains active jobs.
Polling pauses while inspecting a job or reading older pages so that those
results stay visible; use Refresh to return to the latest history. Loaded read
results become visibly stale after sixty seconds, and a failed refresh keeps
available rows visible with an explicit stale-data message. The last successful
response is an observation, not a guarantee of current backend state.

Pending and failed operations do not appear as completed actions. A denied or
expired session clears its data, previews, and token and requires a new
connection. Other request failures preserve available results so that partial
outages remain inspectable. Refreshing a view or changing connection does not
make an earlier preview an execution result.

## API resources

The administration page uses the same versioned API as other clients:

| Method and path | Purpose |
| --- | --- |
| `GET /v1/admin/session` | Current tenant, actor, and allowed operations. |
| `GET /v1/admin/storage` | Redacted configuration and dependency observations. |
| `POST /v1/policies/preview` | Side-effect-free object placement preview. |
| `POST /v1/actions/policy-runs` | Queue a scoped policy run. |
| `POST /v1/actions/catalog-scans` | Queue a scoped catalog scan. |
| `GET /v1/jobs` | Paginated tenant job history. |
| `GET /v1/jobs/{job_id}` | Current retained job status. |
| `GET /v1/policy-decisions` | Retained placement explanations and execution evidence. |
| `GET /v1/audit/events` | Filtered, paginated audit history. |
| `POST /v1/audit/verify` | Verify the tenant audit chain. |
| `GET /v1/admin/repairs` | List configured repairs in this tenant. |
| `GET /v1/admin/repairs/{repair_id}` | Read repair status and outcomes. |
| `POST /v1/admin/repairs/preview` | Preview one registered repair. |
| `POST /v1/admin/repairs` | Confirm and execute the previewed repair. |

Pass pagination cursors unchanged with their original filters. Authentication,
authorization, tenant binding, validation, and error envelopes are documented
in the [REST API guide](rest_api.md) and the checked-in
[OpenAPI contract](openapi/v1.json).
