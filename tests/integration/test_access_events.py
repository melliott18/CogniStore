"""Run the identical access contract on a real PostgreSQL catalog."""

import pytest

from cognistore.db.catalog import SQLCatalog
from tests.conformance.access_store import AccessStoreConformance

pytestmark = pytest.mark.integration


class TestPostgresAccessStore(AccessStoreConformance):
    @pytest.fixture
    def access_catalog(self, postgres_dsn):
        with SQLCatalog(postgres_dsn) as catalog:
            yield catalog


def test_postgres_retry_and_retention_purge_share_a_locked_observation(postgres_dsn):
    """Pause a retry after INSERT and prove retention cannot remove its row.

    INSERT DO NOTHING followed by SELECT used to leave an unlocked gap: the
    purge completed during the pause and the retry raised NoResultFound.
    """
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic

    import sqlalchemy as sa

    from cognistore.db.schema import access_events
    from tests.conformance.access_store import event

    with SQLCatalog(postgres_dsn) as catalog:
        original = event("purge-race", "2026-09-08T11:00:00Z")
        catalog.append_access_event(original)
        assert catalog.prune_access_events("2026-09-08T11:30:00Z", retention_seconds=3600) == 1
        inserted = Event()
        release_retry = Event()
        purge_started = Event()
        purge_pids = []

        def pause_retry(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.startswith("INSERT INTO access_events"):
                inserted.set()
                assert release_retry.wait(10), "retry was never released"

        def identify_purge(connection, _cursor, statement, _parameters, _context, _many):
            if statement.startswith("DELETE FROM access_events"):
                purge_pids.append(
                    connection.execute(sa.select(sa.func.pg_backend_pid())).scalar_one()
                )
                purge_started.set()

        sa.event.listen(catalog.engine, "after_cursor_execute", pause_retry)
        sa.event.listen(catalog.engine, "before_cursor_execute", identify_purge)
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                retry = executor.submit(catalog.append_access_event, event("purge-race"))
                try:
                    assert inserted.wait(10), "retry never reached its INSERT"
                    purge = executor.submit(
                        catalog.prune_access_events,
                        "2026-09-08T13:00:00Z",
                        retention_seconds=3600,
                    )
                    assert purge_started.wait(10), "purge never reached its DELETE"
                    deadline = monotonic() + 10
                    with catalog.engine.connect() as monitor:
                        while True:
                            blockers = monitor.execute(
                                sa.select(sa.func.pg_blocking_pids(purge_pids[0]))
                            ).scalar_one()
                            if blockers:
                                break
                            assert not purge.done(), "purge removed a row still needed by the retry"
                            assert monotonic() < deadline, (
                                "purge never waited on the retry row lock"
                            )
                            release_retry.wait(0.01)
                finally:
                    release_retry.set()
                assert retry.result(timeout=10) == original
                assert purge.result(timeout=10) == 1
            with catalog.engine.connect() as connection:
                assert (
                    connection.execute(
                        sa.select(sa.func.count()).select_from(access_events)
                    ).scalar_one()
                    == 0
                )
        finally:
            release_retry.set()
            sa.event.remove(catalog.engine, "after_cursor_execute", pause_retry)
            sa.event.remove(catalog.engine, "before_cursor_execute", identify_purge)
