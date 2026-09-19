import pytest

from cognistore.db.catalog import SQLCatalog
from tests.conformance.legal_hold_audit_integrity import LegalHoldAuditIntegrityConformance

pytestmark = pytest.mark.integration


class TestPostgresLegalHoldAuditIntegrity(LegalHoldAuditIntegrityConformance):
    @pytest.fixture
    def catalog(self, postgres_dsn):
        with SQLCatalog(postgres_dsn) as catalog:
            yield catalog
