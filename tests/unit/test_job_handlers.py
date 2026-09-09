from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from cognistore.core.audit import AuditEventType, AuditQuery
from cognistore.core.catalog import Catalog
from cognistore.core.move_jobs import (
    MoveJobFailedError,
    MoveJobLeaseError,
    MoveJobState,
)
from cognistore.core.mover import Mover
from cognistore.core.placement_controls import MovementConstraints
from cognistore.core.policy import (
    MAX_POLICY_CONFIG_BYTES,
    EmbeddingPolicyRule,
)
from cognistore.core.policy_runner import ActionResult, PolicyRunner
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.core.throughput import (
    ThroughputConfig,
    ThroughputController,
    TierLimits,
)
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.jobs.handlers import (
    CATALOG_SCAN_JOB,
    POLICY_RUN_JOB,
    _run_blocking_safely,
    build_handlers,
    policy_job_payload,
    policy_job_schema_version,
)
from cognistore.jobs.models import (
    JOB_SCHEMA_VERSION_V1,
    JOB_SCHEMA_VERSION_V2,
    JOB_SCHEMA_VERSION_V3,
    InvalidJobError,
    JobContext,
    JobEnvelope,
)


async def _wait_for_thread_event(event: threading.Event) -> None:
    while not event.is_set():
        await asyncio.sleep(0)


def test_blocking_side_effect_ignores_repeated_cancellation_until_safe_boundary() -> None:
    async def scenario() -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_operation() -> str:
            started.set()
            assert release.wait(timeout=2)
            finished.set()
            return "completed"

        task = asyncio.create_task(_run_blocking_safely(blocking_operation))
        await asyncio.wait_for(_wait_for_thread_event(started), timeout=1)

        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        assert finished.is_set() is False

        release.set()
        assert await asyncio.wait_for(task, timeout=1) == "completed"
        assert finished.is_set() is True

    asyncio.run(scenario())


