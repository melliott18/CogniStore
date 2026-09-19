import pytest

from cognistore.core.catalog import Catalog
from cognistore.db.catalog import SQLCatalog
from tests.conformance.legal_hold_audit_integrity import LegalHoldAuditIntegrityConformance


class TestMemoryLegalHoldAuditIntegrity(LegalHoldAuditIntegrityConformance):
    @pytest.fixture
    def catalog(self):
        return Catalog()


class TestSQLiteLegalHoldAuditIntegrity(LegalHoldAuditIntegrityConformance):
    @pytest.fixture
    def catalog(self, tmp_path):
        with SQLCatalog(tmp_path / "catalog.db") as catalog:
            yield catalog
