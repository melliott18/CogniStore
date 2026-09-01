# Document extraction contract

CogniStore extracts deterministic normalized text and core metadata from PDF
and DOCX objects. Extraction is bounded, versioned, and isolated per object so
one failure does not stop unrelated indexing work. The runtime choice is
recorded in [ADR 0002](adr/0002-isolated-document-parser-runtimes.md).

## Supported inputs

Dispatch uses the canonical content MIME supplied to the extraction adapter.
Ticket #33 supports exactly:

| MIME type | Parser name | Extracted content |
| --- | --- | --- |
| `application/pdf` | `pypdf` | Existing text from pages plus core PDF metadata |
| `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | `python-docx` | Main-document body paragraphs and tables plus core OOXML metadata |

Filename extensions do not expand this registry. Any other MIME type,
including generic ZIP, is `unsupported_mime`. Password decryption, OCR,
scanned-image recognition, headers, footers, footnotes, endnotes, comments,
drawings, and embedded objects are outside the initial contract. In
particular, an image-only PDF can validly produce empty normalized text.

## Limits and subprocess lifecycle

`ExtractionLimits` exposes all three limits. Its defaults are:

| `ExtractionLimits` field | Default | Boundary behavior |
| --- | ---: | --- |
| `max_file_bytes` | 25 MiB (26,214,400 bytes) | A larger complete input fails as `input_too_large` before a parser starts. |
| `timeout_seconds` | 30 seconds | The wall deadline covers child startup, parsing, normalization, and result transfer. |
| `max_output_bytes` | 4 MiB (4,194,304 bytes) | Normalized text and canonical metadata JSON are measured together; a larger result is discarded as `output_too_large`. |

Configured values must be positive, finite values of the appropriate type;
`timeout_seconds` cannot exceed the current platform's
`threading.TIMEOUT_MAX`, and byte limits cannot exceed `sys.maxsize`.
The complete object size is checked rather than a prefix or filename-reported
estimate. The combined output size is:

```text
len(normalized_text.encode("utf-8"))
+ len(canonical_json(document_metadata).encode("utf-8"))
```

Canonical JSON uses lexicographically sorted keys, compact separators, UTF-8
characters rather than ASCII escape expansion, and no non-finite numbers.
The exact byte count is persisted as `output_bytes`.

Each supported object is parsed by a fresh subprocess. Parser adapters must be
pickle-compatible and are snapshotted when the pipeline is constructed.
CogniStore stages that adapter snapshot and each bounded document in shared
memory; only segment handles and scalar values travel through subprocess
bootstrap. The result is received on a deadline-owned thread so a partial or
stalled result transfer cannot block the caller past the extraction deadline.

On timeout, the parent terminates the process, waits a bounded grace period,
kills it if it remains alive, and reaps it before returning. A timeout, crash,
malformed worker response, parser exception, or size rejection becomes that
object's failed record; callers may continue with the next object. Raw
exception text is not part of the deterministic persisted record. Applications
must construct a new pipeline after changing a configured adapter; the
construction-time snapshot is the state used for every extraction.

## Text order and normalization

Extraction establishes source order before applying normalization:

- PDF pages are emitted in ascending page index. Text within a page remains in
  `pypdf`'s extraction order. Pages are separated by a blank line.
- DOCX body blocks are visited in OOXML document order, so paragraphs and
  tables are interleaved where they occur rather than collecting all
  paragraphs first. A paragraph and each table row produce one line, joined to
  the previous line with one LF. Tables are emitted in row-major order; cells
  within a row retain their source order and are separated by the literal
  ` | ` delimiter. Paragraph text within each cell retains its source order.

The normalization algorithm is version 1 and applies in this order:

1. Normalize Unicode to NFC.
2. Convert CRLF and bare CR line endings to LF.
3. Convert tab, vertical-tab, and form-feed characters to ASCII spaces.
4. Remove remaining control characters other than LF.
5. Collapse each run of horizontal whitespace to one ASCII space and remove
   horizontal whitespace at line boundaries.
6. Collapse runs of blank lines so at most one blank line remains (no more than
   two consecutive LF characters).
7. Remove leading and trailing whitespace from the complete text.

The result contains no CR line endings. With the same input bytes, canonical
MIME, limits, parser implementation/runtime version, and normalization
version, successful output and content- or size-derived failure records are
deterministic. A timeout or worker failure is operational evidence and can be
retried; it does not relax the limits.

## Persisted result

The catalog's `document_extraction` value is a flat schema-version-1 record:

| Field | Contract |
| --- | --- |
| `schema_version` | Integer `1`. |
| `status` | `succeeded` or `failed`. |
| `source_mime` | Canonical MIME passed to the adapter. |
| `source_size` | Complete input size in bytes. |
| `parser` | `null` when the MIME is unsupported; otherwise the selected `{name, implementation_version, runtime_version}`, including on failure. |
| `normalization_version` | Integer `1`. |
| `text` | Normalized text on success; `null` on failure. |
| `text_bytes` | UTF-8 byte length of `text`; zero when `text` is `null`. |
| `output_bytes` | Combined normalized-text and canonical-document-metadata byte count; zero when no output is retained. |
| `document_metadata` | Canonical JSON-safe parser metadata on success; `{}` on failure. |
| `failure_code` | `null` on success; one stable code below on failure. |

`parser.name` is `pypdf` or `python-docx`.
`parser.implementation_version` versions CogniStore's adapter and ordering
algorithm; `parser.runtime_version` is the installed third-party package
version. Consumers must use the complete parser object together with
`normalization_version`, not only the parser name, when deciding whether
stored output is reusable.

Successful `document_metadata` always contains these common version-1 keys;
an unavailable value remains present as `null`:

| Key | Value |
| --- | --- |
| `format` | `pdf` or `docx`. |
| `title`, `author`, `subject`, `keywords`, `language` | Normalized document-property text or `null`. |
| `created_at`, `modified_at` | UTC ISO-8601 text when exposed as a date by the parser, normalized source text when exposed only as text, or `null`. |

PDF adds the integer `page_count`. DOCX adds integer `paragraph_count` and
`table_count` values for the main document body. No other parser property is
part of the version-1 metadata contract.

## Stable failure codes

Failure codes are machine-readable API and persistence values. Human
diagnostics may become more specific without changing the code.

| Code | Meaning |
| --- | --- |
| `unsupported_mime` | The canonical MIME has no registered parser. |
| `input_too_large` | The complete input exceeds the configured input limit. |
| `timeout` | The subprocess crossed the configured wall-clock deadline. |
| `encrypted` | The document requires a password or otherwise cannot be read without decryption. |
| `corrupt` | The parser recognized the format but found invalid or truncated document structure. |
| `output_too_large` | Normalized text plus canonical metadata JSON exceeds the configured output limit. |
| `parser_error` | A registered parser failed for a reason not classified above. |
| `worker_error` | The subprocess could not start, exited abnormally, or returned an invalid/missing response, or a supported in-limit read did not contain exactly `source_size` bytes. |

Codes are additive across future schema versions. Existing meanings must not
be reassigned. Unsupported, oversize, encrypted, corrupt, timed-out, and
crashed objects are isolated failures and do not stop extraction of later
objects.
