"""The read-only consistency source has a separate protected evidence journal."""
from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from cognistore.core.audit_integrity import GENESIS_HASH, digest
from cognistore.core.catalog import Catalog
from cognistore.core.consistency import ConsistencyScanner, ScanScope
from cognistore.core.consistency_report import (
    REPORT_VERSION,
    append_event,
    create_report,
    encode,
    export_report,
    open_report,
    read_state,
    report_summary,
    verify_report_integrity,
)
from cognistore.drivers.posix_driver import PosixDriver


@pytest.fixture
def report(tmp_path: Path) -> Path:
    path = tmp_path / "report.sqlite3"
    state = {
        "version": REPORT_VERSION, "scan_id": "consistency-63",
        "scope": {"tenant_id": "tenant-a"}, "binding_id": "binding-63",
        "phase": "completed", "checked": 0,
        "started_at": "2026-09-18T00:00:00Z", "finished_at": "2026-09-18T00:00:01Z",
    }
    create_report(path, state, "tester")
    with open_report(path) as connection, connection:
        append_event(connection, state, "consistency.completed", "tester")
    return path


@pytest.mark.parametrize("statement", [
    "UPDATE audit SET payload='{}' WHERE sequence=1",
    "DELETE FROM audit WHERE sequence=1",
    "INSERT OR REPLACE INTO audit SELECT * FROM audit WHERE sequence=1",
    "UPDATE audit_head SET sequence=0, entry_hash='forged' WHERE id=1",
    "DELETE FROM audit_head",
    "INSERT OR REPLACE INTO audit_head SELECT * FROM audit_head",
])
def test_consistency_evidence_denies_direct_update_delete_replace(report, statement):
    with sqlite3.connect(report) as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(statement)
    with open_report(report, read_only=True) as connection:
        assert verify_report_integrity(connection)["sequence"] == 2


@pytest.mark.parametrize("tamper", ["alter", "delete-first", "delete-tail", "delete-all"])
def test_consistency_verification_detects_privileged_tampering_before_export(report, tamper):
    with sqlite3.connect(report) as connection:
        connection.execute("DROP TRIGGER audit_no_update")
        connection.execute("DROP TRIGGER audit_no_delete")
        if tamper == "alter":
            payload = json.loads(connection.execute("SELECT payload FROM audit WHERE sequence=1").fetchone()[0])
            payload["actor_id"] = "forged-actor"
            connection.execute("UPDATE audit SET payload=? WHERE sequence=1", (encode(payload),))
        elif tamper == "delete-first":
            connection.execute("DELETE FROM audit WHERE sequence=1")
        elif tamper == "delete-tail":
            connection.execute("DELETE FROM audit WHERE sequence=2")
        else:
            connection.execute("DELETE FROM audit")
    output = io.StringIO()
    with pytest.raises(ValueError, match="integrity failure"):
        export_report(report, output, tenant_id="tenant-a")
    assert output.getvalue() == ""
    with pytest.raises(ValueError, match="integrity failure"):
        report_summary(report, tenant_id="tenant-a")


def test_consistency_export_proves_scope_causation_and_verifiable_checkpoint(report):
    output = io.StringIO()
    summary = export_report(report, output, tenant_id="tenant-a", actor_id="exporter")
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    events = [row for row in rows if row["type"] == "audit"]
    assert [event["event_type"] for event in events] == [
        "consistency.started", "consistency.completed", "consistency.exported",
    ]
    previous_hash = GENESIS_HASH
    previous_id = None
    for sequence, event in enumerate(events, start=1):
        proof = event.pop("integrity")
        event.pop("type")
        assert event["correlation_id"] == summary["scan_id"]
        assert event["details"]["tenant_id"] == "tenant-a"
        assert event["causation_id"] == previous_id
        assert proof["sequence"] == sequence
        assert proof["previous_hash"] == previous_hash
        previous_hash = digest({
            "schema": "cognistore.consistency.audit.v1", "sequence": sequence,
            "previous_hash": previous_hash, "payload": event,
        })
        assert proof["entry_hash"] == previous_hash
        previous_id = event["event_id"]
    assert summary["audit_checkpoint"]["entry_hash"] == previous_hash
    assert summary["audit_checkpoint"]["sequence"] == len(events)
    with open_report(report, read_only=True) as connection:
        assert verify_report_integrity(connection, expected_checkpoint=summary["audit_checkpoint"]) == summary["audit_checkpoint"]
    with pytest.raises(ValueError, match="different tenant"):
        export_report(report, io.StringIO(), tenant_id="tenant-b")


