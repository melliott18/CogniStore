from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Literal, Mapping
from uuid import uuid4

from cognistore.core.catalog import Catalog
from cognistore.core.move_jobs import (
    MoveJob,
    MoveJobFailedError,
    MoveJobLeaseError,
    MoveJobState,
    MoveJobTransition,
)
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
    def __init__(
        self,
        drivers: Dict[str, StorageDriver],
        catalog: Catalog,
        *,
        owner_id: str | None = None,
        lease_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
        transition_hook: Callable[[MoveJob], None] | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than zero")
        self.drivers = drivers
        self.catalog = catalog
        self.owner_id = owner_id or str(uuid4())
        self.lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._transition_hook = transition_hook

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
        source_size: int,
        source_checksum: str,
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

        if source_size != plan.size:
            failures.append(
                f"source stream produced {source_size} bytes; expected {plan.size}"
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
        except FileNotFoundError as error:
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
        except (FileNotFoundError, TypeError, ValueError) as error:
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
            if destination_checksum != source_checksum:
                failures.append(
                    f"checksum mismatch ({CHECKSUM_ALGORITHM}): "
                    f"source={source_checksum}, destination={destination_checksum}"
                )

        status: Literal["verified", "failed"] = (
            "failed" if failures else "verified"
        )
        return MoveVerificationResult(
            status=status,
            algorithm=CHECKSUM_ALGORITHM,
            expected_size=plan.size,
            transferred_size=reported_size,
            source_size=source_size,
            destination_stat_size=destination_stat_size,
            destination_size=destination_size,
            source_checksum=source_checksum,
            destination_checksum=destination_checksum,
            failure_details=tuple(failures),
        )

    def move(
        self,
        src_tier: str,
        dst_tier: str,
        bucket: str,
        key: str,
        *,
        idempotency_key: str | None = None,
    ) -> MoveVerificationResult:
        """Execute or resume one durable, idempotent object move.

        An explicit ``idempotency_key`` identifies the move across process and
        message redelivery. Calls without one remain independent manual moves.
        """

        self._drivers_for_move(src_tier, dst_tier)
        move_key = idempotency_key or str(uuid4())
        existing = self.catalog.get_move_job(move_key)
        was_new = existing is None
        if existing is None:
            plan = self.plan(src_tier, dst_tier, bucket, key)
        else:
            plan = MovePlan(
                src_tier=src_tier,
                dst_tier=dst_tier,
                bucket=bucket,
                key=key,
                size=existing.expected_size,
                metadata=existing.source_metadata,
            )

        now, lease_expires_at = self._lease_window()
        job = self.catalog.claim_move_job(
            move_key,
            src_tier=src_tier,
            dst_tier=dst_tier,
            bucket=bucket,
            key=key,
            expected_size=plan.size,
            source_metadata=plan.metadata,
            owner_id=self.owner_id,
            now=now,
            lease_expires_at=lease_expires_at,
        )
        if was_new:
            self._after_transition(job)
        if job.state == MoveJobState.COMPLETED:
            return self._verification_from_job(job)
        if job.state == MoveJobState.FAILED:
            raise MoveJobFailedError(job)
        return self._resume(job)

    def recover_incomplete(
        self, *, idempotency_prefix: str | None = None
    ) -> list[MoveVerificationResult]:
        """Claim and resume every available non-terminal move job."""

        terminal = {MoveJobState.COMPLETED, MoveJobState.FAILED}
        jobs = self.catalog.list_move_jobs(idempotency_prefix=idempotency_prefix)
        recovered: list[MoveVerificationResult] = []
        for job in jobs:
            if job.state in terminal:
                continue
            try:
                recovered.append(
                    self.move(
                        job.src_tier,
                        job.dst_tier,
                        job.bucket,
                        job.key,
                        idempotency_key=job.idempotency_key,
                    )
                )
            except MoveJobLeaseError:
                continue
        return recovered

    def get_job(self, idempotency_key: str) -> MoveJob | None:
        return self.catalog.get_move_job(idempotency_key)

    def list_jobs(self) -> list[MoveJob]:
        return self.catalog.list_move_jobs()

    def get_job_transitions(
        self, idempotency_key: str
    ) -> list[MoveJobTransition]:
        return self.catalog.list_move_job_transitions(idempotency_key)

    def _resume(self, job: MoveJob) -> MoveVerificationResult:
        plan = MovePlan(
            src_tier=job.src_tier,
            dst_tier=job.dst_tier,
            bucket=job.bucket,
            key=job.key,
            size=job.expected_size,
            metadata=job.source_metadata,
        )
        src, dst = self._drivers_for_move(job.src_tier, job.dst_tier)

        while True:
            if job.state == MoveJobState.PREPARED:
                try:
                    destination_exists = True
                    dst.stat_object(job.bucket, job.key)
                except FileNotFoundError:
                    destination_exists = False

                if destination_exists:
                    source_size, source_checksum = self._hash_object(
                        src, job.bucket, job.key
                    )
                    transferred_size = job.expected_size
                    reason = "existing destination recovered after transfer"
                else:
                    with src.open_object_reader(job.bucket, job.key) as source_stream:
                        source = _HashingReader(source_stream)
                        transferred_size = dst.put_object_stream(
                            job.bucket,
                            job.key,
                            source,
                            size=job.expected_size,
                            overwrite=False,
                            metadata=job.source_metadata,
                        )
                    source_size = source.size
                    source_checksum = source.checksum
                    reason = "destination transfer completed"
                job = self._transition(
                    job,
                    MoveJobState.TRANSFERRED,
                    reason,
                    updates={
                        "transferred_size": transferred_size,
                        "source_size": source_size,
                        "source_checksum": source_checksum,
                    },
                )

            elif job.state == MoveJobState.TRANSFERRED:
                if job.source_checksum is None or job.source_size is None:
                    source_size, source_checksum = self._hash_object(
                        src, job.bucket, job.key
                    )
                else:
                    source_size = job.source_size
                    source_checksum = job.source_checksum
                verification = self._verification_result(
                    plan,
                    dst,
                    source_size,
                    source_checksum,
                    job.transferred_size,
                )
                updates = {
                    "destination_size": verification.destination_size,
                    "destination_checksum": verification.destination_checksum,
                    "verification_details": verification.failure_details,
                }
                if not verification.verified:
                    reason = "; ".join(verification.failure_details)
                    job = self._transition(
                        job,
                        MoveJobState.FAILED,
                        reason,
                        updates={**updates, "terminal_reason": reason},
                    )
                    raise MoveVerificationError(plan, verification)
                job = self._transition(
                    job,
                    MoveJobState.VERIFIED,
                    "destination size and checksum verified",
                    updates=updates,
                )

            elif job.state == MoveJobState.VERIFIED:
                assert job.destination_checksum is not None
                assert job.destination_size is not None
                now, lease_expires_at = self._lease_window()
                job = self.catalog.commit_move_job_placement(
                    job.idempotency_key,
                    owner_id=self.owner_id,
                    size=job.destination_size,
                    tier=job.dst_tier,
                    checksum=job.destination_checksum,
                    now=now,
                    lease_expires_at=lease_expires_at,
                )
                self._after_transition(job)

            elif job.state == MoveJobState.COMMITTED:
                job = self._transition(
                    job,
                    MoveJobState.CLEANUP,
                    "source cleanup started",
                )

            elif job.state == MoveJobState.CLEANUP:
                # A recovered cleanup rechecks the committed destination before
                # repeating the only destructive effect. Source deletion is
                # itself required to be idempotent by the driver contract.
                self._verify_committed_destination(plan, dst, job)
                src.delete_object(job.bucket, job.key)
                job = self._transition(
                    job,
                    MoveJobState.COMPLETED,
                    "source cleanup completed",
                    updates={"terminal_reason": "move completed"},
                )

            elif job.state == MoveJobState.COMPLETED:
                return self._verification_from_job(job)
            elif job.state == MoveJobState.FAILED:
                raise MoveJobFailedError(job)

    def _transition(
        self,
        job: MoveJob,
        to_state: MoveJobState,
        reason: str,
        *,
        updates: Mapping[str, Any] | None = None,
    ) -> MoveJob:
        now, lease_expires_at = self._lease_window()
        updated = self.catalog.transition_move_job(
            job.idempotency_key,
            owner_id=self.owner_id,
            expected_state=job.state,
            to_state=to_state,
            reason=reason,
            now=now,
            lease_expires_at=lease_expires_at,
            updates=updates,
        )
        self._after_transition(updated)
        return updated

    def _after_transition(self, job: MoveJob) -> None:
        if self._transition_hook is not None:
            self._transition_hook(job)

    def _lease_window(self) -> tuple[str, str]:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("move-job clock must return a timezone-aware datetime")
        now = now.astimezone(timezone.utc)
        expires = now + timedelta(seconds=self.lease_seconds)
        return self._timestamp(now), self._timestamp(expires)

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _hash_object(
        driver: StorageDriver, bucket: str, key: str
    ) -> tuple[int, str]:
        chunk_size = getattr(driver, "chunk_size", DEFAULT_STREAM_CHUNK_SIZE)
        if (
            isinstance(chunk_size, bool)
            or not isinstance(chunk_size, int)
            or chunk_size <= 0
        ):
            chunk_size = DEFAULT_STREAM_CHUNK_SIZE
        size = 0
        digest = hashlib.sha256()
        with driver.open_object_reader(bucket, key) as stream:
            while True:
                chunk = stream.read(chunk_size)
                if not isinstance(chunk, bytes):
                    raise TypeError("Object stream read() must return bytes")
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

    def _verify_committed_destination(
        self, plan: MovePlan, dst: StorageDriver, job: MoveJob
    ) -> None:
        destination_size, destination_checksum = self._hash_object(
            dst, job.bucket, job.key
        )
        if (
            destination_size != job.destination_size
            or destination_checksum != job.destination_checksum
        ):
            result = MoveVerificationResult(
                status="failed",
                algorithm=CHECKSUM_ALGORITHM,
                expected_size=job.expected_size,
                transferred_size=job.transferred_size,
                source_size=job.source_size or job.expected_size,
                destination_stat_size=destination_size,
                destination_size=destination_size,
                source_checksum=job.source_checksum or "",
                destination_checksum=destination_checksum,
                failure_details=(
                    "committed destination changed before source cleanup",
                ),
            )
            raise MoveVerificationError(plan, result)

    @staticmethod
    def _verification_from_job(job: MoveJob) -> MoveVerificationResult:
        if (
            job.source_checksum is None
            or job.destination_checksum is None
            or job.destination_size is None
        ):
            raise RuntimeError(
                f"Completed move job {job.idempotency_key!r} lacks verification data"
            )
        return MoveVerificationResult(
            status="verified",
            algorithm=CHECKSUM_ALGORITHM,
            expected_size=job.expected_size,
            transferred_size=job.transferred_size,
            source_size=job.source_size or job.expected_size,
            destination_stat_size=job.destination_size,
            destination_size=job.destination_size,
            source_checksum=job.source_checksum,
            destination_checksum=job.destination_checksum,
            failure_details=(),
        )
