from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import pytest

from cognistore.jobs.nats_queue import NatsJetStreamConfig, NatsJetStreamQueue
from cognistore.jobs.runtime import AsyncWorker, WorkerConfig
from cognistore.jobs.scheduler import (
    PeriodicScheduler,
    ScheduledJob,
    ScheduledRunCoordinator,
    SQLiteScheduleStore,
)

NATS_URL = os.environ.get("COGNISTORE_NATS_URL")
JOB_TYPE = "test.scheduled-recovery"
LEASE_SECONDS = 0.75
TEST_STREAM_MAX_BYTES = 16 * 1024 * 1024

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not NATS_URL,
        reason="set COGNISTORE_NATS_URL to an isolated JetStream-enabled NATS server",
    ),
    pytest.mark.skipif(
        not hasattr(signal, "SIGKILL"),
        reason="hard-process-kill recovery requires SIGKILL",
    ),
]


def _queue_config(
    token: str,
    nats_url: str,
    *,
    client_name: str,
) -> NatsJetStreamConfig:
    return NatsJetStreamConfig(
        servers=(nats_url,),
        stream=f"CS_SCHEDULE_RECOVERY_{token.upper()}",
        subject=f"cognistore.test.schedule-recovery.{token}",
        consumer=f"schedule-recovery-workers-{token}",
        ack_wait=0.4,
        duplicate_window=2.0,
        connect_timeout=1.0,
        request_timeout=1.0,
        drain_timeout=1.0,
        client_name=client_name,
        # This test's unique durable stream must not reserve the production
        # default of 1 GiB on space-constrained container CI runners.
        stream_max_bytes=TEST_STREAM_MAX_BYTES,
    )


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    # A reader can race the subprocess between its append and fsync. Parse only
    # newline-terminated records so an in-progress final append is retried.
    complete = path.read_text(encoding="utf-8").rsplit("\n", 1)[0]
    return [
        json.loads(line)
        for line in complete.splitlines()
        if line
    ]


async def _run_worker(payload: dict[str, Any]) -> None:
    # Imported only in the POSIX subprocess used by this integration test.
    import fcntl

    database = Path(payload["database"])
    event_log = Path(payload["event_log"])
    lock_file = Path(payload["lock_file"])
    mode = str(payload["mode"])
    expected_settlements = int(payload["expected_settlements"])
    process_id = os.getpid()
    queue = NatsJetStreamQueue(
        _queue_config(
            str(payload["token"]),
            str(payload["nats_url"]),
            client_name=f"scheduled-recovery-worker-{process_id}",
        )
    )
    store = SQLiteScheduleStore(database)

    async def handler(job, context) -> None:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        with lock_file.open("a+b") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _append_event(
                    event_log,
                    {
                        "action": "overlap_detected",
                        "attempt": context.attempt,
                        "job_id": job.job_id,
                        "mode": mode,
                        "pid": process_id,
                        "time_ns": time.time_ns(),
                    },
                )
                raise RuntimeError("scheduled handlers overlapped across worker processes")

            _append_event(
                event_log,
                {
                    "action": "handler_started",
                    "attempt": context.attempt,
                    "job_id": job.job_id,
                    "mode": mode,
                    "pid": process_id,
                    "time_ns": time.time_ns(),
                },
            )
            if mode == "block":
                await asyncio.Event().wait()
            _append_event(
                event_log,
                {
                    "action": "handler_finished",
                    "attempt": context.attempt,
                    "job_id": job.job_id,
                    "mode": mode,
                    "pid": process_id,
                    "time_ns": time.time_ns(),
                },
            )
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    worker = AsyncWorker(
        queue,
        {JOB_TYPE: handler},
        config=WorkerConfig(
            fetch_timeout=0.05,
            heartbeat_interval=0.1,
            shutdown_grace=1.0,
            settlement_timeout=0.5,
            stop_after_jobs=expected_settlements,
            max_attempts=3,
            retry_base_delay=0.05,
            retry_max_delay=0.05,
            retry_jitter=0,
        ),
        coordinator=ScheduledRunCoordinator(store, lease_seconds=LEASE_SECONDS),
    )
    try:
        await worker.start()
        await asyncio.wait_for(worker.wait_for_shutdown_request(), timeout=20.0)
        report = await worker.shutdown()
        if report.completed != expected_settlements:
            raise RuntimeError(
                "worker settled an unexpected number of scheduled runs: "
                f"completed={report.completed}, nacked={report.nacked}, "
                f"dead_lettered={report.dead_lettered}"
            )
    finally:
        store.close()


