from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

from cognistore.core.audit import AuditContext
from cognistore.core.catalog import Catalog, CatalogStore
from cognistore.core.move_jobs import MoveJobState
from cognistore.core.placement_controls import ImportanceTag
from cognistore.db.catalog import SQLCatalog
from cognistore.db.migrations import MigrationManager
from cognistore.db.schema import object_placements
from cognistore.db.sqlite_import import SQLiteCatalogImportError, import_sqlite_catalog

START = "2026-09-01T00:00:00.000000Z"
MOVED = "2026-09-01T00:01:00.000000Z"
LATER = "2026-09-01T00:02:00.000000Z"
LEASE = "2026-09-01T00:10:00.000000Z"
CONTEXT = AuditContext("tier-stability", "user", "operator")


@pytest.fixture(params=["memory", "sqlite"])
def catalog(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[CatalogStore]:
    if request.param == "memory":
        yield Catalog()
    else:
        with SQLCatalog(tmp_path / "catalog.db") as store:
            yield store


def set_clock(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr("cognistore.core.catalog._content_reference_timestamp", lambda: value)
    monkeypatch.setattr("cognistore.db.catalog._timestamp", lambda: value)


@pytest.mark.parametrize("mutation", ["upsert", "placement", "update", "scan", "pool"])
def test_only_tier_changes_start_move_clock(
    catalog: CatalogStore, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    set_clock(monkeypatch, START)
    catalog.upsert("bucket", "object", 1, "hot", {"last_tier_move_at": "forged"})
    assert catalog.get("bucket", "object").last_tier_move_at is None
    set_clock(monkeypatch, MOVED)
    if mutation == "upsert":
        catalog.upsert("bucket", "object", 2, "warm")
    elif mutation == "placement":
        catalog.upsert_placement("bucket", "object", size=2, tier="warm")
    elif mutation == "update":
        catalog.update_placement("bucket", "object", "warm")
    elif mutation == "scan":
        assert catalog.upsert_scan_observation(
            "bucket", "object", size=2, tier="warm", generation="observed",
            metadata={}, fence=catalog.capture_scan_fence("bucket", "object"),
        )
    else:
        catalog.register_tier("warm")
        catalog.register_pool("pool", "warm", region="local", members=("disk",))
        catalog.assign_pool("bucket", "object", "pool")
    moved = catalog.get("bucket", "object")
    assert moved.last_tier_move_at == moved.placement_started_at == MOVED

    # Scans, content writes, metadata, importance and pool changes cannot reset
    # the trusted clock when the authoritative tier stays the same.
    set_clock(monkeypatch, LATER)
    catalog.upsert("bucket", "object", 3, "warm", {"last_tier_move_at": "forged"})
    catalog.upsert_placement("bucket", "object", size=3, tier="warm")
    catalog.update_placement("bucket", "object", "warm")
    assert catalog.upsert_scan_observation(
        "bucket", "object", size=4, tier="warm", generation="replacement",
        metadata={}, fence=catalog.capture_scan_fence("bucket", "object"),
    )
    catalog.register_pool("another-pool", "warm", region="local", members=("disk",))
    catalog.assign_pool("bucket", "object", "another-pool")
    catalog.assign_pool("bucket", "object", None)
    catalog.set_importance(
        "bucket", "object", ImportanceTag("high", "user", "operator", "retain", LATER),
        audit_context=CONTEXT,
    )
    assert catalog.get("bucket", "object").last_tier_move_at == MOVED
    catalog.delete("bucket", "object")
    catalog.upsert("bucket", "object", 1, "warm")
    assert catalog.get("bucket", "object").last_tier_move_at is None


def prepare_verified_move(catalog: CatalogStore, *, scanned: bool = True) -> None:
    if scanned:
        catalog.upsert("bucket", "object", 1, "hot")
    catalog.claim_move_job(
        "move", src_tier="hot", dst_tier="warm", bucket="bucket", key="object",
        expected_size=1, source_metadata={}, owner_id="worker", now=START,
        lease_expires_at=LEASE, audit_context=CONTEXT,
    )
    for previous, following in (
        (MoveJobState.PREPARED, MoveJobState.TRANSFERRED),
        (MoveJobState.TRANSFERRED, MoveJobState.VERIFIED),
    ):
        catalog.transition_move_job(
            "move", owner_id="worker", expected_state=previous, to_state=following,
            reason="verified", now=START, lease_expires_at=LEASE, audit_context=CONTEXT,
        )


@pytest.mark.parametrize("scanned", [True, False])
def test_move_commit_uses_atomic_commit_clock_and_recovery_preserves_it(
    catalog: CatalogStore, monkeypatch: pytest.MonkeyPatch, scanned: bool,
) -> None:
    set_clock(monkeypatch, START)
    prepare_verified_move(catalog, scanned=scanned)
    assert catalog.get("bucket", "object") is None or (
        catalog.get("bucket", "object").last_tier_move_at is None
    )
    catalog.commit_move_job_placement(
        "move", owner_id="worker", size=1, tier="warm", checksum="abc",
        now=MOVED, lease_expires_at=LEASE, audit_context=CONTEXT,
    )
    committed = catalog.get("bucket", "object")
    assert committed.last_tier_move_at == committed.placement_started_at == MOVED
    catalog.claim_move_job(
        "move", src_tier="hot", dst_tier="warm", bucket="bucket", key="object",
        expected_size=1, source_metadata={}, owner_id="worker", now=LATER,
        lease_expires_at=LEASE, audit_context=CONTEXT,
    )
    assert catalog.get("bucket", "object").last_tier_move_at == MOVED


def test_move_clock_rolls_back_with_failed_commit_audit(
    catalog: CatalogStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_clock(monkeypatch, START)
    prepare_verified_move(catalog)
    previous = catalog.get("bucket", "object")
    events = catalog.list_audit_events()

    def reject(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit unavailable")

    method = "_insert_audit_event" if isinstance(catalog, SQLCatalog) else "append_audit_event"
    monkeypatch.setattr(catalog, method, reject)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        catalog.commit_move_job_placement(
            "move", owner_id="worker", size=1, tier="warm", checksum="abc",
            now=MOVED, lease_expires_at=LEASE, audit_context=CONTEXT,
        )
    assert catalog.get("bucket", "object") == previous
    assert catalog.get_move_job("move").state == MoveJobState.VERIFIED
    assert catalog.list_audit_events() == events


def test_native_restart_and_import_preserve_move_clock_and_never_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_clock(monkeypatch, START)
    path = tmp_path / "source.db"
    with SQLCatalog(path) as source:
        source.upsert("bucket", "moved", 1, "hot")
        source.upsert("bucket", "initial", 1, "hot")
        set_clock(monkeypatch, MOVED)
        source.update_placement("bucket", "moved", "warm")
        expected = source.list("bucket")
    with SQLCatalog(path) as restarted:
        assert restarted.list("bucket") == expected
    with SQLCatalog(tmp_path / "destination.db") as destination:
        import_sqlite_catalog(path, destination)
        assert destination.list("bucket") == expected


def test_legacy_migration_backfills_known_and_unknown_clocks(tmp_path: Path) -> None:
    path = tmp_path / "source.db"
    with SQLCatalog(path) as source:
        source.upsert("bucket", "known", 1, "hot")
        source.upsert("bucket", "unknown", 1, "hot")
        MigrationManager().downgrade(source.engine, "0009_placement_controls")
        with source.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE object_placements SET placement_started_at = CASE "
                "WHEN object_id = (SELECT object_id FROM objects WHERE object_key = 'known') "
                "THEN '2026-09-01T00:00:00.000000Z' ELSE NULL END"
            )
    before = datetime.now(timezone.utc)
    with SQLCatalog(path) as upgraded:
        assert upgraded.get("bucket", "known").last_tier_move_at == START
        unknown = upgraded.get("bucket", "unknown")
        assert unknown.placement_started_at is None
        assert before <= datetime.fromisoformat(unknown.last_tier_move_at.replace("Z", "+00:00"))
        with upgraded.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


@pytest.mark.parametrize("value", ["2026-09-01T00:00:00", "2026-09-01T01:00:00+01:00", "invalid"])
def test_native_import_rejects_invalid_move_clock_atomically(tmp_path: Path, value: str) -> None:
    path = tmp_path / "source.db"
    with SQLCatalog(path) as source:
        source.upsert("bucket", "object", 1, "hot")
        with source.engine.begin() as connection:
            connection.execute(sa.update(object_placements).values(last_tier_move_at=value))
    with SQLCatalog(tmp_path / "destination.db") as destination:
        with pytest.raises(SQLiteCatalogImportError, match="last tier move"):
            import_sqlite_catalog(path, destination)
        assert destination.list("bucket") == []


def test_native_import_rejects_missing_move_clock_column(tmp_path: Path) -> None:
    path = tmp_path / "source.db"
    with SQLCatalog(path) as source:
        source.upsert("bucket", "object", 1, "hot")
        with source.engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE object_placements DROP COLUMN last_tier_move_at")
    with SQLCatalog(tmp_path / "destination.db") as destination:
        with pytest.raises(SQLiteCatalogImportError, match="partial tier stability"):
            import_sqlite_catalog(path, destination)
        assert destination.list("bucket") == []


def test_catalog_cooldown_rechecks_current_clock_before_commit(
    catalog: CatalogStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognistore.core.placement_controls import MovementConstraintError, MovementConstraints

    set_clock(monkeypatch, START)
    controls = MovementConstraints(cooldown_seconds=60).to_dict()
    catalog.upsert("bucket", "object", 1, "hot")
    catalog.claim_move_job(
        "guarded", src_tier="hot", dst_tier="warm", bucket="bucket", key="object",
        expected_size=1, source_metadata={"cognistore_movement_constraints": controls},
        owner_id="worker", now=START, lease_expires_at=LEASE,
    )
    for previous, following in (
        (MoveJobState.PREPARED, MoveJobState.TRANSFERRED),
        (MoveJobState.TRANSFERRED, MoveJobState.VERIFIED),
    ):
        catalog.transition_move_job(
            "guarded", owner_id="worker", expected_state=previous, to_state=following,
            reason="verified", now=START, lease_expires_at=LEASE,
        )
    # A placement change after verification invalidates the earlier timer.
    set_clock(monkeypatch, MOVED)
    catalog.update_placement("bucket", "object", "cold")
    catalog.update_placement("bucket", "object", "hot")
    with pytest.raises(MovementConstraintError, match="cooldown"):
        catalog.commit_move_job_placement(
            "guarded", owner_id="worker", size=1, tier="warm", checksum="abc",
            now=MOVED, lease_expires_at=LEASE,
        )
    assert catalog.get("bucket", "object").tier == "hot"
    assert catalog.get_move_job("guarded").state == MoveJobState.VERIFIED
    catalog.commit_move_job_placement(
        "guarded", owner_id="worker", size=1, tier="warm", checksum="abc",
        now=LATER, lease_expires_at=LEASE,
    )
    assert catalog.get("bucket", "object").last_tier_move_at == LATER


@pytest.mark.parametrize("kind", ["emergency", "compliance"])
def test_catalog_move_audit_records_frozen_override_and_controls(
    catalog: CatalogStore, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    from cognistore.core.placement_controls import MovementConstraints, StabilityOverride

    set_clock(monkeypatch, START)
    catalog.upsert("bucket", "object", 1, "hot")
    set_clock(monkeypatch, MOVED)
    catalog.update_placement("bucket", "object", "warm")
    override = StabilityOverride(kind, "Incident response order 42")
    controls = MovementConstraints(cooldown_seconds=60, stability_override=override).to_dict()
    catalog.claim_move_job(
        "override", src_tier="warm", dst_tier="hot", bucket="bucket", key="object",
        expected_size=1, source_metadata={"cognistore_movement_constraints": controls},
        owner_id="worker", now=MOVED, lease_expires_at=LEASE, audit_context=CONTEXT,
    )
    event = catalog.list_audit_events()[0]
    assert event.actor_id == CONTEXT.actor_id
    assert event.actor_type == CONTEXT.actor_type
    assert event.details["movement_constraints"] == controls
    assert event.details["stability_override"] == override.to_dict()


def test_legacy_import_backfills_missing_move_history(tmp_path: Path) -> None:
    path = tmp_path / "source.db"
    with SQLCatalog(path) as source:
        source.upsert("bucket", "known", 1, "hot")
        source.upsert("bucket", "unknown", 1, "hot")
        MigrationManager().downgrade(source.engine, "0009_placement_controls")
        with source.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE object_placements SET placement_started_at = CASE "
                "WHEN object_id = (SELECT object_id FROM objects WHERE object_key = 'known') "
                "THEN '2026-09-01T00:00:00.000000Z' ELSE NULL END"
            )
    before = datetime.now(timezone.utc)
    with SQLCatalog(tmp_path / "destination.db") as destination:
        import_sqlite_catalog(path, destination)
        assert destination.get("bucket", "known").last_tier_move_at == START
        unknown = destination.get("bucket", "unknown")
        assert before <= datetime.fromisoformat(unknown.last_tier_move_at.replace("Z", "+00:00"))


@pytest.mark.parametrize("scanned", [True, False])
def test_same_tier_catalog_move_checkpoint_preserves_movement_clock(
    catalog: CatalogStore, monkeypatch: pytest.MonkeyPatch, scanned: bool,
) -> None:
    set_clock(monkeypatch, START)
    if scanned:
        catalog.upsert("bucket", "object", 1, "hot")
    catalog.claim_move_job(
        "same-tier", src_tier="hot", dst_tier="hot", bucket="bucket", key="object",
        expected_size=1, source_metadata={}, owner_id="worker", now=START,
        lease_expires_at=LEASE,
    )
    for previous, following in (
        (MoveJobState.PREPARED, MoveJobState.TRANSFERRED),
        (MoveJobState.TRANSFERRED, MoveJobState.VERIFIED),
    ):
        catalog.transition_move_job(
            "same-tier", owner_id="worker", expected_state=previous, to_state=following,
            reason="verified", now=START, lease_expires_at=LEASE,
        )
    catalog.commit_move_job_placement(
        "same-tier", owner_id="worker", size=1, tier="hot", checksum="abc",
        now=MOVED, lease_expires_at=LEASE,
    )
    record = catalog.get("bucket", "object")
    assert record.last_tier_move_at is None
    assert record.placement_started_at == (START if scanned else MOVED)
