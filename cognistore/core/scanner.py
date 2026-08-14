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
        stat = driver.stat_object(bucket, key)
        size = int(stat.get("size", 0))
        if size:
            sample_end = min(size, 1024 * 1024) - 1
            data = driver.get_object(bucket, key, range=f"bytes=0-{sample_end}")
        else:
            data = b""
        indexed = active_indexer.index_bytes(data, filename=key)
        catalog.upsert(
            bucket,
            key,
            size=size,
            tier=tier,
            metadata={
                "path": stat.get("path"),
                "sha256": indexed.sha256,
                "mime": indexed.mime,
                "sample_len": len(indexed.sample),
            },
        )
        results.append(ScanResult(tier=tier, bucket=bucket, key=key, size=size))
    return results
