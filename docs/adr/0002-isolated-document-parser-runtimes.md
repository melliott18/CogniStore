# ADR 0002: Isolated PDF and DOCX parser runtimes

- Status: Accepted
- Date: 2026-08-31
- Ticket: [#33](https://github.com/melliott18/CogniStore/issues/33)

## Context

CogniStore needs deterministic text and metadata extraction for the first two
M2 document formats. Document parsers process storage objects that may be
malformed, encrypted, or deliberately expensive to parse. A parser failure or
hang must remain an object-level failure and must not stop extraction of other
objects.

The extraction boundary must also make reprocessing explainable. Stored output
therefore needs to identify both CogniStore's parser adapter and normalization
contract and the exact third-party runtime that produced it.

## Decision

Use these parser runtimes behind CogniStore's parser-adapter interface:

| MIME type | Runtime | Supported line | License |
| --- | --- | --- | --- |
| `application/pdf` | `pypdf` | 6.x (`>=6.16.2,<7`) | BSD-3-Clause |
| `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | `python-docx` | 1.2.x (`>=1.2,<1.3`) | MIT |

Both licenses are compatible with CogniStore's MIT distribution. Dependency
license notices remain part of the distribution and release-compliance
process.

Every supported extraction runs in a fresh subprocess. The parent process
owns MIME dispatch, input and output accounting, the wall-clock deadline, and
conversion of parser or worker failures into stable result codes. It does not
run either third-party parser in the indexing process. Process isolation
contains a parser crash or hang; it is not a security sandbox and does not make
an untrusted parser safe by itself.

Adapters must be pickle-compatible. The pipeline snapshots each configured
adapter at construction, then stages that snapshot and each bounded document
through shared memory. Subprocess bootstrap therefore carries only bounded
handles and scalar values rather than document bytes or potentially large
adapter state. Result receipt remains interruptible under the same absolute
deadline. Adapter snapshots are trusted application configuration, never
pickle data accepted from a storage object.

The public limits are represented by `ExtractionLimits`. The defaults are:

- 25 MiB (26,214,400 bytes) for the complete input;
- 30 seconds of wall-clock time, including subprocess startup and result
  transfer; and
- 4 MiB (4,194,304 bytes) for normalized UTF-8 text plus canonical metadata
  JSON combined.

The input is rejected before parser startup when its complete byte length
exceeds the input limit. Output accounting uses the UTF-8 length of normalized
text plus the UTF-8 length of the canonical JSON encoding of
`document_metadata`. Canonical JSON sorts object keys, has no insignificant
whitespace, preserves non-ASCII text as UTF-8, and rejects non-finite numbers.
Callers may supply different positive limits through the `max_file_bytes`,
`timeout_seconds`, and `max_output_bytes` fields of `ExtractionLimits`.

When the wall-clock deadline expires, the parent requests termination, waits a
bounded grace period, escalates to a forced kill if the child is still alive,
and reaps it. An abnormal exit or an invalid/missing child response is a stable
`worker_error`; a live child that crosses the deadline is `timeout`. No child
is intentionally reused after a request.

Successful records carry a parser object containing its `name`, CogniStore
adapter `implementation_version`, and installed dependency `runtime_version`,
plus the top-level `normalization_version`. These fields are part of the
content identity used by downstream reprocessing. A parser or normalization
change requires the corresponding version to change; a dependency upgrade
cannot silently mix outputs under one runtime version.

The complete input, output, ordering, normalization, metadata, and failure
semantics are defined by the [document extraction contract](../document_extraction.md).

## Consequences

Extraction is bounded and independently interruptible at the per-object
boundary. A corrupt or hostile document can fail without terminating the
indexing process or suppressing later objects. Fresh processes add startup and
inter-process-transfer overhead, and the byte limits do not constitute an OS
memory or CPU quota. Deployments that require stronger hostile-file isolation
must add container or host-level controls around the worker.

`pypdf` extracts text already represented in a PDF. It does not perform OCR,
so an image-only or scanned PDF may succeed with empty text. OCR and image
recognition remain out of scope for ticket #33.

## Alternatives considered

- Running the libraries in-process avoids subprocess overhead, but a parser
  hang or native/runtime failure would share the indexing process's failure
  boundary.
- A service-based parser such as Apache Tika supports more formats, but adds a
  separately operated network service before CogniStore needs that breadth.
- Office-suite conversion adds a much larger runtime and can introduce
  rendering-dependent output. Direct OOXML traversal is smaller and more
  deterministic for the ticket's body and table contract.
- OCR-capable PDF pipelines cover scanned documents but add models, native
  dependencies, cost, and nondeterminism that are outside this ticket.
