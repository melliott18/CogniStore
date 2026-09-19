import pytest

from cognistore.db import SQLCatalog
from tests.conformance.legal_holds import LegalHoldConformance
from tests.unit.test_legal_hold_catalog import assert_process_fence, assert_serialized_pair

pytestmark = pytest.mark.integration


class TestPostgresLegalHolds(LegalHoldConformance):
    @pytest.fixture
    def catalog(self, postgres_dsn):
        with SQLCatalog(postgres_dsn) as catalog:
            yield catalog


def test_postgres_session_fence_serializes_independent_catalog_handles(postgres_dsn):
    with SQLCatalog(postgres_dsn) as catalog, SQLCatalog(postgres_dsn) as other:
        assert_serialized_pair(catalog, other)


def test_postgres_session_fence_is_cross_process(postgres_dsn):
    with SQLCatalog(postgres_dsn) as catalog:
        assert_process_fence(catalog, postgres_dsn)
