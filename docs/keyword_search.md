# Keyword search

CogniStore's keyword index is a rebuildable Tantivy projection of the catalog.
PostgreSQL (or the backend-neutral catalog used in local tests) remains the
source of truth. Search code does not write catalog rows.

## Data contract

Each current catalog object produces one or more independently ranked passage
documents. Successful extracted text is split with the versioned normalized
passage chunker; empty text and objects without a trustworthy extraction still
produce one metadata-only document so their object key and first-class object
fields remain queryable.

The source-byte chunks recorded by the content-identity manifest are not text
passages. They are never indexed.

Extracted body text and document properties are accepted only when all of these
conditions hold:

- `content_identity` is present and its SHA-256 and size match the current
  object;
- extraction status is `succeeded`, and source size and MIME match the object;
- extraction and normalization versions are supported;
- parser name, adapter version, and runtime version are complete;
- normalized text UTF-8 byte accounting is exact; and
- the extraction has no failure code and contains a metadata object.

This gate matters because a placement update can invalidate current content
identity while old extraction JSON remains in object metadata. A rebuild must
not make that stale text searchable.

Searchable document properties are deliberately limited to `format`, `title`,
`author`, `subject`, `keywords`, `language`, `created_at`, `modified_at`,
`page_count`, `paragraph_count`, and `table_count`. Exact filters also support
bucket, tier, size, MIME type, and current content SHA-256. Arbitrary catalog
metadata is not copied into the index.

## Build and query

Use a persistent index path that is separate from the PostgreSQL data directory
and is writable by only one CogniStore process:

```python
from cognistore.db.catalog import SQLCatalog
from cognistore.search import (
    KeywordSearchFilters,
    KeywordSearchQuery,
    KeywordSearchService,
    TantivyKeywordIndex,
)

with SQLCatalog("postgresql://cognistore@localhost/cognistore") as catalog:
    with TantivyKeywordIndex("/var/lib/cognistore/keyword") as keyword_index:
        search = KeywordSearchService(catalog, keyword_index)
        report = search.rebuild()
        print(report)

        hits = search.search(
            KeywordSearchQuery(
                "retention policy",
                filters=KeywordSearchFilters(
                    bucket="documents",
                    mime="application/pdf",
                    document_metadata={"language": "en"},
                ),
                limit=20,
            )
        )
        for hit in hits:
            print(hit.score, hit.bucket, hit.key, hit.passage_ordinal)
```

Query text is analyzed literally with CogniStore's versioned Tantivy analyzer;
characters such as colons, quotes, parentheses, and leading hyphens are text,
not query-language operators. Tokens search passage text, object keys, and the
allowlisted document-property values. Results are ordered by BM25 score, with
bucket, key, and passage ordinal as deterministic tie breakers across pages.
Scores are backend-specific and should not be persisted as catalog facts.

Catalog coordinates remain lossless even when POSIX names contain undecodable
bytes represented by Python surrogate code points. Those opaque code points
act as token separators for full-text key matching, while exact filters,
returned coordinates, ordering, updates, and deletes use their stored byte-safe
identity.

## Updates, deletes, and consistency

Catalog state must commit first. Then synchronize the derived entry:

```python
# After a successful catalog scan/update transaction:
search.sync_object(bucket, key)

# After catalog.delete(bucket, key) commits:
search.delete_object(bucket, key)
```

`sync_object` fetches the current catalog snapshot. If the object no longer
exists, it performs an idempotent index delete. `delete_object` refuses to
delete while the catalog object still exists, which keeps call ordering
explicit.

A successful replace or delete commits and reloads Tantivy before returning,
so a subsequent search on the same adapter sees the change immediately. If the
adapter raises, catalog state is already authoritative and remains unchanged;
retry the operation or run `rebuild()`.

There is not yet a durable catalog-to-search outbox. A crash after catalog
commit but before index commit can leave search stale until retry/rebuild.
Likewise, run a full rebuild while catalog mutations are quiesced, or replay
mutations that commit during the rebuild window.

## Rebuild and recovery

`CatalogStore.iter_objects(batch_size=...)` streams detached object snapshots
across every bucket, allowing a PostgreSQL rebuild without materializing the
whole catalog in Python. The adapter builds a UUID-named generation under
`generations/`, verifies the committed passage count, and atomically publishes
its name through `CURRENT`. The prior generation remains live until that
publication succeeds.

The generation manifest records the keyword schema, Tantivy adapter, analyzer,
and normalized passage chunking contract. Opening an index with incompatible
settings raises `KeywordIndexCompatibilityError`; rebuild it with the intended
configuration rather than mixing schemas.

The embedded adapter supports one writer process per index path. Do not place
the directory on a shared filesystem. A root-level operating-system lock rejects
a second local writer before it can create or publish a generation. A later
OpenSearch adapter/outbox is the intended path for shared-host operation.
