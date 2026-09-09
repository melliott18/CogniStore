from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from cognistore.jobs.models import (
    JOB_SCHEMA_VERSION_V1,
    JOB_SCHEMA_VERSION_V2,
    JOB_SCHEMA_VERSION_V3,
    REDRIVE_COUNT_METADATA,
    DeadLetterDisposition,
    EnqueueReceipt,
    InvalidJobError,
    JobContext,
    JobEnvelope,
)
from cognistore.jobs.scheduler import (
    SCHEDULE_ID_METADATA,
    SCHEDULED_FOR_METADATA,
    PeriodicScheduler,
    ScheduledJobDefinition,
    ScheduledRunCoordinator,
    ScheduledRunRecoveryError,
    ScheduleRegistry,
    SQLiteScheduleStore,
    default_schedule_registry,
    load_schedule_config,
)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class RecordingQueue:
    def __init__(
        self,
        *,
        failures: int = 0,
        fail_schedule_ids: set[str] | None = None,
    ) -> None:
        self.failures = failures
        self.fail_schedule_ids = fail_schedule_ids or set()
        self.connected = 0
        self.closed = 0
        self.attempted: list[tuple[JobEnvelope, str | None]] = []
        self.enqueued: list[JobEnvelope] = []

    async def connect(self) -> None:
        self.connected += 1

    async def enqueue(
        self, job: JobEnvelope, *, message_id: str | None = None
    ) -> EnqueueReceipt:
        self.attempted.append((job, message_id))
        if (
            self.failures
            or job.metadata.get(SCHEDULE_ID_METADATA) in self.fail_schedule_ids
        ):
            self.failures = max(0, self.failures - 1)
            raise ConnectionError("injected publication failure")
        self.enqueued.append(job)
        return EnqueueReceipt(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            stream="TEST_JOBS",
            sequence=len(self.enqueued),
        )

    async def close(self, *, graceful: bool = True) -> None:
        self.closed += 1


def _write_schedules(path: Path, jobs: str) -> None:
    path.write_text(f"jobs:\n{jobs}", encoding="utf-8")


def _scan_job(
    schedule_id: str,
    *,
    interval: float | str = 60,
    enabled: bool = True,
    tier: str = "hot",
    bucket: str = "demo-bucket",
    prefix: str = "reports/",
) -> str:
    enabled_yaml = "true" if enabled else "false"
    return (
        f"  {schedule_id}:\n"
        "    type: catalog.scan\n"
        f"    enabled: {enabled_yaml}\n"
        f"    interval_seconds: {interval}\n"
        "    payload:\n"
        f"      tier: {tier}\n"
        f"      bucket: {bucket}\n"
        f"      prefix: {prefix}\n"
    )


def _policy_job(
    schedule_id: str,
    *,
    interval: float = 300,
    enabled: bool = True,
) -> str:
    enabled_yaml = "true" if enabled else "false"
    return (
        f"  {schedule_id}:\n"
        "    type: policy.run\n"
        f"    enabled: {enabled_yaml}\n"
        f"    interval_seconds: {interval}\n"
        "    payload:\n"
        "      bucket: demo-bucket\n"
        "      prefix: reports/\n"
        "      policy: simple\n"
        "      threshold: 1048576\n"
        "      llm_threshold: null\n"
        "      allowed_tiers: [hot, warm]\n"
        "      hot_name_patterns: []\n"
        "      warm_name_patterns: []\n"
        "      cold_name_patterns: []\n"
        "      hot_mime_prefixes: []\n"
        "      warm_mime_prefixes: []\n"
        "      cold_mime_prefixes: []\n"
    )


def _embedding_policy_job(
    schedule_id: str,
    *,
    policy: str = "content",
    rules: str,
) -> str:
    return (
        _policy_job(schedule_id).replace("policy: simple", f"policy: {policy}")
        + "      embedding_rules:\n"
        + rules
    )


def _context(*, attempt: int = 1, redrive_count: int = 0) -> JobContext:
    return JobContext(
        attempt=attempt,
        redelivered=attempt > 1,
        stream_sequence=attempt,
        consumer_sequence=attempt,
        shutdown_requested=asyncio.Event(),
        redrive_count=redrive_count,
    )


def _load(path: Path):
    return load_schedule_config(path, known_tiers=("hot", "warm"))


async def _succeed_scheduled_job(
    store: SQLiteScheduleStore,
    job: JobEnvelope,
    clock: MutableClock,
) -> None:
    execution = await ScheduledRunCoordinator(
        store,
        clock=clock,
        lease_seconds=30,
    ).begin(job, _context())
    assert execution is not None and execution.execute is True
    await execution.succeed()


