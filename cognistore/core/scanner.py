from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from uuid import uuid4

from cognistore.auth.principal import current_principal
from cognistore.core.audit import AuditContext, AuditEvent, AuditEventType, AuditOutcome
from cognistore.drivers.storage_driver import (
    ObjectGenerationMismatchError,
    ReadableStream,
    StorageDriver,
)
from cognistore.observability import current_correlation_id

from .catalog import CatalogStore
from .content_identity import ContentSizeMismatchError
from .indexer import Indexer
from .legal_holds import LegalHoldError
from .object_mutation_lock import ObjectMutationConflictError
from .pii import PIIDetectionPipeline

_MAX_MIME_SAMPLE_BYTES = 1024 * 1024


def _read_stream_part(reader: ReadableStream, requested: int) -> bytes:
    part = reader.read(requested)
    if not isinstance(part, bytes):
        raise TypeError("content stream read() must return bytes")
    if len(part) > requested:
        raise ValueError("content stream returned more bytes than requested")
    return part


def _read_prefix(reader: ReadableStream, size: int) -> bytes:
    prefix = bytearray()
    while len(prefix) < size:
        part = _read_stream_part(reader, size - len(prefix))
        if not part:
            break
        prefix.extend(part)
    return bytes(prefix)


class _PrefixReplayReader:
    """Replay an already-read prefix before continuing one source stream."""

    def __init__(self, prefix: bytes, reader: ReadableStream) -> None:
        self._prefix = prefix
        self._reader = reader
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        if self._offset == len(self._prefix):
            return self._reader.read(size)

        if size < 0:
            remaining = self._prefix[self._offset :]
            self._offset = len(self._prefix)
            suffix = self._reader.read()
            if not isinstance(suffix, bytes):
                raise TypeError("content stream read() must return bytes")
            return remaining + suffix

        prefix_size = min(size, len(self._prefix) - self._offset)
        first = self._prefix[self._offset : self._offset + prefix_size]
        self._offset += prefix_size
        if prefix_size == size:
            return first
        suffix = _read_stream_part(self._reader, size - prefix_size)
        return first + suffix


def _open_generation_bound_reader(
    driver: StorageDriver,
    bucket: str,
    key: str,
    generation: str,
) -> AbstractContextManager[ReadableStream]:
    # Fail closed for legacy duck-typed drivers. A before/read/after fallback
    # admits A -> B -> A replacement races and would defeat this contract.
    opener = getattr(driver, "open_object_reader_if_generation", None)
    if opener is None:
        raise NotImplementedError(
            "Storage driver does not support generation-bound object reads"
        )
    return opener(bucket, key, generation)


@dataclass(frozen=True)
class ScanResult:
    tier: str
    bucket: str
    key: str
    size: int


def scan_catalog(
    *,
    tier: str,
    bucket: str,
    driver: StorageDriver,
    catalog: CatalogStore | None,
    prefix: str = "",
    indexer: Indexer | None = None,
    pii_pipeline: PIIDetectionPipeline | None = None,
    dry_run: bool = False,
    audit_context: AuditContext | None = None,
) -> list[ScanResult]:
    """Audit a catalog scan's intent and outcome without recording object content.

    A preview performs the same reads without changing the catalog or audit log.
    Persist intent before any observation can be published. Each actual scan
    attempt has its own start event, including retried worker deliveries.
    """
    if catalog is None and not dry_run:
        raise ValueError("catalog is required unless dry_run is enabled")
    def perform_scan() -> list[ScanResult]:
        return _scan_catalog(
            tier=tier, bucket=bucket, driver=driver, catalog=catalog,
            prefix=prefix, indexer=indexer, pii_pipeline=pii_pipeline, dry_run=dry_run,
        )

    if dry_run:
        return perform_scan()
    assert catalog is not None
    principal = current_principal()
    context = audit_context or AuditContext(
        correlation_id=current_correlation_id() or str(uuid4()),
        actor_type=principal.actor_type if principal is not None else "system",
        actor_id=principal.actor_id if principal is not None else "catalog-scanner",
    )
    details = {"tier": tier, "bucket": bucket, "prefix": prefix}
    started = catalog.append_audit_event(AuditEvent.create(
        AuditEventType.SCAN_STARTED, AuditOutcome.STARTED, context,
        details=details,
    ))
    terminal_context = replace(context, causation_id=started.event_id)
    try:
        results = perform_scan()
    except Exception as error:
        catalog.append_audit_event(AuditEvent.create(
            AuditEventType.SCAN_FAILED, AuditOutcome.FAILED, terminal_context,
            details={**details, "exception_type": (
                f"{type(error).__module__}.{type(error).__name__}"
            )},
        ))
        raise
    catalog.append_audit_event(AuditEvent.create(
        AuditEventType.SCAN_COMPLETED, AuditOutcome.SUCCEEDED, terminal_context,
        details={**details, "objects_observed": len(results)},
    ))
    return results


