from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, Literal, Mapping, Protocol
from uuid import uuid4

from cognistore.auth.principal import current_principal
from cognistore.core.audit import AuditContext, AuditEvent, AuditEventType, AuditOutcome
from cognistore.core.budgets import BudgetOverride
from cognistore.core.catalog import CatalogStore, ObjectRecord
from cognistore.core.legal_holds import LegalHoldError
from cognistore.core.locality import LocalityConstraintError, assert_locality_allowed
from cognistore.core.move_jobs import (
    EXPECTED_SOURCE_SHA256_METADATA_KEY,
    MoveJob,
    MoveJobConflictError,
    MoveJobFailedError,
    MoveJobLeaseError,
    MoveJobState,
    MoveJobTransition,
)
from cognistore.core.placement_controls import (
    MOVEMENT_CONSTRAINTS_METADATA_KEY,
    MovementConstraintError,
    MovementConstraints,
    assert_move_allowed,
)
from cognistore.drivers.storage_driver import (
    DEFAULT_STREAM_CHUNK_SIZE,
    ObjectGenerationMismatchError,
    ReadableStream,
    StorageDriver,
)
from cognistore.observability import instrument, record_movement_bytes

CHECKSUM_ALGORITHM = "sha256"
_LOWERCASE_HEX = frozenset("0123456789abcdef")


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
    source_generation: str
    destination_generation: str | None
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


class MoveGenerationMismatchError(RuntimeError):
    """Raised when cleanup encounters a source or destination replacement."""

    def __init__(self, plan: MovePlan, role: str) -> None:
        self.plan = plan
        self.role = role
        super().__init__(
            f"Move aborted because the {role} generation changed for "
            f"{plan.bucket}/{plan.key}"
        )


class MoveSourceContentMismatchError(RuntimeError):
    """Raised before transfer when policy evidence targets different bytes."""

    def __init__(self, plan: MovePlan) -> None:
        self.plan = plan
        super().__init__(
            "Move aborted because policy evidence does not match the current "
            f"source bytes for {plan.bucket}/{plan.key}"
        )


class ByteThroughputController(Protocol):
    """Thread-safe byte limiter used by streaming move code."""

    def consume_bytes(
        self,
        tier: str,
        amount: int,
        *,
        on_wait: Callable[[], None] | None = None,
    ) -> None: ...


