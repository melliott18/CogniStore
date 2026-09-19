from __future__ import annotations

import pytest
import sqlalchemy as sa

from cognistore.db import CatalogSchemaNotInstalledError, MigrationManager, SQLCatalog
from cognistore.db.migrations import catalog_schema_exists
from cognistore.db.schema import catalog_tenant, object_placements
from tests.conformance.tenant_catalog import TenantCatalogConformance

pytestmark = pytest.mark.integration


class TestPostgresTenantCatalog(TenantCatalogConformance):
    @pytest.fixture
    def tenant_catalogs(self, postgres_dsn):
        with SQLCatalog(postgres_dsn) as catalog:
            yield catalog, catalog.for_tenant("alice"), catalog.for_tenant("bob")


def test_tenant_schema_owns_version_marker_tables_and_foreign_keys(postgres_dsn):
    with SQLCatalog(postgres_dsn) as root:
        root.upsert("bucket", "legacy", size=1, tier="hot")
        alice = root.for_tenant("alice")
        bob = root.for_tenant("bob")
        alice.upsert("bucket", "alice-only", size=1, tier="hot")
        bob.register_tier("hot")
        for catalog in (alice, bob):
            assert MigrationManager().is_at_head(catalog.engine)
            assert catalog_schema_exists(catalog.engine)
            with catalog.engine.connect() as connection:
                assert connection.execute(sa.select(catalog_tenant.c.tenant_id)).scalar_one() == catalog.tenant_id
                assert connection.exec_driver_sql("SELECT current_schema()").scalar_one() == catalog.schema_name
        with alice.engine.connect() as connection:
            placement = dict(connection.execute(sa.select(object_placements)).mappings().one())
        with pytest.raises(sa.exc.IntegrityError):
            with bob.engine.begin() as connection:
                # Reusing another tenant's UUID cannot satisfy a local FK.
                connection.execute(sa.insert(object_placements).values(**placement))
        assert bob.list("bucket") == []
        assert root.get("bucket", "legacy").size == 1
        with bob.engine.begin() as connection:
            quoted = connection.dialect.identifier_preparer.quote(bob.schema_name)
            connection.exec_driver_sql(f"DROP TABLE {quoted}.objects CASCADE")
        assert not catalog_schema_exists(bob.engine)
        assert catalog_schema_exists(root.engine)
        with pytest.raises(CatalogSchemaNotInstalledError):
            SQLCatalog(postgres_dsn, tenant_id="bob", migrate=False)


def test_tenant_schema_initializes_without_a_public_catalog(postgres_dsn):
    with SQLCatalog(postgres_dsn, tenant_id="alice") as alice:
        alice.upsert("bucket", "key", size=58, tier="hot")
        with alice.engine.connect() as connection:
            assert not sa.inspect(connection).has_table("objects", schema="public")
            assert connection.exec_driver_sql(
                "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace "
                "WHERE e.extname='vector'"
            ).scalar_one() == "public"
    with SQLCatalog(postgres_dsn, tenant_id="alice", read_only=True) as reopened:
        assert reopened.get("bucket", "key").size == 58
        assert reopened.tenant_id == "alice"
