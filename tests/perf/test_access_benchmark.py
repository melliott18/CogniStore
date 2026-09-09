"""Checks that access qualification detects incorrect histories and retention."""

from dataclasses import replace
from datetime import timedelta

import pytest

from cognistore.core.access import AccessConfig, AccessEvent
from cognistore.db.catalog import SQLCatalog
from tests.perf.access_benchmark import (
    AS_OF,
    BUCKET,
    DAY,
    BenchmarkConfig,
    fixture_events,
    latency_summary,
    reference_snapshot,
    run_benchmark,
)


def test_fixture_is_repeatable_and_exercises_boundary_samples_and_namespaces():
    config = BenchmarkConfig(event_count=40, object_count=3)
    events = fixture_events(config)
    assert events == fixture_events(config)
    assert len({event.event_id for event in events}) == 40
    assert {event.kind for event in events} == {"read", "write", "list", "touch"}
    assert all(event.key is None for event in events if event.kind == "list")
    assert any(event.sample_rate == 0.25 for event in events)
    assert events[0].occurred_at == "2026-07-03T00:00:00.000000Z"
    assert events[1].occurred_at == "2026-08-02T00:00:00.000000Z"
    assert events[4].occurred_at == "2026-09-01T00:00:00.000000Z"
    assert events[5].occurred_at == "2026-09-01T00:00:01.000000Z"


def test_oracle_excludes_cutoff_future_and_other_coordinates():
    events = tuple(
        AccessEvent.create(
            kind="read",
            bucket=BUCKET,
            key=key,
            operation_id=f"operation-{index}",
            occurred_at=AS_OF - timedelta(seconds=age),
            sample_rate=rate,
        )
        for index, (age, key, rate) in enumerate(
            ((DAY, "a", 1), (DAY - 1, "a", 0.25), (0, "a", 1), (-1, "a", 1), (0, "b", 1))
        )
    )
    snapshot = reference_snapshot(
        events, "a", AccessConfig(windows_seconds=(DAY,), retention_seconds=DAY)
    )
    assert snapshot.observed_events == 2
    assert snapshot.windows[0].counts == (2, 0, 0, 0)
    assert snapshot.windows[0].estimated_counts == (5.0, 0.0, 0.0, 0.0)
    assert snapshot.minimum_sample_rate == 0.25
    assert snapshot.last_access_at == "2026-09-01T00:00:00.000000Z"


def test_small_qualification_measures_expiration_purge_and_retry_invariance(tmp_path):
    report = run_benchmark(
        tmp_path / "benchmark.sqlite",
        BenchmarkConfig(event_count=40, object_count=3, query_runs=2, prune_batch_size=5),
    )
    assert report["status"] == "passed"
    assert report["replays"]["added_rows"] == 0
    assert report["replays"]["expired_rows_resurrected"] == 0
    assert report["aggregation"]["latency_ms"]["sample_count"] == 10
    assert report["aggregation"]["missing_object"]["observed_events"] == 0
    assert report["aggregation"]["in_retention_unchanged_after_prune"] is True
    assert (
        report["aggregation"]["wide_window_events_before"]
        > report["aggregation"]["wide_window_events_after"]
    )
    volume = report["volume"]
    assert volume["after_insert"]["events"] == 40
    assert volume["after_prune"]["events"] == 40
    assert volume["after_prune"]["active_events"] < 40
    assert volume["after_dedup_horizon"]["events"] == volume["after_prune"]["active_events"]
    assert volume["serialized_jsonl_bytes"] > 0
    assert volume["allocated_growth_bytes"] > 0
    for phase in ("expiration", "advanced_dedup_horizon"):
        batches = report["retention"][phase]["batches"]
        assert all(0 <= batch["expired"] <= 5 and 0 <= batch["purged"] <= 5 for batch in batches)
        assert batches[-1] == {"expired": 0, "purged": 0}


def test_qualification_rejects_incorrect_dal_aggregation(tmp_path, monkeypatch):
    original = SQLCatalog.aggregate_access_events

    def incorrect(self, *args, **kwargs):
        snapshot = original(self, *args, **kwargs)
        return replace(snapshot, observed_events=snapshot.observed_events + 1)

    monkeypatch.setattr(SQLCatalog, "aggregate_access_events", incorrect)
    with pytest.raises(RuntimeError, match="window oracle mismatch"):
        run_benchmark(tmp_path / "bad.sqlite", BenchmarkConfig(event_count=8, object_count=1))


def test_qualification_refuses_existing_database(tmp_path):
    database = tmp_path / "existing.sqlite"
    database.write_bytes(b"must survive")
    with pytest.raises(ValueError, match="must not already exist"):
        run_benchmark(database)
    assert database.read_bytes() == b"must survive"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"event_count": 0},
        {"event_count": 7},
        {"object_count": True},
        {"query_runs": 0},
        {"prune_batch_size": 10001},
        {"history_seconds": DAY},
    ],
)
def test_invalid_workloads_are_rejected(kwargs):
    with pytest.raises(ValueError):
        BenchmarkConfig(**kwargs)


def test_latency_summary_uses_nearest_rank_and_rejects_invalid_samples():
    assert latency_summary([1, 2, 3, 4])["p50"] == 2
    assert latency_summary([1, 2, 3, 4])["p95"] == 4
    for samples in ([], [-1], [float("nan")], [float("inf")]):
        with pytest.raises(ValueError):
            latency_summary(samples)
