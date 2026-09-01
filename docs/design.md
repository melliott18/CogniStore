# CogniStore design contracts

This document records the invariants that current code and future tickets must
preserve. The [architecture reference](architecture.md) describes component
placement; this document explains the safety choices between them.

## Storage-driver contract

Every driver provides object put/get, context-managed streaming reads,
exact-size streaming writes, idempotent delete, conditional generation-aware
delete, paginated/logical listing, and stat metadata containing size, modified
time, and an opaque stable generation.

Capabilities declare optional range operations and atomic no-overwrite. A
caller must distinguish an unsupported capability from a backend failure.
Drivers must not publish a partial object when a source stream is short, long,
cancelled, or raises. Conditional delete must never remove a different live
generation.

Backend identity is explicit. A move between aliases of the same physical
backend is rejected rather than treated as a safe cross-tier copy.

## Movement invariants

The mover follows these non-negotiable rules:

1. Persist identity and the observed source generation before transfer.
2. Stream with bounded memory and hash the complete source bytes.
3. Verify destination size and SHA-256 before committing placement.
4. Preserve unrelated catalog metadata when committing verified placement.
5. Reconfirm destination durability and generation before cleanup.
6. Delete only the exact recorded source generation.
7. Retain the source and durable failure evidence whenever a generation,
   integrity, durability, or catalog precondition fails.
8. Make every phase replayable by the same idempotency key; reject that key if
   it is reused for different coordinates.

The catalog transition graph rejects invalid shortcuts. Terminal jobs are
immutable except for read-only inspection and explicitly defined redrive or
recovery records.

## Scan/move coordination

A scanner captures an object-specific move fence before reading storage. The
catalog compares that fence atomically with publication, so an active or newer
move prevents a stale scan observation from overwriting authoritative
placement.

Scan publication includes canonical content identity. The scanner hashes the
complete stable source stream, creates a deterministic versioned source-byte
chunk manifest, and derives byte-addressed CAS keys. Object metadata, the
manifest, and its ordered mappings publish atomically under the existing move
fence; unrelated metadata is merged rather than replaced. The full contract is
documented in [Content identity and source-byte chunking](content_identity.md).

MIME detection is content-first. The indexing adapter asks libmagic to inspect
the sampled bytes and uses Python's filename inference only when libmagic is
unavailable, fails for that object, or returns no usable type. A libmagic
result wins when it disagrees with the filename. Scans retain the selected
type in the compatibility `mime` field and persist a versioned
`mime_detection` record containing both candidates, detector, provenance,
qualitative confidence, status, fallback reason, and disagreement. Confidence
is a CogniStore trust tier rather than a probability reported by libmagic;
generic binary/empty types and filename guesses are low confidence.
Disagreement is the literal inequality of the content and filename MIME
candidates. Filename compression encoding is retained separately because it
can describe an outer layer while the filename MIME describes inner content.

The `python-magic` wrapper requires the native libmagic library. CogniStore's
container images include it; host installations must provide their platform's
compatible libmagic package or library as described in the README. If the
native library is missing, indexing remains available through the recorded
filename fallback; one failed detection never aborts the rest of a catalog
scan.

## Queue and worker contract

Delivery is at least once. A versioned JSON envelope carries one stable job ID,
job type, UTC creation time, correlation ID, JSON payload, and string metadata.
Worker correctness must never depend on delivery attempt being exactly one.

Settlement order protects evidence:

- handlers and durable coordination finish before ACK;
- retry ownership is released before delayed NAK;
- exhausted/terminal diagnostics reach the DLQ before ACK;
- malformed deliveries are quarantined without blocking later work; and
- an unknown ACK result is not followed by a NAK that could contradict it.

Worker concurrency and queue capacity are bounded. Per-tier admission is fair,
cancellable, and separately accounts for source and destination roles. Live
rate changes do not revoke already granted work.

## Scheduler contract

The scheduler reserves and persists a complete occurrence before publishing.
Publication retries reuse the same envelope and job ID. Logical scope prevents
two scheduled instances of the same catalog or policy target from overlapping.
Manual operator jobs are independent and must not be aimed at a scheduled scope
without coordination.

Lease expiry is a liveness signal, not proof that a thread-backed side effect
stopped. Automatic takeover based only on TTL is forbidden. Operators can
inspect stale occurrences read-only and release one only after explicitly
confirming that its former worker is fenced. The audited, idempotent recovery
preserves the occurrence's job ID, generation, history, and logical scope.

## Configuration and external contracts

YAML loaders use safe parsing, reject duplicate/unknown keys, and validate
types and finite numeric bounds. CLI values resolve independently in this
order: explicit CLI, environment, named profile, file defaults, built-in
default.

JSON stdout uses a versioned `cognistore.cli` envelope. Human explanations,
logs, and verbose diagnostics use stderr. Failures are non-zero. Consumers must
branch on `schema`, `schema_version`, `command`, `status`, and `error_type`, and
tolerate additive fields.

Redaction is defense in depth, not secret storage. Credentials belong in
backend credential chains or environment indirection, never literal command
arguments or committed configuration. Parser diagnostics operate on a
token-preserving redacted argument view so attached, space-separated, repeated,
short-option, multi-token, and leading-dash secret values are not echoed.

Dry-run forbids CogniStore-managed storage, catalog, queue, cache, and local
output writes. The CLI reference discloses which live preconditions a preview
does not check. In particular, `move-resume` previews report the journal and
driver pair as checked while marking ownership and phase-specific storage
preconditions unchecked; they do not claim that writable recovery is ready.

## Failure policy

Safety-critical ambiguity fails closed. CogniStore prefers retained bytes,
unsettled delivery, quarantined state, and explicit evidence over guessing that
an operation did or did not happen. Retries must be bounded by policy unless
the condition represents healthy local backpressure or coordination deferral
that should not consume a failure attempt.

An operator recovery path must state its fencing assumptions, be idempotent,
record who/why/when, and preserve original evidence. Direct database edits are
not a supported recovery interface.

## Test and evidence contract

- Shared conformance tests enforce the storage-driver contract.
- Unit tests use controllable clocks and deterministic fault boundaries.
- Integration tests use isolated NATS, MinIO, and PostgreSQL/pgvector; they
  skip only when their documented environment variables are absent.
- CI runs every supported Python minor, branch coverage of at least 80%, Ruff,
  mypy, package validation, dependency/static security scans, and secret scan.
- Compose validates non-root images, health, live integration, and interrupted
  movement diagnostics.
- Reduced qualification is regression evidence only. M1 scale acceptance
  requires a retained clean-revision report with
  `acceptance_status: full_scale_passed`.

The default `python -m pytest` command uses collision-safe import identities
and collects the complete unit, conformance, and integration suite. Tests that
require external services retain their documented skip behavior.

## Evolution rules

M2 persistence, extraction, checksum/chunk, embedding, index, Ask, API, SDK,
and UI tickets must version every persisted or public contract before a
downstream consumer depends on it. Migrations and indexes need clean install,
upgrade, rollback/failure, concurrency, and rebuild coverage appropriate to
their state.

Later features may add stronger guarantees, but must not weaken source
retention, generation fencing, bounded memory, idempotent delivery, evidence
ordering, strict configuration, or secret-safe output.