class _HashingReader:
    """Record the digest and byte count consumed from a source stream."""

    def __init__(
        self,
        source: ReadableStream,
        *,
        on_bytes: Callable[[int], None] | None = None,
    ) -> None:
        self._source = source
        self._on_bytes = on_bytes
        self._hasher = hashlib.sha256()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        data = self._source.read(size)
        if not isinstance(data, bytes):
            raise TypeError("Object stream read() must return bytes")
        if data and self._on_bytes is not None:
            self._on_bytes(len(data))
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
        catalog: CatalogStore,
        *,
        owner_id: str | None = None,
        lease_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
        transition_hook: Callable[[MoveJob], None] | None = None,
        throughput: ByteThroughputController | None = None,
        audit_context: AuditContext | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than zero")
        self.drivers = drivers
        self.catalog = catalog
        self.owner_id = owner_id or str(uuid4())
        self.lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._transition_hook = transition_hook
        self._throughput = throughput
        self.audit_context = audit_context

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

    def validate_driver_pair(self, src_tier: str, dst_tier: str) -> None:
        """Validate a recorded tier pair without touching object or catalog state."""

        self._drivers_for_move(src_tier, dst_tier)

    def plan(
        self, src_tier: str, dst_tier: str, bucket: str, key: str, *,
        movement_constraints: MovementConstraints | None = None,
        as_of: str | datetime | None = None,
        destination_pool_id: str | None = None,
        locality_exception_id: str | None = None,
    ) -> MovePlan:
        """Validate a move using read-only operations and return its plan.

        Destination collisions fail closed. Executions additionally use an
        atomic no-overwrite write so a collision introduced after this
        preflight still cannot replace data.
        """

        src, dst = self._drivers_for_move(src_tier, dst_tier)
        self.catalog.assert_not_held(
            bucket, key, operation="move.plan", context=self.audit_context,
        )
        self._check_movement_constraints(
            src_tier, dst_tier, bucket, key, movement_constraints, as_of=as_of
        )
        locality = self._check_locality(
            src_tier, dst_tier, bucket, key, as_of=as_of,
            destination_pool_id=destination_pool_id, exception_id=locality_exception_id,
        )
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
        source_generation = source_metadata.get("generation")
        if not isinstance(source_generation, str) or not source_generation:
            raise ValueError(
                f"Source object has no generation metadata: {src_tier}:{bucket}/{key}"
            )
        try:
            dst.stat_object(bucket, key)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(
                f"Destination object already exists: {dst_tier}:{bucket}/{key}"
            )

        source_metadata = self._destination_metadata(source_metadata)
        if locality["configured"]:
            source_metadata["cognistore_expected_destination_pool_id"] = locality["tier_pools"][dst_tier]
            source_metadata["cognistore_locality"] = locality
        if locality_exception_id is not None:
            source_metadata["cognistore_locality_exception_id"] = locality_exception_id

        return MovePlan(
            src_tier=src_tier,
            dst_tier=dst_tier,
            bucket=bucket,
            key=key,
            size=source_size,
            metadata=source_metadata,
        )

    def _check_locality(
        self, src_tier: str, dst_tier: str, bucket: str, key: str, *,
        as_of: str | datetime | None = None,
        destination_pool_id: str | None = None,
        exception_id: str | None = None,
        audit_context: AuditContext | None = None,
        move_id: str | None = None,
        stage: str | None = None,
        required: bool = False,
    ) -> dict[str, Any]:
        record = self.catalog.get(bucket, key)
        if record is None:
            record = ObjectRecord(bucket=bucket, key=key, size=0, tier=src_tier)
        evidence = assert_locality_allowed(
            record, dst_tier, tenant_id=self.catalog.tenant_id,
            tiers=self.catalog.list_tiers(), pools=self.catalog.list_pools(),
            as_of=self._clock() if as_of is None else as_of,
            exception_id=exception_id, destination_pool_id=destination_pool_id,
        )
        if required and not evidence["configured"]:
            raise LocalityConstraintError(
                "locality policy is required by the durable move contract", evidence,
            )
        exception = evidence.get("exception")
        if stage is not None and isinstance(exception, dict) and exception.get("used"):
            self._audit_locality(
                bucket, key, dst_tier, evidence, audit_context=audit_context,
                move_id=move_id, stage=stage,
            )
        return evidence

    def _audit_locality(
        self, bucket: str, key: str, dst_tier: str, evidence: dict[str, Any], *,
        audit_context: AuditContext | None, move_id: str | None,
        stage: str, rejection: str | None = None,
    ) -> None:
        context = audit_context or self.audit_context or AuditContext(
            correlation_id=move_id or str(uuid4()), actor_type="system", actor_id="mover",
        )
        principal = current_principal()
        if principal is not None:
            from dataclasses import replace
            context = replace(context, actor_type=principal.actor_type, actor_id=principal.actor_id)
        self.catalog.append_audit_event(AuditEvent.create(
            AuditEventType.LOCALITY_DECISION if rejection else AuditEventType.LOCALITY_EXCEPTION,
            AuditOutcome.REJECTED if rejection else AuditOutcome.ALLOWED,
            context, bucket=bucket, object_key=key, move_id=move_id,
            occurred_at=self._clock(),
            details={"destination_tier": dst_tier, "stage": stage,
                     "locality": evidence, "rejection": rejection},
        ))

    def _check_movement_constraints(
        self, src_tier: str, dst_tier: str, bucket: str, key: str,
        controls: MovementConstraints | None = None,
        *, as_of: str | datetime | None = None,
    ) -> None:
        record = self.catalog.get(bucket, key)
        tier = self.catalog.get_tier(src_tier)
        if record is None:
            # An unscanned source has no trustworthy placement start. A tier
            # timer must still fail closed for direct/manual moves.
            from cognistore.core.catalog import ObjectRecord
            record = ObjectRecord(bucket=bucket, key=key, size=0, tier=src_tier)
        elif record.tier != src_tier:
            raise MovementConstraintError("source placement changed since the move was selected")
        assert_move_allowed(
            record, dst_tier, controls,
            tier_metadata=None if tier is None else tier.metadata,
            as_of=self._clock() if as_of is None else as_of,
        )

    def _verification_result(
        self,
        plan: MovePlan,
        dst: StorageDriver,
        source_size: int,
        source_checksum: str,
        transferred_size: Any,
        job: MoveJob,
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
        destination_generation: str | None = None
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
            observed_generation = destination_metadata.get("generation")
            if not isinstance(observed_generation, str) or not observed_generation:
                failures.append("destination stat returned no generation token")
            else:
                destination_generation = observed_generation
        except FileNotFoundError as error:
            failures.append(
                f"destination stat failed with {type(error).__name__}"
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
                    self._consume_bytes(plan.dst_tier, len(chunk), job)
                    destination_hasher.update(chunk)
                    destination_size += len(chunk)
            destination_checksum = destination_hasher.hexdigest()
        except (FileNotFoundError, TypeError, ValueError) as error:
            failures.append(
                "destination checksum read failed after "
                f"{destination_size} bytes with {type(error).__name__}"
            )

        if destination_generation is not None:
            try:
                final_generation = dst.object_generation(plan.bucket, plan.key)
            except FileNotFoundError as error:
                failures.append(
                    f"destination generation check failed with {type(error).__name__}"
                )
            else:
                if final_generation != destination_generation:
                    failures.append("destination generation changed during verification")

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
            source_generation=str(plan.metadata["generation"]),
            destination_generation=destination_generation,
            failure_details=tuple(failures),
        )

    @instrument("movement", "move")
    def move(
        self,
        src_tier: str,
        dst_tier: str,
        bucket: str,
        key: str,
        *,
        idempotency_key: str | None = None,
        audit_context: AuditContext | None = None,
        expected_source_sha256: str | None = None,
        movement_constraints: MovementConstraints | None = None,
        budget_override: BudgetOverride | None = None,
        destination_pool_id: str | None = None,
        locality_exception_id: str | None = None,
    ) -> MoveVerificationResult:
        """Execute a durable move, retaining locality rejections outside rollback."""
        move_key = idempotency_key or str(uuid4())
        try:
            return self._move(
                src_tier, dst_tier, bucket, key, idempotency_key=move_key,
                audit_context=audit_context, expected_source_sha256=expected_source_sha256,
                movement_constraints=movement_constraints, budget_override=budget_override,
                destination_pool_id=destination_pool_id, locality_exception_id=locality_exception_id,
            )
        except LocalityConstraintError as exc:
            self._audit_locality(
                bucket, key, dst_tier, exc.evidence, audit_context=audit_context,
                move_id=move_key, stage="execution", rejection=str(exc),
            )
            raise

    def _move(
        self,
        src_tier: str,
        dst_tier: str,
        bucket: str,
        key: str,
        *,
        idempotency_key: str | None = None,
        audit_context: AuditContext | None = None,
        expected_source_sha256: str | None = None,
        movement_constraints: MovementConstraints | None = None,
        budget_override: BudgetOverride | None = None,
        destination_pool_id: str | None = None,
        locality_exception_id: str | None = None,
    ) -> MoveVerificationResult:
        """Execute or resume one durable, idempotent object move.

        An explicit ``idempotency_key`` identifies the move across process and
        message redelivery. Calls without one remain independent manual moves.
        """

        self._drivers_for_move(src_tier, dst_tier)
        if expected_source_sha256 is not None and (
            not isinstance(expected_source_sha256, str)
            or len(expected_source_sha256) != 64
            or any(character not in _LOWERCASE_HEX for character in expected_source_sha256)
        ):
            raise ValueError(
                "expected_source_sha256 must be a lowercase 64-character SHA-256 digest"
            )
        move_key = idempotency_key or str(uuid4())
        context = audit_context or self.audit_context
        existing = self.catalog.get_move_job(move_key)
        was_new = existing is None
        # Budgeted content verification consumes modeled source reads. Obtain
        # admission before hashing; a denied action must not read object bytes.
        guarded_content = expected_source_sha256 is not None or (
            existing is not None
            and existing.source_metadata.get(EXPECTED_SOURCE_SHA256_METADATA_KEY) is not None
        )
        defer_source_check = guarded_content and any(
            budget.matches(bucket, key) for budget in self.catalog.list_budgets()
        )
        if existing is None:
            self.catalog.assert_not_held(
                bucket, key, operation="move", context=context,
            )
            plan = self.plan(
                src_tier, dst_tier, bucket, key,
                movement_constraints=movement_constraints,
                destination_pool_id=destination_pool_id,
                locality_exception_id=locality_exception_id,
            )
            # Freeze policy controls into the durable contract for retries and
            # recovery by a worker that did not perform the original evaluation.
            plan = MovePlan(
                src_tier=plan.src_tier, dst_tier=plan.dst_tier,
                bucket=plan.bucket, key=plan.key, size=plan.size,
                metadata={
                    **self._destination_metadata(plan.metadata),
                    **{name: plan.metadata[name] for name in (
                        "cognistore_locality", "cognistore_locality_exception_id",
                        "cognistore_expected_destination_pool_id",
                    ) if name in plan.metadata},
                    "cognistore_movement_constraints":
                        (movement_constraints or MovementConstraints()).to_dict(),
                    **({"cognistore_budget_override": budget_override.to_dict()}
                       if budget_override is not None else {}),
                    **({"cognistore_expected_destination_pool_id": destination_pool_id}
                       if destination_pool_id is not None else {}),
                },
            )
            if expected_source_sha256 is not None:
                if not defer_source_check:
                    self._verify_expected_source_content(
                        plan,
                        expected_source_sha256,
                    )
                plan = MovePlan(
                    src_tier=plan.src_tier,
                    dst_tier=plan.dst_tier,
                    bucket=plan.bucket,
                    key=plan.key,
                    size=plan.size,
                    metadata={
                        **dict(plan.metadata),
                        EXPECTED_SOURCE_SHA256_METADATA_KEY: expected_source_sha256,
                    },
                )
        else:
            plan = MovePlan(
                src_tier=src_tier,
                dst_tier=dst_tier,
                bucket=bucket,
                key=key,
                size=existing.expected_size,
                metadata=existing.source_metadata,
            )
            self._validate_expected_source_contract(
                plan,
                expected_source_sha256,
            )
            self._validate_movement_contract(plan, movement_constraints)
            self._validate_locality_contract(plan, locality_exception_id)
            if destination_pool_id is not None and (
                plan.metadata.get("cognistore_expected_destination_pool_id") != destination_pool_id
            ):
                raise MoveJobConflictError("Destination pool differs from the durable move contract")
            if budget_override is not None and (
                plan.metadata.get("cognistore_budget_override") != budget_override.to_dict()
            ):
                raise MoveJobConflictError("Budget override differs from the durable move contract")

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
            audit_context=context,
        )
        # ``get_move_job`` above is only a preflight optimization.  The claim
        # is the atomic authority: another caller may have created this key
        # between the read and claim, so revalidate the contract that was
        # actually returned before any terminal result or transfer is accepted.
        claimed_plan = MovePlan(
            src_tier=job.src_tier,
            dst_tier=job.dst_tier,
            bucket=job.bucket,
            key=job.key,
            size=job.expected_size,
            metadata=job.source_metadata,
        )
        self._validate_expected_source_contract(
            claimed_plan,
            expected_source_sha256,
        )
        self._validate_movement_contract(claimed_plan, movement_constraints)
        self._validate_locality_contract(claimed_plan, locality_exception_id)
        if destination_pool_id is not None and (
            claimed_plan.metadata.get("cognistore_expected_destination_pool_id") != destination_pool_id
        ):
            raise MoveJobConflictError("Destination pool differs from the durable move contract")
        if budget_override is not None and (
            claimed_plan.metadata.get("cognistore_budget_override") != budget_override.to_dict()
        ):
            raise MoveJobConflictError("Budget override differs from the durable move contract")
        if was_new:
            self._after_transition(job)
        if job.state == MoveJobState.COMPLETED:
            return self._verification_from_job(job)
        if job.state == MoveJobState.FAILED:
            raise MoveJobFailedError(job)
        with self._lease_heartbeat(job.idempotency_key):
            if defer_source_check and job.state == MoveJobState.PREPARED:
                digest = claimed_plan.metadata.get(EXPECTED_SOURCE_SHA256_METADATA_KEY)
                if isinstance(digest, str):
                    try:
                        self._verify_expected_source_content(claimed_plan, digest)
                    except MoveSourceContentMismatchError:
                        self._transition(
                            job, MoveJobState.FAILED,
                            "budget-admitted source does not match policy content",
                            updates={"terminal_reason": "policy content mismatch"},
                            audit_context=context,
                        )
                        raise
            try:
                return self._resume(job, audit_context=context)
            except LegalHoldError:
                # A hold placed after selection also stops recovered jobs,
                # including jobs whose destination is already committed. Leave
                # every extant copy intact and terminate this durable attempt.
                current = self.catalog.get_move_job(job.idempotency_key)
                if current is not None and not current.state.terminal:
                    self._transition(
                        current, MoveJobState.FAILED,
                        "legal hold blocks object movement",
                        updates={"terminal_reason": "legal hold blocks object movement"},
                        audit_context=context,
                    )
                raise

    @staticmethod
    def _validate_locality_contract(plan: MovePlan, exception_id: str | None) -> None:
        if exception_id is not None and (
            plan.metadata.get("cognistore_locality_exception_id") != exception_id
        ):
            raise MoveJobConflictError("Locality exception differs from the durable move contract")

    @staticmethod
    def _validate_movement_contract(
        plan: MovePlan, controls: MovementConstraints | None,
    ) -> None:
        if controls is None:
            return
        raw = plan.metadata.get(MOVEMENT_CONSTRAINTS_METADATA_KEY)
        recorded = MovementConstraints() if raw is None else MovementConstraints.from_mapping(raw)
        if recorded.to_dict() != controls.to_dict():
            raise MoveJobConflictError("Movement constraints differ from the durable move contract")

    def _verify_expected_source_content(
        self,
        plan: MovePlan,
        expected_sha256: str,
    ) -> None:
        """Bind policy evidence to one generation before any move is claimed."""

        source, _destination = self._drivers_for_move(plan.src_tier, plan.dst_tier)
        generation = plan.metadata.get("generation")
        if not isinstance(generation, str) or not generation:
            raise ValueError("move plan lacks a valid source generation")
        chunk_size = getattr(source, "chunk_size", DEFAULT_STREAM_CHUNK_SIZE)
        if (
            isinstance(chunk_size, bool)
            or not isinstance(chunk_size, int)
            or chunk_size <= 0
        ):
            chunk_size = DEFAULT_STREAM_CHUNK_SIZE
        size = 0
        digest = hashlib.sha256()
        try:
            reader = source.open_object_reader_if_generation(
                plan.bucket,
                plan.key,
                generation,
            )
        except NotImplementedError as exc:
            raise RuntimeError(
                "policy-fenced moves require generation-bound source reads"
            ) from exc
        with reader as stream:
            while True:
                chunk = stream.read(chunk_size)
                if not isinstance(chunk, bytes):
                    raise TypeError("Object stream read() must return bytes")
                if not chunk:
                    break
                if self._throughput is not None:
                    self._throughput.consume_bytes(plan.src_tier, len(chunk))
                size += len(chunk)
                digest.update(chunk)
        if size != plan.size or digest.hexdigest() != expected_sha256:
            raise MoveSourceContentMismatchError(plan)

    @staticmethod
    def _validate_expected_source_contract(
        plan: MovePlan,
        expected_sha256: str | None,
    ) -> None:
        durable_expected = plan.metadata.get(EXPECTED_SOURCE_SHA256_METADATA_KEY)
        if durable_expected is not None and (
            not isinstance(durable_expected, str)
            or len(durable_expected) != 64
            or any(
                character not in _LOWERCASE_HEX
                for character in durable_expected
            )
        ):
            raise RuntimeError(
                "move job has invalid expected source SHA-256 metadata"
            )
        if expected_sha256 is not None and durable_expected != expected_sha256:
            raise MoveSourceContentMismatchError(plan)

    @instrument("movement", "recover")
    def recover_incomplete(
        self, *, idempotency_prefix: str | None = None
    ) -> list[MoveVerificationResult]:
        """Claim and resume every available non-terminal move job."""

        terminal = {MoveJobState.COMPLETED, MoveJobState.FAILED}
        jobs = self.catalog.list_move_jobs(
            states=set(MoveJobState).difference(terminal),
            idempotency_prefix=idempotency_prefix,
        )
        recovered: list[MoveVerificationResult] = []
        first_terminal_failure: Exception | None = None
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
            except Exception as error:
                # A move can fail terminally while the rest of this recovery
                # batch remains safe to run. Preserve transient failures by
                # propagating them immediately; only isolate errors whose
                # durable job record proves that recovery terminalized them.
                failed_job = self.catalog.get_move_job(job.idempotency_key)
                if failed_job is None or failed_job.state != MoveJobState.FAILED:
                    raise
                if first_terminal_failure is None:
                    first_terminal_failure = error
        if first_terminal_failure is not None:
            raise first_terminal_failure
        return recovered

    def get_job(self, idempotency_key: str) -> MoveJob | None:
        return self.catalog.get_move_job(idempotency_key)

    def list_jobs(
        self,
        *,
        states: set[MoveJobState] | None = None,
        idempotency_prefix: str | None = None,
    ) -> list[MoveJob]:
        return self.catalog.list_move_jobs(
            states=states,
            idempotency_prefix=idempotency_prefix,
        )

    def get_job_transitions(
        self, idempotency_key: str
    ) -> list[MoveJobTransition]:
        return self.catalog.list_move_job_transitions(idempotency_key)

    def _resume(
        self,
        job: MoveJob,
        *,
        audit_context: AuditContext | None,
    ) -> MoveVerificationResult:
        plan = MovePlan(
            src_tier=job.src_tier,
            dst_tier=job.dst_tier,
            bucket=job.bucket,
            key=job.key,
            size=job.expected_size,
            metadata=job.source_metadata,
        )
        src, dst = self._drivers_for_move(job.src_tier, job.dst_tier)
        source_generation = job.source_metadata.get("generation")
        if not isinstance(source_generation, str) or not source_generation:
            raise RuntimeError(
                "move job lacks the source generation required for transfer"
            )

        self.catalog.assert_not_held(
            job.bucket, job.key, operation="move.resume", context=audit_context,
        )

        if job.state in {
            MoveJobState.PREPARED, MoveJobState.TRANSFERRED, MoveJobState.VERIFIED
        }:
            raw_controls = job.source_metadata.get("cognistore_movement_constraints")
            controls = None if raw_controls is None else MovementConstraints.from_mapping(raw_controls)
            self._check_movement_constraints(
                job.src_tier, job.dst_tier, job.bucket, job.key, controls
            )

        while True:
            if not job.state.terminal:
                self.catalog.assert_not_held(
                    job.bucket, job.key,
                    operation=f"move.{job.state.value}", context=audit_context,
                )
                # Recheck each checkpoint, including recovery after placement
                # commit. A newly prohibited destination must never authorize
                # deletion of the retained source.
                self._check_locality(
                    job.src_tier, job.dst_tier, job.bucket, job.key,
                    destination_pool_id=job.source_metadata.get(
                        "cognistore_expected_destination_pool_id"
                    ),
                    exception_id=job.source_metadata.get("cognistore_locality_exception_id"),
                    audit_context=audit_context, move_id=job.idempotency_key,
                    stage=job.state.value,
                    required="cognistore_locality" in job.source_metadata,
                )
            if job.state == MoveJobState.PREPARED:
                try:
                    destination_exists = True
                    dst.stat_object(job.bucket, job.key)
                except FileNotFoundError:
                    destination_exists = False

                try:
                    if destination_exists:
                        # A prior publication can be visible even when its
                        # final durability barrier raised. Confirm it again
                        # before recovery advances toward source cleanup.
                        dst.ensure_object_durable(job.bucket, job.key)
                        source_size, source_checksum = self._hash_object(
                            src,
                            job.bucket,
                            job.key,
                            tier=job.src_tier,
                            job=job,
                            generation=source_generation,
                        )
                        transferred_size = job.expected_size
                        reason = "existing destination recovered after transfer"
                    else:
                        with self.catalog.destructive_operation(
                            job.bucket, job.key,
                            operation="move.transfer", context=audit_context,
                        ), src.open_object_reader_if_generation(
                            job.bucket,
                            job.key,
                            source_generation,
                        ) as source_stream:
                            source = _HashingReader(
                                source_stream,
                                on_bytes=lambda amount: self._consume_transfer_bytes(
                                    job, amount
                                ),
                            )
                            transferred_size = dst.put_object_stream(
                                job.bucket,
                                job.key,
                                source,
                                size=job.expected_size,
                                overwrite=False,
                                metadata=self._destination_metadata(
                                    job.source_metadata
                                ),
                            )
                            record_movement_bytes(transferred_size)
                            source_size = source.size
                            source_checksum = source.checksum
                        reason = "destination transfer completed"
                    observed_source_generation = src.object_generation(
                        job.bucket, job.key
                    )
                except ObjectGenerationMismatchError as error:
                    # ``put_object_stream`` does not return the generation it
                    # published. A later observation could already belong to a
                    # concurrent writer, so source-fence failures deliberately
                    # leave any destination residue for explicit reconciliation.
                    reason = "source generation changed during transfer"
                    job = self._transition(
                        job,
                        MoveJobState.FAILED,
                        reason,
                        updates={"terminal_reason": reason},
                        audit_context=audit_context,
                    )
                    raise MoveGenerationMismatchError(plan, "source") from error
                except FileNotFoundError as error:
                    # FileNotFoundError can also originate from a destination
                    # backend. Only terminalize the job after proving that its
                    # recorded source is actually absent.
                    try:
                        src.stat_object(job.bucket, job.key)
                    except FileNotFoundError:
                        failure_reason = (
                            "source object is missing before transfer completed: "
                            f"{type(error).__name__}"
                        )
                        job = self._transition(
                            job,
                            MoveJobState.FAILED,
                            failure_reason,
                            updates={
                                "verification_details": (failure_reason,),
                                "terminal_reason": failure_reason,
                            },
                            audit_context=audit_context,
                        )
                        raise MoveJobFailedError(job) from error
                    raise
                if observed_source_generation != job.source_metadata.get("generation"):
                    reason = "source generation changed during transfer"
                    job = self._transition(
                        job,
                        MoveJobState.FAILED,
                        reason,
                        updates={"terminal_reason": reason},
                        audit_context=audit_context,
                    )
                    raise MoveGenerationMismatchError(plan, "source")
                job = self._transition(
                    job,
                    MoveJobState.TRANSFERRED,
                    reason,
                    updates={
                        "transferred_size": transferred_size,
                        "source_size": source_size,
                        "source_checksum": source_checksum,
                    },
                    audit_context=audit_context,
                )

            elif job.state == MoveJobState.TRANSFERRED:
                if job.source_checksum is None or job.source_size is None:
                    source_size, source_checksum = self._hash_object(
                        src,
                        job.bucket,
                        job.key,
                        tier=job.src_tier,
                        job=job,
                        generation=source_generation,
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
                    job,
                )
                updates = {
                    "destination_size": verification.destination_size,
                    "destination_checksum": verification.destination_checksum,
                    "destination_generation": verification.destination_generation,
                    "verification_details": verification.failure_details,
                }
                if not verification.verified:
                    reason = "; ".join(verification.failure_details)
                    job = self._transition(
                        job,
                        MoveJobState.FAILED,
                        reason,
                        updates={**updates, "terminal_reason": reason},
                        audit_context=audit_context,
                    )
                    raise MoveVerificationError(plan, verification)
                job = self._transition(
                    job,
                    MoveJobState.VERIFIED,
                    "destination size and checksum verified",
                    updates=updates,
                    audit_context=audit_context,
                )

            elif job.state == MoveJobState.VERIFIED:
                assert job.destination_checksum is not None
                assert job.destination_size is not None
                assert job.destination_generation is not None
                now, lease_expires_at = self._lease_window()
                job = self.catalog.commit_move_job_placement(
                    job.idempotency_key,
                    owner_id=self.owner_id,
                    size=job.destination_size,
                    tier=job.dst_tier,
                    checksum=job.destination_checksum,
                    now=now,
                    lease_expires_at=lease_expires_at,
                    audit_context=audit_context,
                )
                self._after_transition(job)

            elif job.state == MoveJobState.COMMITTED:
                job = self._transition(
                    job,
                    MoveJobState.CLEANUP,
                    "source cleanup started",
                    audit_context=audit_context,
                )

            elif job.state == MoveJobState.CLEANUP:
                # A recovered cleanup rechecks the committed destination before
                # repeating the only destructive effect. Source deletion is
                # itself required to be idempotent by the driver contract.
                try:
                    self._verify_committed_destination(plan, dst, job)
                    self._require_destination_generation(dst, job)
                    source_generation = job.source_metadata.get("generation")
                    if not isinstance(source_generation, str) or not source_generation:
                        raise RuntimeError(
                            "move job lacks the source generation required for cleanup"
                        )
                    try:
                        source_reader = src.open_object_reader_if_generation(
                            job.bucket,
                            job.key,
                            source_generation,
                        )
                        with self.catalog.destructive_operation(
                            job.bucket, job.key,
                            operation="move.source_cleanup", context=audit_context,
                        ), source_reader as retained_source:
                            src.delete_object_if_generation(
                                job.bucket, job.key, source_generation
                            )
                            try:
                                # This is the cleanup linearization point.  If
                                # the destination changed while the source was
                                # being deleted, republish the still-open source
                                # generation before failing the move.
                                self._require_destination_generation(dst, job)
                            except ObjectGenerationMismatchError:
                                self._restore_retained_source(
                                    job,
                                    src,
                                    retained_source,
                                )
                                raise
                    except FileNotFoundError:
                        # Recovery can legitimately revisit CLEANUP after the
                        # conditional delete succeeded but before COMPLETED was
                        # checkpointed.  The destination must still match.
                        self._require_destination_generation(dst, job)
                except (MoveVerificationError, ObjectGenerationMismatchError) as error:
                    role = (
                        "destination"
                        if isinstance(error, MoveVerificationError)
                        or "destination" in str(error)
                        else "source"
                    )
                    self._restore_source_placement(job, src)
                    reason = f"{role} generation changed; source cleanup aborted"
                    job = self._transition(
                        job,
                        MoveJobState.FAILED,
                        reason,
                        updates={
                            "verification_details": (reason,),
                            "terminal_reason": reason,
                        },
                        audit_context=audit_context,
                    )
                    raise MoveGenerationMismatchError(plan, role) from error
                job = self._transition(
                    job,
                    MoveJobState.COMPLETED,
                    "source cleanup completed",
                    updates={"terminal_reason": "move completed"},
                    audit_context=audit_context,
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
        audit_context: AuditContext | None = None,
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
            audit_context=audit_context,
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

    def _hash_object(
        self,
        driver: StorageDriver,
        bucket: str,
        key: str,
        *,
        tier: str,
        job: MoveJob,
        generation: str | None = None,
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
        reader = (
            driver.open_object_reader(bucket, key)
            if generation is None
            else driver.open_object_reader_if_generation(
                bucket,
                key,
                generation,
            )
        )
        with reader as stream:
            while True:
                chunk = stream.read(chunk_size)
                if not isinstance(chunk, bytes):
                    raise TypeError("Object stream read() must return bytes")
                if not chunk:
                    break
                self._consume_bytes(tier, len(chunk), job)
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

    @staticmethod
    def _destination_metadata(
        source_metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Remove durable mover-only fields before publishing an object."""

        return {
            name: value
            for name, value in source_metadata.items()
            if name not in {
                EXPECTED_SOURCE_SHA256_METADATA_KEY, MOVEMENT_CONSTRAINTS_METADATA_KEY,
                "cognistore_budget_override", "cognistore_destination_pool_id",
                "cognistore_source_pool_id", "cognistore_expected_destination_pool_id",
                "cognistore_locality", "cognistore_locality_exception_id",
            }
        }

    @staticmethod
    def _require_destination_generation(
        dst: StorageDriver,
        job: MoveJob,
    ) -> None:
        try:
            observed = dst.object_generation(job.bucket, job.key)
        except FileNotFoundError as error:
            raise ObjectGenerationMismatchError(
                "destination generation is missing after final verification"
            ) from error
        if observed != job.destination_generation:
            raise ObjectGenerationMismatchError(
                "destination generation changed after final verification"
            )

    def _restore_retained_source(
        self,
        job: MoveJob,
        src: StorageDriver,
        retained_source: ReadableStream,
    ) -> None:
        """Republish retained bytes after a cleanup-time destination race."""

        source = _HashingReader(
            retained_source,
            on_bytes=lambda amount: self._consume_bytes(
                job.src_tier,
                amount,
                job,
            ),
        )
        try:
            transferred = src.put_object_stream(
                job.bucket,
                job.key,
                source,
                size=job.expected_size,
                overwrite=False,
                metadata=self._destination_metadata(job.source_metadata),
            )
        except FileExistsError:
            # Preserve any concurrent source replacement.  The outer failure
            # path will make that retained source authoritative in the catalog.
            return
        if (
            transferred != job.expected_size
            or source.size != job.expected_size
            or source.checksum != job.source_checksum
        ):
            raise RuntimeError(
                "failed to restore the verified source after destination replacement"
            )

    def _verify_committed_destination(
        self, plan: MovePlan, dst: StorageDriver, job: MoveJob
    ) -> None:
        destination_size, destination_checksum = self._hash_object(
            dst,
            job.bucket,
            job.key,
            tier=job.dst_tier,
            job=job,
        )
        if (
            destination_size != job.destination_size
            or destination_checksum != job.destination_checksum
            or dst.object_generation(job.bucket, job.key)
            != job.destination_generation
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
                source_generation=str(job.source_metadata.get("generation", "")),
                destination_generation=job.destination_generation,
                failure_details=(
                    "committed destination changed before source cleanup",
                ),
            )
            raise MoveVerificationError(plan, result)

    def _restore_source_placement(
        self, job: MoveJob, src: StorageDriver
    ) -> None:
        """Point the catalog at a retained source after cleanup is fenced off."""

        try:
            source_size, source_checksum = self._hash_object(
                src,
                job.bucket,
                job.key,
                tier=job.src_tier,
                job=job,
            )
        except FileNotFoundError:
            return
        self.catalog.upsert_placement(
            job.bucket,
            job.key,
            size=source_size,
            tier=job.src_tier,
            checksum=source_checksum,
        )

    def _consume_transfer_bytes(self, job: MoveJob, amount: int) -> None:
        self._consume_bytes(job.src_tier, amount, job)
        self._consume_bytes(job.dst_tier, amount, job)

    def _consume_bytes(self, tier: str, amount: int, job: MoveJob) -> None:
        if self._throughput is None or amount <= 0:
            return
        self._throughput.consume_bytes(
            tier,
            amount,
            on_wait=lambda: self._renew_move_lease(job),
        )

    def _renew_move_lease(self, job: MoveJob) -> None:
        now, lease_expires_at = self._lease_window()
        self.catalog.renew_move_job_lease(
            job.idempotency_key,
            owner_id=self.owner_id,
            expected_state=job.state,
            now=now,
            lease_expires_at=lease_expires_at,
        )

    @contextmanager
    def _lease_heartbeat(self, idempotency_key: str) -> Iterator[None]:
        """Keep ownership live across long, indivisible backend operations."""

        stop = threading.Event()
        failures: list[BaseException] = []
        interval = min(10.0, self.lease_seconds / 3.0)

        def heartbeat() -> None:
            while not stop.wait(interval):
                try:
                    now, lease_expires_at = self._lease_window()
                    renewed = self.catalog.renew_move_job_lease(
                        idempotency_key,
                        owner_id=self.owner_id,
                        expected_state=None,
                        now=now,
                        lease_expires_at=lease_expires_at,
                    )
                    if renewed.state.terminal:
                        return
                except BaseException as exc:
                    failures.append(exc)
                    return

        thread = threading.Thread(
            target=heartbeat,
            name="cognistore-move-lease-heartbeat",
            daemon=True,
        )
        thread.start()
        body_succeeded = False
        try:
            yield
            body_succeeded = True
        finally:
            stop.set()
            thread.join()
            if body_succeeded and failures:
                raise failures[0]

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
            source_generation=str(job.source_metadata.get("generation", "")),
            destination_generation=job.destination_generation,
            failure_details=(),
        )
