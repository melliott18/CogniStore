from __future__ import annotations

from pathlib import Path

import pytest

from cognistore.cli.cognistore_cli import main
from cognistore.core.audit import AuditContext
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver


@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_put_respects_persistent_legal_hold(tmp_path: Path, dry_run: bool) -> None:
    root = tmp_path / "storage"
    driver = PosixDriver(root)
    driver.put_object("bucket", "records/item", b"preserved")
    source = tmp_path / "replacement"
    source.write_bytes(b"replacement")
    database = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(database) as catalog:
        hold = catalog.place_legal_hold(
            "bucket", prefix="records/", reason="preserve evidence",
            context=AuditContext(actor_type="user", actor_id="custodian", correlation_id="test"),
        )
        before = len(catalog.list_audit_events())
    arguments = [
        "--base", str(root), "--catalog-db", str(database),
        "put", "bucket", "records/item", str(source),
    ]
    if dry_run:
        arguments.append("--dry-run")
    assert main(arguments) != 0
    assert driver.get_object("bucket", "records/item") == b"preserved"
    with SQLiteCatalog(database) as catalog:
        assert catalog.list_legal_holds(active_only=True)[0].hold_id == hold.hold_id
        events = catalog.list_audit_events()[before:]
        if dry_run:
            assert events == []
        else:
            assert len([event for event in events if event.event_type == "legal_hold.denied"]) == 1
            storage_events = [event for event in events if event.event_type == "storage.operation"]
            assert [event.outcome for event in storage_events] == ["started", "failed"]
            assert storage_events[1].causation_id == storage_events[0].event_id
        assert catalog.verify_audit_integrity().valid


def test_cli_put_checks_catalog_before_new_upload(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    source = tmp_path / "source"
    source.write_bytes(b"content")
    database = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(database) as catalog:
        catalog.place_legal_hold(
            "bucket", reason="preserve bucket",
            context=AuditContext(actor_type="user", actor_id="custodian", correlation_id="test"),
        )
    assert main([
        "--base", str(root), "--catalog-db", str(database),
        "put", "bucket", "new", str(source),
    ]) != 0
    with pytest.raises(FileNotFoundError):
        PosixDriver(root).get_object("bucket", "new")
