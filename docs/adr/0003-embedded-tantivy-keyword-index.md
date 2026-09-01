# ADR 0003: Embedded Tantivy keyword index

- Status: Accepted
- Date: 2026-09-01
- Ticket: [#37](https://github.com/melliott18/CogniStore/issues/37)

## Context

CogniStore needs ranked full-text retrieval over normalized PDF and DOCX
passages plus selected object/document metadata. The index is derived state:
PostgreSQL remains authoritative, and operators must be able to reconstruct the
entire index without reading storage objects again. Search failures must never
participate in, roll back, or otherwise corrupt a catalog transaction.

The roadmap permits either OpenSearch or a Tantivy-based implementation.
OpenSearch matches the original proposal and is a good fit for a later shared,
horizontally operated search service. It would add another network service,
authentication and TLS configuration, health orchestration, and bulk/alias
failure handling to the current M2 deployment, however. Multi-region search
clusters are explicitly outside ticket #37.

## Decision

Use the official Python bindings for the embedded
[Tantivy](https://github.com/quickwit-oss/tantivy) search library, constrained
to the 0.26 line (`tantivy>=0.26,<0.27`). The binding publishes wheels for the
repository's CPython 3.10–3.14 support matrix on the primary macOS, Linux, and
Windows architectures. Platforms without a wheel need a Rust toolchain to
build the dependency from source.

`KeywordIndexAdapter` is the backend-neutral boundary. `TantivyKeywordIndex`
implements ranked search, exact filters, replace, delete, and full rebuild.
Keeping this boundary independent of Tantivy preserves a later migration path
to OpenSearch without changing the catalog projection or retrieval contract.

The index contains one Tantivy document per normalized text passage. Passage
identity includes the logical object coordinate, current full-content digest,
extraction schema and parser/runtime identity, normalization version, passage
chunker version/configuration, ordinal, extent, and text digest. Raw
`content_manifest_chunks` are byte extents for CAS and deduplication and are
never sent to text search.

Only a strict allowlist is searchable or filterable: bucket, object key, tier,
size, MIME type, current content digest, and the extracted format, title,
author, subject, keywords, language, timestamps, and document counts. Arbitrary
catalog JSON can be large or sensitive and is not copied into the index.

An extraction is eligible only when it is successful, has the supported schema
and normalization versions, has complete parser identity, has correct UTF-8
byte accounting, matches the catalog object's size and MIME type, and is backed
by the current catalog-owned `content_identity`. An invalid or stale extraction
becomes a metadata-only entry with no document properties or body text.

Incremental replace and delete operations use Tantivy's immutable-document
model: delete the object's stable term, add its current passages, commit once,
and reload the reader. A successful call is visible to searches acquired after
the call returns. A failed or uncertain call has no visibility guarantee and
must be retried or repaired by rebuild; the catalog is never mutated by the
adapter.

Rebuild consumes the catalog's bounded full-object iterator into a fresh index
generation. It commits and verifies the passage count before atomically
replacing the `CURRENT` generation pointer. A projection, catalog-stream, or
backend failure before publication leaves the previous generation searchable.
Rebuild and incremental writes are serialized inside one adapter process.
Until a durable catalog outbox exists, operators must quiesce catalog mutations
during a rebuild or replay them after it; a process crash between a catalog
commit and its derived-index write is repaired by retry or full rebuild.

One `TantivyKeywordIndex` instance owns one long-lived writer for an index
directory. An operating-system lock at the index root rejects another local
writer before generation creation; Tantivy also locks the selected generation's
writer. A shared network filesystem, multiple hosts writing the same local
directory, and multi-region replication are unsupported topologies for this
adapter.

## Consequences

Local development and fixture qualification need no fourth service. Ranking,
filter, mutation, persistence, and failed-rebuild behavior can run in ordinary
unit tests against real Tantivy indexes. The on-disk manifest records the
schema, adapter, analyzer, passage algorithm/version, and chunk configuration;
incompatible opens fail closed.

The selected topology is deliberately single-writer and host-local. A future
shared Ask deployment may add an OpenSearch adapter and a transactional catalog
outbox, then rebuild and cut over without treating Tantivy files as source
data.

## Alternatives considered

- OpenSearch provides a networked multi-client service and atomic alias swaps,
  but adds a substantial operational and integration surface before ticket #37
  requires it.
- PostgreSQL full-text search would keep fewer moving parts, but it is not one
  of the roadmap's approved choices and would couple derived ranking/index
  behavior to the catalog database.
- Indexing source-byte chunks would reuse #34's manifest, but those chunks are
  not normalized passages and can contain compressed or binary document bytes.
