"""SQLite access-history cutover contract shared by both SQL destinations."""

from dataclasses import replace
from pathlib import Path

import pytest
import sqlalchemy as sa

from cognistore.core.access import AccessConfig, AccessEvent
from cognistore.db.catalog import SQLCatalog
from cognistore.db.schema import access_events
from cognistore.db.sqlite_import import (
    CatalogImportDestinationNotEmptyError,
    import_sqlite_catalog,
)


class AccessImportConformance:
    @pytest.fixture
    def import_destination(self) -> SQLCatalog:
        raise NotImplementedError

    def test_import_preserves_access_history_and_retry_tombstones(
        self,
        import_destination: SQLCatalog,
        tmp_path: Path,
    ) -> None:
        source_path = tmp_path / "access-source.db"
        observations = (
            AccessEvent.create(
                kind="read",
                bucket="bucket",
                key="object",
                tier="hot",
                source="api",
                operation_id="expired",
                correlation_id="first-request",
                occurred_at="2026-09-07T23:00:00Z",
                sample_rate=0.5,
            ),
            AccessEvent.create(
                kind="write",
                bucket="bucket",
                key="object",
                tier="warm",
                source="driver",
                operation_id="write",
                correlation_id="write-request",
                occurred_at="2026-09-08T11:30:00Z",
                sample_rate=0.25,
            ),
            AccessEvent.create(
                kind="read",
                bucket="bucket\x00雪",
                key="object\x00🌍",
                tier="warm\x00",
                source="driver\x00",
                operation_id="nul\x00",
                correlation_id="request\x00",
                occurred_at="2026-09-08T11:45:00Z",
            ),
            AccessEvent.create(
                kind="list",
                bucket="bucket",
                key=None,
                source="api",
                operation_id="list",
                occurred_at="2026-09-08T11:59:00Z",
            ),
        )
        config = AccessConfig(windows_seconds=(3600, 86400), retention_seconds=172800)
        as_of = "2026-09-08T12:00:00Z"
        with SQLCatalog(source_path) as source:
            # The history is independent of the catalog object's lifecycle.
            source.upsert("bucket", "object", 1, "hot")
            for event in observations:
                source.append_access_event(event)
            source.delete("bucket", "object")
            assert source.prune_access_events("2026-09-08T00:00:00Z") == 1
            coordinates = {(event.bucket, event.key) for event in observations}
            expected = {
                coordinate: source.aggregate_access_events(*coordinate, config=config, as_of=as_of)
                for coordinate in coordinates
            }
            with source.engine.connect() as connection:
                source_rows = list(
                    connection.execute(
                        sa.select(access_events).order_by(access_events.c.event_id)
                    ).mappings()
                )
        source_before = source_path.read_bytes()

        report = import_sqlite_catalog(source_path, import_destination, batch_size=1)

        assert report.access_events == 4
        with import_destination.engine.connect() as connection:
            target_rows = list(
                connection.execute(
                    sa.select(access_events).order_by(access_events.c.event_id)
                ).mappings()
            )
        assert target_rows == source_rows
        for coordinate, snapshot in expected.items():
            assert (
                import_destination.aggregate_access_events(*coordinate, config=config, as_of=as_of)
                == snapshot
            )
        retry = replace(observations[0], occurred_at=as_of, source="retry", sample_rate=1.0)
        assert import_destination.append_access_event(retry) == observations[0]
        assert (
            import_destination.aggregate_access_events(
                "bucket", "object", config=config, as_of=as_of
            ).observed_events
            == 1
        )
        assert source_path.read_bytes() == source_before

    @pytest.mark.parametrize("expired", [False, True])
    def test_import_refuses_an_access_only_destination(
        self,
        import_destination: SQLCatalog,
        tmp_path: Path,
        expired: bool,
    ) -> None:
        source_path = tmp_path / "empty-source.db"
        with SQLCatalog(source_path):
            pass
        original = AccessEvent.create(
            kind="read",
            bucket="bucket",
            key="object",
            operation_id="existing",
            occurred_at="2026-09-08T11:00:00Z",
        )
        import_destination.append_access_event(original)
        if expired:
            assert import_destination.prune_access_events("2026-09-08T12:00:00Z") == 1
        with pytest.raises(CatalogImportDestinationNotEmptyError, match="access_events"):
            import_sqlite_catalog(source_path, import_destination)
        assert import_destination.append_access_event(original) == original
        with import_destination.engine.connect() as connection:
            assert connection.execute(sa.select(access_events.c.expired)).scalar_one() is expired
