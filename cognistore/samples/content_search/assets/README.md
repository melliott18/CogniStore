# Content-search sample corpus

This directory contains the small, deterministic corpus used by CogniStore's
content-search example. It has two PDF sources and one DOCX source. The manifest
loads the DOCX at two catalog keys so the workflow exercises exact-byte
deduplication while retaining two independently citable objects.

All corpus prose and identifying metadata were written for CogniStore. The
scenarios, organizations, systems, and procedures are fictional. The documents
contain no customer data, employee data, credentials, network locations,
personal contact details, external links, macros, or embedded files. Author and
producer metadata uses only the generic names `CogniStore Contributors` and
`CogniStore sample corpus generator`; timestamps are fixed at
`2000-01-01T00:00:00Z`. The DOCX package scaffold is generated from
python-docx's MIT-licensed default template; its required notice is retained in
`THIRD_PARTY_NOTICES`.

## Inventory

| Source | Logical object key | Tier | Purpose |
| --- | --- | --- | --- |
| `records-retention.pdf` | `policies/records-retention.pdf` | `warm` | Legal-hold and retention queries |
| `incident-response.pdf` | `security/incident-response.pdf` | `hot` | A distinct PDF ranking candidate |
| `database-backups.docx` | `runbooks/database-backups.docx` | `hot` | Backup and restore queries |
| `database-backups.docx` | `z-archive/database-backups-copy.docx` | `cold` | Exact-byte duplicate with its own catalog coordinate |

`manifest.json` is the machine-readable load contract. Every object records its
source file, storage key, tier, media type, and source SHA-256. Its sample queries
record expected leading result keys:

- keyword: `quarterly restore drill` returns both DOCX coordinates;
- vector: `How does an isolated restore prove backup recovery?` ranks the hot
  runbook first; and
- Ask: `What prevents contract deletion during a legal hold?` ranks the
  retention PDF first and supplies source-qualified passage citations.

The expected Ask citation coordinate is
`sample-documents/policies/records-retention.pdf`; its supporting passage begins
`A signed legal hold suspends scheduled deletion.` The expected vector result is
`sample-documents/runbooks/database-backups.docx`, supported by the passage that
begins `A quarterly restore drill loads the newest backup into an isolated
database`.

The two DOCX objects must have the same content SHA-256 and normalized-document
identity. Their object and citation identities remain distinct because a catalog
coordinate is part of an object citation. Opaque passage identifiers include
parser/runtime provenance and should be validated structurally rather than copied
into documentation.

## Integrity

Verify the checked payload before loading it:

```shell
cd cognistore/samples/content_search/assets
shasum -a 256 -c SHA256SUMS
```

The checksum manifest covers all three physical documents and `manifest.json`.
Loading should fail closed if a checksum, media type, source name, duplicate
mapping, or manifest field is invalid.

## Deterministic loading

`cognistore.samples.content_search.read_sample_corpus()` reads the packaged
manifest, verifies `SHA256SUMS`, and returns the three unique payloads plus four
logical object mappings. Pass that corpus to `ContentSearchRuntime.load()` after
constructing the runtime with the sample bucket's `hot`, `warm`, and `cold`
storage drivers, a SQL catalog with pgvector enabled, and a writable Tantivy
index path.

The loader writes each manifest object to its declared tier, scans and extracts
the objects, rebuilds keyword search, and indexes embeddings with the sample's
offline feature-hashing provider. Reusing the same checked payloads and
dependencies yields the same content identities, normalized text, embedding
space, duplicate group, and expected result coordinates. Passage IDs also
include parser/runtime provenance and can change when an extraction dependency
changes.

## Deterministic regeneration

Ordinary users do not need to regenerate the assets. Maintainers can reproduce
them with CPython 3.12, `python-docx` 1.2.0, and `reportlab` 4.4.9:

```shell
python3.12 -m venv .venv-content-search-assets
.venv-content-search-assets/bin/python -m pip install \
  "python-docx==1.2.0" "reportlab==4.4.9"
.venv-content-search-assets/bin/python \
  cognistore/samples/content_search/assets/generate.py
.venv-content-search-assets/bin/python \
  cognistore/samples/content_search/assets/generate.py --check
```

The generator uses ReportLab's invariant PDF mode, built-in PDF fonts, fixed
document timestamps and metadata, and canonical OOXML ZIP ordering, timestamps,
permissions, and compression. It refuses generation when its layout-producing
dependency versions do not match the pinned versions above. Any intentional
content or layout change must regenerate the documents, `manifest.json`, and
`SHA256SUMS` together.

## License

The corpus prose, manifest, generator, and original document layout additions
are CogniStore project material, copyright (c) 2026 Mitchell Elliott, and are
licensed under the MIT License in this directory and at the repository root.
Modified python-docx template portions contained in the generated DOCX remain
under their MIT license and copyright notice in `THIRD_PARTY_NOTICES`.
