from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from cognistore.core.audit import AuditContext
from cognistore.core.catalog import Catalog, CatalogStore
from cognistore.core.placement_controls import (
    ImportanceTag,
    MovementConstraintError,
    assert_move_allowed,
)
from cognistore.db.catalog import SQLCatalog
from cognistore.db.migrations import MigrationManager
from cognistore.db.schema import object_placements
from cognistore.db.sqlite_import import import_sqlite_catalog
from tests.catalog_fixtures import create_prototype_sqlite_catalog


@pytest.fixture(params=["memory", "sqlite"])
def catalog(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[CatalogStore]:
    if request.param == "memory":
        yield Catalog()
    else:
        with SQLCatalog(tmp_path / "catalog.db") as value:
            yield value


def test_importance_and_audit_are_atomic_on_write_failure(
    catalog: CatalogStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog.upsert("bucket", "object", 1, "hot")
    previous = catalog.get("bucket", "object")
    tag = ImportanceTag("critical", "user", "alice", "retain", "2026-01-01T00:00:00Z")

    def reject(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit unavailable")

    method = "_insert_audit_event" if isinstance(catalog, SQLCatalog) else "append_audit_event"
    monkeypatch.setattr(catalog, method, reject)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        catalog.set_importance(
            "bucket", "object", tag, audit_context=AuditContext("tag", "user", "alice"),
        )
    assert catalog.get("bucket", "object") == previous
    assert catalog.list_audit_events() == []


def test_migration_preserves_conservative_residency_and_unknown_history(tmp_path: Path) -> None:
    path = tmp_path / "catalog.db"
    with SQLCatalog(path) as catalog:
        catalog.upsert("bucket", "known", 1, "hot")
        catalog.upsert("bucket", "unknown", 1, "hot")
        MigrationManager().downgrade(catalog.engine, "0008_access_events")
        with catalog.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE object_placements SET updated_at = CASE "
                "WHEN object_id = (SELECT object_id FROM objects WHERE object_key = 'known') "
                "THEN '2026-01-01T00:00:00.000000Z' ELSE '1970-01-01T00:00:00.000000Z' END"
            )
    with SQLCatalog(path) as upgraded:
        known = upgraded.get("bucket", "known")
        unknown = upgraded.get("bucket", "unknown")
        assert known is not None and unknown is not None
        assert known.placement_started_at == "2026-01-01T00:00:00.000000Z"
        assert unknown.placement_started_at is None
        assert known.importance is None and known.importance_revision == 0
        with pytest.raises(MovementConstraintError, match="unknown"):
            assert_move_allowed(unknown, "cold", tier_metadata={"minimum_residency_seconds": 60})
        with upgraded.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_native_import_retains_trusted_importance_and_placement_start(tmp_path: Path) -> None:
    source_path = tmp_path / "source.db"
    tag = ImportanceTag("critical", "user", "alice", "retain", "2026-01-01T00:00:00Z")
    with SQLCatalog(source_path) as source:
        source.upsert("bucket", "object", 1, "hot")
        source.set_importance(
            "bucket", "object", tag, audit_context=AuditContext("tag", "user", "alice"),
        )
        expected = source.get("bucket", "object")
        source.upsert("bucket", "unknown", 1, "hot")
        with source.engine.begin() as connection:
            connection.execute(sa.update(object_placements).where(
                object_placements.c.object_id == source._object_id("bucket", "unknown")
            ).values(placement_started_at=None))
    with SQLCatalog(tmp_path / "destination.db") as destination:
        import_sqlite_catalog(source_path, destination)
        assert destination.get("bucket", "object") == expected
        unknown = destination.get("bucket", "unknown")
        assert unknown is not None and unknown.placement_started_at is None
        assert len(destination.list_audit_events()) == 1
    with SQLCatalog(source_path, read_only=True) as reopened:
        assert reopened.get("bucket", "object") == expected


@pytest.mark.parametrize("layout", ["prototype", "normalized"])
def test_old_imports_preserve_only_available_residency_history(tmp_path: Path, layout: str) -> None:
    source_path = tmp_path / "source.db"
    if layout == "prototype":
        create_prototype_sqlite_catalog(source_path)
        key = "reports/annual.pdf"
        expected_start = None
    else:
        key = "object"
        expected_start = "2026-01-01T00:00:00.000000Z"
        with SQLCatalog(source_path) as source:
            source.upsert("bucket", key, 1, "hot")
            MigrationManager().downgrade(source.engine, "0008_access_events")
            with source.engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE object_placements SET updated_at = '2026-01-01T00:00:00.000000Z'"
                )
    with SQLCatalog(tmp_path / "destination.db") as destination:
        import_sqlite_catalog(source_path, destination)
        imported = destination.get("bucket", key)
        assert imported is not None
        assert imported.importance is None and imported.importance_revision == 0
        assert imported.placement_started_at == expected_start


@pytest.mark.parametrize("sql", [
    "ALTER TABLE objects DROP COLUMN importance_revision",
    "ALTER TABLE object_placements DROP COLUMN placement_started_at",
    "UPDATE objects SET importance_revision = -1",
    "UPDATE object_placements SET placement_started_at = '2026-01-01T00:00:00'",
])
def test_import_rejects_partial_or_invalid_controls_atomically(tmp_path: Path, sql: str) -> None:
    from cognistore.db.sqlite_import import SQLiteCatalogImportError

    source_path = tmp_path / "source.db"
    with SQLCatalog(source_path) as source:
        source.upsert("bucket", "object", 1, "hot")
        with source.engine.begin() as connection:
            connection.exec_driver_sql(sql)
    with SQLCatalog(tmp_path / "destination.db") as destination:
        with pytest.raises(SQLiteCatalogImportError):
            import_sqlite_catalog(source_path, destination)
        assert destination.list("bucket") == []
        assert destination.list_tiers() == []
