"""Verify full access-history preservation during SQLite-to-PostgreSQL cutover."""

import pytest

from cognistore.db.catalog import SQLCatalog
from tests.conformance.access_import import AccessImportConformance

pytestmark = pytest.mark.integration


class TestPostgresAccessImport(AccessImportConformance):
    @pytest.fixture
    def import_destination(self, postgres_dsn):
        with SQLCatalog(postgres_dsn) as destination:
            yield destination
