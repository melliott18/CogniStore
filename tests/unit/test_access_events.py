from dataclasses import FrozenInstanceError, replace
from datetime import datetime

import pytest
import sqlalchemy as sa

from cognistore.core.access import (
    AccessConfig,
    AccessEvent,
    access_timestamp,
    compute_access_features,
)
from cognistore.core.catalog import Catalog
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.db.migrations import MigrationManager
from cognistore.db.schema import access_events
from tests.conformance.access_store import AS_OF, CONFIG, AccessStoreConformance, event


class TestMemoryAccessStore(AccessStoreConformance):
    @pytest.fixture
    def access_catalog(self):
        return Catalog()


class TestSQLiteAccessStore(AccessStoreConformance):
    @pytest.fixture
    def access_catalog(self, tmp_path):
        with SQLiteCatalog(tmp_path / "access.db") as catalog:
            yield catalog


@pytest.mark.parametrize(
    "values",
    [
        {"sample_rate": 0},
        {"sample_rate": -0.1},
        {"sample_rate": True},
        {"sample_rate": float("nan")},
        {"sample_rate": float("inf")},
        {"sample_rate": 1.1},
        {"sample_rate": 5e-324},
        {"sample_rate": 1e-10},
        {"sample_rate": 10**1000},
        {"windows_seconds": ()},
        {"windows_seconds": (60, 60)},
        {"windows_seconds": (True,)},
        {"windows_seconds": (1.5,)},
        {"windows_seconds": (2592001,)},
        {"windows_seconds": tuple(range(1, 18))},
        {"retention_seconds": 0},
        {"retention_seconds": True},
        {"freshness_seconds": 0},
        {"freshness_seconds": float("inf")},
    ],
)
def test_access_configuration_is_validated(values):
    with pytest.raises(ValueError):
        AccessConfig(**values)


def test_event_validation_normalization_and_immutability():
    observed = event("stable", "2026-09-08T05:00:00-07:00")
    assert observed.occurred_at == "2026-09-08T12:00:00.000000Z"
    assert observed.event_id == event("stable").event_id
    with pytest.raises(FrozenInstanceError):
        observed.kind = "write"
    for change in (
        {"kind": "delete"},
        {"kind": "read", "key": None},
        {"schema_version": True},
        {"schema_version": 1.0},
        {"bucket": ""},
        {"operation_id": ""},
        {"correlation_id": ""},
        {"source": ""},
        {"source": "x" * 129},
        {"tier": ""},
        {"event_id": "x" * 257},
        {"bucket": "\ud800"},
    ):
        with pytest.raises(ValueError):
            replace(observed, **change)
    assert AccessEvent.create(kind="list", bucket="bucket").key is None


@pytest.mark.parametrize("value", ["not-a-date", "2026-09-08T12:00:00", datetime(2026, 9, 8), 123])
def test_access_timestamps_require_valid_aware_instants(value):
    with pytest.raises(ValueError):
        access_timestamp(value)


def test_absent_and_unavailable_history_are_distinct_and_safe():
    class UnavailableCatalog:
        def aggregate_access_events(self, *_args, **_kwargs):
            raise RuntimeError("password=secret should not enter policy features")

    missing = compute_access_features(None, "bucket", "object", CONFIG, AS_OF).to_dict()
    unavailable = compute_access_features(
        UnavailableCatalog(), "bucket", "object", CONFIG, AS_OF
    ).to_dict()
    assert missing["freshness"] == "missing"
    assert missing["reason"] == "history_not_configured"
    assert unavailable["freshness"] == "unavailable"
    assert unavailable["missing"] is True
    assert unavailable["partial"] is True
    assert unavailable["reason"] == "history_unavailable"
    assert "secret" not in str(unavailable)


def test_sqlite_reopen_read_only_and_migration_preserve_history(tmp_path):
    path = tmp_path / "access.db"
    original = event("persistent")
    with SQLiteCatalog(path) as catalog:
        catalog.append_access_event(original)
        assert MigrationManager().current(catalog.engine) == "0013_audit_integrity"
        assert {
            index["name"] for index in sa.inspect(catalog.engine).get_indexes("access_events")
        } == {"access_events_object_time_idx", "access_events_retention_idx"}
    with SQLiteCatalog(path, read_only=True) as readonly:
        assert (
            compute_access_features(
                readonly, "bucket", "object", CONFIG, AS_OF
            ).snapshot.observed_events
            == 1
        )
        with pytest.raises(PermissionError):
            readonly.append_access_event(original)
        with pytest.raises(PermissionError):
            readonly.prune_access_events(AS_OF)
    with SQLiteCatalog(path) as reopened:
        assert reopened.append_access_event(original) == original
        with reopened.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(access_events)
                ).scalar_one()
                == 1
            )


def test_access_migration_upgrade_existing_catalog_and_downgrade(tmp_path):
    path = tmp_path / "migrate.db"
    with SQLiteCatalog(path) as catalog:
        catalog.upsert("bucket", "object", 1, "hot")
        manager = MigrationManager()
        manager.downgrade(catalog.engine, "0007_tier_pools")
        assert not sa.inspect(catalog.engine).has_table("access_events")
        manager.upgrade(catalog.engine)
        assert catalog.get("bucket", "object").size == 1
        assert (
            catalog.aggregate_access_events(
                "bucket", "object", config=CONFIG, as_of=AS_OF
            ).observed_events
            == 0
        )


def test_sql_aggregation_does_not_enumerate_event_rows(tmp_path):
    with SQLiteCatalog(tmp_path / "query.db") as catalog:
        catalog.append_access_event(event("read"))
        statements = []
        sa.event.listen(
            catalog.engine,
            "before_cursor_execute",
            lambda _conn, _cursor, statement, _parameters, _context, _many: statements.append(
                statement
            ),
        )
        assert (
            catalog.aggregate_access_events(
                "bucket", "object", config=CONFIG, as_of=AS_OF
            ).observed_events
            == 1
        )
        assert len(statements) == 1
        assert "sum(CASE" in statements[0]
        assert "SELECT access_events.event_id" not in statements[0]