def _scan_catalog(
    *,
    tier: str,
    bucket: str,
    driver: StorageDriver,
    catalog: CatalogStore | None,
    prefix: str = "",
    indexer: Indexer | None = None,
    pii_pipeline: PIIDetectionPipeline | None = None,
    dry_run: bool = False,
) -> list[ScanResult]:
    """Scan one tier and return object summaries.

    A dry-run performs the same stable storage reads and indexing work, but it
    never captures catalog fences or publishes observations.  This keeps the
    preview useful while guaranteeing that it cannot mutate either storage or
    catalog state.
    """

    if catalog is None and not dry_run:
        raise ValueError("catalog is required unless dry_run is enabled")

    active_indexer = indexer or Indexer()
    active_pii_pipeline = pii_pipeline or PIIDetectionPipeline(detectors=())
    results: list[ScanResult] = []
    for key in driver.list_objects(bucket, prefix=prefix):
        # Cleanup and API mutations must not retire a generation between the
        # final backend check and publication of its content references. Keep
        # hold/lifecycle protection outside the same per-key fence used by API
        # PUT/DELETE, and retain both through catalog publication.
        protection: AbstractContextManager[None] = nullcontext()
        mutation: AbstractContextManager[None] = nullcontext()
        if not dry_run:
            assert catalog is not None
            protection = catalog.destructive_operation(
                bucket, key, operation="catalog.scan_observation",
            )
            mutation = catalog.object_mutation(bucket, key)
        try:
            with protection, mutation:
                if dry_run:
                    fence = None
                else:
                    assert catalog is not None
                    fence = catalog.capture_scan_fence(bucket, key)
                try:
                    stat = driver.stat_object(bucket, key)
                    generation = stat.get("generation")
                    if not isinstance(generation, str) or not generation:
                        raise RuntimeError(
                            f"Storage driver returned no generation for {bucket}/{key}"
                        )
                    size = int(stat.get("size", 0))
                    try:
                        with _open_generation_bound_reader(
                            driver,
                            bucket,
                            key,
                            generation,
                        ) as reader:
                            sample = _read_prefix(
                                reader,
                                min(size, _MAX_MIME_SAMPLE_BYTES) if size > 0 else 0,
                            )
                            indexed = active_indexer.index_stream(
                                _PrefixReplayReader(sample, reader),
                                sample=sample,
                                filename=key,
                                source_size=size,
                            )
                    except ContentSizeMismatchError:
                        # A concurrent replacement can invalidate the stat size while
                        # the full stream is being consumed. Treat that like the
                        # generation race below, but surface a mismatch from an
                        # otherwise unchanged backend as a storage contract failure.
                        if driver.object_generation(bucket, key) != generation:
                            continue
                        raise
                    content = indexed.content
                    if content is None:
                        raise RuntimeError("Stream indexing returned no content identity")
                    classification = active_pii_pipeline.detect(
                        indexed.document_extraction, content_sha256=content.sha256
                    )
                    # Do not combine bytes and metadata from different physical
                    # generations. The catalog fence below separately protects this
                    # stable storage observation from concurrent move transitions.
                    if driver.object_generation(bucket, key) != generation:
                        continue
                except (FileNotFoundError, ObjectGenerationMismatchError):
                    # Listings are snapshots. A concurrent move or external deletion
                    # or replacement may retire an entry before it can be observed
                    # consistently.
                    continue

                if dry_run:
                    results.append(ScanResult(tier=tier, bucket=bucket, key=key, size=size))
                    continue

                assert catalog is not None
                assert fence is not None
                extraction_metadata = indexed.document_extraction.to_metadata()
                if classification.status != "disabled":
                    # Detection consumes extracted text only in memory. Withhold the
                    # full text and document properties even on failure: an unknown
                    # classification must not accidentally publish sensitive content.
                    extraction_metadata["text"] = None
                    extraction_metadata["document_metadata"] = {}
                published = catalog.upsert_scan_observation(
                    bucket,
                    key,
                    size=size,
                    tier=tier,
                    generation=generation,
                    metadata={
                        "path": stat.get("path"),
                        "sha256": indexed.sha256,
                        "content_identity": content.to_metadata(),
                        "mime": indexed.mime,
                        "mime_detection": indexed.mime_detection.to_metadata(),
                        "document_extraction": extraction_metadata,
                        "pii_detection": classification.to_metadata(),
                        "sample_len": len(indexed.sample),
                    },
                    fence=fence,
                    content=content,
                )
                if not published:
                    continue
                results.append(ScanResult(tier=tier, bucket=bucket, key=key, size=size))
        except (LegalHoldError, ObjectMutationConflictError):
            # A held or busy key has no authoritative observation this pass.
            # A later scan can revisit it after the competing work settles.
            continue
    return results