def _worker_entrypoint() -> None:
    if len(sys.argv) != 3 or sys.argv[1] != "--scheduled-recovery-worker":
        raise SystemExit("invalid scheduled recovery worker invocation")
    asyncio.run(_run_worker(json.loads(sys.argv[2])))


def _start_worker(
    *,
    token: str,
    nats_url: str,
    database: Path,
    event_log: Path,
    lock_file: Path,
    output_log: Path,
    mode: str,
    expected_settlements: int,
) -> subprocess.Popen[bytes]:
    payload = json.dumps(
        {
            "token": token,
            "nats_url": nats_url,
            "database": str(database),
            "event_log": str(event_log),
            "lock_file": str(lock_file),
            "mode": mode,
            "expected_settlements": expected_settlements,
        }
    )
    with output_log.open("ab") as output:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.integration.test_scheduled_run_recovery",
                "--scheduled-recovery-worker",
                payload,
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
            cwd=Path(__file__).resolve().parents[2],
        )


async def _eventually(
    assertion: Callable[[], Any],
    *,
    timeout: float = 5.0,
) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return assertion()
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.02)


def _process_failure(process: subprocess.Popen[bytes], output_log: Path) -> str:
    output = output_log.read_text(encoding="utf-8") if output_log.exists() else ""
    return f"worker pid={process.pid} returncode={process.returncode}\n{output}"


