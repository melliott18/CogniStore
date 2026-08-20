from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class MoveJobState(str, Enum):
    """Durable checkpoints in the object movement state machine."""

    PREPARED = "prepared"
    TRANSFERRED = "transferred"
    VERIFIED = "verified"
    COMMITTED = "committed"
    CLEANUP = "cleanup"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {MoveJobState.COMPLETED, MoveJobState.FAILED}


MOVE_JOB_TRANSITIONS = {
    MoveJobState.PREPARED: frozenset(
        {MoveJobState.TRANSFERRED, MoveJobState.FAILED}
    ),
    MoveJobState.TRANSFERRED: frozenset(
        {MoveJobState.VERIFIED, MoveJobState.FAILED}
    ),
    MoveJobState.VERIFIED: frozenset({MoveJobState.COMMITTED}),
    MoveJobState.COMMITTED: frozenset({MoveJobState.CLEANUP}),
    MoveJobState.CLEANUP: frozenset(
        {MoveJobState.COMPLETED, MoveJobState.FAILED}
    ),
    MoveJobState.COMPLETED: frozenset(),
    MoveJobState.FAILED: frozenset(),
}


def validate_move_job_transition(
    from_state: MoveJobState, to_state: MoveJobState
) -> None:
    if to_state not in MOVE_JOB_TRANSITIONS[from_state]:
        raise ValueError(
            f"Invalid move-job transition: {from_state.value} -> {to_state.value}"
        )


@dataclass(frozen=True)
class MoveJob:
    idempotency_key: str
    src_tier: str
    dst_tier: str
    bucket: str
    key: str
    expected_size: int
    source_metadata: Mapping[str, Any]
    state: MoveJobState
    owner_id: str | None
    lease_expires_at: str | None
    transferred_size: int | None = None
    source_size: int | None = None
    source_checksum: str | None = None
    destination_size: int | None = None
    destination_checksum: str | None = None
    destination_generation: str | None = None
    verification_details: tuple[str, ...] = ()
    terminal_reason: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class MoveJobTransition:
    sequence: int
    idempotency_key: str
    from_state: MoveJobState | None
    to_state: MoveJobState
    reason: str
    created_at: str


class MoveJobConflictError(ValueError):
    """Raised when an idempotency key is reused for a different move."""


class MoveJobLeaseError(RuntimeError):
    """Raised when another owner still holds a live move-job lease."""


class MoveJobFailedError(RuntimeError):
    """Raised when an idempotency key names an already failed move."""

    def __init__(self, job: MoveJob) -> None:
        self.job = job
        super().__init__(
            f"Move job {job.idempotency_key!r} failed: "
            f"{job.terminal_reason or 'no terminal reason recorded'}"
        )
