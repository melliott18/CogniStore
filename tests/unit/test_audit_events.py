from __future__ import annotations

import copy
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa

from cognistore.core.audit import (
    AuditContext,
    AuditEvent,
    AuditEventType,
    AuditOutcome,
    AuditQuery,
    AuditRetentionPolicy,
    audit_text_identity,
    stable_audit_event_id,
)
from cognistore.core.catalog import Catalog
from cognistore.core.move_jobs import MoveJobState
from cognistore.core.mover import Mover
from cognistore.db import SQLCatalog
from cognistore.db.schema import (
    audit_event_tombstones,
    audit_events,
    audit_move_heads,
)
from cognistore.drivers.posix_driver import PosixDriver

_BASE_TIME = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
_NO_RETENTION = AuditRetentionPolicy(None)


def _timestamp(seconds: int) -> str:
    return (
        (_BASE_TIME + timedelta(seconds=seconds))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _event(
    number: int,
    *,
    schema_version: int = 1,
    event_type: str = "move.completed",
    outcome: str = "succeeded",
    occurred_seconds: int = 0,
    expires_seconds: int | None = None,
    correlation_id: str = "request-31",
    actor_type: str = "worker",
    actor_id: str = "worker-1",
    bucket: str | None = "bucket",
    object_key: str | None = "objects/report.pdf",
    job_id: str | None = "policy-job-31",
    move_id: str | None = None,
    policy_name: str | None = "simple",
    policy_version: str | None = "simple:v1",
    details: Mapping[str, Any] | None = None,
) -> AuditEvent:
    return AuditEvent(
        event_id=str(UUID(int=number)),
        schema_version=schema_version,
        event_type=event_type,
        outcome=outcome,
        occurred_at=_timestamp(occurred_seconds),
        recorded_at=_timestamp(occurred_seconds + 1),
        expires_at=(None if expires_seconds is None else _timestamp(expires_seconds)),
        correlation_id=correlation_id,
        actor_type=actor_type,
        actor_id=actor_id,
        bucket=bucket,
        object_key=object_key,
        job_id=job_id,
        move_id=move_id,
        policy_name=policy_name,
        policy_version=policy_version,
        details={} if details is None else details,
    )


@contextmanager
def _open_catalog(
    backend: str,
    tmp_path: Path,
    *,
    retention: AuditRetentionPolicy = _NO_RETENTION,
) -> Iterator[Any]:
    if backend == "memory":
        yield Catalog(audit_retention=retention)
        return
    with SQLCatalog(
        tmp_path / "audit-events.sqlite3",
        audit_retention=retention,
    ) as catalog:
        yield catalog


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_append_get_filter_and_stable_order_are_backend_neutral(
    backend: str,
    tmp_path: Path,
) -> None:
    earliest = _event(
        4,
        occurred_seconds=0,
        event_type="policy.decision",
        outcome="selected",
    )
    tied_first = _event(2, occurred_seconds=10)
    tied_second = _event(3, occurred_seconds=10)
    unrelated = _event(
        1,
        occurred_seconds=20,
        correlation_id="other-request",
        actor_id="other-worker",
        bucket="other-bucket",
        object_key="other-object",
        job_id="other-job",
        move_id="other-move",
        policy_name=None,
        policy_version=None,
    )

    with _open_catalog(backend, tmp_path) as catalog:
        for event in (unrelated, tied_second, earliest, tied_first):
            assert catalog.append_audit_event(event) == event

        assert catalog.get_audit_event(tied_first.event_id) == tied_first
        assert catalog.get_audit_event(str(UUID(int=999))) is None
        assert catalog.list_audit_events() == [
            earliest,
            tied_first,
            tied_second,
            unrelated,
        ]

        filtered = catalog.list_audit_events(
            AuditQuery(
                correlation_id="request-31",
                job_id="policy-job-31",
                bucket="bucket",
                object_key="objects/report.pdf",
                policy_name="simple",
                policy_version="simple:v1",
                actor_type="worker",
                actor_id="worker-1",
                event_types=frozenset({"move.completed"}),
                outcomes=frozenset({"succeeded"}),
                occurred_after=_timestamp(1),
                occurred_before=_timestamp(19),
            )
        )
        assert filtered == [tied_first, tied_second]
        assert catalog.list_audit_events(AuditQuery(limit=2, ascending=False)) == [
            unrelated,
            tied_second,
        ]


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_audit_keyset_pages_preserve_timestamp_ties_during_inserts(
    backend: str,
    tmp_path: Path,
) -> None:
    events = [_event(number, occurred_seconds=number // 3) for number in range(1, 8)]
    with _open_catalog(backend, tmp_path) as catalog:
        for event in events:
            catalog.append_audit_event(event)
        first = catalog.list_audit_events(AuditQuery(limit=2, ascending=False))
        assert first == events[-2:][::-1]
        boundary = (first[-1].occurred_at, first[-1].event_id)
        catalog.append_audit_event(_event(8, occurred_seconds=3))
        catalog.append_audit_event(_event(9, occurred_seconds=2, bucket="other"))
        remaining = []
        while page := catalog.list_audit_events(AuditQuery(
            bucket="bucket", before_event=boundary, ascending=False, limit=2,
        )):
            remaining.extend(page)
            boundary = (page[-1].occurred_at, page[-1].event_id)
        assert first + remaining == events[::-1]
        # A before boundary is chronological, even for ascending reads.
        assert catalog.list_audit_events(AuditQuery(
            before_event=(events[2].occurred_at, events[2].event_id),
        )) == events[:2]


@pytest.mark.parametrize("boundary", [
    (), ("2026-09-11",), ("invalid", str(UUID(int=1))),
    (_timestamp(0), "invalid"), [_timestamp(0), str(UUID(int=1))],
])
def test_audit_keyset_boundary_rejects_malformed_values(boundary: Any) -> None:
    with pytest.raises(ValueError, match="before_event"):
        AuditQuery(before_event=boundary)


def test_audit_keyset_boundary_normalizes_timestamp_and_uuid() -> None:
    query = AuditQuery(before_event=(
        "2026-08-29T05:00:00-07:00", "00000000000000000000000000000001",
    ))
    assert query.before_event == (_timestamp(0), str(UUID(int=1)))


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_v1_and_future_additive_event_schemas_remain_readable(
    backend: str,
    tmp_path: Path,
) -> None:
    version_one = _event(10, details={"phase": "completed"})
    future_version = _event(
        11,
        schema_version=2,
        event_type="move.future_outcome",
        outcome="future-state",
        occurred_seconds=1,
        details={
            "phase": "future",
            "additive": {"explanation": "retained", "signals": [1, 2, 3]},
        },
    )

    with _open_catalog(backend, tmp_path) as catalog:
        catalog.append_audit_event(version_one)
        catalog.append_audit_event(future_version)

        assert catalog.get_audit_event(version_one.event_id) == version_one
        assert catalog.get_audit_event(future_version.event_id) == future_version
        assert catalog.list_audit_events() == [version_one, future_version]


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_same_event_id_is_idempotent_but_conflicting_content_is_rejected(
    backend: str,
    tmp_path: Path,
) -> None:
    event = _event(20, details={"attempt": 1})
    conflicting = replace(event, details={"attempt": 2})

    with _open_catalog(backend, tmp_path) as catalog:
        assert catalog.append_audit_event(event) == event
        assert catalog.append_audit_event(event) == event
        assert catalog.list_audit_events() == [event]

        with pytest.raises((ValueError, RuntimeError), match="event"):
            catalog.append_audit_event(conflicting)

        assert catalog.get_audit_event(event.event_id) == event
        assert catalog.list_audit_events() == [event]


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_catalog_retention_assigns_expiry_when_the_event_has_none(
    backend: str,
    tmp_path: Path,
) -> None:
    event = _event(30, occurred_seconds=5)

    with _open_catalog(
        backend,
        tmp_path,
        retention=AuditRetentionPolicy(60),
    ) as catalog:
        stored = catalog.append_audit_event(event)

        assert event.expires_at is None
        assert stored.expires_at == _timestamp(65)
        assert catalog.get_audit_event(event.event_id) == stored


def test_sqlite_replay_keeps_original_expiry_after_retention_change(
    tmp_path: Path,
) -> None:
    database = tmp_path / "retention-replay.sqlite3"
    event = _event(31, occurred_seconds=5)
    with SQLCatalog(database, audit_retention=AuditRetentionPolicy(60)) as catalog:
        original = catalog.append_audit_event(event)

    with SQLCatalog(database, audit_retention=AuditRetentionPolicy(120)) as catalog:
        replayed = catalog.append_audit_event(event)

    assert original.expires_at == _timestamp(65)
    assert replayed == original


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_occurred_before_pruning_is_strict_bounded_and_idempotent(
    backend: str,
    tmp_path: Path,
) -> None:
    events = [
        _event(40, occurred_seconds=0),
        _event(41, occurred_seconds=10),
        _event(42, occurred_seconds=20),
        _event(43, occurred_seconds=30),
    ]

    with _open_catalog(backend, tmp_path) as catalog:
        for event in events:
            catalog.append_audit_event(event)

        assert catalog.prune_audit_events(_timestamp(20), limit=1) == 1
        first_pass = catalog.list_audit_events()
        assert len(first_pass) == 3
        assert events[2] in first_pass
        assert events[3] in first_pass

        assert catalog.prune_audit_events(_timestamp(20), limit=1) == 1
        assert catalog.list_audit_events() == events[2:]
        assert catalog.prune_audit_events(_timestamp(20), limit=1) == 0


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_expiry_pruning_is_bounded_and_preserves_unexpired_events(
    backend: str,
    tmp_path: Path,
) -> None:
    expired_before_now = _event(50, occurred_seconds=0)
    expires_at_now = _event(51, occurred_seconds=10)
    expires_later = _event(52, occurred_seconds=20)

    with _open_catalog(
        backend,
        tmp_path,
        retention=AuditRetentionPolicy(10),
    ) as catalog:
        for event in (
            expires_later,
            expires_at_now,
            expired_before_now,
        ):
            catalog.append_audit_event(event)

        assert catalog.prune_expired_audit_events(_timestamp(20), limit=1) == 1
        assert len(catalog.list_audit_events()) == 2
        assert catalog.prune_expired_audit_events(_timestamp(20), limit=1) == 1
        remaining = catalog.list_audit_events()
        assert len(remaining) == 1
        assert remaining[0].event_id == expires_later.event_id
        assert remaining[0].expires_at == _timestamp(30)
        assert catalog.prune_expired_audit_events(_timestamp(20), limit=1) == 0


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_pruning_does_not_rewind_or_fork_the_move_causal_head(
    backend: str,
    tmp_path: Path,
) -> None:
    move_id = "retained-head-31"
    newer_occurrence = _event(
        53,
        occurred_seconds=30,
        move_id=move_id,
    )
    backdated_head = _event(
        54,
        occurred_seconds=0,
        move_id=move_id,
    )
    next_event = _event(
        55,
        occurred_seconds=40,
        move_id=move_id,
    )

    with _open_catalog(backend, tmp_path) as catalog:
        stored_newer = catalog.append_audit_event(newer_occurrence)
        stored_backdated = catalog.append_audit_event(backdated_head)
        assert stored_backdated.causation_id == stored_newer.event_id
        assert catalog.prune_audit_events(_timestamp(10)) == 1
        assert catalog.append_audit_event(backdated_head) == stored_backdated
        assert catalog.list_audit_events() == [stored_newer]

        stored_next = catalog.append_audit_event(next_event)
        assert stored_next.causation_id == stored_backdated.event_id

        if backend == "memory":
            assert catalog._audit_move_heads[move_id] == (3, stored_next.event_id)
        else:
            with catalog.engine.connect() as connection:
                head = connection.execute(
                    sa.select(audit_move_heads).where(
                        audit_move_heads.c.move_id == move_id
                    )
                ).mappings().one()
                sequence = connection.execute(
                    sa.select(audit_events.c.move_sequence).where(
                        audit_events.c.event_id == UUID(stored_next.event_id)
                    )
                ).scalar_one()
            assert (head["last_sequence"], sequence) == (3, 3)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_replaying_a_pruned_non_head_event_cannot_create_a_causal_cycle(
    backend: str,
    tmp_path: Path,
) -> None:
    move_id = "non-head-replay-31"
    first = _event(58, occurred_seconds=0, move_id=move_id)
    second = _event(59, occurred_seconds=20, move_id=move_id)
    conflicting_first = replace(first, details={"changed": True})

    with _open_catalog(backend, tmp_path) as catalog:
        stored_first = catalog.append_audit_event(first)
        stored_second = catalog.append_audit_event(second)
        assert stored_second.causation_id == stored_first.event_id
        assert catalog.prune_audit_events(_timestamp(10)) == 1

        assert catalog.append_audit_event(first) == stored_first
        with pytest.raises(ValueError, match="pruned with different data"):
            catalog.append_audit_event(conflicting_first)
        assert catalog.list_audit_events() == [stored_second]

        third = catalog.append_audit_event(
            _event(63, occurred_seconds=30, move_id=move_id)
        )
        assert third.causation_id == stored_second.event_id
        assert stored_second.causation_id == stored_first.event_id
        if backend == "memory":
            assert set(catalog._audit_event_tombstones) == {stored_first.event_id}
        else:
            with catalog.engine.connect() as connection:
                tombstone = connection.execute(
                    sa.select(audit_event_tombstones)
                ).mappings().one()
            assert str(tombstone["event_id"]) == stored_first.event_id
            assert len(tombstone["replay_digest"]) == 64


def test_sqlite_move_head_survives_pruning_and_catalog_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "retained-head-restart.sqlite3"
    first = _event(56, occurred_seconds=0, move_id="restart-head-31")
    with SQLCatalog(database, audit_retention=_NO_RETENTION) as catalog:
        stored_first = catalog.append_audit_event(first)
        assert catalog.prune_audit_events(_timestamp(1)) == 1

    second = _event(57, occurred_seconds=2, move_id="restart-head-31")
    with SQLCatalog(database, audit_retention=_NO_RETENTION) as restarted:
        stored_second = restarted.append_audit_event(second)
        assert stored_second.causation_id == stored_first.event_id
        with restarted.engine.connect() as connection:
            head = connection.execute(sa.select(audit_move_heads)).mappings().one()
        assert head["last_sequence"] == 2
        assert restarted.append_audit_event(first) == stored_first
        assert restarted.list_audit_events() == [stored_second]


def test_sqlite_retry_after_pruning_keeps_the_pruned_transition_as_its_cause(
    tmp_path: Path,
) -> None:
    database = tmp_path / "retained-retry-head.sqlite3"
    move_id = "retry-after-prune-31"
    claim = {
        "src_tier": "hot",
        "dst_tier": "warm",
        "bucket": "bucket",
        "key": "object",
        "expected_size": 1,
        "source_metadata": {"generation": "source:v1"},
    }
    with SQLCatalog(database, audit_retention=_NO_RETENTION) as catalog:
        catalog.claim_move_job(
            move_id,
            **claim,
            owner_id="worker-one",
            now=_timestamp(0),
            lease_expires_at=_timestamp(1),
        )
        prepared = catalog.list_audit_events(AuditQuery(move_id=move_id))[0]
        assert catalog.prune_audit_events(_timestamp(2)) == 1

    with SQLCatalog(database, audit_retention=_NO_RETENTION) as restarted:
        restarted.claim_move_job(
            move_id,
            **claim,
            owner_id="worker-two",
            now=_timestamp(3),
            lease_expires_at=_timestamp(60),
        )
        retry = restarted.list_audit_events(AuditQuery(move_id=move_id))[0]
        assert retry.event_type == AuditEventType.MOVE_RETRY.value
        assert retry.causation_id == prepared.event_id
        with restarted.engine.connect() as connection:
            head = connection.execute(sa.select(audit_move_heads)).mappings().one()
        assert head["last_sequence"] == 2


def test_sqlite_redacts_secrets_before_raw_persistence_without_mutating_input(
    tmp_path: Path,
) -> None:
    private_key = "-----BEGIN PRIVATE KEY-----\nsensitive-key-material\n-----END PRIVATE KEY-----"
    details = {
        "profile": "production",
        "idempotency_key": "move-31",
        "password": "open sesame",
        "nested": {
            "clientSecret": "nested-secret",
            "safe": ["keep", {"refresh_token": "refresh-me"}],
        },
        "endpoint": ("https://alice:p%40ss@example.test/api?profile=prod&access_token=http-secret"),
        "diagnostic": ("Authorization: Bearer abc.def.ghi; password=diagnostic-secret"),
        "private_key": private_key,
        "nul": "before\0after",
        "https://key-user:key-secret@example.test/private": "safe-value",
    }
    caller_snapshot = copy.deepcopy(details)
    event = _event(
        60,
        correlation_id="password=correlation-secret",
        actor_id="https://actor:actor-secret@example.test",
        job_id="token=job-secret",
        move_id="secret=move-secret",
        details=details,
    )
    event_snapshot = copy.deepcopy(event.details)

    with SQLCatalog(
        tmp_path / "redaction.sqlite3",
        audit_retention=_NO_RETENTION,
    ) as catalog:
        stored = catalog.append_audit_event(event)
        with catalog.engine.connect() as connection:
            raw_row = (
                connection.execute(
                    sa.select(audit_events).where(audit_events.c.event_id == UUID(event.event_id))
                )
                .mappings()
                .one()
            )
            raw_details = raw_row["details"]
        query_result = catalog.list_audit_events(
            AuditQuery(
                correlation_id=event.correlation_id,
                job_id=event.job_id,
                move_id=event.move_id,
            )
        )

    assert details == caller_snapshot
    assert event.details == event_snapshot
    assert raw_details == stored.details
    rendered = json.dumps(raw_details, sort_keys=True)
    for secret in (
        "open sesame",
        "nested-secret",
        "refresh-me",
        "alice",
        "p%40ss",
        "http-secret",
        "abc.def.ghi",
        "diagnostic-secret",
        "sensitive-key-material",
        "correlation-secret",
        "actor-secret",
        "job-secret",
        "move-secret",
        "key-secret",
        "before\0after",
    ):
        assert secret not in rendered
        assert secret not in str(dict(raw_row))
    assert raw_details["profile"] == "production"
    assert raw_details["idempotency_key"] == "move-31"
    assert rendered.count("[REDACTED]") >= 6
    assert query_result == [stored]


def test_in_memory_results_are_defensive_copies() -> None:
    catalog = Catalog(audit_retention=_NO_RETENTION)
    event = _event(70, details={"nested": {"attempt": 1}})

    stored = catalog.append_audit_event(event)
    stored.details["nested"]["attempt"] = 99  # type: ignore[index]
    fetched = catalog.get_audit_event(event.event_id)
    assert fetched is not None
    assert fetched.details == {"nested": {"attempt": 1}}

    fetched.details["nested"]["attempt"] = 42  # type: ignore[index]
    assert catalog.list_audit_events()[0].details == {"nested": {"attempt": 1}}


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_move_creation_rolls_back_when_atomic_audit_append_conflicts(
    backend: str,
    tmp_path: Path,
) -> None:
    move_id = "atomic-move-31"
    event_id = stable_audit_event_id("move-transition", move_id, "1")
    conflicting = AuditEvent.create(
        AuditEventType.MANUAL_ACTION,
        AuditOutcome.REQUESTED,
        AuditContext(
            correlation_id="conflicting-request",
            actor_type="operator",
            actor_id="test",
        ),
        event_id=event_id,
        occurred_at=_timestamp(0),
        recorded_at=_timestamp(0),
        retention=_NO_RETENTION,
        details={"operation": "preseed-conflict"},
    )

    with _open_catalog(backend, tmp_path) as catalog:
        catalog.append_audit_event(conflicting)
        with pytest.raises(ValueError, match="already exists with different data"):
            catalog.claim_move_job(
                move_id,
                src_tier="hot",
                dst_tier="warm",
                bucket="bucket",
                key="object",
                expected_size=1,
                source_metadata={"generation": "source:v1"},
                owner_id="worker",
                now=_timestamp(0),
                lease_expires_at=_timestamp(60),
            )

        assert catalog.get_move_job(move_id) is None
        assert catalog.list_move_job_transitions(move_id) == []
        assert catalog.list_audit_events() == [conflicting]


def test_fixed_clock_move_chain_follows_transition_sequence(tmp_path: Path) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    data = b"same timestamp chain"
    hot.put_object("bucket", "object", data)
    catalog = Catalog(audit_retention=_NO_RETENTION)
    catalog.upsert("bucket", "object", len(data), "hot")
    move_id = "fixed-clock-move-31"
    decision = catalog.append_audit_event(
        AuditEvent.create(
            AuditEventType.POLICY_DECISION,
            AuditOutcome.SELECTED,
            AuditContext(
                correlation_id="fixed-clock-policy-31",
                actor_type="worker",
                actor_id="job-31",
                job_id="job-31",
            ),
            occurred_at=_timestamp(0),
            recorded_at=_timestamp(0),
            retention=_NO_RETENTION,
            bucket="bucket",
            object_key="object",
            move_id=move_id,
            policy_name="simple",
            policy_version="1",
        )
    )
    context = AuditContext(
        correlation_id=decision.correlation_id,
        causation_id=decision.event_id,
        actor_type=decision.actor_type,
        actor_id=decision.actor_id,
        job_id=decision.job_id,
    )

    Mover(
        {"hot": hot, "warm": warm},
        catalog,
        clock=lambda: _BASE_TIME,
        audit_context=context,
    ).move("hot", "warm", "bucket", "object", idempotency_key=move_id)

    move_events = sorted(
        (
            event
            for event in catalog.list_audit_events(AuditQuery(move_id=move_id))
            if event.event_type.startswith("move.")
        ),
        key=lambda event: int(event.details["transition_sequence"]),
    )
    assert move_events[0].causation_id == decision.event_id
    assert [event.causation_id for event in move_events[1:]] == [
        event.event_id for event in move_events[:-1]
    ]
    assert move_events[-1].event_type == AuditEventType.MOVE_COMPLETED.value


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_retries_and_terminal_transition_advance_one_linear_move_head(
    backend: str,
    tmp_path: Path,
) -> None:
    move_id = "linear-retry-31"
    with _open_catalog(backend, tmp_path) as catalog:
        manual = catalog.append_audit_event(
            AuditEvent.create(
                AuditEventType.MANUAL_ACTION,
                AuditOutcome.REQUESTED,
                AuditContext(
                    correlation_id=move_id,
                    actor_type="operator",
                    actor_id="test",
                ),
                event_id=str(UUID(int=90)),
                occurred_at=_timestamp(0),
                recorded_at=_timestamp(0),
                retention=_NO_RETENTION,
                bucket="bucket",
                object_key="object",
                move_id=move_id,
            )
        )
        context = AuditContext(
            correlation_id=move_id,
            actor_type="operator",
            actor_id="test",
            causation_id=manual.event_id,
        )
        claim_arguments = {
            "src_tier": "hot",
            "dst_tier": "warm",
            "bucket": "bucket",
            "key": "object",
            "expected_size": 1,
            "source_metadata": {"generation": "source:v1"},
            "owner_id": "worker",
            "now": _timestamp(1),
            "lease_expires_at": _timestamp(60),
            "audit_context": context,
        }
        catalog.claim_move_job(move_id, **claim_arguments)
        resume_manual = catalog.append_audit_event(
            AuditEvent.create(
                AuditEventType.MANUAL_ACTION,
                AuditOutcome.REQUESTED,
                AuditContext(
                    correlation_id=move_id,
                    actor_type="operator",
                    actor_id="test",
                ),
                event_id=str(UUID(int=93)),
                occurred_at=_timestamp(1),
                recorded_at=_timestamp(1),
                retention=_NO_RETENTION,
                bucket="bucket",
                object_key="object",
                move_id=move_id,
                details={"operation": "move-resume"},
            )
        )
        claim_arguments["audit_context"] = AuditContext(
            correlation_id=move_id,
            actor_type="operator",
            actor_id="test",
            causation_id=resume_manual.event_id,
        )
        catalog.claim_move_job(move_id, **claim_arguments)
        catalog.claim_move_job(move_id, **claim_arguments)
        catalog.transition_move_job(
            move_id,
            owner_id="worker",
            expected_state=MoveJobState.PREPARED,
            to_state=MoveJobState.FAILED,
            reason="terminal failure",
            now=_timestamp(1),
            lease_expires_at=_timestamp(60),
            updates={"terminal_reason": "terminal failure"},
            audit_context=context,
        )

        events = catalog.list_audit_events(AuditQuery(move_id=move_id))
        by_cause = {event.causation_id: event for event in events[1:]}
        chain = [manual]
        while chain[-1].event_id in by_cause:
            chain.append(by_cause[chain[-1].event_id])

    assert [event.event_type for event in chain] == [
        AuditEventType.MANUAL_ACTION.value,
        AuditEventType.MOVE_PREPARED.value,
        AuditEventType.MANUAL_ACTION.value,
        AuditEventType.MOVE_RETRY.value,
        AuditEventType.MOVE_RETRY.value,
        AuditEventType.MOVE_FAILED.value,
    ]
    assert len({event.event_id for event in chain}) == len(chain)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_sensitive_identifier_pseudonyms_remain_distinct_and_queryable(
    backend: str,
    tmp_path: Path,
) -> None:
    first = _event(
        91,
        correlation_id="password=one",
        job_id="token=one",
        move_id="secret=one",
    )
    second = replace(
        first,
        event_id=str(UUID(int=92)),
        correlation_id="password=two",
        job_id="token=two",
        move_id="secret=two",
    )
    with _open_catalog(backend, tmp_path) as catalog:
        stored_first = catalog.append_audit_event(first)
        stored_second = catalog.append_audit_event(second)
        assert catalog.list_audit_events(
            AuditQuery(correlation_id=first.correlation_id)
        ) == [stored_first]
        assert catalog.list_audit_events(
            AuditQuery(correlation_id=second.correlation_id)
        ) == [stored_second]

    assert stored_first.correlation_id != stored_second.correlation_id
    assert "one" not in stored_first.correlation_id
    assert "two" not in stored_second.correlation_id


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_reserved_pseudonym_text_cannot_alias_a_sensitive_move_identity(
    backend: str,
    tmp_path: Path,
) -> None:
    sensitive_move_id = "password=one"
    reserved_alias = audit_text_identity(sensitive_move_id)
    with pytest.raises(ValueError, match="reserved pseudonym namespace"):
        audit_text_identity(reserved_alias)

    with _open_catalog(backend, tmp_path) as catalog:
        first = catalog.claim_move_job(
            sensitive_move_id,
            src_tier="hot",
            dst_tier="warm",
            bucket="bucket",
            key="object",
            expected_size=1,
            source_metadata={"generation": "source:v1"},
            owner_id="worker",
            now=_timestamp(0),
            lease_expires_at=_timestamp(60),
        )
        stored = catalog.list_audit_events(
            AuditQuery(move_id=sensitive_move_id)
        )[0]
        assert catalog.append_audit_event(stored) == stored

        with pytest.raises(ValueError, match="reserved pseudonym namespace"):
            catalog.claim_move_job(
                reserved_alias,
                src_tier="hot",
                dst_tier="warm",
                bucket="bucket",
                key="object",
                expected_size=1,
                source_metadata={"generation": "source:v1"},
                owner_id="worker",
                now=_timestamp(0),
                lease_expires_at=_timestamp(60),
            )

        assert catalog.get_move_job(first.idempotency_key) == first
        assert catalog.get_move_job(reserved_alias) is None
        assert len(catalog.list_audit_events()) == 1
        with pytest.raises(ValueError, match="reserved pseudonym namespace"):
            AuditQuery(move_id=reserved_alias)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_move_failure_diagnostics_are_redacted_in_journal_and_audit(
    backend: str,
    tmp_path: Path,
) -> None:
    move_id = "redacted-journal-31"
    with _open_catalog(backend, tmp_path) as catalog:
        catalog.claim_move_job(
            move_id,
            src_tier="hot",
            dst_tier="warm",
            bucket="bucket",
            key="object",
            expected_size=1,
            source_metadata={"generation": "source:v1"},
            owner_id="worker",
            now=_timestamp(0),
            lease_expires_at=_timestamp(60),
        )
        catalog.transition_move_job(
            move_id,
            owner_id="worker",
            expected_state=MoveJobState.PREPARED,
            to_state=MoveJobState.FAILED,
            reason="password=hunter2",
            now=_timestamp(1),
            lease_expires_at=_timestamp(60),
            updates={
                "terminal_reason": "password=hunter2",
                "verification_details": ("token=opaque-secret",),
            },
        )
        job = catalog.get_move_job(move_id)
        transitions = catalog.list_move_job_transitions(move_id)
        events = catalog.list_audit_events(AuditQuery(move_id=move_id))

    assert job is not None
    persisted = json.dumps(
        {
            "terminal_reason": job.terminal_reason,
            "verification_details": job.verification_details,
            "transition_reason": transitions[-1].reason,
            "audit": [dict(event.details) for event in events],
        },
        sort_keys=True,
    )
    assert "hunter2" not in persisted
    assert "opaque-secret" not in persisted


@pytest.mark.parametrize("field", ["event_type", "outcome", "actor_type"])
def test_postgres_text_vocabulary_rejects_nul_before_persistence(field: str) -> None:
    values = {field: "invalid\0value"}
    with pytest.raises(ValueError, match="cannot contain NUL"):
        replace(_event(80), **values)


@pytest.mark.parametrize("field", ["event_type", "outcome", "actor_type"])
def test_vocabulary_fields_reject_secret_bearing_free_text(field: str) -> None:
    values = {field: "password=hunter2"}
    with pytest.raises(ValueError, match="vocabulary token"):
        replace(_event(81), **values)


def test_query_vocabulary_rejects_nul_before_backend_access() -> None:
    with pytest.raises(ValueError, match="cannot contain NUL"):
        AuditQuery(event_types=frozenset({"move.completed\0invalid"}))
    with pytest.raises(ValueError, match="cannot contain NUL"):
        AuditQuery(outcomes=frozenset({"succeeded\0invalid"}))
    with pytest.raises(ValueError, match="cannot contain NUL"):
        AuditQuery(actor_type="worker\0invalid")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_id", ""),
        ("occurred_at", ""),
        ("recorded_at", ""),
        ("details", []),
    ],
)
def test_event_factory_does_not_treat_invalid_explicit_values_as_defaults(
    field: str,
    value: object,
) -> None:
    values = {field: value}
    with pytest.raises(ValueError):
        AuditEvent.create(
            AuditEventType.MANUAL_ACTION,
            AuditOutcome.REQUESTED,
            AuditContext(
                correlation_id="request-31",
                actor_type="operator",
                actor_id="test",
            ),
            **values,
        )
