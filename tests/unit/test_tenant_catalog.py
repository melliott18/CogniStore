from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import sqlalchemy as sa

from cognistore.auth.tenancy import TenantIsolationError, tenant_context
from cognistore.core.catalog import Catalog
from cognistore.db import MigrationManager, SQLCatalog
from cognistore.db.engine import tenant_catalog_locator
from cognistore.db.schema import catalog_tenant, objects
from cognistore.db.sqlite_import import import_sqlite_catalog
from tests.conformance.tenant_catalog import TenantCatalogConformance


class TestMemoryTenantCatalog(TenantCatalogConformance):
    @pytest.fixture
    def tenant_catalogs(self):
        catalog = Catalog()
        return catalog, catalog.for_tenant("alice"), catalog.for_tenant("bob")


class TestSQLiteTenantCatalog(TenantCatalogConformance):
    @pytest.fixture
    def tenant_catalogs(self, tmp_path):
        with SQLCatalog(tmp_path / "catalog.db") as catalog:
            yield catalog, catalog.for_tenant("alice"), catalog.for_tenant("bob")


def test_sqlite_owner_survives_restart_and_blocks_foreign_partition_adoption(tmp_path):
    path = tmp_path / "catalog.db"
    with SQLCatalog(path) as catalog:
        alice = catalog.for_tenant("alice")
        alice.upsert("bucket", "key", size=58, tier="hot")
        alice_path = Path(alice.db_path)
    with SQLCatalog(path, tenant_id="alice", read_only=True) as reopened:
        assert reopened.get("bucket", "key").size == 58
        assert reopened.tenant_id == "alice"
    with pytest.raises(TenantIsolationError):
        SQLCatalog(alice_path)
    bob_path, _ = tenant_catalog_locator(path, "bob")
    bob_path.parent.mkdir(parents=True)
    shutil.copyfile(alice_path, bob_path)
    with pytest.raises(TenantIsolationError):
        SQLCatalog(path, tenant_id="bob")


def test_legacy_catalog_is_migrated_only_to_default_owner(tmp_path):
    path = tmp_path / "legacy.db"
    with SQLCatalog(path) as catalog:
        catalog.upsert("bucket", "key", size=58, tier="hot")
        MigrationManager().downgrade(catalog.engine, "0011_policy_budgets")
    with SQLCatalog(path) as reopened:
        assert reopened.get("bucket", "key").size == 58
        assert reopened.tenant_id == "default"
        with reopened.engine.connect() as connection:
            assert connection.execute(sa.select(catalog_tenant.c.tenant_id)).scalar_one() == "default"
        assert reopened.for_tenant("alice").list("bucket") == []


def test_empty_or_tampered_owner_marker_is_rejected(tmp_path):
    path = tmp_path / "catalog.db"
    with SQLCatalog(path, tenant_id="alice") as catalog:
        with catalog.engine.begin() as connection:
            connection.execute(sa.delete(catalog_tenant))
    with pytest.raises(TenantIsolationError):
        SQLCatalog(path, tenant_id="alice")


def test_cached_sql_engine_and_connection_cannot_cross_active_tenants(tmp_path):
    with SQLCatalog(tmp_path / "catalog.db", tenant_id="alice") as alice:
        engine = alice.engine
        with engine.connect() as connection:
            with tenant_context("bob"):
                with pytest.raises(TenantIsolationError):
                    alice.engine
                with pytest.raises(TenantIsolationError):
                    connection.execute(sa.select(objects))
                with pytest.raises(TenantIsolationError):
                    connection.exec_driver_sql("SELECT * FROM objects")
                with engine.connect() as cached:
                    with pytest.raises(TenantIsolationError):
                        cached.execute(sa.select(objects))


def test_parallel_catalog_partition_creation_is_stable(tmp_path):
    with SQLCatalog(tmp_path / "catalog.db") as catalog:
        with ThreadPoolExecutor(max_workers=4) as workers:
            values = list(workers.map(lambda _: catalog.for_tenant("alice"), range(12)))
        assert all(value is values[0] for value in values)
        values[0].upsert("bucket", "key", size=1, tier="hot")
        values[0].close()
        replacement = catalog.for_tenant("alice")
        assert replacement is not values[0]
        assert replacement.get("bucket", "key").size == 1


def test_import_cannot_reassign_tenant_ownership(tmp_path):
    source_base = tmp_path / "source.db"
    with SQLCatalog(source_base, tenant_id="alice") as source:
        source.upsert("bucket", "key", size=1, tier="hot")
        source_path = source.db_path
    with SQLCatalog(tmp_path / "target.db", tenant_id="bob") as destination:
        with pytest.raises(TenantIsolationError):
            import_sqlite_catalog(source_path, destination)
        assert destination.list("bucket") == []
    with SQLCatalog(tmp_path / "target.db", tenant_id="alice") as destination:
        assert import_sqlite_catalog(source_path, destination).objects == 1
        assert destination.get("bucket", "key").size == 1


@pytest.mark.parametrize("tenant_id", ["../alice", "a/b", "a\\b", "a\x00b", "", "'public'", "a" * 129])
def test_partition_names_reject_unsafe_tenant_ids(tmp_path, tenant_id):
    with pytest.raises(ValueError):
        SQLCatalog(tmp_path / "catalog.db", tenant_id=tenant_id)


def test_import_current_catalog_without_owner_fails_closed(tmp_path):
    source_path = tmp_path / "source.db"
    with SQLCatalog(source_path) as source:
        source.upsert("bucket", "key", size=1, tier="hot")
        with source.engine.begin() as connection:
            connection.execute(sa.schema.DropTable(catalog_tenant))
    with SQLCatalog(tmp_path / "target.db") as destination:
        with pytest.raises(TenantIsolationError):
            import_sqlite_catalog(source_path, destination)
        assert destination.list("bucket") == []
