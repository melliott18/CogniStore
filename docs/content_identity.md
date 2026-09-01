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
occur. Ticket #34 derives and persists these keys; physically reclaiming
unreferenced CAS data remains outside this contract.

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

## Relationship to document extraction

Source-byte chunks are suitable for byte deduplication and exact object
identity. They are not normalized passages and must not be sent directly to a
text embedding or keyword index. Extracted document text carries parser,
runtime, and normalization versions; any future passage chunker must use a
separate representation and versioned identity domain.
