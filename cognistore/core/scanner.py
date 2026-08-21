from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cognistore.drivers.storage_driver import StorageDriver

from .indexer import Indexer


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
    catalog: Any,
    prefix: str = "",
    indexer: Indexer | None = None,
) -> list[ScanResult]:
    """Scan one tier into a catalog and return the indexed object summaries."""

    active_indexer = indexer or Indexer()
    results: list[ScanResult] = []
    for key in driver.list_objects(bucket, prefix=prefix):
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
            indexed = active_indexer.index_bytes(data, filename=key)
            # Do not combine bytes and metadata from different physical
            # generations. The catalog fence below separately protects this
            # stable storage observation from concurrent move transitions.
            if driver.object_generation(bucket, key) != generation:
                continue
        except FileNotFoundError:
            # Listings are snapshots. A concurrent move or external deletion
            # may retire an entry before it can be observed consistently.
            continue

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
                "sample_len": len(indexed.sample),
            },
            fence=fence,
        )
        if not published:
            continue
        results.append(ScanResult(tier=tier, bucket=bucket, key=key, size=size))
    return results
