"""SQL export pages bound the backend payload work as well as the response."""

from cognistore.core.audit import AuditContext, AuditEvent
from cognistore.db import SQLCatalog


def test_sql_export_only_materializes_payloads_in_requested_page(tmp_path, monkeypatch):
    with SQLCatalog(tmp_path / "audit.db") as catalog:
        for number in range(20):
            catalog.append_audit_event(AuditEvent.create(
                "manual.action", "succeeded", AuditContext("seed", "system", "export-test"),
                details={"number": number},
            ))
        checkpoint = catalog.audit_checkpoint()
        decode = SQLCatalog._audit_event_from_row
        decoded = []

        def record_decode(row):
            decoded.append(str(row["event_id"]))
            return decode(row)

        monkeypatch.setattr(SQLCatalog, "_audit_event_from_row", staticmethod(record_decode))
        page = catalog.export_audit_evidence(checkpoint=checkpoint, after_sequence=10, limit=2)
        assert [record["sequence"] for record in page["records"]] == [11, 12]
        assert page["next_sequence"] == 12 and page["complete"] is False
        assert len(decoded) == 2
        assert set(decoded) == {record["event_id"] for record in page["records"]}
