"""Offline catalog cutover must retain legal hold enforcement and history."""

from pathlib import Path

import pytest

from cognistore.core.audit import AuditContext
from cognistore.core.legal_holds import LegalHoldError
from cognistore.db import SQLCatalog
from cognistore.db.sqlite_import import import_sqlite_catalog

pytestmark = pytest.mark.integration


def test_sqlite_to_postgres_retains_holds_and_release_history(
    tmp_path: Path, postgres_dsn: str,
) -> None:
    source_path = tmp_path / "source.db"
    actor = AuditContext("import-62", "user", "custodian")
    with SQLCatalog(source_path) as source:
        source.upsert("records", "evidence", size=4, tier="hot")
        active = source.place_legal_hold(
            "records", key="evidence", reason="preserve", context=actor,
        )
        old = source.place_legal_hold("old", reason="preserve", context=actor)
        old = source.release_legal_hold(old.hold_id, reason="withdrawn", context=actor)
        audit = source.list_audit_events()
    with SQLCatalog(postgres_dsn) as destination:
        report = import_sqlite_catalog(source_path, destination)
        assert report.legal_holds == 2
        assert destination.list_legal_holds() == [active, old]
        assert destination.list_audit_events() == audit
        with pytest.raises(LegalHoldError):
            destination.delete("records", "evidence")
        assert destination.get("records", "evidence") is not None
        destination.release_legal_hold(active.hold_id, reason="complete", context=actor)
        destination.delete("records", "evidence")
        assert destination.get("records", "evidence") is None
        assert destination.prune_audit_events("2099-01-01T00:00:00Z") == 0