def test_due_catalog_scan_and_policy_pass_are_only_enqueued(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_schedules(
        config_path,
        _scan_job("scan-reports") + _policy_job("place-reports"),
    )
    schedules = _load(config_path)
    queue = RecordingQueue()
    clock = MutableClock()

    async def scenario() -> None:
        store = SQLiteScheduleStore(tmp_path / "scheduler.db")
        scheduler = PeriodicScheduler(
            queue,
            store,
            schedules,
            clock=clock,
            publication_lease_seconds=30,
        )
        await scheduler.start()
        assert await scheduler.run_due() == 2
        assert await scheduler.run_due() == 0
        await scheduler.close()
        store.close()

    asyncio.run(scenario())

    assert queue.connected == 1
    assert queue.closed == 1
    assert [job.job_type for job in queue.enqueued] == ["catalog.scan", "policy.run"]
    scan, policy = queue.enqueued
    assert scan.schema_version == JOB_SCHEMA_VERSION_V1
    assert policy.schema_version == JOB_SCHEMA_VERSION_V1
    assert scan.payload == {
        "tier": "hot",
        "bucket": "demo-bucket",
        "prefix": "reports/",
    }
    assert policy.payload["bucket"] == "demo-bucket"
    assert policy.payload["allowed_tiers"] == ["hot", "warm"]
    assert policy.payload["threshold"] == 1048576
    assert scan.created_at == "2026-08-23T12:00:00Z"
    assert scan.metadata == {
        "cognistore.schedule_id": "scan-reports",
        "cognistore.schedule_scope": schedules[0].scope,
        "cognistore.scheduled_for": "2026-08-23T12:00:00.000000Z",
    }
    assert policy.metadata["cognistore.schedule_id"] == "place-reports"
    assert scan.job_id != policy.job_id


def test_intervals_are_independent_and_disabled_jobs_never_enqueue(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_schedules(
        config_path,
        _scan_job("scan-reports", interval=60)
        + _policy_job("place-reports", interval=90)
        + _scan_job(
            "disabled-scan",
            interval=5,
            enabled=False,
            bucket="disabled",
        ),
    )
    queue = RecordingQueue()
    clock = MutableClock()

    async def scenario() -> None:
        store = SQLiteScheduleStore(tmp_path / "scheduler.db")
        scheduler = PeriodicScheduler(
            queue,
            store,
            _load(config_path),
            clock=clock,
            publication_lease_seconds=30,
        )
        await scheduler.start()
        assert await scheduler.run_due() == 2
        for job in queue.enqueued:
            await _succeed_scheduled_job(store, job, clock)

        clock.advance(59)
        assert await scheduler.run_due() == 0
        clock.advance(1)
        assert await scheduler.run_due() == 1
        await _succeed_scheduled_job(store, queue.enqueued[-1], clock)

        clock.advance(29)
        assert await scheduler.run_due() == 0
        clock.advance(1)
        assert await scheduler.run_due() == 1
        await _succeed_scheduled_job(store, queue.enqueued[-1], clock)

        clock.advance(30)
        assert await scheduler.run_due() == 1
        await _succeed_scheduled_job(store, queue.enqueued[-1], clock)
        await scheduler.close()
        store.close()

    asyncio.run(scenario())

    assert [
        job.metadata[SCHEDULE_ID_METADATA] for job in queue.enqueued
    ] == [
        "scan-reports",
        "place-reports",
        "scan-reports",
        "place-reports",
        "scan-reports",
    ]
    assert len({job.job_id for job in queue.enqueued}) == 5


def test_slow_publication_does_not_backdate_later_schedule_reservations(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_schedules(
        config_path,
        _scan_job("first-scan", interval=60, bucket="first")
        + _scan_job("second-scan", interval=60, bucket="second"),
    )
    clock = MutableClock()

    class AdvancingQueue(RecordingQueue):
        async def enqueue(
            self, job: JobEnvelope, *, message_id: str | None = None
        ) -> EnqueueReceipt:
            receipt = await super().enqueue(job, message_id=message_id)
            if len(self.enqueued) == 1:
                clock.advance(120)
            return receipt

    queue = AdvancingQueue()

    async def scenario() -> None:
        store = SQLiteScheduleStore(tmp_path / "scheduler.db")
        scheduler = PeriodicScheduler(
            queue,
            store,
            _load(config_path),
            clock=clock,
            publication_lease_seconds=30,
        )
        await scheduler.start()
        assert await scheduler.run_due() == 2
        assert queue.enqueued[0].created_at == "2026-08-23T12:00:00Z"
        assert queue.enqueued[1].created_at == "2026-08-23T12:02:00Z"

        for job in queue.enqueued:
            await _succeed_scheduled_job(store, job, clock)

        # Only the first schedule is overdue: the second interval started when
        # its reservation was actually made after the slow publication.
        assert await scheduler.run_due() == 1
        assert queue.enqueued[-1].payload["bucket"] == "first"
        await _succeed_scheduled_job(store, queue.enqueued[-1], clock)
        await scheduler.close()
        store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("old_interval", "new_interval"),
    ((3600, 60), (60, 3600)),
)
def test_interval_change_reschedules_from_the_last_reservation(
    tmp_path: Path,
    old_interval: int,
    new_interval: int,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    database = tmp_path / "scheduler.db"
    clock = MutableClock()
    queue = RecordingQueue()
    _write_schedules(
        config_path,
        _scan_job("scan-reports", interval=old_interval),
    )

    async def scenario() -> None:
        first_store = SQLiteScheduleStore(database)
        first_scheduler = PeriodicScheduler(
            queue,
            first_store,
            _load(config_path),
            clock=clock,
            publication_lease_seconds=30,
        )
        await first_scheduler.start()
        assert await first_scheduler.run_due() == 1
        await _succeed_scheduled_job(first_store, queue.enqueued[-1], clock)
        await first_scheduler.close()
        first_store.close()

        clock.advance(10)
        _write_schedules(
            config_path,
            _scan_job("scan-reports", interval=new_interval),
        )
        second_store = SQLiteScheduleStore(database)
        second_scheduler = PeriodicScheduler(
            queue,
            second_store,
            _load(config_path),
            clock=clock,
            publication_lease_seconds=30,
        )
        await second_scheduler.start()
        assert await second_scheduler.run_due() == 0

        clock.advance(new_interval - 11)
        assert await second_scheduler.run_due() == 0
        clock.advance(1)
        assert await second_scheduler.run_due() == 1
        await _succeed_scheduled_job(second_store, queue.enqueued[-1], clock)
        await second_scheduler.close()
        second_store.close()

    asyncio.run(scenario())


def test_lengthened_interval_reanchors_an_overdue_normal_deadline(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    database = tmp_path / "scheduler.db"
    clock = MutableClock()
    queue = RecordingQueue()
    _write_schedules(config_path, _scan_job("scan-reports", interval=60))

    async def scenario() -> None:
        first_store = SQLiteScheduleStore(database)
        first_scheduler = PeriodicScheduler(
            queue,
            first_store,
            _load(config_path),
            clock=clock,
        )
        await first_scheduler.start()
        assert await first_scheduler.run_due() == 1
        await _succeed_scheduled_job(first_store, queue.enqueued[-1], clock)
        await first_scheduler.close()
        first_store.close()

        clock.advance(100)
        _write_schedules(config_path, _scan_job("scan-reports", interval=3600))
        second_store = SQLiteScheduleStore(database)
        second_scheduler = PeriodicScheduler(
            queue,
            second_store,
            _load(config_path),
            clock=clock,
        )
        await second_scheduler.start()
        assert await second_scheduler.run_due() == 0

        clock.advance(3_499)
        assert await second_scheduler.run_due() == 0
        clock.advance(1)
        assert await second_scheduler.run_due() == 1
        await _succeed_scheduled_job(second_store, queue.enqueued[-1], clock)
        await second_scheduler.close()
        second_store.close()

    asyncio.run(scenario())


def test_reenabled_schedule_is_immediately_due_after_disabled_restart(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    database = tmp_path / "scheduler.db"
    clock = MutableClock()

    async def run_enabled_once() -> None:
        _write_schedules(config_path, _scan_job("scan-reports", interval=60))
        store = SQLiteScheduleStore(database)
        queue = RecordingQueue()
        scheduler = PeriodicScheduler(queue, store, _load(config_path), clock=clock)
        try:
            await scheduler.start()
            assert await scheduler.run_due() == 1
            await _succeed_scheduled_job(store, queue.enqueued[0], clock)
        finally:
            await scheduler.close()
            store.close()

    async def restart_disabled() -> None:
        _write_schedules(
            config_path,
            _scan_job("scan-reports", interval=60, enabled=False),
        )
        store = SQLiteScheduleStore(database)
        scheduler = PeriodicScheduler(
            RecordingQueue(), store, _load(config_path), clock=clock
        )
        try:
            await scheduler.start()
            assert await scheduler.run_due() == 0
        finally:
            await scheduler.close()
            store.close()

    async def restart_reenabled() -> JobEnvelope:
        _write_schedules(config_path, _scan_job("scan-reports", interval=60))
        store = SQLiteScheduleStore(database)
        queue = RecordingQueue()
        scheduler = PeriodicScheduler(queue, store, _load(config_path), clock=clock)
        try:
            await scheduler.start()
            assert await scheduler.run_due() == 1
            return queue.enqueued[0]
        finally:
            await scheduler.close()
            store.close()

    async def scenario() -> None:
        await run_enabled_once()
        clock.advance(10)
        await restart_disabled()
        clock.advance(3_600)
        reenabled = await restart_reenabled()
        assert reenabled.metadata[SCHEDULED_FOR_METADATA] == (
            "2026-08-23T13:00:10.000000Z"
        )

    asyncio.run(scenario())


def test_interval_change_preserves_reenabled_due_marker_behind_active_run(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    database = tmp_path / "scheduler.db"
    clock = MutableClock()
    initial_queue = RecordingQueue()

    async def scenario() -> None:
        _write_schedules(config_path, _scan_job("scan-reports", interval=60))
        initial_store = SQLiteScheduleStore(database)
        initial_scheduler = PeriodicScheduler(
            initial_queue,
            initial_store,
            _load(config_path),
            clock=clock,
        )
        await initial_scheduler.start()
        assert await initial_scheduler.run_due() == 1
        original = initial_queue.enqueued[0]
        await initial_scheduler.close()
        initial_store.close()

        clock.advance(10)
        _write_schedules(
            config_path,
            _scan_job("scan-reports", interval=60, enabled=False),
        )
        disabled_store = SQLiteScheduleStore(database)
        disabled_scheduler = PeriodicScheduler(
            RecordingQueue(),
            disabled_store,
            _load(config_path),
            clock=clock,
        )
        await disabled_scheduler.start()
        assert await disabled_scheduler.run_due() == 0
        await disabled_scheduler.close()
        disabled_store.close()

        _write_schedules(config_path, _scan_job("scan-reports", interval=60))
        reenabled_store = SQLiteScheduleStore(database)
        reenabled_scheduler = PeriodicScheduler(
            RecordingQueue(),
            reenabled_store,
            _load(config_path),
            clock=clock,
        )
        await reenabled_scheduler.start()
        assert await reenabled_scheduler.run_due() == 0
        await reenabled_scheduler.close()
        reenabled_store.close()

        _write_schedules(config_path, _scan_job("scan-reports", interval=3600))
        final_queue = RecordingQueue()
        final_store = SQLiteScheduleStore(database)
        final_scheduler = PeriodicScheduler(
            final_queue,
            final_store,
            _load(config_path),
            clock=clock,
        )
        await final_scheduler.start()
        assert await final_scheduler.run_due() == 0

        await _succeed_scheduled_job(final_store, original, clock)
        assert await final_scheduler.run_due() == 1
        assert final_queue.enqueued[0].metadata[SCHEDULED_FOR_METADATA] == (
            "2026-08-23T12:00:10.000000Z"
        )
        await _succeed_scheduled_job(final_store, final_queue.enqueued[0], clock)
        await final_scheduler.close()
        final_store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("active_state", ("enqueued", "running"))
def test_active_occurrence_survives_store_reopen_and_suppresses_later_due(
    tmp_path: Path,
    active_state: str,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    database = tmp_path / "scheduler.db"
    _write_schedules(config_path, _scan_job("scan-reports", interval=60))
    schedules = _load(config_path)
    clock = MutableClock()
    first_queue = RecordingQueue()

    async def scenario() -> None:
        first_store = SQLiteScheduleStore(database)
        first = PeriodicScheduler(first_queue, first_store, schedules, clock=clock)
        try:
            await first.start()
            assert await first.run_due() == 1
            original = first_queue.enqueued[0]
            if active_state == "running":
                execution = await ScheduledRunCoordinator(
                    first_store,
                    clock=clock,
                    lease_seconds=30,
                ).begin(original, _context())
                assert execution is not None and execution.execute is True
        finally:
            await first.close()
            first_store.close()

        clock.advance(120)
        recovered_queue = RecordingQueue()
        recovered_store = SQLiteScheduleStore(database)
        recovered = PeriodicScheduler(
            recovered_queue,
            recovered_store,
            schedules,
            clock=clock,
        )
        try:
            await recovered.start()
            assert await recovered.run_due() == 0

            coordinator = ScheduledRunCoordinator(
                recovered_store,
                clock=clock,
                lease_seconds=30,
            )
            if active_state == "running":
                # Expiry cannot prove that thread-backed work in the prior
                # process stopped, so a durable running owner fails closed
                # across restart instead of authorizing an overlapping owner.
                with pytest.raises(RuntimeError, match="leased"):
                    await coordinator.begin(original, _context(attempt=2))
                assert await recovered.run_due() == 0
            else:
                execution = await coordinator.begin(original, _context(attempt=2))
                assert execution is not None and execution.execute is True
                await execution.succeed()

                assert await recovered.run_due() == 1
                assert recovered_queue.enqueued[0].job_id != original.job_id
        finally:
            await recovered.close()
            recovered_store.close()

    asyncio.run(scenario())


def test_successful_schedule_state_survives_restart_without_early_duplicate(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    database = tmp_path / "scheduler.db"
    _write_schedules(config_path, _scan_job("scan-reports", interval=60))
    schedules = _load(config_path)
    clock = MutableClock()
    first_queue = RecordingQueue()

    async def scenario() -> None:
        first_store = SQLiteScheduleStore(database)
        first = PeriodicScheduler(
            first_queue,
            first_store,
            schedules,
            clock=clock,
            publication_lease_seconds=30,
        )
        await first.start()
        assert await first.run_due() == 1
        await _succeed_scheduled_job(first_store, first_queue.enqueued[0], clock)
        await first.close()
        first_store.close()

        first_job_id = first_queue.enqueued[0].job_id
        clock.advance(30)
        restarted_queue = RecordingQueue()
        restarted_store = SQLiteScheduleStore(database)
        restarted = PeriodicScheduler(
            restarted_queue,
            restarted_store,
            schedules,
            clock=clock,
            publication_lease_seconds=30,
        )
        await restarted.start()
        assert await restarted.run_due() == 0

        clock.advance(30)
        assert await restarted.run_due() == 1
        assert restarted_queue.enqueued[0].job_id != first_job_id
        await _succeed_scheduled_job(
            restarted_store, restarted_queue.enqueued[0], clock
        )
        await restarted.close()
        restarted_store.close()

    asyncio.run(scenario())


def test_pending_publication_reuses_identical_job_identity_after_restart(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    database = tmp_path / "scheduler.db"
    _write_schedules(config_path, _scan_job("scan-reports", interval=60))
    schedules = _load(config_path)
    clock = MutableClock()
    failing_queue = RecordingQueue(failures=1)

    async def scenario() -> None:
        first_store = SQLiteScheduleStore(database)
        first = PeriodicScheduler(
            failing_queue,
            first_store,
            schedules,
            clock=clock,
            publication_lease_seconds=30,
        )
        await first.start()
        with pytest.raises(ConnectionError, match="injected publication failure"):
            await first.run_due()
        attempted = failing_queue.attempted[0][0]
        await first.close()
        first_store.close()

        recovered_queue = RecordingQueue()
        recovered_store = SQLiteScheduleStore(database)
        recovered = PeriodicScheduler(
            recovered_queue,
            recovered_store,
            schedules,
            clock=clock,
            publication_lease_seconds=30,
        )
        await recovered.start()
        assert await recovered.run_due() == 1
        republished = recovered_queue.enqueued[0]
        assert republished.job_id == attempted.job_id
        assert republished.to_bytes() == attempted.to_bytes()
        await recovered.close()
        recovered_store.close()

    asyncio.run(scenario())


def test_failing_pending_publication_does_not_starve_another_due_schedule(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    database = tmp_path / "scheduler.db"
    _write_schedules(
        config_path,
        _scan_job("blocked-scan", bucket="blocked")
        + _scan_job("healthy-scan", bucket="healthy"),
    )
    schedules = _load(config_path)
    clock = MutableClock()

    async def scenario() -> None:
        first_store = SQLiteScheduleStore(database)
        first = PeriodicScheduler(
            RecordingQueue(failures=1),
            first_store,
            schedules[:1],
            clock=clock,
            publication_lease_seconds=30,
        )
        try:
            await first.start()
            with pytest.raises(ConnectionError, match="injected publication failure"):
                await first.run_due()
        finally:
            await first.close()
            first_store.close()

        recovered_queue = RecordingQueue(fail_schedule_ids={"blocked-scan"})
        recovered_store = SQLiteScheduleStore(database)
        recovered = PeriodicScheduler(
            recovered_queue,
            recovered_store,
            schedules,
            clock=clock,
            publication_lease_seconds=30,
        )
        try:
            await recovered.start()
            with pytest.raises(
                ConnectionError, match="injected publication failure"
            ):
                await recovered.run_due()
        finally:
            await recovered.close()
            recovered_store.close()

        assert [
            job.metadata[SCHEDULE_ID_METADATA]
            for job, _ in recovered_queue.attempted
        ] == ["blocked-scan", "healthy-scan"]
        assert [
            job.metadata[SCHEDULE_ID_METADATA] for job in recovered_queue.enqueued
        ] == ["healthy-scan"]

    asyncio.run(scenario())


def test_row_inconsistent_stored_envelope_is_terminalized_without_starvation(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    database = tmp_path / "scheduler.db"
    _write_schedules(
        config_path,
        _scan_job("corrupt-scan", bucket="corrupt")
        + _scan_job("healthy-scan", bucket="healthy"),
    )
    schedules = _load(config_path)
    clock = MutableClock()

    async def create_pending_occurrences() -> None:
        store = SQLiteScheduleStore(database)
        scheduler = PeriodicScheduler(
            RecordingQueue(failures=2),
            store,
            schedules,
            clock=clock,
            publication_lease_seconds=30,
        )
        try:
            await scheduler.start()
            with pytest.raises(ConnectionError, match="injected publication failure"):
                await scheduler.run_due()
        finally:
            await scheduler.close()
            store.close()

    asyncio.run(create_pending_occurrences())

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT job_id, envelope FROM scheduled_runs WHERE schedule_id=?",
            ("corrupt-scan",),
        ).fetchone()
        assert row is not None
        corrupt_job_id = row[0]
        stored = JobEnvelope.from_bytes(bytes(row[1]))
        row_inconsistent = JobEnvelope(
            schema_version=stored.schema_version,
            job_id=stored.job_id,
            job_type=stored.job_type,
            created_at=stored.created_at,
            correlation_id=stored.correlation_id,
            payload=stored.payload,
            metadata={
                **stored.metadata,
                SCHEDULE_ID_METADATA: "different-schedule",
            },
        )
        assert JobEnvelope.from_bytes(row_inconsistent.to_bytes()) == row_inconsistent
        connection.execute(
            "UPDATE scheduled_runs SET envelope=? WHERE job_id=?",
            (row_inconsistent.to_bytes(), corrupt_job_id),
        )

    recovered_queue = RecordingQueue()

    async def recover_without_starving_sibling() -> None:
        store = SQLiteScheduleStore(database)
        scheduler = PeriodicScheduler(
            recovered_queue,
            store,
            schedules,
            clock=clock,
            publication_lease_seconds=30,
        )
        try:
            await scheduler.start()
            assert await scheduler.run_due() == 1
            assert await scheduler.run_due() == 0

            clock.advance(60)
            assert await scheduler.run_due() == 1
        finally:
            await scheduler.close()
            store.close()

    asyncio.run(recover_without_starving_sibling())

    published_schedule_ids = [
        job.metadata[SCHEDULE_ID_METADATA] for job in recovered_queue.enqueued
    ]
    assert published_schedule_ids == ["healthy-scan", "corrupt-scan"]
    assert corrupt_job_id not in {
        job.job_id for job, _ in recovered_queue.attempted
    }
    assert recovered_queue.enqueued[1].job_id != corrupt_job_id

    with sqlite3.connect(database) as connection:
        failed = connection.execute(
            "SELECT state, outcome FROM scheduled_runs WHERE job_id=?",
            (corrupt_job_id,),
        ).fetchone()
        active = connection.execute(
            "SELECT active_job_id FROM scheduled_jobs WHERE scope=?",
            (schedules[0].scope,),
        ).fetchone()
    assert failed is not None
    assert failed[0] == "publication_failed"
    assert "invalid durable envelope" in failed[1]
    assert active is not None
    assert active[0] == recovered_queue.enqueued[1].job_id


@pytest.mark.parametrize(
    "mutation",
    ("payload", "job_type", "created_at", "scheduled_for", "naive_scheduled_for"),
)
def test_durable_scheduled_envelope_mismatch_cannot_execute(
    tmp_path: Path,
    mutation: str,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_schedules(config_path, _scan_job("scan-reports"))
    queue = RecordingQueue()
    clock = MutableClock()

    async def scenario() -> None:
        store = SQLiteScheduleStore(tmp_path / "scheduler.db")
        scheduler = PeriodicScheduler(
            queue,
            store,
            _load(config_path),
            clock=clock,
            publication_lease_seconds=30,
        )
        try:
            await scheduler.start()
            assert await scheduler.run_due() == 1
            original = queue.enqueued[0]
            payload = dict(original.payload)
            metadata = dict(original.metadata)
            job_type = original.job_type
            created_at = original.created_at
            if mutation == "payload":
                payload["bucket"] = "attacker-controlled"
            elif mutation == "job_type":
                job_type = "policy.run"
            elif mutation == "created_at":
                created_at = "2026-08-23T12:00:01Z"
            elif mutation == "scheduled_for":
                metadata[SCHEDULED_FOR_METADATA] = (
                    "2026-08-23T12:00:01.000000Z"
                )
            else:
                metadata[SCHEDULED_FOR_METADATA] = "2026-08-23T12:00:00"
            changed = JobEnvelope(
                schema_version=original.schema_version,
                job_id=original.job_id,
                job_type=job_type,
                created_at=created_at,
                correlation_id=original.correlation_id,
                payload=payload,
                metadata=metadata,
            )
            coordinator = ScheduledRunCoordinator(
                store,
                clock=clock,
                lease_seconds=30,
            )

            with pytest.raises(InvalidJobError) as failure:
                await coordinator.begin(changed, _context())
            assert getattr(failure.value, "fail_worker", False)

            original_execution = await coordinator.begin(original, _context())
            assert original_execution is not None
            assert original_execution.execute is True
            await original_execution.succeed()
        finally:
            await scheduler.close()
            store.close()

    asyncio.run(scenario())


def test_scope_lock_excludes_overlap_then_releases_on_success(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules.yaml"
    database = tmp_path / "scheduler.db"
    _write_schedules(config_path, _scan_job("scan-reports"))
    queue = RecordingQueue()
    clock = MutableClock()

    async def scenario() -> None:
        store = SQLiteScheduleStore(database)
        scheduler = PeriodicScheduler(
            queue,
            store,
            _load(config_path),
            clock=clock,
            publication_lease_seconds=30,
        )
        await scheduler.start()
        assert await scheduler.run_due() == 1
        coordinator = ScheduledRunCoordinator(
            store,
            clock=clock,
            lease_seconds=30,
        )
        first_job = queue.enqueued[0]
        first = await coordinator.begin(first_job, _context())
        assert first is not None and first.execute is True
        with pytest.raises(RuntimeError, match="leased"):
            await coordinator.begin(first_job, _context(attempt=2))

        await first.succeed()
        settled = await coordinator.begin(first_job, _context(attempt=2))
        assert settled is not None and settled.execute is False

        clock.advance(60)
        assert await scheduler.run_due() == 1
        released = await coordinator.begin(queue.enqueued[1], _context())
        assert released is not None and released.execute is True
        await released.succeed()
        await scheduler.close()
        store.close()

    asyncio.run(scenario())


def test_cancelled_execution_claim_is_resolved_and_released_before_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_schedules(config_path, _scan_job("scan-reports"))
    queue = RecordingQueue()
    clock = MutableClock()

    async def scenario() -> None:
        store = SQLiteScheduleStore(tmp_path / "scheduler.db")
        scheduler = PeriodicScheduler(
            queue,
            store,
            _load(config_path),
            clock=clock,
            publication_lease_seconds=30,
        )
        await scheduler.start()
        assert await scheduler.run_due() == 1

        coordinator = ScheduledRunCoordinator(
            store,
            clock=clock,
            lease_seconds=30,
        )
        original_begin = store.begin_execution
        entered = threading.Event()
        release = threading.Event()

        def blocked_begin(*args: Any, **kwargs: Any):
            entered.set()
            assert release.wait(timeout=2)
            return original_begin(*args, **kwargs)

        monkeypatch.setattr(store, "begin_execution", blocked_begin)
        claim_task = asyncio.create_task(
            coordinator.begin(queue.enqueued[0], _context())
        )
        assert await asyncio.to_thread(entered.wait, 2)
        claim_task.cancel()
        await asyncio.sleep(0)
        release.set()

        with pytest.raises(asyncio.CancelledError):
            await claim_task

        replacement = await coordinator.begin(queue.enqueued[0], _context(attempt=2))
        assert replacement is not None and replacement.execute is True
        await replacement.succeed()
        await scheduler.close()
        store.close()

    asyncio.run(scenario())


def test_execution_claim_database_error_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_schedules(config_path, _scan_job("scan-reports"))
    queue = RecordingQueue()
    clock = MutableClock()

    async def scenario() -> None:
        store = SQLiteScheduleStore(tmp_path / "scheduler.db")
        scheduler = PeriodicScheduler(
            queue,
            store,
            _load(config_path),
            clock=clock,
            publication_lease_seconds=30,
        )
        await scheduler.start()
        assert await scheduler.run_due() == 1

        def unavailable(*args: Any, **kwargs: Any) -> None:
            raise sqlite3.OperationalError("database is unavailable")

        monkeypatch.setattr(store, "begin_execution", unavailable)
        coordinator = ScheduledRunCoordinator(store, clock=clock, lease_seconds=30)
        with pytest.raises(InvalidJobError) as failure:
            await coordinator.begin(queue.enqueued[0], _context())
        assert getattr(failure.value, "fail_worker", False)
        await scheduler.close()
        store.close()

    asyncio.run(scenario())


def test_execution_claim_naive_clock_fails_closed(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_schedules(config_path, _scan_job("scan-reports"))
    queue = RecordingQueue()
    clock = MutableClock()

    async def scenario() -> None:
        store = SQLiteScheduleStore(tmp_path / "scheduler.db")
        scheduler = PeriodicScheduler(queue, store, _load(config_path), clock=clock)
        await scheduler.start()
        assert await scheduler.run_due() == 1

        coordinator = ScheduledRunCoordinator(
            store,
            clock=lambda: datetime(2026, 8, 23, 12, 0),
            lease_seconds=30,
        )
        with pytest.raises(InvalidJobError) as failure:
            await coordinator.begin(queue.enqueued[0], _context())
        assert getattr(failure.value, "fail_worker", False)
        await scheduler.close()
        store.close()

    asyncio.run(scenario())


def test_running_scope_owner_remains_locked_after_lease_expiry(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules.yaml"
    database = tmp_path / "scheduler.db"
    _write_schedules(config_path, _scan_job("scan-reports"))
    queue = RecordingQueue()
    clock = MutableClock()

    async def scenario() -> None:
        store = SQLiteScheduleStore(database)
        scheduler = PeriodicScheduler(
            queue,
            store,
            _load(config_path),
            clock=clock,
            publication_lease_seconds=30,
        )
        await scheduler.start()
        assert await scheduler.run_due() == 1

        coordinator = ScheduledRunCoordinator(
            store,
            clock=clock,
            lease_seconds=30,
        )
        job = queue.enqueued[0]
        first = await coordinator.begin(job, _context())
        assert first is not None and first.execute is True
        assert 0 < first.heartbeat_interval < 30

        clock.advance(20)
        await first.renew()
        clock.advance(20)
        with pytest.raises(RuntimeError, match="leased"):
            await coordinator.begin(job, _context(attempt=2))

        clock.advance(11)
        with pytest.raises(RuntimeError, match="leased"):
            await coordinator.begin(job, _context(attempt=2))

        # The fenced owner may still settle after its heartbeat timestamp has
        # passed; only that explicit transition permits a later occurrence.
        await first.succeed()
        settled = await coordinator.begin(job, _context(attempt=2))
        assert settled is not None and settled.execute is False
        await scheduler.close()
        store.close()

    asyncio.run(scenario())


def test_fenced_stale_run_recovery_is_audited_idempotent_and_restart_safe(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    database = tmp_path / "scheduler.db"
    _write_schedules(config_path, _scan_job("scan-reports"))
    schedules = _load(config_path)
    queue = RecordingQueue()
    clock = MutableClock()
    recovery_id = "b18b9b9e-cc06-432a-a273-4de85441e773"

    async def scenario() -> None:
        store = SQLiteScheduleStore(database)
        scheduler = PeriodicScheduler(queue, store, schedules, clock=clock)
        await scheduler.start()
        assert await scheduler.run_due() == 1

        coordinator = ScheduledRunCoordinator(store, clock=clock, lease_seconds=30)
        original = queue.enqueued[0]
        first = await coordinator.begin(original, _context())
        assert first is not None and first.execute is True
        assert first.owner_id is not None
        prior_owner = first.owner_id

        running = store.get_run(original.job_id, now=clock())
        assert running is not None
        assert running.state == "running"
        assert running.stale is False
        assert store.list_runs(now=clock(), stale_only=True) == ()
        with pytest.raises(ScheduledRunRecoveryError, match="explicit confirmation"):
            store.inspect_recovery(
                original.job_id,
                expected_owner=prior_owner,
                former_worker_fenced=False,
                now=clock(),
            )
        with pytest.raises(ScheduledRunRecoveryError, match="has not expired"):
            store.inspect_recovery(
                original.job_id,
                expected_owner=prior_owner,
                former_worker_fenced=True,
                now=clock(),
            )

        clock.advance(31)
        reader = SQLiteScheduleStore(database, read_only=True)
        try:
            stale_runs = reader.list_runs(now=clock(), stale_only=True)
            assert [run.job_id for run in stale_runs] == [original.job_id]
            assert stale_runs[0].stale is True
            with pytest.raises(ScheduledRunRecoveryError, match="owner changed"):
                reader.inspect_recovery(
                    original.job_id,
                    expected_owner="different-owner",
                    former_worker_fenced=True,
                    now=clock(),
                )
            inspected = reader.inspect_recovery(
                original.job_id,
                expected_owner=prior_owner,
                former_worker_fenced=True,
                now=clock(),
            )
            assert inspected.execution_generation == 0
        finally:
            reader.close()

        recovery = store.recover_stale_run(
            original.job_id,
            recovery_id=recovery_id,
            expected_owner=prior_owner,
            operator="test-operator",
            reason="worker process was killed during the handler",
            fence_evidence="process exit code -9 observed and supervisor stopped",
            former_worker_fenced=True,
            now=clock(),
        )
        assert recovery.execution_generation == 0
        assert recovery.prior_execution_owner == prior_owner
        assert store.recover_stale_run(
            original.job_id,
            recovery_id=recovery_id,
            expected_owner=prior_owner,
            operator="test-operator",
            reason="worker process was killed during the handler",
            fence_evidence="process exit code -9 observed and supervisor stopped",
            former_worker_fenced=True,
            now=clock(),
        ) == recovery
        with pytest.raises(ScheduledRunRecoveryError, match="different request"):
            store.recover_stale_run(
                original.job_id,
                recovery_id=recovery_id,
                expected_owner=prior_owner,
                operator="test-operator",
                reason="different recovery reason",
                fence_evidence="process exit code -9 observed and supervisor stopped",
                former_worker_fenced=True,
                now=clock(),
            )
        with pytest.raises(ScheduledRunRecoveryError, match="not running"):
            store.recover_stale_run(
                original.job_id,
                recovery_id="7610b48d-1e0c-4306-84d4-91831037ba95",
                expected_owner=prior_owner,
                operator="test-operator",
                reason="duplicate recovery",
                fence_evidence="process exit code -9 observed and supervisor stopped",
                former_worker_fenced=True,
                now=clock(),
            )
        assert store.list_recoveries(original.job_id) == (recovery,)
        released = store.get_run(original.job_id, now=clock())
        assert released is not None
        assert released.state == "retry_wait"
        assert released.execution_owner is None
        assert released.execution_generation == 0

        with pytest.raises(RuntimeError, match="lease.*lost"):
            await first.succeed()

        store.close()
        restarted_store = SQLiteScheduleStore(database)
        replacement = await ScheduledRunCoordinator(
            restarted_store,
            clock=clock,
            lease_seconds=30,
        ).begin(original, _context(attempt=2))
        assert replacement is not None and replacement.execute is True
        assert replacement.owner_id != prior_owner
        await replacement.succeed()

        clock.advance(29)
        restarted_scheduler = PeriodicScheduler(
            queue,
            restarted_store,
            schedules,
            clock=clock,
        )
        await restarted_scheduler.start()
        assert await restarted_scheduler.run_due() == 1
        assert queue.enqueued[-1].job_id != original.job_id
        await restarted_scheduler.close()
        restarted_store.close()
        await scheduler.close()

    asyncio.run(scenario())


def test_retry_retains_logical_scope_and_dead_letter_releases_it(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    database = tmp_path / "scheduler.db"
    _write_schedules(config_path, _scan_job("scan-reports"))
    queue = RecordingQueue()
    clock = MutableClock()

    async def scenario() -> None:
        store = SQLiteScheduleStore(database)
        scheduler = PeriodicScheduler(
            queue,
            store,
            _load(config_path),
            clock=clock,
            publication_lease_seconds=30,
        )
        await scheduler.start()
        assert await scheduler.run_due() == 1

        coordinator = ScheduledRunCoordinator(
            store,
            clock=clock,
            lease_seconds=30,
        )
        first_job = queue.enqueued[0]
        first = await coordinator.begin(first_job, _context())
        assert first is not None and first.execute is True
        await first.retry()

        # The logical scope remains active, so no later occurrence is published.
        clock.advance(60)
        assert await scheduler.run_due() == 0
        redelivery = await coordinator.begin(first_job, _context(attempt=2))
        assert redelivery is not None and redelivery.execute is True
        with pytest.raises(RuntimeError, match="leased"):
            await coordinator.begin(first_job, _context(attempt=3))
        await redelivery.dead_letter(DeadLetterDisposition.EXHAUSTED)

        redriven = JobEnvelope(
            schema_version=first_job.schema_version,
            job_id=first_job.job_id,
            job_type=first_job.job_type,
            created_at=first_job.created_at,
            correlation_id=first_job.correlation_id,
            payload=first_job.payload,
            metadata={**first_job.metadata, REDRIVE_COUNT_METADATA: "1"},
        )
        redrive_execution = await coordinator.begin(
            redriven, _context(redrive_count=1)
        )
        assert redrive_execution is not None and redrive_execution.execute is True
        await redrive_execution.succeed()

        assert await scheduler.run_due() == 1
        after_terminal = await coordinator.begin(queue.enqueued[1], _context())
        assert after_terminal is not None and after_terminal.execute is True
        await after_terminal.succeed()
        await scheduler.close()
        store.close()

    asyncio.run(scenario())


def test_dead_lettered_redrive_generation_cannot_be_reopened_twice(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_schedules(config_path, _scan_job("scan-reports"))
    queue = RecordingQueue()
    clock = MutableClock()

    async def scenario() -> None:
        store = SQLiteScheduleStore(tmp_path / "scheduler.db")
        scheduler = PeriodicScheduler(
            queue,
            store,
            _load(config_path),
            clock=clock,
            publication_lease_seconds=30,
        )
        try:
            await scheduler.start()
            assert await scheduler.run_due() == 1
            original = queue.enqueued[0]
            coordinator = ScheduledRunCoordinator(
                store,
                clock=clock,
                lease_seconds=30,
            )

            def redrive(generation: int) -> JobEnvelope:
                return JobEnvelope(
                    schema_version=original.schema_version,
                    job_id=original.job_id,
                    job_type=original.job_type,
                    created_at=original.created_at,
                    correlation_id=original.correlation_id,
                    payload=original.payload,
                    metadata={
                        **original.metadata,
                        REDRIVE_COUNT_METADATA: str(generation),
                    },
                )

            initial = await coordinator.begin(original, _context())
            assert initial is not None and initial.execute is True
            with pytest.raises(RuntimeError, match="prior generation") as waiting:
                await coordinator.begin(
                    redrive(1),
                    _context(redrive_count=1),
                )
            assert getattr(waiting.value, "defer_without_exhaustion", False)
            await initial.dead_letter(DeadLetterDisposition.TERMINAL)

            first_redrive = redrive(1)
            first_generation = await coordinator.begin(
                first_redrive,
                _context(redrive_count=1),
            )
            assert first_generation is not None and first_generation.execute is True

            stale_initial_delivery = await coordinator.begin(
                original,
                _context(attempt=2),
            )
            assert (
                stale_initial_delivery is not None
                and stale_initial_delivery.execute is False
            )
            await first_generation.dead_letter(DeadLetterDisposition.EXHAUSTED)

            duplicate = await coordinator.begin(
                first_redrive,
                _context(attempt=2, redrive_count=1),
            )
            assert duplicate is not None and duplicate.execute is False

            second_generation = await coordinator.begin(
                redrive(2),
                _context(redrive_count=2),
            )
            assert second_generation is not None and second_generation.execute is True
            await second_generation.succeed()
        finally:
            await scheduler.close()
            store.close()

    asyncio.run(scenario())


def test_dead_lettered_occurrence_cannot_redrive_after_newer_completion(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_schedules(config_path, _scan_job("scan-reports", interval=60))
    queue = RecordingQueue()
    clock = MutableClock()

    async def scenario() -> None:
        store = SQLiteScheduleStore(tmp_path / "scheduler.db")
        scheduler = PeriodicScheduler(
            queue,
            store,
            _load(config_path),
            clock=clock,
            publication_lease_seconds=30,
        )
        try:
            await scheduler.start()
            assert await scheduler.run_due() == 1
            older = queue.enqueued[0]
            coordinator = ScheduledRunCoordinator(
                store,
                clock=clock,
                lease_seconds=30,
            )
            older_execution = await coordinator.begin(older, _context())
            assert older_execution is not None and older_execution.execute is True
            await older_execution.dead_letter(DeadLetterDisposition.TERMINAL)

            # A disable/re-enable config reload at the same wall-clock instant
            # creates a distinct occurrence with an equal scheduled_for value.
            _write_schedules(
                config_path,
                _scan_job("scan-reports", interval=60, enabled=False),
            )
            await asyncio.to_thread(store.sync, _load(config_path), clock())
            _write_schedules(
                config_path,
                _scan_job("scan-reports", interval=60),
            )
            await asyncio.to_thread(store.sync, _load(config_path), clock())
            assert await scheduler.run_due() == 1
            newer = queue.enqueued[1]
            assert (
                newer.metadata[SCHEDULED_FOR_METADATA]
                == older.metadata[SCHEDULED_FOR_METADATA]
            )
            newer_execution = await coordinator.begin(newer, _context())
            assert newer_execution is not None and newer_execution.execute is True
            await newer_execution.succeed()

            older_redrive = JobEnvelope(
                schema_version=older.schema_version,
                job_id=older.job_id,
                job_type=older.job_type,
                created_at=older.created_at,
                correlation_id=older.correlation_id,
                payload=older.payload,
                metadata={
                    **older.metadata,
                    REDRIVE_COUNT_METADATA: "1",
                },
            )
            with pytest.raises(
                InvalidJobError,
                match="cannot be redriven after a newer occurrence",
            ):
                await coordinator.begin(
                    older_redrive,
                    _context(redrive_count=1),
                )

            settled_newer = await coordinator.begin(
                newer,
                _context(attempt=2),
            )
            assert settled_newer is not None and settled_newer.execute is False
        finally:
            await scheduler.close()
            store.close()

    asyncio.run(scenario())


def test_registry_extension_enqueues_a_future_repair_job(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules.yaml"
    observed_tiers: list[frozenset[str] | None] = []

    def normalize(
        payload: Mapping[str, Any], known_tiers: frozenset[str] | None
    ) -> Mapping[str, Any]:
        observed_tiers.append(known_tiers)
        bucket = payload.get("bucket")
        if not isinstance(bucket, str) or not bucket:
            raise ValueError("repair bucket must be a non-empty string")
        return {"bucket": bucket}

    definition = ScheduledJobDefinition(
        "repair.check",
        normalize,
        lambda payload: ("repair.check", payload["bucket"]),
    )
    registry: ScheduleRegistry = default_schedule_registry()
    registry.register(definition)
    assert registry.resolve("repair.check") is definition

    _write_schedules(
        config_path,
        """  repair-demo:
    type: repair.check
    enabled: true
    interval_seconds: 600
    payload:
      bucket: demo-bucket
""",
    )
    schedules = load_schedule_config(
        config_path,
        registry=registry,
        known_tiers=("hot", "warm"),
    )
    assert schedules[0].schedule_id == "repair-demo"
    assert schedules[0].job_type == "repair.check"
    assert schedules[0].payload == {"bucket": "demo-bucket"}
    assert observed_tiers == [frozenset(("hot", "warm"))]

    queue = RecordingQueue()
    clock = MutableClock()

    async def scenario() -> None:
        store = SQLiteScheduleStore(tmp_path / "scheduler.db")
        scheduler = PeriodicScheduler(
            queue,
            store,
            schedules,
            clock=clock,
            publication_lease_seconds=30,
        )
        await scheduler.start()
        assert await scheduler.run_due() == 1
        await scheduler.close()
        store.close()

    asyncio.run(scenario())
    assert queue.enqueued[0].job_type == "repair.check"
    assert queue.enqueued[0].payload == {"bucket": "demo-bucket"}


def test_schedule_config_requires_jobs_mapping(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules.yaml"
    config_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"configuration\.jobs is required"):
        _load(config_path)


def test_schedule_config_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules.yaml"
    config_path.write_text(
        "jobs:\n"
        + _scan_job("scan-reports", interval=60)
        + _scan_job("scan-reports", interval=300),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate mapping key 'scan-reports'"):
        _load(config_path)


def test_schedule_config_rejects_duplicate_execution_scope(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_schedules(
        config_path,
        _scan_job("first-scan") + _scan_job("duplicate-scan"),
    )

    with pytest.raises(ValueError, match="same execution scope"):
        _load(config_path)


@pytest.mark.parametrize("escaped_control", (r"\r", r"\n"))
def test_schedule_config_rejects_cr_or_lf_in_schedule_id(
    tmp_path: Path,
    escaped_control: str,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_schedules(
        config_path,
        _scan_job(f'"bad{escaped_control}id"'),
    )

    with pytest.raises(ValueError, match="CR or LF"):
        _load(config_path)


@pytest.mark.parametrize(
    ("interval", "message"),
    (
        ("0.0000001", "one microsecond"),
        ("0.0000006", "one microsecond"),
        ("0.0000011", "whole-microsecond precision"),
        ("3155760001", "100 years"),
    ),
)
def test_schedule_config_rejects_unrepresentable_interval(
    tmp_path: Path,
    interval: str,
    message: str,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_schedules(config_path, _scan_job("scan-reports", interval=interval))

    with pytest.raises(ValueError, match=message):
        _load(config_path)


def test_schedule_config_rejects_unknown_tier(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_schedules(config_path, _scan_job("scan-reports", tier="archive"))

    with pytest.raises(ValueError, match="unknown tier: archive"):
        _load(config_path)


def test_schedule_config_rejects_invalid_policy_payload(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules.yaml"
    invalid_policy = _policy_job("place-reports").replace(
        "policy: simple",
        "policy: unsupported",
    )
    _write_schedules(config_path, invalid_policy)

    with pytest.raises(ValueError, match="unknown policy: unsupported"):
        _load(config_path)


def test_schedule_normalization_preserves_ordered_embedding_rules(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_schedules(
        config_path,
        _embedding_policy_job(
            "classify-reports",
            rules=(
                "        - name: invoice\n"
                "          query: invoice accounts payable\n"
                "          minimum_similarity: 0.8\n"
                "          destination_tier: hot\n"
                "        - name: retention\n"
                "          query: long-term records retention\n"
                "          minimum_similarity: 0.65\n"
                "          destination_tier: warm\n"
            ),
        ),
    )

    schedules = _load(config_path)

    assert schedules[0].payload["embedding_rules"] == [
        {
            "name": "invoice",
            "query": "invoice accounts payable",
            "minimum_similarity": 0.8,
            "destination_tier": "hot",
        },
        {
            "name": "retention",
            "query": "long-term records retention",
            "minimum_similarity": 0.65,
            "destination_tier": "warm",
        },
    ]


def test_schedule_config_rejects_oversized_aggregate_policy_strings(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    query = "q" * 14_000
    rules = "".join(
        "        - name: rule-{index}\n"
        "          query: {query}\n"
        "          minimum_similarity: 0.8\n"
        "          destination_tier: hot\n".format(index=index, query=query)
        for index in range(5)
    )
    _write_schedules(
        config_path,
        _embedding_policy_job("classify-reports", rules=rules),
    )

    with pytest.raises(
        ValueError,
        match="policy strings must total at most 65536 bytes",
    ):
        _load(config_path)


def test_scheduled_embedding_policy_is_enqueued_as_v2(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_schedules(
        config_path,
        _embedding_policy_job(
            "classify-reports",
            rules=(
                "        - name: invoice\n"
                "          query: invoice accounts payable\n"
                "          minimum_similarity: 0.8\n"
                "          destination_tier: hot\n"
            ),
        ),
    )
    queue = RecordingQueue()
    clock = MutableClock()

    async def scenario() -> None:
        store = SQLiteScheduleStore(tmp_path / "scheduler.db")
        scheduler = PeriodicScheduler(
            queue,
            store,
            _load(config_path),
            clock=clock,
            publication_lease_seconds=30,
        )
        await scheduler.start()
        assert await scheduler.run_due() == 1
        await scheduler.close()
        store.close()

    asyncio.run(scenario())

    assert queue.enqueued[0].schema_version == JOB_SCHEMA_VERSION_V2


@pytest.mark.parametrize(
    ("policy", "rules", "message"),
    [
        (
            "content",
            "        - name: invoice\n"
            "          query: invoice accounts payable\n"
            "          minimum_similarity: 2.0\n"
            "          destination_tier: hot\n",
            "minimum_similarity must be between -1 and 1",
        ),
        (
            "content",
            "        - name: invoice\n"
            "          query: first query\n"
            "          minimum_similarity: 0.8\n"
            "          destination_tier: hot\n"
            "        - name: invoice\n"
            "          query: second query\n"
            "          minimum_similarity: 0.7\n"
            "          destination_tier: warm\n",
            "cannot contain duplicate rule names",
        ),
        (
            "simple",
            "        - name: invoice\n"
            "          query: invoice accounts payable\n"
            "          minimum_similarity: 0.8\n"
            "          destination_tier: hot\n",
            "require the content policy",
        ),
        (
            "content",
            "        - name: invoice\n"
            "          query: invoice accounts payable\n"
            "          minimum_similarity: 0.8\n"
            "          destination_tier: cold\n",
            "contain disallowed destination tier\\(s\\): cold",
        ),
        (
            "content",
            "        - 1: invalid-field-name\n"
            "          name: invoice\n"
            "          query: invoice accounts payable\n"
            "          minimum_similarity: 0.8\n"
            "          destination_tier: hot\n",
            "field names must be strings",
        ),
    ],
)
def test_schedule_config_rejects_invalid_embedding_rules(
    tmp_path: Path,
    policy: str,
    rules: str,
    message: str,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_schedules(
        config_path,
        _embedding_policy_job(
            "classify-reports",
            policy=policy,
            rules=rules,
        ),
    )

    with pytest.raises(ValueError, match=message):
        _load(config_path)


def test_scheduled_constraints_survive_normalization_and_publication_recovery(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    database = tmp_path / "scheduler.db"
    _write_schedules(
        config_path,
        _policy_job("place-reports")
        + "      movement_constraints:\n"
        + "        minimum_residency_seconds: {hot: 3600, warm: 300}\n"
        + "        importance_tiers: {high: [warm], critical: [hot]}\n",
    )
    schedules = _load(config_path)
    expected = {
        "minimum_residency_seconds": {"hot": 3600, "warm": 300},
        "importance_tiers": {"high": ["warm"], "critical": ["hot"]},
    }
    assert schedules[0].payload["movement_constraints"] == expected
    clock = MutableClock()
    first_queue = RecordingQueue(failures=1)

    async def scenario() -> None:
        first_store = SQLiteScheduleStore(database)
        first = PeriodicScheduler(first_queue, first_store, schedules, clock=clock)
        await first.start()
        try:
            with pytest.raises(ConnectionError, match="injected publication failure"):
                await first.run_due()
            attempted = first_queue.attempted[0][0]
            assert attempted.schema_version == JOB_SCHEMA_VERSION_V3
        finally:
            await first.close()
            first_store.close()

        # A changed config must not replace the durable occurrence's controls.
        _write_schedules(config_path, _policy_job("place-reports"))
        recovered_queue = RecordingQueue()
        recovered_store = SQLiteScheduleStore(database)
        recovered = PeriodicScheduler(
            recovered_queue, recovered_store, _load(config_path), clock=clock,
        )
        await recovered.start()
        try:
            assert await recovered.run_due() == 1
            republished = recovered_queue.enqueued[0]
            assert republished.to_bytes() == attempted.to_bytes()
            assert republished.schema_version == JOB_SCHEMA_VERSION_V3
            assert republished.payload["movement_constraints"] == expected
            await _succeed_scheduled_job(recovered_store, republished, clock)
        finally:
            await recovered.close()
            recovered_store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "constraints_yaml",
    (
        "null",
        "[]",
        "{unknown: 1}",
        "{minimum_residency_seconds: {hot: true}}",
        "{minimum_residency_seconds: {hot: -1}}",
        "{minimum_residency_seconds: {hot: 315360001}}",
        "{minimum_residency_seconds: {wram: 3600}}",
        "{importance_tiers: {urgent: [hot]}}",
        "{importance_tiers: {high: [hot, hot]}}",
    ),
)
def test_schedule_config_rejects_invalid_movement_constraints(
    tmp_path: Path, constraints_yaml: str,
) -> None:
    config_path = tmp_path / "schedules.yaml"
    _write_schedules(
        config_path,
        _policy_job("place-reports")
        + f"      movement_constraints: {constraints_yaml}\n",
    )

    with pytest.raises(ValueError, match="movement_constraints"):
        _load(config_path)