def test_consistency_external_checkpoint_detects_privileged_tail_and_head_rewrite(report):
    with open_report(report, read_only=True) as connection:
        checkpoint = verify_report_integrity(connection)
    with sqlite3.connect(report) as connection:
        connection.execute("DROP TRIGGER audit_no_delete")
        connection.execute("DROP TRIGGER audit_head_update_guard")
        first_hash = connection.execute("SELECT entry_hash FROM audit WHERE sequence=1").fetchone()[0]
        connection.execute("DELETE FROM audit WHERE sequence=2")
        connection.execute("UPDATE audit_head SET sequence=1, entry_hash=?", (first_hash,))
    with open_report(report, read_only=True) as connection:
        assert verify_report_integrity(connection)["sequence"] == 1
        with pytest.raises(ValueError, match="checkpoint mismatch"):
            verify_report_integrity(connection, expected_checkpoint=checkpoint)


def test_consistency_resume_refuses_damaged_evidence_before_source_access(tmp_path, monkeypatch):
    driver = PosixDriver(str(tmp_path / "hot"))
    catalog = Catalog()
    scanner = ConsistencyScanner(
        catalog, {"hot": driver}, ScanScope("tenant-a", "bucket", "", ("hot",)),
        binding_id="test", requests_per_second=1e9, bytes_per_second=1e12,
    )
    path = tmp_path / "incomplete.sqlite3"
    scanner.run(path, max_items=1)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER audit_no_delete")
        connection.execute("DELETE FROM audit WHERE sequence=1")

    def forbidden(*args, **kwargs):
        raise AssertionError("damaged report must not read sources")

    monkeypatch.setattr(catalog, "list_page", forbidden)
    monkeypatch.setattr(driver, "list_objects", forbidden)
    with pytest.raises(ValueError, match="integrity failure"):
        scanner.run(path, resume=True)


def test_unprotected_legacy_consistency_reports_require_a_new_scan(report):
    with sqlite3.connect(report) as connection:
        state = read_state(connection)
        state["version"] = 1
        connection.execute("UPDATE checkpoint SET state=?", (encode(state),))
    with pytest.raises(ValueError, match="create a new scan"):
        export_report(report, io.StringIO(), tenant_id="tenant-a")


def test_consistency_scan_and_export_extend_one_chain_concurrently(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    driver = PosixDriver(str(tmp_path / "hot"))
    catalog = Catalog()
    for number in range(12):
        key = f"object-{number:02d}"
        driver.put_object("bucket", key, b"data")
        catalog.upsert("bucket", key, 4, tier="hot")
    scanner = ConsistencyScanner(
        catalog, {"hot": driver}, ScanScope("tenant-a", "bucket", "", ("hot",)),
        binding_id="test", requests_per_second=1e9, bytes_per_second=1e12, page_size=1,
    )
    path = tmp_path / "concurrent.sqlite3"
    first = scanner.run(path, max_items=1)["audit_checkpoint"]
    barrier = Barrier(2)

    def resume():
        barrier.wait(timeout=5)
        return scanner.run(path, resume=True)

    def export():
        barrier.wait(timeout=5)
        for _ in range(8):
            export_report(path, io.StringIO(), tenant_id="tenant-a")

    with ThreadPoolExecutor(max_workers=2) as executor:
        scan_future = executor.submit(resume)
        export_future = executor.submit(export)
        assert scan_future.result(timeout=15)["complete"]
        export_future.result(timeout=15)
    with open_report(path, read_only=True) as connection:
        checkpoint = verify_report_integrity(connection, expected_checkpoint=first)
        events = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM audit ORDER BY sequence")]
    assert checkpoint["sequence"] == len(events)
    assert sum(event["event_type"] == "consistency.exported" for event in events) == 8
    assert sum(event["event_type"] == "consistency.resumed" for event in events) == 1
    assert all(right["causation_id"] == left["event_id"] for left, right in zip(events, events[1:]))


def test_consistency_export_refuses_newly_sensitive_historical_proof(report, monkeypatch):
    import cognistore.utils.redaction as redaction

    with open_report(report) as connection, connection:
        append_event(connection, read_state(connection), "consistency.checkpoint", "late-registered-63")
    with open_report(report, read_only=True) as connection:
        before = verify_report_integrity(connection)
    monkeypatch.setattr(redaction, "_known_values", set(redaction._known_values))
    redaction.register_secret_value("late-registered-63")
    output = io.StringIO()
    with pytest.raises(ValueError, match="redaction reconciliation"):
        export_report(report, output, tenant_id="tenant-a")
    assert output.getvalue() == ""
    with open_report(report, read_only=True) as connection:
        assert verify_report_integrity(connection) == before
