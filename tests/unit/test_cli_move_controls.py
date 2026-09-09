from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cognistore.cli import cognistore_cli
from cognistore.core.audit import AuditContext, AuditQuery
from cognistore.core.move_jobs import MoveJob, MoveJobState
from cognistore.core.mover import Mover
from cognistore.core.placement_controls import ImportanceTag
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver


@pytest.mark.parametrize("resume", (False, True))
@pytest.mark.parametrize("restriction", ("residency", "importance"))
def test_manual_move_preview_reads_authoritative_controls_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    resume: bool,
    restriction: str,
) -> None:
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    drivers["hot"].put_object("bucket", "key", b"data")
    database = tmp_path / "catalog.sqlite3"
    catalog = SQLiteCatalog(database)
    catalog.upsert("bucket", "key", 4, "hot")
    if resume:
        def interrupt(job: MoveJob) -> None:
            if job.state == MoveJobState.PREPARED:
                raise RuntimeError("paused before transfer")

        with pytest.raises(RuntimeError, match="paused before transfer"):
            Mover(drivers, catalog, transition_hook=interrupt).move(
                "hot", "warm", "bucket", "key",
                idempotency_key="held-move",
            )
    if restriction == "residency":
        catalog.register_tier("hot", {"minimum_residency_seconds": 3600})
    else:
        catalog.set_importance(
            "bucket", "key",
            ImportanceTag(
                level="critical", actor_type="user", actor_id="operator",
                provenance="Current incident", updated_at=datetime.now(timezone.utc).isoformat(),
            ),
            audit_context=AuditContext(
                correlation_id="tag", actor_type="user", actor_id="operator",
            ),
        )
    before_record = catalog.get("bucket", "key")
    before_events = catalog.list_audit_events(AuditQuery())
    before_job = catalog.get_move_job("held-move")
    catalog.close()
    before_bytes = database.read_bytes()
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda _path: drivers)
    command = ["move-resume", "held-move"] if resume else ["move", "hot", "warm", "bucket", "key"]

    result = cognistore_cli.main([
        "--drivers", "ignored.yaml", "--catalog-db", str(database),
        *command, "--dry-run", "--json",
    ])

    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "MovementConstraintError"
    assert payload["dry_run"] is True
    if restriction == "residency":
        assert payload["constraints"]["residency_active"] is True
    else:
        assert payload["constraints"]["importance"]["level"] == "critical"
    assert drivers["hot"].get_object("bucket", "key") == b"data"
    assert list(drivers["warm"].list_objects("bucket")) == []
    assert database.read_bytes() == before_bytes
    checked = SQLiteCatalog(database, read_only=True)
    try:
        assert checked.get("bucket", "key") == before_record
        assert checked.get_move_job("held-move") == before_job
        assert checked.list_audit_events(AuditQuery()) == before_events
    finally:
        checked.close()
