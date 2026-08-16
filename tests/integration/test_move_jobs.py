from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cognistore.core.move_jobs import (
    MoveJobConflictError,
    MoveJobLeaseError,
    MoveJobState,
)
from cognistore.core.mover import Mover, MoveVerificationError
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver


class InjectedCrash(BaseException):
    pass


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 16, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.mark.parametrize(
    "crash_state",
    [
        MoveJobState.PREPARED,
        MoveJobState.TRANSFERRED,
        MoveJobState.VERIFIED,
        MoveJobState.COMMITTED,
        MoveJobState.CLEANUP,
        MoveJobState.COMPLETED,
    ],
)
def test_move_recovers_after_crash_at_every_state_transition(
    tmp_path: Path, crash_state: MoveJobState
) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    bucket = "bucket"
    key = "recover/object.bin"
    data = b"one durable logical object"
    hot.put_object(bucket, key, data)
    database = tmp_path / "catalog.db"
    catalog = SQLiteCatalog(database)
    catalog.upsert(bucket, key, len(data), "hot", {"revision": 1})
    clock = MutableClock()
    crashed = False

    def crash_after_checkpoint(job) -> None:
        nonlocal crashed
        if not crashed and job.state == crash_state:
            crashed = True
            raise InjectedCrash(job.state.value)

    first = Mover(
        {"hot": hot, "warm": warm},
        catalog,
        owner_id="worker-a",
        lease_seconds=1,
        clock=clock,
        transition_hook=crash_after_checkpoint,
    )
    with pytest.raises(InjectedCrash, match=crash_state.value):
        first.move(
            "hot",
            "warm",
            bucket,
            key,
            idempotency_key="policy-job:object-1",
        )

    interrupted = catalog.get_move_job("policy-job:object-1")
    assert interrupted is not None
    assert interrupted.state == crash_state
    placement = catalog.get(bucket, key)
    assert placement is not None
    if crash_state in {
        MoveJobState.PREPARED,
        MoveJobState.TRANSFERRED,
        MoveJobState.VERIFIED,
    }:
        assert placement.tier == "hot"
    else:
        assert placement.tier == "warm"
    catalog.close()

    # A new process can claim the durable job once the old ownership lease
    # expires. Every external effect is safe to repeat from its checkpoint.
    clock.advance(2)
    recovered_catalog = SQLiteCatalog(database)
    recovered = Mover(
        {"hot": hot, "warm": warm},
        recovered_catalog,
        owner_id="worker-b",
        lease_seconds=1,
        clock=clock,
    )
    result = recovered.move(
        "hot",
        "warm",
        bucket,
        key,
        idempotency_key="policy-job:object-1",
    )

    assert result.verified is True
    assert warm.get_object(bucket, key) == data
    with pytest.raises(FileNotFoundError):
        hot.stat_object(bucket, key)
    final_placement = recovered_catalog.get(bucket, key)
    assert final_placement is not None
    assert final_placement.tier == "warm"
    final_job = recovered.get_job("policy-job:object-1")
    assert final_job is not None
    assert final_job.state == MoveJobState.COMPLETED
    assert final_job.terminal_reason == "move completed"
    assert [item.to_state for item in recovered.get_job_transitions(final_job.idempotency_key)] == [
        MoveJobState.PREPARED,
        MoveJobState.TRANSFERRED,
        MoveJobState.VERIFIED,
        MoveJobState.COMMITTED,
        MoveJobState.CLEANUP,
        MoveJobState.COMPLETED,
    ]
    recovered_catalog.close()


def test_move_job_lease_and_idempotency_key_ownership(tmp_path: Path) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    hot.put_object("bucket", "one", b"data")
    catalog_a = SQLiteCatalog(tmp_path / "catalog.db")
    catalog_a.upsert("bucket", "one", 4, "hot")
    catalog_b = SQLiteCatalog(tmp_path / "catalog.db")
    clock = MutableClock()

    def crash_prepared(job) -> None:
        if job.state == MoveJobState.PREPARED:
            raise InjectedCrash

    with pytest.raises(InjectedCrash):
        Mover(
            {"hot": hot, "warm": warm},
            catalog_a,
            owner_id="owner-a",
            lease_seconds=10,
            clock=clock,
            transition_hook=crash_prepared,
        ).move("hot", "warm", "bucket", "one", idempotency_key="same-key")

    with pytest.raises(ValueError, match="prepared -> completed"):
        catalog_a.transition_move_job(
            "same-key",
            owner_id="owner-a",
            expected_state=MoveJobState.PREPARED,
            to_state=MoveJobState.COMPLETED,
            reason="invalid shortcut",
            now="2026-08-16T00:00:00.000000Z",
            lease_expires_at="2026-08-16T00:00:10.000000Z",
        )

    second = Mover(
        {"hot": hot, "warm": warm},
        catalog_b,
        owner_id="owner-b",
        lease_seconds=10,
        clock=clock,
    )
    with pytest.raises(MoveJobLeaseError, match="owner-a"):
        second.move("hot", "warm", "bucket", "one", idempotency_key="same-key")
    with pytest.raises(MoveJobConflictError, match="already identifies"):
        second.move("warm", "hot", "bucket", "one", idempotency_key="same-key")

    clock.advance(11)
    second.move("hot", "warm", "bucket", "one", idempotency_key="same-key")
    # Replaying the completed key is a pure read of its verified result.
    second.move("hot", "warm", "bucket", "one", idempotency_key="same-key")
    assert warm.get_object("bucket", "one") == b"data"
    assert list(hot.list_objects("bucket")) == []
    catalog_b.close()
    catalog_a.close()


def test_verification_failure_and_terminal_reason_are_queryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    data = b"retain this source"
    hot.put_object("bucket", "object", data)
    catalog = SQLiteCatalog(tmp_path / "catalog.db")
    catalog.upsert("bucket", "object", len(data), "hot")
    original_put = warm.put_object_stream

    def corrupt_destination(*args, **kwargs):
        written = original_put(*args, **kwargs)
        destination = warm.base / "bucket" / "object"
        destination.write_bytes(b"different bytes!!")
        return written

    monkeypatch.setattr(warm, "put_object_stream", corrupt_destination)
    mover = Mover({"hot": hot, "warm": warm}, catalog)
    with pytest.raises(MoveVerificationError):
        mover.move(
            "hot",
            "warm",
            "bucket",
            "object",
            idempotency_key="failed-key",
        )

    failed = mover.get_job("failed-key")
    assert failed is not None
    assert failed.state == MoveJobState.FAILED
    assert failed.terminal_reason is not None
    assert "checksum mismatch" in failed.terminal_reason
    assert mover.get_job_transitions("failed-key")[-1].reason == failed.terminal_reason
    assert hot.get_object("bucket", "object") == data
    assert catalog.get("bucket", "object").tier == "hot"  # type: ignore[union-attr]
    catalog.close()
