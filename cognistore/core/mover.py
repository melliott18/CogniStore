from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping

from cognistore.core.catalog import Catalog
from cognistore.drivers.storage_driver import StorageDriver


@dataclass(frozen=True)
class MovePlan:
    src_tier: str
    dst_tier: str
    bucket: str
    key: str
    size: int
    metadata: Mapping[str, Any]


class Mover:
    def __init__(self, drivers: Dict[str, StorageDriver], catalog: Catalog) -> None:
        self.drivers = drivers
        self.catalog = catalog

    def _drivers_for_move(
        self, src_tier: str, dst_tier: str
    ) -> tuple[StorageDriver, StorageDriver]:
        if src_tier == dst_tier:
            raise ValueError("Source and destination tiers must be different")

        if src_tier not in self.drivers:
            raise ValueError(f"Unknown source tier: {src_tier}")
        if dst_tier not in self.drivers:
            raise ValueError(f"Unknown destination tier: {dst_tier}")

        src = self.drivers[src_tier]
        dst = self.drivers[dst_tier]
        if src.same_backend(dst):
            raise ValueError(
                f"Source tier '{src_tier}' and destination tier '{dst_tier}' "
                "use the same storage backend"
            )
        return src, dst

    def plan(self, src_tier: str, dst_tier: str, bucket: str, key: str) -> MovePlan:
        """Validate a move using read-only operations and return its plan.

        Destination collisions fail closed. Executions additionally use an
        atomic no-overwrite write so a collision introduced after this
        preflight still cannot replace data.
        """

        src, dst = self._drivers_for_move(src_tier, dst_tier)
        source_metadata = dict(src.stat_object(bucket, key))
        source_size = source_metadata.get("size")
        if (
            isinstance(source_size, bool)
            or not isinstance(source_size, int)
            or source_size < 0
        ):
            raise ValueError(
                f"Source object has invalid size metadata: {src_tier}:{bucket}/{key}"
            )
        try:
            dst.stat_object(bucket, key)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(
                f"Destination object already exists: {dst_tier}:{bucket}/{key}"
            )

        return MovePlan(
            src_tier=src_tier,
            dst_tier=dst_tier,
            bucket=bucket,
            key=key,
            size=source_size,
            metadata=source_metadata,
        )

    def move(self, src_tier: str, dst_tier: str, bucket: str, key: str) -> None:
        plan = self.plan(src_tier, dst_tier, bucket, key)
        src, dst = self._drivers_for_move(src_tier, dst_tier)

        with src.open_object_reader(bucket, key) as source:
            transferred_size = dst.put_object_stream(
                bucket,
                key,
                source,
                size=plan.size,
                overwrite=False,
                metadata=plan.metadata,
            )

        # The destination stream is committed before the source or catalog is
        # mutated. A failed or interrupted write therefore leaves the source
        # placement intact and available for a later retry.
        src.delete_object(bucket, key)
        self.catalog.upsert_placement(
            bucket,
            key,
            size=transferred_size,
            tier=dst_tier,
        )
