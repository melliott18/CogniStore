# Content identity and source-byte chunking

CogniStore assigns every scanned object a canonical SHA-256 identity and an
ordered, versioned manifest of source-byte chunks. The identity pass reads the
complete object through the storage driver's stream interface; the bounded
MIME sample is never used as an object checksum.

## Version 1 contract

Version 1 uses these independent contracts:

| Field | Value |
| --- | --- |
| Identity schema | `1` |
| Representation | `source-bytes` |
| Digest algorithm | `sha256` |
| Chunking algorithm | `fixed-size` |
| Chunking version | `1` |
| Default chunk size | 1 MiB (1,048,576 bytes) |
| CAS-key version | `1` |

Chunk boundaries are offsets in the original byte stream. Every non-final
chunk is exactly the configured canonical chunk size; the final chunk contains
the remaining bytes. An empty object has the standard SHA-256 digest of empty
bytes and no chunk rows. An exact multiple of the chunk size does not create a
trailing empty chunk.

Storage readers may return fewer bytes than requested. The identity builder
coalesces those short reads until it reaches the next canonical boundary, so
driver buffering and transfer chunk sizes cannot affect the manifest. It also
rejects non-byte responses, over-reads, early EOF, and bytes beyond the size
reported by the stable object observation.

The default chunk size is an indexing contract, not a storage tuning value. In
particular, changing a POSIX or S3 driver's transfer `chunk_size` does not
change content identities. A boundary-affecting change requires a new chunking
version (and its complete parameters are persisted in the manifest).

## Content-addressed keys

Objects and chunks use the same byte-addressed key format:

```text
cas/v1/sha256/<first-two-digest-characters>/<remaining-digest-characters>
```

Keys contain no bucket, logical object key, tier, generation, chunk position,
or chunker version. Equal bytes therefore have equal CAS keys wherever they
occur. The catalog stores one global blob row per digest even when that digest
is used by multiple logical objects, by multiple chunk positions, or in both
the full-object and chunk roles.

## Catalog publication

The catalog stores global byte identities and manifests separately from the
active logical-object reference. A scan publishes its object observation,
merged metadata, manifest, and ordered object-to-chunk mapping in the same
move-fenced catalog transaction. A rejected fence, conflicting identity, or
failed write leaves the previous object and manifest unchanged.

`ObjectRecord.metadata["sha256"]` remains a compatibility field, but scans now
set it from the complete source stream. The versioned
`ObjectRecord.metadata["content_identity"]` record identifies its
representation, algorithms, versions, canonical chunk size, object CAS key,
and chunk count. Ordered per-chunk mappings are read through the catalog's
content-identity API rather than duplicated into object JSON metadata.

Existing catalog rows are not backfilled from a legacy `sha256` metadata
value: older scans may have stored only the first-megabyte digest there. Such
objects acquire canonical identities on their next successful scan. Revision
`0004_content_identity` also removes any preexisting `content_identity`
metadata member because that name becomes a catalog-owned projection and no
safe normalized mapping can be inferred for an older row.

## Shared references and logical deletion

Revision `0005_content_references` materializes the number of active reference
edges for every global blob. Each active logical object contributes one edge
for its full-object digest and one edge for every position in its ordered chunk
manifest. These are deliberately edge counts rather than distinct-owner
counts:

- a digest used for both a one-chunk object's full identity and its chunk has
  two edges;
- two equal chunk positions in one manifest contribute two chunk edges; and
- two logical objects sharing one manifest each contribute the manifest's
  complete set of edges.

Creating, replacing, invalidating, or deleting a logical mapping updates its
edge counts in the same catalog transaction. Shared blob rows are updated in
digest order, so concurrent changes to different logical objects cannot expose
partial counts or acquire shared rows in conflicting orders.

Catalog `delete` is logical and idempotent. It removes the addressed live
object and its active manifest mapping; it does not remove global manifests,
blob records, source storage, or any future physical CAS bytes. A later scan or
upsert of the same coordinates is a new logical creation. Deleting one of two
objects with equal content therefore releases only that object's edges and
leaves the other mapping readable.

When a blob's last edge is released, the catalog records `unreferenced_at`.
Adding any edge clears that timestamp. A blob is only an advisory physical
reclamation candidate when both its stored and topology-derived counts are
zero, its lifecycle state is internally consistent, and its zero-reference age
is at least the caller-selected grace period. No current command deletes a
candidate or its bytes.

## Non-destructive reconciliation

`CatalogStore.reconcile_content_references()` independently derives full-object
and chunk edge counts from the active topology, compares their total with the
materialized count, and evaluates grace-period eligibility. It never repairs
rows or touches storage. Entries are ordered by digest and use these stable
issue codes:

- `reference_count_mismatch`;
- `referenced_blob_marked_unreferenced`;
- `unreferenced_blob_missing_timestamp`; and
- `invalid_unreferenced_timestamp`.

An inconsistent entry is never reclamation-eligible, even if one of its counts
is zero. Operators can obtain the same report with the read-only
`content-reference-report` CLI command.

## Relationship to document extraction

Source-byte chunks are suitable for byte deduplication and exact object
identity. They are not normalized passages and must not be sent directly to a
text embedding or keyword index. Extracted document text carries parser,
runtime, and normalization versions; any future passage chunker must use a
separate representation and versioned identity domain.
