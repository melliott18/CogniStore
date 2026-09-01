# Embedding search fixture corpus

`corpus.json` is a small project-authored semantic corpus for exercising
version-isolated pgvector indexing, metadata filters, HNSW latency, and recall
against exact nearest-neighbor search. It contains 16 normalized passages from
eight fictional documents and eight project-authored queries. Query relevance
labels are fixture expectations; they are not benchmark results.

The fixture deliberately stays at the normalized-passage boundary. It does not
duplicate the PDF and DOCX parser fixtures or imply that the source-byte chunks
from the content-identity manifest are valid embedding inputs.

## Integrity and provenance

The content was written for CogniStore and contains no third-party document
text. It is available under the MIT license in `LICENSE`. Verify the checked-in
payload before a benchmark:

```shell
cd tests/fixtures/embeddings
shasum -a 256 -c SHA256SUMS
```

Changing any passage, query, filter, or relevance label requires regenerating
`SHA256SUMS`. Historical benchmark reports retain the prior checksum and must
not be silently relabeled as results for the changed corpus.