def test_blocking_side_effect_can_propagate_cancellation_after_safe_boundary() -> None:
    async def scenario() -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_operation() -> None:
            started.set()
            assert release.wait(timeout=2)
            finished.set()

        task = asyncio.create_task(
            _run_blocking_safely(
                blocking_operation,
                _propagate_cancellation=True,
            )
        )
        await asyncio.wait_for(_wait_for_thread_event(started), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert finished.is_set() is True

    asyncio.run(scenario())


def _context() -> JobContext:
    return JobContext(
        attempt=1,
        redelivered=False,
        stream_sequence=1,
        consumer_sequence=1,
        shutdown_requested=asyncio.Event(),
    )


def test_policy_job_payload_round_trips_embedding_rules() -> None:
    rules = (
        EmbeddingPolicyRule(
            name="invoice",
            query="invoice accounts payable",
            minimum_similarity=0.8,
            destination_tier="hot",
        ),
        {
            "name": "archive",
            "query": "long-term records retention",
            "minimum_similarity": 0.65,
            "destination_tier": "warm",
        },
    )
    payload = policy_job_payload(
        bucket="bucket",
        prefix="reports/",
        policy="content",
        threshold=1024,
        llm_threshold=None,
        allowed_tiers=("hot", "warm"),
        hot_name_patterns=(),
        warm_name_patterns=(),
        cold_name_patterns=(),
        hot_mime_prefixes=(),
        warm_mime_prefixes=(),
        cold_mime_prefixes=(),
        embedding_rules=rules,
    )

    restored = JobEnvelope.from_bytes(
        JobEnvelope.create(
            POLICY_RUN_JOB,
            payload,
            schema_version=policy_job_schema_version(payload),
        ).to_bytes()
    )

    assert restored.schema_version == JOB_SCHEMA_VERSION_V2
    assert restored.payload["embedding_rules"] == [
        {
            "name": "invoice",
            "query": "invoice accounts payable",
            "minimum_similarity": 0.8,
            "destination_tier": "hot",
        },
        {
            "name": "archive",
            "query": "long-term records retention",
            "minimum_similarity": 0.65,
            "destination_tier": "warm",
        },
    ]


def test_policy_job_payload_rejects_oversized_aggregate_policy_strings() -> None:
    query = "q" * (MAX_POLICY_CONFIG_BYTES // 5 + 1)
    rules = tuple(
        EmbeddingPolicyRule(
            name=f"rule-{index}",
            query=query,
            minimum_similarity=0.8,
            destination_tier="hot",
        )
        for index in range(5)
    )

    with pytest.raises(
        ValueError,
        match="policy strings must total at most 65536 bytes",
    ):
        policy_job_payload(
            bucket="bucket",
            prefix="",
            policy="content",
            threshold=1024,
            llm_threshold=None,
            allowed_tiers=("hot", "warm"),
            hot_name_patterns=(),
            warm_name_patterns=(),
            cold_name_patterns=(),
            hot_mime_prefixes=(),
            warm_mime_prefixes=(),
            cold_mime_prefixes=(),
            embedding_rules=rules,
        )


def test_policy_handler_rejects_oversized_aggregate_policy_strings(
    tmp_path: Path,
) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    payload = policy_job_payload(
        bucket="bucket",
        prefix="",
        policy="content",
        threshold=1024,
        llm_threshold=None,
        allowed_tiers=("hot", "warm"),
        hot_name_patterns=(),
        warm_name_patterns=(),
        cold_name_patterns=(),
        hot_mime_prefixes=(),
        warm_mime_prefixes=(),
        cold_mime_prefixes=(),
    )
    payload["hot_name_patterns"] = ["x" * MAX_POLICY_CONFIG_BYTES]
    job = JobEnvelope.create(POLICY_RUN_JOB, payload)
    handler = build_handlers({"hot": hot, "warm": warm}, Catalog())[POLICY_RUN_JOB]

    with pytest.raises(
        InvalidJobError,
        match="policy strings must total at most 65536 bytes",
    ):
        asyncio.run(handler(job, _context()))


def test_policy_handler_stays_when_embedding_provider_is_not_configured(
    tmp_path: Path,
) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    data = b"small enough for the fallback to move"
    warm.put_object("bucket", "invoice.txt", data)
    catalog = Catalog()
    catalog.upsert("bucket", "invoice.txt", len(data), "warm")
    job = JobEnvelope.create(
        POLICY_RUN_JOB,
        policy_job_payload(
            bucket="bucket",
            prefix="",
            policy="content",
            threshold=len(data),
            llm_threshold=None,
            allowed_tiers=("hot", "warm"),
            hot_name_patterns=(),
            warm_name_patterns=(),
            cold_name_patterns=(),
            hot_mime_prefixes=(),
            warm_mime_prefixes=(),
            cold_mime_prefixes=(),
            embedding_rules=(
                EmbeddingPolicyRule(
                    name="invoice",
                    query="invoice accounts payable",
                    minimum_similarity=0.8,
                    destination_tier="hot",
                ),
            ),
        ),
        schema_version=JOB_SCHEMA_VERSION_V2,
    )

    handler = build_handlers({"hot": hot, "warm": warm}, catalog)[POLICY_RUN_JOB]
    asyncio.run(handler(job, _context()))

    record = catalog.get("bucket", "invoice.txt")
    assert record is not None
    assert record.tier == "warm"
    assert warm.get_object("bucket", "invoice.txt") == data
    with pytest.raises(FileNotFoundError):
        hot.get_object("bucket", "invoice.txt")
    decisions = [
        event
        for event in catalog.list_audit_events()
        if event.event_type == AuditEventType.POLICY_DECISION.value
    ]
    assert len(decisions) == 1
    assert decisions[0].outcome == "stayed"
    assert decisions[0].details["action"] == "stay"


@pytest.mark.parametrize(
    ("policy_name", "embedding_rules", "message"),
    [
        (
            "content",
            [
                {
                    "name": "invoice",
                    "query": "invoice accounts payable",
                    "minimum_similarity": 2.0,
                    "destination_tier": "hot",
                }
            ],
            "minimum_similarity must be between -1 and 1",
        ),
        (
            "content",
            [
                {
                    "name": "invoice",
                    "query": "first query",
                    "minimum_similarity": 0.8,
                    "destination_tier": "hot",
                },
                {
                    "name": "invoice",
                    "query": "second query",
                    "minimum_similarity": 0.7,
                    "destination_tier": "warm",
                },
            ],
            "cannot contain duplicate rule names",
        ),
        (
            "simple",
            [
                {
                    "name": "invoice",
                    "query": "invoice accounts payable",
                    "minimum_similarity": 0.8,
                    "destination_tier": "hot",
                }
            ],
            "embedding rules require the content policy",
        ),
        (
            "content",
            [
                {
                    "name": "invoice",
                    "query": "invoice accounts payable",
                    "minimum_similarity": 0.8,
                    "destination_tier": "cold",
                }
            ],
            "embedding rule destination tier\\(s\\) are not allowed: cold",
        ),
    ],
)
def test_policy_handler_rejects_invalid_embedding_rule_contracts(
    tmp_path: Path,
    policy_name: str,
    embedding_rules: list[dict[str, object]],
    message: str,
) -> None:
    payload = policy_job_payload(
        bucket="bucket",
        prefix="",
        policy=policy_name,
        threshold=1024,
        llm_threshold=None,
        allowed_tiers=("hot", "warm"),
        hot_name_patterns=(),
        warm_name_patterns=(),
        cold_name_patterns=(),
        hot_mime_prefixes=(),
        warm_mime_prefixes=(),
        cold_mime_prefixes=(),
    )
    payload["embedding_rules"] = embedding_rules
    handler = build_handlers(
        {
            "hot": PosixDriver(str(tmp_path / "hot")),
            "warm": PosixDriver(str(tmp_path / "warm")),
        },
        Catalog(),
    )[POLICY_RUN_JOB]

    with pytest.raises(InvalidJobError, match=message):
        asyncio.run(
            handler(
                JobEnvelope.create(
                    POLICY_RUN_JOB,
                    payload,
                    schema_version=JOB_SCHEMA_VERSION_V2,
                ),
                _context(),
            )
        )


def test_policy_handler_rejects_embedding_rules_in_v1_envelope(
    tmp_path: Path,
) -> None:
    payload = policy_job_payload(
        bucket="bucket",
        prefix="",
        policy="content",
        threshold=1024,
        llm_threshold=None,
        allowed_tiers=("hot", "warm"),
        hot_name_patterns=(),
        warm_name_patterns=(),
        cold_name_patterns=(),
        hot_mime_prefixes=(),
        warm_mime_prefixes=(),
        cold_mime_prefixes=(),
        embedding_rules=(
            EmbeddingPolicyRule(
                name="invoice",
                query="invoice accounts payable",
                minimum_similarity=0.8,
                destination_tier="hot",
            ),
        ),
    )
    handler = build_handlers(
        {
            "hot": PosixDriver(str(tmp_path / "hot")),
            "warm": PosixDriver(str(tmp_path / "warm")),
        },
        Catalog(),
    )[POLICY_RUN_JOB]
    job = JobEnvelope.create(
        POLICY_RUN_JOB,
        payload,
        schema_version=JOB_SCHEMA_VERSION_V1,
    )

    with pytest.raises(InvalidJobError, match="requires schema_version 2"):
        asyncio.run(handler(job, _context()))


def test_policy_handler_accepts_legacy_v1_payload(tmp_path: Path) -> None:
    payload = policy_job_payload(
        bucket="bucket",
        prefix="",
        policy="simple",
        threshold=1024,
        llm_threshold=None,
        allowed_tiers=("hot", "warm"),
        hot_name_patterns=(),
        warm_name_patterns=(),
        cold_name_patterns=(),
        hot_mime_prefixes=(),
        warm_mime_prefixes=(),
        cold_mime_prefixes=(),
    )
    handler = build_handlers(
        {
            "hot": PosixDriver(str(tmp_path / "hot")),
            "warm": PosixDriver(str(tmp_path / "warm")),
        },
        Catalog(),
    )[POLICY_RUN_JOB]
    job = JobEnvelope.create(POLICY_RUN_JOB, payload)

    assert job.schema_version == JOB_SCHEMA_VERSION_V1
    assert "embedding_rules" not in job.payload
    asyncio.run(handler(job, _context()))


def test_catalog_scan_handler_indexes_into_worker_catalog(tmp_path: Path) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    hot.put_object("bucket", "reports/one.txt", b"hello worker")
    catalog = SQLiteCatalog(tmp_path / "catalog.db")
    try:
        handler = build_handlers({"hot": hot}, catalog)[CATALOG_SCAN_JOB]
        job = JobEnvelope.create(
            CATALOG_SCAN_JOB,
            {"tier": "hot", "bucket": "bucket", "prefix": "reports/"},
        )

        asyncio.run(handler(job, _context()))

        record = catalog.get("bucket", "reports/one.txt")
        assert record is not None
        assert record.tier == "hot"
        assert record.metadata["mime"] == "text/plain"
        assert record.metadata["mime_detection"]["schema_version"] == 1
        assert record.metadata["mime_detection"]["detector"] in {
            "libmagic",
            "filename",
        }
        assert record.metadata["sample_len"] == len(b"hello worker")
    finally:
        catalog.close()


def test_policy_handler_moves_and_tolerates_duplicate_delivery(tmp_path: Path) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    data = b"small"
    warm.put_object("bucket", "one.txt", data)
    catalog = SQLiteCatalog(tmp_path / "catalog.db")
    catalog.upsert("bucket", "one.txt", len(data), "warm")
    try:
        handler = build_handlers({"hot": hot, "warm": warm}, catalog)[POLICY_RUN_JOB]
        job = JobEnvelope.create(
            POLICY_RUN_JOB,
            policy_job_payload(
                bucket="bucket",
                prefix="",
                policy="simple",
                threshold=len(data),
                llm_threshold=None,
                allowed_tiers=("hot", "warm"),
                hot_name_patterns=(),
                warm_name_patterns=(),
                cold_name_patterns=(),
                hot_mime_prefixes=(),
                warm_mime_prefixes=(),
                cold_mime_prefixes=(),
            ),
        )

        asyncio.run(handler(job, _context()))
        # At-least-once delivery is expected. The updated placement makes the
        # same policy pass a no-op rather than attempting the move twice.
        asyncio.run(handler(job, _context()))

        assert hot.get_object("bucket", "one.txt") == data
        assert list(warm.list_objects("bucket")) == []
        record = catalog.get("bucket", "one.txt")
        assert record is not None
        assert record.tier == "hot"
    finally:
        catalog.close()


def test_policy_handler_correlates_worker_decisions_and_moves(tmp_path: Path) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    data = b"audited policy move"
    warm.put_object("bucket", "one.txt", data)
    catalog = Catalog()
    catalog.upsert("bucket", "one.txt", len(data), "warm")
    job = JobEnvelope.create(
        POLICY_RUN_JOB,
        policy_job_payload(
            bucket="bucket",
            prefix="",
            policy="simple",
            threshold=len(data),
            llm_threshold=None,
            allowed_tiers=("hot", "warm"),
            hot_name_patterns=(),
            warm_name_patterns=(),
            cold_name_patterns=(),
            hot_mime_prefixes=(),
            warm_mime_prefixes=(),
            cold_mime_prefixes=(),
        ),
        job_id="00000000-0000-4000-8000-000000000031",
        correlation_id="policy-correlation-31",
    )

    handler = build_handlers({"hot": hot, "warm": warm}, catalog)[POLICY_RUN_JOB]
    asyncio.run(handler(job, _context()))

    events = catalog.list_audit_events(
        AuditQuery(correlation_id=job.correlation_id, job_id=job.job_id)
    )
    decision = next(
        event for event in events if event.event_type == AuditEventType.POLICY_DECISION.value
    )
    move_events = [
        event
        for event in events
        if event.move_id is not None and event.event_type != AuditEventType.POLICY_DECISION.value
    ]

    assert decision.actor_type == "worker"
    assert decision.actor_id == job.job_id
    assert decision.policy_name == "simple"
    assert decision.policy_version == "1"
    assert move_events
    assert decision.move_id == move_events[0].move_id
    assert move_events[0].causation_id == decision.event_id
    assert [event.causation_id for event in move_events[1:]] == [
        event.event_id for event in move_events[:-1]
    ]
    assert move_events[-1].event_type == AuditEventType.MOVE_COMPLETED.value
    assert all(event.actor_type == "worker" for event in move_events)
    assert all(event.actor_id == job.job_id for event in move_events)
    assert all(event.job_id == job.job_id for event in move_events)
    assert hot.get_object("bucket", "one.txt") == data


def test_policy_handler_uses_job_audit_context_for_move_recovery(tmp_path: Path) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    data = b"audited recovery"
    warm.put_object("bucket", "recover.txt", data)
    catalog = Catalog()
    catalog.upsert("bucket", "recover.txt", len(data), "warm")
    job = JobEnvelope.create(
        POLICY_RUN_JOB,
        policy_job_payload(
            bucket="bucket",
            prefix="",
            policy="simple",
            threshold=len(data),
            llm_threshold=None,
            allowed_tiers=("hot", "warm"),
            hot_name_patterns=(),
            warm_name_patterns=(),
            cold_name_patterns=(),
            hot_mime_prefixes=(),
            warm_mime_prefixes=(),
            cold_mime_prefixes=(),
        ),
        job_id="00000000-0000-4000-8000-000000000032",
        correlation_id="policy-recovery-correlation-31",
    )
    move_id = f"{job.job_id}:recovery"
    source_metadata = warm.stat_object("bucket", "recover.txt")
    catalog.claim_move_job(
        move_id,
        src_tier="warm",
        dst_tier="hot",
        bucket="bucket",
        key="recover.txt",
        expected_size=len(data),
        source_metadata=source_metadata,
        owner_id="crashed-worker",
        now="2000-01-01T00:00:00.000000Z",
        lease_expires_at="2000-01-01T00:00:01.000000Z",
    )

    handler = build_handlers({"hot": hot, "warm": warm}, catalog)[POLICY_RUN_JOB]
    asyncio.run(handler(job, _context()))

    recovery_events = [
        event
        for event in catalog.list_audit_events(AuditQuery(move_id=move_id))
        if event.event_type != AuditEventType.MOVE_PREPARED.value
    ]
    assert any(event.event_type == AuditEventType.MOVE_RETRY.value for event in recovery_events)
    assert any(event.event_type == AuditEventType.MOVE_COMPLETED.value for event in recovery_events)
    assert all(event.correlation_id == job.correlation_id for event in recovery_events)
    assert all(event.actor_type == "worker" for event in recovery_events)
    assert all(event.actor_id == job.job_id for event in recovery_events)
    assert all(event.job_id == job.job_id for event in recovery_events)
    assert hot.get_object("bucket", "recover.txt") == data


def test_policy_recovery_queries_only_its_nonterminal_move_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = Catalog()
    observed: list[tuple[set[MoveJobState] | None, str | None]] = []
    list_move_jobs = catalog.list_move_jobs

    def record_list_move_jobs(
        *,
        states: set[MoveJobState] | None = None,
        idempotency_prefix: str | None = None,
    ):
        observed.append((states, idempotency_prefix))
        return list_move_jobs(
            states=states,
            idempotency_prefix=idempotency_prefix,
        )

    monkeypatch.setattr(catalog, "list_move_jobs", record_list_move_jobs)
    handler = build_handlers(
        {
            "hot": PosixDriver(str(tmp_path / "hot")),
            "warm": PosixDriver(str(tmp_path / "warm")),
        },
        catalog,
    )[POLICY_RUN_JOB]
    job = JobEnvelope.create(
        POLICY_RUN_JOB,
        policy_job_payload(
            bucket="empty-bucket",
            prefix="",
            policy="simple",
            threshold=1,
            llm_threshold=None,
            allowed_tiers=("hot", "warm"),
            hot_name_patterns=(),
            warm_name_patterns=(),
            cold_name_patterns=(),
            hot_mime_prefixes=(),
            warm_mime_prefixes=(),
            cold_mime_prefixes=(),
        ),
    )

    asyncio.run(handler(job, _context()))

    assert observed == [
        (
            {state for state in MoveJobState if not state.terminal},
            f"{job.job_id}:",
        )
    ]


def test_policy_handler_isolates_failed_recovery_from_later_healthy_job(
    tmp_path: Path,
) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    catalog = SQLiteCatalog(tmp_path / "catalog.db")
    bucket = "bucket"
    bad_key = "missing.bin"
    healthy_key = "healthy.bin"
    healthy_data = b"healthy recovery"
    warm.put_object(bucket, bad_key, b"disappearing source")
    warm.put_object(bucket, healthy_key, healthy_data)
    job = JobEnvelope.create(
        POLICY_RUN_JOB,
        policy_job_payload(
            bucket=bucket,
            prefix="",
            policy="simple",
            threshold=len(healthy_data),
            llm_threshold=None,
            allowed_tiers=("hot", "warm"),
            hot_name_patterns=(),
            warm_name_patterns=(),
            cold_name_patterns=(),
            hot_mime_prefixes=(),
            warm_mime_prefixes=(),
            cold_mime_prefixes=(),
        ),
        job_id="00000000-0000-4000-8000-000000000006",
    )

    prepared_at = "2000-01-01T00:00:00.000000Z"
    expired_at = "2000-01-01T00:00:01.000000Z"
    for suffix, key in (("01-missing", bad_key), ("02-healthy", healthy_key)):
        source_metadata = warm.stat_object(bucket, key)
        catalog.upsert(bucket, key, source_metadata["size"], "warm")
        catalog.claim_move_job(
            f"{job.job_id}:{suffix}",
            src_tier="warm",
            dst_tier="hot",
            bucket=bucket,
            key=key,
            expected_size=source_metadata["size"],
            source_metadata=source_metadata,
            owner_id="crashed-worker",
            now=prepared_at,
            lease_expires_at=expired_at,
        )
    warm.delete_object(bucket, bad_key)

    try:
        handler = build_handlers({"hot": hot, "warm": warm}, catalog)[POLICY_RUN_JOB]
        with pytest.raises(MoveJobFailedError, match="01-missing") as recovery_failure:
            asyncio.run(handler(job, _context()))
        assert isinstance(recovery_failure.value.__cause__, FileNotFoundError)

        failed = catalog.get_move_job(f"{job.job_id}:01-missing")
        assert failed is not None
        assert failed.state == MoveJobState.FAILED
        assert failed.terminal_reason is not None
        assert "source object is missing" in failed.terminal_reason
        recovered = catalog.get_move_job(f"{job.job_id}:02-healthy")
        assert recovered is not None
        assert recovered.state == MoveJobState.COMPLETED
        assert hot.get_object(bucket, healthy_key) == healthy_data
        with pytest.raises(FileNotFoundError):
            warm.stat_object(bucket, healthy_key)
    finally:
        catalog.close()


def test_policy_handler_exposes_unrelated_lanes_from_one_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        drivers = {
            tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm", "cold", "archive")
        }
        catalog = Catalog()
        hot_started = threading.Event()
        cold_started = threading.Event()
        release_hot = threading.Event()
        starts: list[str] = []
        actions = [
            ActionResult("bucket", "hot-1", "hot", "warm", "test"),
            ActionResult("bucket", "hot-2", "hot", "warm", "test"),
            ActionResult("bucket", "cold-1", "cold", "archive", "test"),
        ]

        monkeypatch.setattr(
            PolicyRunner,
            "plan_once",
            lambda self, bucket, prefix="", dry_run=False: actions,
        )

        def execute(self, action: ActionResult) -> None:
            starts.append(action.key)
            if action.key.startswith("hot"):
                hot_started.set()
                assert release_hot.wait(timeout=2)
            else:
                cold_started.set()

        monkeypatch.setattr(PolicyRunner, "execute", execute)
        throughput = ThroughputController(
            ThroughputConfig(
                max_queue_depth=4,
                tiers={tier: TierLimits() for tier in drivers},
            )
        )
        handler = build_handlers(
            drivers,
            catalog,
            throughput=throughput,
        )[POLICY_RUN_JOB]
        job = JobEnvelope.create(
            POLICY_RUN_JOB,
            policy_job_payload(
                bucket="bucket",
                prefix="",
                policy="simple",
                threshold=1,
                llm_threshold=None,
                allowed_tiers=tuple(drivers),
                hot_name_patterns=(),
                warm_name_patterns=(),
                cold_name_patterns=(),
                hot_mime_prefixes=(),
                warm_mime_prefixes=(),
                cold_mime_prefixes=(),
            ),
        )

        task = asyncio.create_task(handler(job, _context()))
        try:
            await asyncio.wait_for(_wait_for_thread_event(hot_started), timeout=1)
            await asyncio.wait_for(_wait_for_thread_event(cold_started), timeout=1)
            assert set(starts) == {"hot-1", "cold-1"}
            assert "hot-2" not in starts
        finally:
            release_hot.set()
        await asyncio.wait_for(task, timeout=1)
        assert starts[-1] == "hot-2"

    asyncio.run(scenario())


def test_concurrent_duplicate_policy_deliveries_use_distinct_move_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        hot = PosixDriver(str(tmp_path / "hot"), chunk_size=2)
        warm = PosixDriver(str(tmp_path / "warm"), chunk_size=2)
        data = b"concurrent"
        warm.put_object("bucket", "one.txt", data)
        catalog = SQLiteCatalog(tmp_path / "catalog.db")
        catalog.upsert("bucket", "one.txt", len(data), "warm")
        started = threading.Event()
        release = threading.Event()
        coordination_timeout = 5.0
        original_put = hot.put_object_stream

        def blocking_put(*args, **kwargs):
            started.set()
            assert release.wait(timeout=coordination_timeout)
            return original_put(*args, **kwargs)

        monkeypatch.setattr(hot, "put_object_stream", blocking_put)
        handler = build_handlers({"hot": hot, "warm": warm}, catalog)[POLICY_RUN_JOB]
        job = JobEnvelope.create(
            POLICY_RUN_JOB,
            policy_job_payload(
                bucket="bucket",
                prefix="",
                policy="simple",
                threshold=len(data),
                llm_threshold=None,
                allowed_tiers=("hot", "warm"),
                hot_name_patterns=(),
                warm_name_patterns=(),
                cold_name_patterns=(),
                hot_mime_prefixes=(),
                warm_mime_prefixes=(),
                cold_mime_prefixes=(),
            ),
        )

        first = asyncio.create_task(handler(job, _context()))
        await asyncio.wait_for(_wait_for_thread_event(started), timeout=coordination_timeout)
        second = asyncio.create_task(handler(job, _context()))
        try:
            with pytest.raises(MoveJobLeaseError):
                await asyncio.wait_for(second, timeout=coordination_timeout)
        finally:
            release.set()
        await asyncio.wait_for(first, timeout=coordination_timeout)

        assert hot.get_object("bucket", "one.txt") == data
        assert list(warm.list_objects("bucket")) == []
        catalog.close()

    asyncio.run(scenario())


def test_duplicate_with_published_destination_retries_live_move_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        hot = PosixDriver(str(tmp_path / "hot"), chunk_size=2)
        warm = PosixDriver(str(tmp_path / "warm"), chunk_size=2)
        data = b"published-before-transition"
        warm.put_object("bucket", "one.txt", data)
        catalog = SQLiteCatalog(tmp_path / "catalog.db")
        catalog.upsert("bucket", "one.txt", len(data), "warm")
        transition_started = threading.Event()
        release_transition = threading.Event()
        original_transition = Mover._transition

        def block_first_transfer_transition(
            mover,
            move,
            to_state,
            reason,
            *,
            updates=None,
            audit_context=None,
        ):
            if move.state == MoveJobState.PREPARED:
                transition_started.set()
                assert release_transition.wait(timeout=2)
            return original_transition(
                mover,
                move,
                to_state,
                reason,
                updates=updates,
                audit_context=audit_context,
            )

        monkeypatch.setattr(Mover, "_transition", block_first_transfer_transition)
        handler = build_handlers({"hot": hot, "warm": warm}, catalog)[POLICY_RUN_JOB]
        job = JobEnvelope.create(
            POLICY_RUN_JOB,
            policy_job_payload(
                bucket="bucket",
                prefix="",
                policy="simple",
                threshold=len(data),
                llm_threshold=None,
                allowed_tiers=("hot", "warm"),
                hot_name_patterns=(),
                warm_name_patterns=(),
                cold_name_patterns=(),
                hot_mime_prefixes=(),
                warm_mime_prefixes=(),
                cold_mime_prefixes=(),
            ),
        )

        first = asyncio.create_task(handler(job, _context()))
        assert await asyncio.to_thread(transition_started.wait, 5)
        assert hot.get_object("bucket", "one.txt") == data

        second = asyncio.create_task(handler(job, _context()))
        try:
            with pytest.raises(MoveJobLeaseError):
                await asyncio.wait_for(second, timeout=1)
        finally:
            release_transition.set()
        await asyncio.wait_for(first, timeout=1)

        record = catalog.get("bucket", "one.txt")
        assert record is not None
        assert record.tier == "hot"
        catalog.close()

    asyncio.run(scenario())


def _placement_payload(constraints: MovementConstraints | None = None) -> dict:
    return policy_job_payload(
        bucket="bucket",
        prefix="",
        policy="simple",
        threshold=1024,
        llm_threshold=None,
        allowed_tiers=("hot", "warm"),
        hot_name_patterns=(),
        warm_name_patterns=(),
        cold_name_patterns=(),
        hot_mime_prefixes=(),
        warm_mime_prefixes=(),
        cold_mime_prefixes=(),
        movement_constraints=constraints,
    )


@pytest.mark.parametrize(
    "constraints",
    (
        MovementConstraints(),
        MovementConstraints(
            minimum_residency_seconds={"hot": 3600, "warm": 300},
            importance_tiers={"high": ("warm",), "critical": ("hot",)},
        ),
    ),
)
def test_policy_job_payload_round_trips_complete_movement_constraints(
    constraints: MovementConstraints,
) -> None:
    payload = _placement_payload(constraints)
    restored = JobEnvelope.from_bytes(
        JobEnvelope.create(
            POLICY_RUN_JOB,
            payload,
            schema_version=policy_job_schema_version(payload),
        ).to_bytes()
    )

    assert restored.schema_version == JOB_SCHEMA_VERSION_V3
    assert restored.payload["movement_constraints"] == constraints.to_dict()
    assert MovementConstraints.from_mapping(restored.payload["movement_constraints"]) == constraints
    assert policy_job_schema_version({**payload, "embedding_rules": []}) == JOB_SCHEMA_VERSION_V3
    assert "movement_constraints" not in _placement_payload()


@pytest.mark.parametrize("schema_version", (JOB_SCHEMA_VERSION_V1, JOB_SCHEMA_VERSION_V2))
def test_policy_handler_rejects_constraints_in_older_envelopes(
    schema_version: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_policy(*args, **kwargs):
        pytest.fail("invalid controls must be rejected before policy creation")

    monkeypatch.setattr("cognistore.jobs.handlers.build_policy", unexpected_policy)
    handler = build_handlers({}, Catalog())[POLICY_RUN_JOB]
    job = JobEnvelope.create(
        POLICY_RUN_JOB,
        _placement_payload(MovementConstraints()),
        schema_version=schema_version,
    )

    with pytest.raises(InvalidJobError, match="requires schema_version 3"):
        asyncio.run(handler(job, _context()))


@pytest.mark.parametrize(
    "raw_constraints",
    (
        None,
        [],
        {"unknown": 60},
        {"minimum_residency_seconds": {"hot": True}},
        {"minimum_residency_seconds": {"hot": -1}},
        {"minimum_residency_seconds": {"hot": 315360001}},
        {"importance_tiers": {"urgent": ["hot"]}},
        {"importance_tiers": {"high": ["hot", "hot"]}},
    ),
)
def test_policy_handler_rejects_invalid_constraints_before_opening_move_resources(
    raw_constraints: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_policy(*args, **kwargs):
        pytest.fail("invalid controls must be rejected before policy creation")

    monkeypatch.setattr("cognistore.jobs.handlers.build_policy", unexpected_policy)
    payload = _placement_payload()
    payload["movement_constraints"] = raw_constraints
    job = JobEnvelope.create(POLICY_RUN_JOB, payload, schema_version=JOB_SCHEMA_VERSION_V3)
    handler = build_handlers({}, Catalog())[POLICY_RUN_JOB]

    with pytest.raises(InvalidJobError, match="movement_constraints"):
        asyncio.run(handler(job, _context()))


def test_queued_policy_honors_residency_on_fresh_catalog_placement(tmp_path: Path) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    warm = PosixDriver(str(tmp_path / "warm"))
    data = b"small"
    warm.put_object("bucket", "one.txt", data)
    catalog = SQLiteCatalog(tmp_path / "catalog.db")
    try:
        catalog.upsert("bucket", "one.txt", len(data), "warm")
        handler = build_handlers({"hot": hot, "warm": warm}, catalog)[POLICY_RUN_JOB]
        payload = _placement_payload(
            MovementConstraints(minimum_residency_seconds={"warm": 3600})
        )
        job = JobEnvelope.create(POLICY_RUN_JOB, payload, schema_version=JOB_SCHEMA_VERSION_V3)

        asyncio.run(handler(job, _context()))
        asyncio.run(handler(job, _context()))

        record = catalog.get("bucket", "one.txt")
        assert record is not None and record.tier == "warm"
        assert warm.get_object("bucket", "one.txt") == data
        assert list(hot.list_objects("bucket")) == []
        assert catalog.list_move_jobs() == []
        decisions = catalog.list_audit_events(
            AuditQuery(correlation_id=job.correlation_id, job_id=job.job_id)
        )
        assert any("residency" in str(event.details).lower() for event in decisions)
    finally:
        catalog.close()


def test_policy_handler_rejects_unknown_residency_tier_before_policy_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_policy(*args, **kwargs):
        pytest.fail("unknown residency tiers must be rejected before policy creation")

    monkeypatch.setattr("cognistore.jobs.handlers.build_policy", unexpected_policy)
    handler = build_handlers(
        {tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm")}, Catalog(),
    )[POLICY_RUN_JOB]
    payload = _placement_payload(MovementConstraints(minimum_residency_seconds={"wram": 3600}))
    job = JobEnvelope.create(POLICY_RUN_JOB, payload, schema_version=JOB_SCHEMA_VERSION_V3)

    with pytest.raises(InvalidJobError, match="unknown residency tier.*wram"):
        asyncio.run(handler(job, _context()))
