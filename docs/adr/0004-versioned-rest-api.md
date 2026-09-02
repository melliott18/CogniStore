# ADR 0004: Versioned REST API and gRPC deferral

- Status: Accepted
- Date: 2026-09-01
- Ticket: [#39](https://github.com/melliott18/CogniStore/issues/39)

## Context

CogniStore needs one stable external contract for object bytes, catalog and
search results, policy decisions, and asynchronous control-plane actions. The
near-term consumers are a typed Python SDK, a browser content-search UI, and
operators using ordinary HTTP tooling. Internal long-running work already uses
durable NATS JetStream envelopes rather than a synchronous RPC connection.

The roadmap permits REST and/or gRPC, but M2 must avoid maintaining two public
contracts unless gRPC has a concrete use case that justifies independent
schemas, compatibility testing, transport operations, and client generation.

## Decision

Use URI-versioned REST over FastAPI for M2. Check a deterministic OpenAPI 3.1
document into the repository and treat it as the source contract for the SDK
and UI. Routes call injected application services, which in turn use
`StorageDriver`, `CatalogStore`, `AskService`, policy services, and `JobQueue`;
the HTTP layer never reads database tables or backend-specific clients.

Long-running scans and policy runs return `202 Accepted`, a durable job UUID,
and a polling URL. Object downloads use HTTP streaming and range semantics.
Catalog collections use bounded opaque keyset pagination. These operations map
directly onto widely supported HTTP behavior and need no bidirectional stream.

gRPC has no justified near-term M2 use case and is deferred. Reconsider it only
when a measured internal service-to-service workload requires high-rate typed
binary RPC or bidirectional streaming that cannot meet its objective through
REST/object streaming plus NATS actions. A later gRPC adapter must reuse the
same application-service boundaries and define explicit compatibility with the
REST/OpenAPI contract rather than bypassing the DAL.

## Consequences

The Python SDK and browser UI can generate or validate clients from one
checked contract. HTTP debugging and deployment need no gRPC proxy or protobuf
toolchain. Exact dependency pins and a CI byte comparison prevent unnoticed
OpenAPI drift.

REST does not provide protobuf's compact binary messages or generated
bidirectional streaming. If a later measured workload crosses that boundary,
the team will incur a separate protocol decision and compatibility suite then,
with evidence for the added operational surface.
