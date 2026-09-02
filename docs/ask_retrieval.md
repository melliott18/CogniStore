# Ask retrieval

CogniStore's Ask retriever is a Python service that combines authoritative
catalog metadata with optional keyword and vector search results. It returns a
bounded, deterministically ranked retrieval response and can optionally ask an
answer provider to synthesize an answer from those results.

Ask does not expose a CLI command or an HTTP endpoint. The versioned REST and
OpenAPI surface is a separate milestone built on top of this service contract.

## Service and contract

The Ask request and response use the version 1 Ask schema. The public contract
defines a query, common filters, result limits, retrieval status, score
components, source citations, and an optional generated answer. This contract
is backend-neutral: callers do not construct Tantivy or pgvector query types.

An Ask retriever is assembled from these capabilities:

- an authoritative `CatalogStore`, which is required;
- a bounded metadata retriever, which defaults to the catalog-backed
  implementation;
- an optional keyword retriever;
- an optional vector retriever; and
- an optional answer provider.

The default metadata retriever reads detached catalog snapshots in bounded
batches and retains only the configured candidate budget. Ask is read-only: it
does not mutate the catalog, rebuild either derived index, or persist generated
answers.

The Python-facing construction is conceptually:

```python
from cognistore.search import AskFilters, AskQuery, AskService

ask = AskService(
    catalog,
    keyword=keyword_search,  # optional
    vector=embedding_search,  # optional
    answer_provider=answer_provider,  # optional
)

response = ask.ask(
    AskQuery(
        text="Which records are under legal hold?",
        filters=AskFilters(
            bucket="documents",
            key_prefix="policies/",
            tier="warm",
            mime="application/pdf",
            object_metadata={"department": "legal"},
            document_metadata={"language": "en"},
        ),
        limit=10,
    )
)
```

Concrete providers are configured by the embedding and keyword search layers;
Ask receives ready-to-query objects through constructor injection. It does not
resolve model credentials or backend paths from CLI configuration.

## Filters and authoritative validation

Ask owns one common filter vocabulary for catalog, keyword, and vector
retrieval. It covers catalog coordinates and current object properties such as
bucket, key prefix, tier, MIME type, and bounded exact metadata predicates.
The service translates that vocabulary into each provider's native filter
shape where possible.

Derived indexes can briefly lag the catalog, and their native filter surfaces
are not identical. Ask therefore applies every common filter again to current
catalog snapshots before a candidate can be returned. Objects that were
deleted, moved outside the requested tier, or changed so they no longer match
are discarded even if a derived index still returns them. Backend filtering is
an optimization; the authoritative catalog post-filter defines the observable
semantics.

Requests and provider fan-out are resource bounded. The caller's result limit,
the per-provider candidate budget, metadata batch size, metadata-predicate
counts, and text sizes are validated before retrieval. A result limit does not
permit an unbounded in-memory catalog materialization.

`candidate_limit` bounds each provider's pre-fusion candidate pool. Because
some common filters can only be enforced by the authoritative catalog
post-filter, a restrictive filter may discard the whole bounded pool even when
a matching object exists below it in a provider's unfiltered ranking. Raising
the candidate budget trades additional provider work for recall; it is not a
promise of an exhaustive filtered top-k search.

## Object-level fusion

Keyword and vector search intentionally use different normalized-passage
layouts and identifiers. A keyword passage SHA-256 is not interchangeable with
a vector passage UUID, even when both came from the same source object. Ask
therefore fuses candidates at the catalog object level, keyed by the lossless
`(bucket, key)` coordinate.

Each retrieval signal contributes a weighted reciprocal-rank value:

```text
signal contribution = signal weight / (rank constant + one-based rank)
fused object score  = sum(signal contributions)
```

The rank constant and signal weights are part of the versioned retrieval
behavior. Reciprocal-rank fusion avoids treating BM25 scores, cosine
similarities, and metadata relevance as though they shared a numeric scale.
The response still exposes each signal's one-based rank, backend score when
available, configured weight, contribution, and the final fused score for
inspection.

Objects are ordered by fused score, followed by deterministic catalog-coordinate
tie breakers. Repeated runs over the same provider results therefore return the
same order. Passage evidence remains attached to its contributing signal; Ask
does not merge chunks merely because their unqualified identifiers or text look
similar.

## Citations and generated answers

Every returned passage citation identifies both its catalog object and the
retrieval source that defined the passage. A source-qualified citation retains
the provider's opaque passage identifier plus its ordinal or passage index,
text span, source-content SHA-256, and normalized-document-text SHA-256. The
enclosing passage evidence carries the cited text and its own SHA-256. Vector
citations additionally retain their document and embedding-space provenance.
Metadata-only object evidence remains an object citation and is not represented
as a fabricated text passage.

An answer provider receives only the query and the bounded object/passage
citations selected by Ask. Its output may cite only identifiers supplied in
that request. Unknown, invented, or out-of-range citation identifiers are not
valid provider output. The provider contract does not grant independent access
to storage, the catalog, or either search backend; grounding comes from the
retrieval response assembled before synthesis.

Generated text is optional and never replaces the retrieval evidence. The
response always retains the ranked results, score components, retrieval mode,
and citations used for synthesis so a caller can inspect the answer's basis.

## Modes, statuses, and degradation

The response reports the retrieval mode that actually ran and a status for
each optional capability. Supported modes include metadata-only,
metadata-plus-keyword, metadata-plus-vector, and full hybrid retrieval. Answer
generation has a separate status because it does not change retrieval ranking.

Missing optional providers are a supported deployment state:

- without a keyword retriever, metadata and vector retrieval continue;
- without a vector retriever, metadata and keyword retrieval continue;
- without either search retriever, bounded metadata retrieval continues; and
- without an answer provider, retrieval succeeds with no generated answer.

The status vocabulary distinguishes a provider that was not configured from a
provider that ran and returned no candidates. Classified unavailability, such
as an unsupported vector backend or an exhausted transient embedding request,
is reported separately and the remaining signals continue. Compatibility
corruption and malformed provider output fail closed instead of being silently
reported as an empty successful search.

## Current boundaries

Ask is a stateless orchestration layer over the existing catalog and derived
indexes. It does not add conversation memory, autonomous tool use, background
index repair, or an answer-model implementation. The Tantivy adapter remains
host-local and single-writer, and pgvector search continues to select one exact
embedding space. REST, SDK, and UI consumers are later components that must
preserve the Ask schema, modes, score inspection, and citation provenance when
they expose this service externally.
