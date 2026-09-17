"""Offline qualification of credential rotation across durable move checkpoints."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cognistore.core.audit import AuditQuery
from cognistore.core.move_jobs import MoveJob, MoveJobState
from cognistore.core.mover import Mover
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.drivers.rotating import RotatingStorageDriver
from cognistore.jobs.retry import classify_job_error
from cognistore.secrets import (
    SecretReference,
    SecretResolver,
    SecretUnavailableError,
    SecretValue,
)

_OLD_CREDENTIAL = "rotation-recovery-old-credential-sentinel"
_NEW_CREDENTIAL = "rotation-recovery-new-credential-sentinel"
_PROVIDER_DIAGNOSTIC = "rotation-recovery-bootstrap-token-sentinel"


class _Clock:
    def __init__(self) -> None:
        self.elapsed = 0.0

    def monotonic(self) -> float:
        return self.elapsed

    def wall_time(self) -> datetime:
        return datetime(2026, 9, 17, tzinfo=timezone.utc) + timedelta(seconds=self.elapsed)


class _RotatingProvider:
    def __init__(self) -> None:
        self.available = True
        self.credential = _OLD_CREDENTIAL
        self.calls = 0

    def fetch(self, reference: SecretReference) -> SecretValue:
        assert reference.name == "storage/rotation-test"
        self.calls += 1
        if not self.available:
            # A provider may include sensitive details in its raw exception.
            # The resolver must discard them before the mover sees the error.
            raise RuntimeError(f"provider request failed: {_PROVIDER_DIAGNOSTIC}")
        return SecretValue(json.dumps({"credential": self.credential}))


def _assert_persisted_records_have_no_credentials(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        persisted = "\n".join(connection.iterdump())
    for sentinel in (_OLD_CREDENTIAL, _NEW_CREDENTIAL, _PROVIDER_DIAGNOSTIC):
        assert sentinel not in persisted


@pytest.mark.parametrize("checkpoint", [MoveJobState.TRANSFERRED, MoveJobState.CLEANUP])
def test_move_recovers_same_durable_job_after_secret_outage_and_rotation(
    tmp_path: Path, checkpoint: MoveJobState,
) -> None:
    clock = _Clock()
    provider = _RotatingProvider()
    resolver = SecretResolver(
        {"test": provider}, cache_ttl_seconds=1, clock=clock.monotonic,
    )
    constructed_credentials: list[str] = []
    destination_root = tmp_path / "warm"

    def credentialed_backend(*, credential: str) -> PosixDriver:
        assert credential == provider.credential
        constructed_credentials.append(credential)
        return PosixDriver(str(destination_root))

    warm = RotatingStorageDriver(
        credentialed_backend,
        {},
        {"credential": SecretReference("test", "storage/rotation-test", field="credential")},
        resolver,
    )
    hot = PosixDriver(str(tmp_path / "hot"))
    destination = PosixDriver(str(destination_root))
    bucket, key = "rotation-test", "durable/object.bin"
    data = b"retain the durable move and both object generations across credential rotation"
    move_id = f"rotation-recovery:{checkpoint.value}"
    database = tmp_path / "catalog.sqlite"
    catalog = SQLiteCatalog(database)
    hot.put_object(bucket, key, data)
    catalog.upsert(bucket, key, len(data), "hot")

    def expire_at_checkpoint(job: MoveJob) -> None:
        if job.state == checkpoint:
            clock.elapsed += 2
            provider.available = False

    mover = Mover(
        {"hot": hot, "warm": warm}, catalog,
        owner_id="before-rotation", lease_seconds=1, clock=clock.wall_time,
        transition_hook=expire_at_checkpoint,
    )
    try:
        with pytest.raises(SecretUnavailableError) as failure:
            mover.move("hot", "warm", bucket, key, idempotency_key=move_id)
        assert classify_job_error(failure.value).retryable
        assert _PROVIDER_DIAGNOSTIC not in str(failure.value)
        assert failure.value.__context__ is None
        assert constructed_credentials == [_OLD_CREDENTIAL]
        assert provider.calls == 2

        interrupted = catalog.get_move_job(move_id)
        assert interrupted is not None
        assert interrupted.idempotency_key == move_id
        assert interrupted.state == checkpoint
        assert interrupted.terminal_reason is None
        transitions_before = catalog.list_move_job_transitions(move_id)
        assert transitions_before[-1].to_state == checkpoint
        placement = catalog.get(bucket, key)
        assert placement is not None
        assert placement.tier == ("hot" if checkpoint == MoveJobState.TRANSFERRED else "warm")
        assert hot.get_object(bucket, key) == data
        assert destination.get_object(bucket, key) == data
        published_generation = destination.object_generation(bucket, key)
    finally:
        catalog.close()

    _assert_persisted_records_have_no_credentials(database)
    provider.credential = _NEW_CREDENTIAL
    provider.available = True
    # Reopen the durable catalog with a different worker owner. The same stable
    # driver handle refreshes its expired credentials without an image rebuild.
    recovered_catalog = SQLiteCatalog(database)
    try:
        persisted = recovered_catalog.get_move_job(move_id)
        assert persisted is not None
        assert persisted.state == checkpoint
        recovered = Mover(
            {"hot": hot, "warm": warm}, recovered_catalog,
            owner_id="after-rotation", lease_seconds=1, clock=clock.wall_time,
        )
        result = recovered.move("hot", "warm", bucket, key, idempotency_key=move_id)
        assert result.verified
        assert constructed_credentials == [_OLD_CREDENTIAL, _NEW_CREDENTIAL]
        assert provider.calls == 3
        final_job = recovered_catalog.get_move_job(move_id)
        assert final_job is not None
        assert final_job.idempotency_key == move_id
        assert final_job.created_at == interrupted.created_at
        assert final_job.state == MoveJobState.COMPLETED
        final_transitions = recovered_catalog.list_move_job_transitions(move_id)
        assert final_transitions[:len(transitions_before)] == transitions_before
        assert [transition.to_state for transition in final_transitions] == [
            MoveJobState.PREPARED, MoveJobState.TRANSFERRED, MoveJobState.VERIFIED,
            MoveJobState.COMMITTED, MoveJobState.CLEANUP, MoveJobState.COMPLETED,
        ]
        placement = recovered_catalog.get(bucket, key)
        assert placement is not None
        assert placement.tier == "warm"
        assert destination.get_object(bucket, key) == data
        assert destination.object_generation(bucket, key) == published_generation
        with pytest.raises(FileNotFoundError):
            hot.stat_object(bucket, key)
        events = recovered_catalog.list_audit_events(AuditQuery(move_id=move_id))
        assert events
        assert any(event.event_type == "move.completed" for event in events)
        for sentinel in (_OLD_CREDENTIAL, _NEW_CREDENTIAL, _PROVIDER_DIAGNOSTIC):
            assert sentinel not in repr(events)
    finally:
        recovered_catalog.close()
        warm.close()
        resolver.close()

    _assert_persisted_records_have_no_credentials(database)
