from __future__ import annotations

from dataclasses import dataclass

from cognistore.drivers.storage_driver import StorageDriver

from .catalog import CatalogStore
from .indexer import Indexer

_DOCUMENT_READ_CHUNK_SIZE = 1024 * 1024


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
    results: list[ScanResult] = []
    for key in driver.list_objects(bucket, prefix=prefix):
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
            if size:
                sample_end = min(size, 1024 * 1024) - 1
                data = driver.get_object(
                    bucket, key, range=f"bytes=0-{sample_end}"
                )
            else:
                data = b""

            def load_document() -> bytes:
                if len(data) == size:
                    return data
                # Read one byte beyond the cap so an object that grew after
                # stat is rejected. Using the stream contract keeps the read
                # bounded even for backends without native range support.
                remaining = active_indexer.max_document_file_bytes + 1
                chunks: list[bytes] = []
                with driver.open_object_reader(bucket, key) as reader:
                    while remaining:
                        chunk = reader.read(min(_DOCUMENT_READ_CHUNK_SIZE, remaining))
                        if not isinstance(chunk, bytes):
                            raise TypeError("Object stream read() must return bytes")
                        if not chunk:
                            break
                        if len(chunk) > remaining:
                            raise RuntimeError(
                                "Object stream returned more bytes than requested"
                            )
                        chunks.append(chunk)
                        remaining -= len(chunk)
                return b"".join(chunks)

            indexed = active_indexer.index_bytes(
                data,
                filename=key,
                source_size=size,
                document_loader=load_document,
            )
            # Do not combine bytes and metadata from different physical
            # generations. The catalog fence below separately protects this
            # stable storage observation from concurrent move transitions.
            if driver.object_generation(bucket, key) != generation:
                continue
        except FileNotFoundError:
            # Listings are snapshots. A concurrent move or external deletion
            # may retire an entry before it can be observed consistently.
            continue

        if dry_run:
            results.append(ScanResult(tier=tier, bucket=bucket, key=key, size=size))
            continue

        assert catalog is not None
        assert fence is not None
        published = catalog.upsert_scan_observation(
            bucket,
            key,
            size=size,
            tier=tier,
            generation=generation,
            metadata={
                "path": stat.get("path"),
                "sha256": indexed.sha256,
                "mime": indexed.mime,
                "mime_detection": indexed.mime_detection.to_metadata(),
                "document_extraction": indexed.document_extraction.to_metadata(),
                "sample_len": len(indexed.sample),
            },
            fence=fence,
        )
        if not published:
            continue
        results.append(ScanResult(tier=tier, bucket=bucket, key=key, size=size))
    return results
