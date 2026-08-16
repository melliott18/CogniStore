from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Literal, Mapping

from cognistore.core.catalog import Catalog
from cognistore.drivers.storage_driver import (
    DEFAULT_STREAM_CHUNK_SIZE,
    ReadableStream,
    StorageDriver,
)

CHECKSUM_ALGORITHM = "sha256"


@dataclass(frozen=True)
class MovePlan:
    src_tier: str
    dst_tier: str
    bucket: str
    key: str
    size: int
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class MoveVerificationResult:
    """Observed integrity data for a completed or rejected move."""

    status: Literal["verified", "failed"]
    algorithm: str
    expected_size: int
    transferred_size: int | None
    source_size: int
    destination_stat_size: int | None
    destination_size: int
    source_checksum: str
    destination_checksum: str | None
    failure_details: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return self.status == "verified"


class MoveVerificationError(RuntimeError):
    """Raised when the committed destination cannot be verified."""

    def __init__(self, plan: MovePlan, result: MoveVerificationResult) -> None:
        self.plan = plan
        self.result = result
        details = "; ".join(result.failure_details) or "verification was incomplete"
        super().__init__(
            f"Move verification failed for {plan.src_tier}:{plan.bucket}/{plan.key} "
            f"-> {plan.dst_tier}:{plan.bucket}/{plan.key}: {details}"
        )


class _HashingReader:
    """Record the digest and byte count consumed from a source stream."""

    def __init__(self, source: ReadableStream) -> None:
        self._source = source
        self._hasher = hashlib.sha256()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        data = self._source.read(size)
        if not isinstance(data, bytes):
            raise TypeError("Object stream read() must return bytes")
        self._hasher.update(data)
        self.size += len(data)
        return data

    @property
    def checksum(self) -> str:
        return self._hasher.hexdigest()


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

    @staticmethod
    def _verification_result(
        plan: MovePlan,
        dst: StorageDriver,
        source: _HashingReader,
        transferred_size: Any,
    ) -> MoveVerificationResult:
        failures: list[str] = []
        reported_size: int | None
        if (
            isinstance(transferred_size, bool)
            or not isinstance(transferred_size, int)
            or transferred_size < 0
        ):
            reported_size = None
            failures.append(
                f"destination write reported invalid byte count {transferred_size!r}"
            )
        else:
            reported_size = transferred_size
            if reported_size != plan.size:
                failures.append(
                    f"destination write reported {reported_size} bytes; "
                    f"expected {plan.size}"
                )

        if source.size != plan.size:
            failures.append(
                f"source stream produced {source.size} bytes; expected {plan.size}"
            )

        destination_stat_size: int | None = None
        try:
            destination_metadata = dst.stat_object(plan.bucket, plan.key)
            observed_stat_size = destination_metadata.get("size")
            if (
                isinstance(observed_stat_size, bool)
                or not isinstance(observed_stat_size, int)
                or observed_stat_size < 0
            ):
                failures.append(
                    "destination stat returned invalid size "
                    f"{observed_stat_size!r}"
                )
            else:
                destination_stat_size = observed_stat_size
                if destination_stat_size != plan.size:
                    failures.append(
                        f"destination stat observed {destination_stat_size} bytes; "
                        f"expected {plan.size}"
                    )
        except Exception as error:
            failures.append(
                f"destination stat failed with {type(error).__name__}: {error}"
            )

        destination_hasher = hashlib.sha256()
        destination_size = 0
        destination_checksum: str | None = None
        destination_chunk_size = getattr(
            dst,
            "chunk_size",
            DEFAULT_STREAM_CHUNK_SIZE,
        )
        if (
            isinstance(destination_chunk_size, bool)
            or not isinstance(destination_chunk_size, int)
            or destination_chunk_size <= 0
        ):
            destination_chunk_size = DEFAULT_STREAM_CHUNK_SIZE
        try:
            with dst.open_object_reader(plan.bucket, plan.key) as destination:
                while True:
                    chunk = destination.read(destination_chunk_size)
                    if not isinstance(chunk, bytes):
                        raise TypeError("Object stream read() must return bytes")
                    if not chunk:
                        break
                    destination_hasher.update(chunk)
                    destination_size += len(chunk)
            destination_checksum = destination_hasher.hexdigest()
        except Exception as error:
            failures.append(
                "destination checksum read failed after "
                f"{destination_size} bytes with {type(error).__name__}: {error}"
            )

        if destination_checksum is not None:
            if destination_size != plan.size:
                failures.append(
                    f"destination checksum read observed {destination_size} bytes; "
                    f"expected {plan.size}"
                )
            if destination_checksum != source.checksum:
                failures.append(
                    f"checksum mismatch ({CHECKSUM_ALGORITHM}): "
                    f"source={source.checksum}, destination={destination_checksum}"
                )

        status: Literal["verified", "failed"] = (
            "failed" if failures else "verified"
        )
        return MoveVerificationResult(
            status=status,
            algorithm=CHECKSUM_ALGORITHM,
            expected_size=plan.size,
            transferred_size=reported_size,
            source_size=source.size,
            destination_stat_size=destination_stat_size,
            destination_size=destination_size,
            source_checksum=source.checksum,
            destination_checksum=destination_checksum,
            failure_details=tuple(failures),
        )

    def move(
        self, src_tier: str, dst_tier: str, bucket: str, key: str
    ) -> MoveVerificationResult:
        plan = self.plan(src_tier, dst_tier, bucket, key)
        src, dst = self._drivers_for_move(src_tier, dst_tier)

        with src.open_object_reader(bucket, key) as source_stream:
            source = _HashingReader(source_stream)
            transferred_size = dst.put_object_stream(
                bucket,
                key,
                source,
                size=plan.size,
                overwrite=False,
                metadata=plan.metadata,
            )

        verification = self._verification_result(
            plan,
            dst,
            source,
            transferred_size,
        )
        if not verification.verified:
            raise MoveVerificationError(plan, verification)
        assert verification.destination_checksum is not None

        # The destination stream is committed before the source or catalog is
        # mutated. Verification independently reads the committed destination,
        # so a failed or interrupted write leaves the source placement intact.
        src.delete_object(bucket, key)
        self.catalog.upsert_placement(
            bucket,
            key,
            size=verification.destination_size,
            tier=dst_tier,
            checksum=verification.destination_checksum,
        )
        return verification