def test_hard_killed_scheduled_run_is_fenced_recovered_and_followed_by_next_interval(
    tmp_path: Path,
) -> None:
    assert NATS_URL is not None

    async def scenario() -> None:
        token = uuid4().hex[:12]
        database = tmp_path / "scheduler.db"
        event_log = tmp_path / "handler-events.jsonl"
        lock_file = tmp_path / "handler.lock"
        first_output = tmp_path / "first-worker.log"
        replacement_output = tmp_path / "replacement-worker.log"
        schedule = ScheduledJob(
            schedule_id="live-hard-kill-recovery",
            job_type=JOB_TYPE,
            interval_seconds=0.25,
            enabled=True,
            payload={"case": token},
            scope=f"live-hard-kill-recovery-{token}",
        )
        store = SQLiteScheduleStore(database)
        publisher = NatsJetStreamQueue(
            _queue_config(token, NATS_URL, client_name="scheduled-recovery-publisher"),
            consume=False,
        )
        scheduler = PeriodicScheduler(publisher, store, (schedule,))
        first_worker: subprocess.Popen[bytes] | None = None
        replacement: subprocess.Popen[bytes] | None = None
        try:
            await scheduler.start()
            assert await scheduler.run_due() == 1
            initial_runs = store.list_runs(
                now=datetime.now(timezone.utc),
                schedule_id=schedule.schedule_id,
            )
            assert len(initial_runs) == 1
            original_job_id = initial_runs[0].job_id

            first_worker = _start_worker(
                token=token,
                nats_url=NATS_URL,
                database=database,
                event_log=event_log,
                lock_file=lock_file,
                output_log=first_output,
                mode="block",
                expected_settlements=1,
            )

            def running_after_handler_start():
                assert first_worker is not None
                assert first_worker.poll() is None, _process_failure(
                    first_worker, first_output
                )
                run = store.get_run(original_job_id, now=datetime.now(timezone.utc))
                assert run is not None and run.state == "running"
                starts = [
                    event
                    for event in _read_events(event_log)
                    if event["action"] == "handler_started"
                    and event["job_id"] == original_job_id
                    and event["pid"] == first_worker.pid
                ]
                assert len(starts) == 1
                assert run.execution_owner is not None
                assert run.execution_lease_expires_at is not None
                return run

            running = await _eventually(running_after_handler_start)
            prior_owner = running.execution_owner
            assert prior_owner is not None

            first_worker.send_signal(signal.SIGKILL)
            await asyncio.to_thread(first_worker.wait, 5.0)
            fenced_at_ns = time.time_ns()
            assert first_worker.returncode == -signal.SIGKILL, _process_failure(
                first_worker, first_output
            )

            def stale_after_process_death():
                stale = store.list_runs(
                    now=datetime.now(timezone.utc),
                    states=("running",),
                    schedule_id=schedule.schedule_id,
                    stale_only=True,
                )
                assert [run.job_id for run in stale] == [original_job_id]
                return stale[0]

            stale_run = await _eventually(stale_after_process_death)
            inspected = store.inspect_recovery(
                original_job_id,
                expected_owner=prior_owner,
                former_worker_fenced=True,
                now=datetime.now(timezone.utc),
            )
            assert inspected == stale_run
            fence_evidence = (
                f"pid={first_worker.pid} exited after SIGKILL with "
                f"returncode={first_worker.returncode} before replacement start"
            )
            recovery = store.recover_stale_run(
                original_job_id,
                recovery_id=str(uuid4()),
                expected_owner=prior_owner,
                operator="pytest-live-jetstream",
                reason="worker was hard-killed after the scheduled handler started",
                fence_evidence=fence_evidence,
                former_worker_fenced=True,
                now=datetime.now(timezone.utc),
            )
            assert recovery.prior_execution_owner == prior_owner
            assert recovery.fence_evidence == fence_evidence
            assert store.list_recoveries(original_job_id) == (recovery,)
            released = store.get_run(original_job_id, now=datetime.now(timezone.utc))
            assert released is not None
            assert released.state == "retry_wait"
            assert released.execution_owner is None
            assert released.execution_generation == running.execution_generation

            replacement = _start_worker(
                token=token,
                nats_url=NATS_URL,
                database=database,
                event_log=event_log,
                lock_file=lock_file,
                output_log=replacement_output,
                mode="complete",
                expected_settlements=2,
            )

            def original_recovered():
                assert replacement is not None
                assert replacement.poll() is None, _process_failure(
                    replacement, replacement_output
                )
                run = store.get_run(original_job_id, now=datetime.now(timezone.utc))
                assert run is not None and run.state == "succeeded"
                return run

            await _eventually(original_recovered)
            assert await scheduler.run_due() == 1

            await asyncio.to_thread(replacement.wait, 10.0)
            assert replacement.returncode == 0, _process_failure(
                replacement, replacement_output
            )
            runs = store.list_runs(
                now=datetime.now(timezone.utc),
                schedule_id=schedule.schedule_id,
            )
            assert len(runs) == 2
            assert [run.state for run in runs] == ["succeeded", "succeeded"]
            assert runs[0].job_id == original_job_id
            assert runs[1].job_id != original_job_id

            events = _read_events(event_log)
            assert not [event for event in events if event["action"] == "overlap_detected"]
            starts = [event for event in events if event["action"] == "handler_started"]
            finishes = [event for event in events if event["action"] == "handler_finished"]
            assert [event["job_id"] for event in starts] == [
                original_job_id,
                original_job_id,
                runs[1].job_id,
            ]
            assert [event["job_id"] for event in finishes] == [
                original_job_id,
                runs[1].job_id,
            ]
            assert starts[0]["pid"] == first_worker.pid
            assert all(event["pid"] != first_worker.pid for event in finishes)
            assert starts[1]["pid"] == starts[2]["pid"] == replacement.pid
            assert starts[1]["attempt"] >= 2
            assert starts[1]["time_ns"] > fenced_at_ns
        finally:
            for process in (replacement, first_worker):
                if process is not None and process.poll() is None:
                    process.send_signal(signal.SIGKILL)
                    await asyncio.to_thread(process.wait, 5.0)
            await scheduler.close()
            store.close()

    asyncio.run(scenario())


if __name__ == "__main__":
    _worker_entrypoint()
