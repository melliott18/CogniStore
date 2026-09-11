"""Trace HTTP placement explanations through real policy and move execution.

Only the message transport is in memory: the worker, job handlers, durable
catalog, integrity checks, and source/destination files all run normally.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cognistore.api import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.core.audit import AuditContext, AuditEventType, AuditQuery
from cognistore.core.placement_controls import ImportanceTag
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.jobs.handlers import build_handlers
from cognistore.jobs.models import (
    BusState,
    DeadLetterReceipt,
    DeadLetterRecord,
    EnqueueReceipt,
    JobEnvelope,
    QueueHealth,
)
from cognistore.jobs.runtime import AsyncWorker, ShutdownReport, WorkerConfig


@dataclass
class _Delivery:
    job: JobEnvelope
    attempt: int = 1
    source_stream: str = "PLACEMENT_TEST_JOBS"
    source_consumer: str = "placement-explanation-worker"
    source_published_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    stream_sequence: int = 1
    consumer_sequence: int = 1
    headers: dict[str, str] = field(default_factory=dict)
    acknowledgements: int = 0
    rejections: int = 0

    @property
    def raw_data(self) -> bytes:
        return self.job.to_bytes()

    async def ack(self) -> None:
        self.acknowledgements += 1

    async def nack(self, delay: float | None = None) -> None:
        self.rejections += 1

    async def in_progress(self) -> None:
        return None


class _LocalQueue:
    def __init__(self) -> None:
        self.deliveries: list[_Delivery] = []
        self.dead_letters: list[DeadLetterRecord] = []
        self.connected = False
        self.claimed = 0

    async def enqueue(
        self, job: JobEnvelope, *, message_id: str | None = None
    ) -> EnqueueReceipt:
        assert message_id == job.job_id
        # Preserve the actual queue serialization boundary as well.
        self.deliveries.append(_Delivery(JobEnvelope.from_bytes(job.to_bytes())))
        return EnqueueReceipt(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            stream="PLACEMENT_TEST_JOBS",
            sequence=len(self.deliveries),
        )

    async def connect(self) -> None:
        self.connected = True

    async def claim(self, timeout: float) -> _Delivery | None:
        if self.claimed < len(self.deliveries):
            delivery = self.deliveries[self.claimed]
            self.claimed += 1
            return delivery
        await asyncio.sleep(timeout)
        return None

    async def publish_dead_letter(self, record: DeadLetterRecord) -> DeadLetterReceipt:
        self.dead_letters.append(record)
        return DeadLetterReceipt(record.dead_letter_id, "PLACEMENT_TEST_DLQ", len(self.dead_letters))

    async def probe(self) -> QueueHealth:
        return QueueHealth(
            state=BusState.CONNECTED if self.connected else BusState.DISCONNECTED,
            ready=self.connected,
            jetstream=self.connected,
            stream="PLACEMENT_TEST_JOBS",
            consumer="placement-explanation-worker",
        )

    async def close(self, *, graceful: bool = True) -> None:
        self.connected = False


def _run_worker(
    queue: _LocalQueue, catalog: SQLiteCatalog, drivers: dict[str, PosixDriver]
) -> ShutdownReport:
    async def scenario() -> ShutdownReport:
        worker = AsyncWorker(
            queue,  # type: ignore[arg-type]
            build_handlers(drivers, catalog),
            config=WorkerConfig(
                fetch_timeout=0.01,
                heartbeat_interval=0,
                shutdown_grace=1,
                settlement_timeout=1,
                stop_after_jobs=1,
                max_attempts=1,
                retry_jitter=0,
            ),
            audit_catalog=catalog,
        )
        await worker.start()
        try:
            await asyncio.wait_for(worker.wait_for_shutdown_request(), timeout=10)
        finally:
            report = await worker.shutdown()
        assert report.unsettled == 0
        assert queue.deliveries[-1].acknowledgements == 1
        assert queue.deliveries[-1].rejections == 0
        return report

    return asyncio.run(scenario())


def _preview(client: TestClient, key: str, config: dict) -> dict:
    response = client.post(
        "/v1/policies/preview",
        json={"bucket": "documents", "key": key, "config": config},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _submit(client: TestClient, config: dict) -> dict:
    response = client.post(
        "/v1/actions/policy-runs", json={"bucket": "documents", "config": config}
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued"
    return response.json()


def _decisions(client: TestClient, job_id: str) -> list[dict]:
    response = client.get("/v1/policy-decisions", params={"job_id": job_id})
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_preview_traces_moved_stayed_and_suppressed_objects_to_final_job(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite"
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    payloads = {
        "archive/moved.bin": b"move these larger archive bytes",
        "active/stayed.bin": b"ok",
        "protected/suppressed.bin": b"retain this important object on hot storage",
    }
    expected = {
        "archive/moved.bin": ("move", "size_threshold", "warm"),
        "active/stayed.bin": ("stay", "size_threshold", "hot"),
        "protected/suppressed.bin": ("suppressed", "importance_restriction", "hot"),
    }
    config = {"policy": "simple", "threshold": 4, "allowed_tiers": ["hot", "warm"]}
    queue = _LocalQueue()
    with SQLiteCatalog(database) as catalog:
        for key, payload in payloads.items():
            drivers["hot"].put_object("documents", key, payload)
            catalog.upsert("documents", key, len(payload), "hot")
        catalog.set_importance(
            "documents",
            "protected/suppressed.bin",
            ImportanceTag(
                level="critical",
                actor_type="user",
                actor_id="ticket-50-test",
                provenance="protected document fixture",
                updated_at=datetime.now(timezone.utc).isoformat(),
            ),
            audit_context=AuditContext(
                correlation_id="ticket-50-fixture",
                actor_type="user",
                actor_id="ticket-50-test",
            ),
        )
        gateway = CogniStoreGateway(catalog, drivers, queue=queue)  # type: ignore[arg-type]
        with TestClient(create_app(gateway)) as client:
            events_before_preview = catalog.list_audit_events()
            previews = {key: _preview(client, key, config) for key in payloads}
            for key, preview in previews.items():
                disposition, reason_code, proposed_tier = expected[key]
                assert preview["schema_version"] == 1
                assert preview["decision_id"] is None
                assert preview["disposition"] == disposition
                assert preview["current"] == {"tier": "hot"}
                assert preview["proposed"] == {"tier": proposed_tier}
                assert preview["changed_fields"] == (["tier"] if disposition == "move" else [])
                assert preview["execution"]["mode"] == "preview"
                assert preview["execution"]["state"] == "dry_run"
                assert preview["execution"]["job"] is None
                assert preview["execution"]["move_id"] is None
                assert preview["explanation"]["state"] == "available"
                assert preview["explanation"]["model_details"] == "not_applicable"
                assert preview["explanation"]["structured_reason"]["code"] == reason_code
                assert drivers["hot"].get_object("documents", key) == payloads[key]
            assert catalog.list_audit_events() == events_before_preview
            assert list(drivers["warm"].list_objects("documents")) == []

            submitted = _submit(client, config)
            assert _decisions(client, submitted["job_id"]) == []
            report = _run_worker(queue, catalog, drivers)
            assert report.completed == 1
            assert report.dead_lettered == 0
            status = client.get(submitted["status_url"])
            assert status.status_code == 200, status.text
            assert status.json()["status"] == "succeeded"

            decisions = _decisions(client, submitted["job_id"])
            assert {item["key"] for item in decisions} == set(payloads)
            assert len(catalog.list_move_jobs()) == 1
            for decision in decisions:
                key = decision["key"]
                disposition, reason_code, final_tier = expected[key]
                preview = previews[key]
                assert decision["disposition"] == disposition
                assert decision["current"] == preview["current"] == {"tier": "hot"}
                assert decision["proposed"] == preview["proposed"]
                assert decision["changed_fields"] == preview["changed_fields"]
                reason = decision["explanation"]["structured_reason"]
                assert reason["code"] == reason_code
                assert reason["decisive_signals"] == (
                    preview["explanation"]["structured_reason"]["decisive_signals"]
                )
                execution = decision["execution"]
                assert execution["mode"] == "persisted"
                assert execution["job_id"] == submitted["job_id"]
                assert execution["correlation_id"] == submitted["correlation_id"]
                assert execution["job"]["status"] == "succeeded"
                assert execution["state"] == (
                    "completed" if disposition == "move" else "not_requested"
                )
                event = catalog.get_audit_event(decision["decision_id"])
                assert event is not None
                assert event.event_type == AuditEventType.POLICY_DECISION
                assert event.job_id == submitted["job_id"]
                assert event.details["structured_reason"] == reason
                if disposition == "move":
                    move = catalog.get_move_job(execution["move_id"])
                    assert move is not None
                    assert move.state.value == "completed"
                    assert execution["event_id"] is not None
                    with pytest.raises(FileNotFoundError):
                        drivers["hot"].stat_object("documents", key)
                else:
                    assert execution["move_id"] is None
                record = catalog.get("documents", key)
                assert record is not None
                assert record.tier == final_tier
                assert drivers[final_tier].get_object("documents", key) == payloads[key]
                detail = client.get(f"/v1/policy-decisions/{decision['decision_id']}")
                assert detail.status_code == 200, detail.text
                assert detail.json() == decision
            retained_decisions = decisions

    # Explanations and execution evidence survive a fresh catalog/API process.
    with SQLiteCatalog(database) as reopened:
        with TestClient(create_app(CogniStoreGateway(reopened, drivers))) as client:
            assert _decisions(client, submitted["job_id"]) == retained_decisions


def test_integrity_failure_keeps_preview_separate_from_failed_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    key = "archive/corrupted-destination.bin"
    payload = b"source remains intact after failed verification"
    drivers["hot"].put_object("documents", key, payload)
    original_write = drivers["warm"].put_object_stream

    def corrupt_destination(*args, **kwargs):
        result = original_write(*args, **kwargs)
        (drivers["warm"].base / "documents" / key).write_bytes(b"x" * len(payload))
        return result

    monkeypatch.setattr(drivers["warm"], "put_object_stream", corrupt_destination)
    queue = _LocalQueue()
    config = {"policy": "simple", "threshold": 4, "allowed_tiers": ["hot", "warm"]}
    with SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog:
        catalog.upsert("documents", key, len(payload), "hot")
        gateway = CogniStoreGateway(catalog, drivers, queue=queue)  # type: ignore[arg-type]
        with TestClient(create_app(gateway)) as client:
            preview = _preview(client, key, config)
            assert preview["disposition"] == "move"
            assert preview["execution"]["state"] == "dry_run"
            submitted = _submit(client, config)
            report = _run_worker(queue, catalog, drivers)
            assert report.completed == 0
            assert report.dead_lettered == 1
            assert len(queue.dead_letters) == 1
            status = client.get(submitted["status_url"])
            assert status.status_code == 200, status.text
            assert status.json()["status"] == "failed"

            decisions = _decisions(client, submitted["job_id"])
            assert len(decisions) == 1
            decision = decisions[0]
            assert decision["disposition"] == "move"
            assert decision["current"] == preview["current"] == {"tier": "hot"}
            assert decision["proposed"] == preview["proposed"] == {"tier": "warm"}
            assert decision["changed_fields"] == ["tier"]
            assert decision["explanation"]["structured_reason"]["code"] == "size_threshold"
            execution = decision["execution"]
            assert execution["mode"] == "persisted"
            assert execution["state"] == "failed"
            assert execution["job"]["status"] == "failed"
            assert execution["job_id"] == submitted["job_id"]
            assert execution["event_id"] is not None
            move = catalog.get_move_job(execution["move_id"])
            assert move is not None
            assert move.state.value == "failed"
            assert "checksum mismatch" in move.terminal_reason
            failures = catalog.list_audit_events(AuditQuery(job_id=submitted["job_id"]))
            assert AuditEventType.JOB_DEAD_LETTERED in {event.event_type for event in failures}
            assert drivers["hot"].get_object("documents", key) == payload
            record = catalog.get("documents", key)
            assert record is not None
            assert record.tier == "hot"


def test_unavailable_model_keeps_fallback_reason_and_guardrails_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Missing deployment configuration exercises the supported provider-unavailable
    # path without a network service or a fabricated persisted decision.
    monkeypatch.delenv("COGNISTORE_LLM_ENDPOINT", raising=False)
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    key = "archive/no-model.bin"
    payload = b"large enough that an unrelated size fallback would move it"
    drivers["hot"].put_object("documents", key, payload)
    queue = _LocalQueue()
    config = {"policy": "llm", "threshold": 4, "allowed_tiers": ["hot", "warm"]}
    with SQLiteCatalog(tmp_path / "catalog.sqlite") as catalog:
        catalog.upsert("documents", key, len(payload), "hot")
        gateway = CogniStoreGateway(catalog, drivers, queue=queue)  # type: ignore[arg-type]
        with TestClient(create_app(gateway)) as client:
            preview = _preview(client, key, config)
            assert preview["execution"]["state"] == "dry_run"
            submitted = _submit(client, config)
            report = _run_worker(queue, catalog, drivers)
            assert report.completed == 1
            decisions = _decisions(client, submitted["job_id"])
            assert len(decisions) == 1
            persisted = decisions[0]
            assert persisted["execution"]["state"] == "not_requested"
            assert persisted["execution"]["job"]["status"] == "succeeded"
            assert persisted["explanation"]["structured_reason"]["policy"] == (
                preview["explanation"]["structured_reason"]["policy"]
            )
            for decision in (preview, persisted):
                assert decision["disposition"] == "stay"
                assert decision["changed_fields"] == []
                assert decision["current"] == decision["proposed"] == {"tier": "hot"}
                explanation = decision["explanation"]
                assert explanation["state"] == "available"
                assert explanation["model_details"] == "unavailable"
                reason = explanation["structured_reason"]
                assert reason["code"] == "provider_error"
                assert reason["constraints"]["allowed_destination_tiers"] == ["hot", "warm"]
                assert reason["constraints"]["residency_active"] is False
            assert catalog.list_move_jobs() == []
            assert drivers["hot"].get_object("documents", key) == payload
            assert list(drivers["warm"].list_objects("documents")) == []
