"""One access-history contract exercised by memory, SQLite, and PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from cognistore.core.access import AccessConfig, AccessEvent, compute_access_features
from cognistore.core.catalog import CatalogStore

AS_OF = "2026-09-08T12:00:00Z"
CONFIG = AccessConfig(windows_seconds=(60, 3600), retention_seconds=7200, freshness_seconds=60)


def event(operation: str, when: str = AS_OF, **values: object) -> AccessEvent:
    arguments = {"kind": "read", "bucket": "bucket", "key": "object", **values}
    return AccessEvent.create(operation_id=operation, occurred_at=when, **arguments)  # type: ignore[arg-type]


class AccessStoreConformance:
    @pytest.fixture
    def access_catalog(self) -> CatalogStore:
        raise NotImplementedError

    def test_event_time_windows_late_arrival_and_namespace_isolation(self, access_catalog):
        # Deliberately append out of event-time order. Boundaries are exclusive
        # below and inclusive at as_of; future observations cannot affect replay.
        events = [
            event("now"),
            event("minute-boundary", "2026-09-08T11:59:00Z"),
            event("retention-boundary", "2026-09-08T10:00:00Z"),
            event("hour-boundary", "2026-09-08T11:00:00Z"),
            event("future", "2026-09-08T12:00:00.000001Z"),
            event("old-within-retention", "2026-09-08T10:30:00Z"),
            event("sampled-write", "2026-09-08T11:59:30Z", kind="write", sample_rate=0.25),
            event("touch", "2026-09-08T11:59:59Z", kind="touch"),
            event("other-object", key="other"),
            event("other-bucket", bucket="other"),
            event("list", key=None, kind="list"),
        ]
        for observation in events:
            access_catalog.append_access_event(observation)
        first = compute_access_features(access_catalog, "bucket", "object", CONFIG, AS_OF)
        value = first.to_dict()
        assert value["windows"] == {
            "60": {"read": 1, "write": 1, "list": 0, "touch": 1},
            "3600": {"read": 2, "write": 1, "list": 0, "touch": 1},
        }
        assert value["estimated_windows"]["60"]["write"] == 4.0
        assert value["observed_events"] == 6
        assert value["observed_since"] == "2026-09-08T10:30:00.000000Z"
        assert value["last_access_at"] == "2026-09-08T12:00:00.000000Z"
        assert value["recency_seconds"] == 0
        assert value["freshness"] == "fresh"
        assert value["partial"] is True
        assert value["sampling"] == {"configured_rate": 1.0, "minimum_rate": 0.25, "sampled": True}
        assert first == compute_access_features(access_catalog, "bucket", "object", CONFIG, AS_OF)
        namespace = compute_access_features(access_catalog, "bucket", None, CONFIG, AS_OF).to_dict()
        assert namespace["windows"]["60"] == {"read": 0, "write": 0, "list": 1, "touch": 0}
        # New evidence for an earlier instant deterministically updates that
        # instant, while future data still cannot leak into the result.
        access_catalog.append_access_event(event("late", "2026-09-08T11:59:45Z"))
        updated = compute_access_features(
            access_catalog, "bucket", "object", CONFIG, AS_OF
        ).to_dict()
        assert updated["windows"]["60"]["read"] == 2
        assert updated["observed_events"] == 7

    def test_retries_preserve_first_event_and_conflicts_are_rejected(self, access_catalog):
        original = event("retry", "2026-09-08T11:59:00Z", tier="hot", source="api")
        assert access_catalog.append_access_event(original) == original
        retry = event("retry", tier="warm", source="driver", correlation_id="new-request")
        assert retry.event_id == original.event_id
        assert access_catalog.append_access_event(retry) == original
        assert (
            access_catalog.aggregate_access_events(
                "bucket", "object", config=CONFIG, as_of=AS_OF
            ).observed_events
            == 1
        )
        with pytest.raises(ValueError, match="conflicts"):
            access_catalog.append_access_event(replace(original, key="collision"))
        with pytest.raises(ValueError, match="conflicts"):
            access_catalog.append_access_event(replace(original, kind="touch"))
        assert access_catalog.append_access_event(replace(original, sample_rate=0.5)) == original
        assert access_catalog.append_access_event(original) == original

    def test_concurrent_retries_count_once(self, access_catalog):
        original = event("concurrent")
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(access_catalog.append_access_event, [original] * 12))
        assert results == [original] * 12
        assert (
            access_catalog.aggregate_access_events(
                "bucket", "object", config=CONFIG, as_of=AS_OF
            ).observed_events
            == 1
        )

    def test_retention_is_bounded_and_keeps_retry_tombstones(self, access_catalog):
        original = event("expired", "2026-09-08T11:00:00Z")
        access_catalog.append_access_event(original)
        access_catalog.append_access_event(event("boundary", "2026-09-08T11:30:00Z"))
        access_catalog.append_access_event(event("recent"))
        assert (
            access_catalog.prune_access_events(
                "2026-09-08T11:30:00Z", limit=1, retention_seconds=7200
            )
            == 1
        )
        assert access_catalog.append_access_event(event("expired")) == original
        assert (
            access_catalog.aggregate_access_events(
                "bucket", "object", config=CONFIG, as_of=AS_OF
            ).observed_events
            == 2
        )
        assert (
            access_catalog.prune_access_events(
                "2026-09-08T11:30:00Z", limit=1, retention_seconds=7200
            )
            == 0
        )
        # Once older than the extra horizon, the original tombstone is removed.
        # Processing counts include purges so a bounded draining loop progresses.
        assert (
            access_catalog.prune_access_events(
                "2026-09-08T13:30:01Z", limit=1, retention_seconds=7200
            )
            == 2
        )
        assert (
            access_catalog.prune_access_events(
                "2026-09-08T13:30:01Z", limit=1, retention_seconds=7200
            )
            == 2
        )
        assert (
            access_catalog.prune_access_events(
                "2026-09-08T13:30:01Z", limit=1, retention_seconds=7200
            )
            == 0
        )
        assert compute_access_features(access_catalog, "bucket", "object", CONFIG, AS_OF).missing

    def test_sparse_expired_and_unknown_history_are_explicitly_partial(self, access_catalog):
        missing = compute_access_features(
            access_catalog, "bucket", "object", CONFIG, AS_OF
        ).to_dict()
        assert missing["freshness"] == "missing"
        assert missing["recency_seconds"] is None
        assert missing["missing"] is True
        assert missing["partial"] is True
        access_catalog.append_access_event(event("stale", "2026-09-08T11:58:59Z"))
        stale = compute_access_features(access_catalog, "bucket", "object", CONFIG, AS_OF).to_dict()
        assert stale["freshness"] == "stale"
        assert stale["recency_seconds"] == 61
        assert stale["missing"] is False
        assert stale["partial"] is True
        access_catalog.prune_access_events(AS_OF)
        expired = compute_access_features(
            access_catalog, "bucket", "object", CONFIG, AS_OF
        ).to_dict()
        assert expired["missing"] is True
        assert expired["partial"] is True
        assert expired["freshness"] == "missing"

    def test_coordinate_encoding_and_object_deletion_do_not_change_history(self, access_catalog):
        observation = event(
            "nul-operation\x00",
            bucket="b\x00雪",
            key="key\x00🌍",
            source="source\x00",
            tier="tier\x00",
        )
        access_catalog.append_access_event(observation)
        access_catalog.upsert(observation.bucket, observation.key, 1, "hot")
        access_catalog.delete(observation.bucket, observation.key)
        assert (
            access_catalog.aggregate_access_events(
                observation.bucket, observation.key, config=CONFIG, as_of=AS_OF
            ).observed_events
            == 1
        )
        assert access_catalog.append_access_event(observation) == observation

    @pytest.mark.parametrize("limit", [0, -1, True, 1.5, 10001])
    def test_invalid_prune_limit(self, access_catalog, limit):
        with pytest.raises(ValueError, match="limit"):
            access_catalog.prune_access_events(AS_OF, limit=limit)
