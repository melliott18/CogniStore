# Ask retrieval golden fixture

`golden.json` is a small project-authored fixture for CogniStore's version 1
hybrid Ask contract. It defines three authoritative catalog objects, two query
requests, ranked metadata/keyword/vector provider outputs, and the exact object
order and source-qualified passage citations expected after catalog
post-filtering and object-level reciprocal-rank fusion.

Metadata candidates are ordered to match `CatalogMetadataRetriever` over the
fixture's current records and query terms. Their score values are not trusted
as proof of current eligibility: their order drives fusion, but hydration must
still find a current metadata match and all common filters are enforced against
the catalog snapshot.

The `legal-hold` query deliberately returns two keyword passages for the same
object. They remain distinct evidence passages but contribute only one keyword
rank to that object's fused score. Its keyword SHA-256 passage identifiers and
vector UUID passage identifiers are intentionally incompatible.

The `restore-proof` query deliberately places a nonmatching object ahead of the
matching object in both derived-provider rankings. The common bucket, prefix,
tier, MIME, object-metadata, and document-metadata filters must be applied again
to the current catalog before ranking or citation selection.

The fixture is original CogniStore test content and is available under the MIT
license in `LICENSE`. `SHA256SUMS` pins the exact JSON payload; update it whenever
the corpus, rankings, or expectations change.

Verify the payload with:

```shell
cd tests/fixtures/ask
shasum -a 256 -c SHA256SUMS
```
