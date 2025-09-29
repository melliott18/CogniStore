from __future__ import annotations

from typing import Dict

from cognistore.core.catalog import Catalog
from cognistore.drivers.storage_driver import StorageDriver


class Mover:
    def __init__(self, drivers: Dict[str, StorageDriver], catalog: Catalog) -> None:
        self.drivers = drivers
        self.catalog = catalog

    def move(self, src_tier: str, dst_tier: str, bucket: str, key: str) -> None:
        src = self.drivers[src_tier]
        dst = self.drivers[dst_tier]

        data = src.get_object(bucket, key)
        dst.put_object(bucket, key, data)
        src.delete_object(bucket, key)

        # Update catalog
        rec = self.catalog.get(bucket, key)
        size = len(data)
        if rec is None:
            self.catalog.upsert(bucket=bucket, key=key, size=size, tier=dst_tier)
        else:
            rec.size = size
            self.catalog.update_placement(bucket, key, dst_tier)
