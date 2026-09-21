"""Fenced local restore drills; these do not qualify a production recovery set.

There is no broker or encryption-key service in this fixture. SQLite partitions,
POSIX bytes, retained staging files, and saved configuration are restored into
new paths, including a former tenant absent from active membership. Interrupted
moves must preserve their durable identity and fail safely on new generations.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from cognistore.core.access import AccessEvent
from cognistore.core.audit import AuditContext
from cognistore.core.legal_holds import LegalHoldError
from cognistore.core.move_jobs import MoveJob, MoveJobFailedError, MoveJobState
from cognistore.core.mover import MoveGenerationMismatchError, Mover
from cognistore.db import SQLCatalog
from cognistore.db.engine import tenant_catalog_locator
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.drivers.tenancy import TenantStorageDriver

_TENANTS = ("default", "active-team", "former-team")
_BUCKET = "restore-drill"
_KEY = "pending/object.bin"
_HELD_KEY = "held/evidence.bin"
_MOVE_ID = "restore-drill:pending-move"
_NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)
_ACTOR = AuditContext("restore-drill", "user", "fixture-operator")


class _InterruptedWorker(BaseException):
    """Stop without the mover's ordinary Exception recovery path."""


def _files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def _logical_digest(path: Path) -> str:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        return hashlib.sha256("\n".join(connection.iterdump()).encode()).hexdigest()


def _drivers(root: Path, tenant_id: str) -> dict[str, TenantStorageDriver]:
    return {
        tier: TenantStorageDriver(PosixDriver(str(root / tier)), tenant_id)
        for tier in ("hot", "warm")
    }


def _catalog_path(root: Path, tenant_id: str) -> Path:
    path, _ = tenant_catalog_locator(root / "catalog.sqlite3", tenant_id)
    assert isinstance(path, Path)
    return path


@pytest.mark.parametrize("checkpoint", [
    MoveJobState.PREPARED, MoveJobState.TRANSFERRED, MoveJobState.VERIFIED,
    MoveJobState.COMMITTED, MoveJobState.CLEANUP,
])
def test_fenced_restore_preserves_all_partitions_and_quarantines_changed_generations(
    tmp_path: Path, checkpoint: MoveJobState,
) -> None:
    live, backup, restored = (tmp_path / name for name in ("live", "backup", "restored"))
    live.mkdir()
    saved: dict[str, dict[str, Any]] = {}

    def interrupt(job: MoveJob) -> None:
        if job.state == checkpoint:
            raise _InterruptedWorker()

    for tenant_id in _TENANTS:
        drivers = _drivers(live, tenant_id)
        payload = f"{tenant_id}: retain pending bytes after restore".encode()
        held_payload = f"{tenant_id}: preserve governance evidence".encode()
        drivers["hot"].put_object(_BUCKET, _KEY, payload)
        drivers["hot"].put_object(_BUCKET, _HELD_KEY, held_payload)
        with SQLCatalog(live / "catalog.sqlite3", tenant_id=tenant_id) as catalog:
            catalog.upsert(_BUCKET, _KEY, len(payload), "hot", {"owner": tenant_id})
            catalog.upsert(_BUCKET, _HELD_KEY, len(held_payload), "hot")
            held = catalog.place_legal_hold(
                _BUCKET, key=_HELD_KEY, reason="Retain recovery drill evidence", context=_ACTOR,
            )
            catalog.append_access_event(AccessEvent.create(
                kind="read", bucket=_BUCKET, key=_HELD_KEY,
                occurred_at=_NOW, operation_id="same-access-in-every-tenant",
                source="local-recovery-drill",
            ))
            mover = Mover(
                drivers, catalog, owner_id="fenced-original-worker", lease_seconds=1,
                clock=lambda: _NOW, transition_hook=interrupt, audit_context=_ACTOR,
            )
            with pytest.raises(_InterruptedWorker):
                mover.move("hot", "warm", _BUCKET, _KEY, idempotency_key=_MOVE_ID)
            job = catalog.get_move_job(_MOVE_ID)
            assert job is not None and job.state == checkpoint
            assert not job.state.terminal
            saved[tenant_id] = {
                "job": job,
                "transitions": catalog.list_move_job_transitions(_MOVE_ID),
                "record": catalog.get(_BUCKET, _KEY),
                "hold": held,
                "audit": catalog.list_audit_events(),
                "checkpoint": catalog.audit_checkpoint(),
                "payload": payload,
                "held_payload": held_payload,
            }

    # All synchronous producers and catalogs have exited. A real deployment
    # needs an external write fence; lease expiry alone cannot establish one.
    # Membership deliberately omits the retained former tenant, while the
    # recovery inventory includes every partition present at the boundary.
    configuration = {
        "recovery_set": "isolated-local-164",
        "active_tenants": ["default", "active-team"],
        "catalog_partitions": list(_TENANTS),
        "tiers": ["hot", "warm"],
        "queue": "not configured",
        "scheduler": "not configured",
    }
    (live / "configuration.json").write_text(json.dumps(configuration), encoding="utf-8")
    for tier in ("hot", "warm"):
        staging = live / tier / ".cognistore-staging"
        staging.mkdir(parents=True, exist_ok=True)
        # Full tier snapshots must retain unlisted private staging evidence.
        (staging / "upload-retained-fixture.tmp").write_bytes(b"fenced transfer residue")
    original_files = _files(live)

    backup.mkdir()
    for tenant_id in _TENANTS:
        source, target = _catalog_path(live, tenant_id), _catalog_path(backup, tenant_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(target)) as dst:
                src.backup(dst)
        saved[tenant_id]["logical_digest"] = _logical_digest(source)
        assert _logical_digest(target) == saved[tenant_id]["logical_digest"]
    for tier in ("hot", "warm"):
        shutil.copytree(live / tier, backup / tier)
        assert _files(backup / tier) == _files(live / tier)
    shutil.copy2(live / "configuration.json", backup / "configuration.json")
    backup_files = _files(backup)

    shutil.copytree(backup, restored)
    assert _files(restored) == backup_files
    assert json.loads((restored / "configuration.json").read_text()) == configuration
    assert set(saved) > set(configuration["active_tenants"])

    # Inspect every restored partition before any application writer resumes.
    # Logical equality covers access history and all catalog domains in addition
    # to the explicit governance, placement, journal and checkpoint assertions.
    for tenant_id in _TENANTS:
        expected = saved[tenant_id]
        assert _logical_digest(_catalog_path(restored, tenant_id)) == expected["logical_digest"]
        drivers = _drivers(restored, tenant_id)
        with SQLCatalog(
            restored / "catalog.sqlite3", tenant_id=tenant_id, read_only=True,
        ) as catalog:
            assert catalog.tenant_id == tenant_id
            assert catalog.get(_BUCKET, _KEY) == expected["record"]
            assert catalog.get_move_job(_MOVE_ID) == expected["job"]
            assert catalog.list_move_job_transitions(_MOVE_ID) == expected["transitions"]
            assert catalog.list_legal_holds() == [expected["hold"]]
            assert catalog.list_audit_events() == expected["audit"]
            assert catalog.audit_checkpoint() == expected["checkpoint"]
            integrity = catalog.verify_audit_integrity(expected["checkpoint"])
            assert integrity.valid and integrity.anchored
            assert drivers["hot"].get_object(_BUCKET, _KEY) == expected["payload"]
            assert drivers["hot"].get_object(_BUCKET, _HELD_KEY) == expected["held_payload"]
            assert drivers["hot"].object_generation(_BUCKET, _KEY) != (
                expected["job"].source_metadata["generation"]
            )
            if checkpoint != MoveJobState.PREPARED:
                assert drivers["warm"].get_object(_BUCKET, _KEY) == expected["payload"]

    # A byte-identical POSIX copy has different inode/change-time generations.
    # The supported outcome is a durable FAILED quarantine for owner review;
    # never rewrite journal generations to force destructive cleanup.
    storage_before_recovery = {tier: _files(restored / tier) for tier in ("hot", "warm")}
    for tenant_id in _TENANTS:
        expected = saved[tenant_id]
        drivers = _drivers(restored, tenant_id)
        with SQLCatalog(restored / "catalog.sqlite3", tenant_id=tenant_id) as catalog:
            mover = Mover(
                drivers, catalog, owner_id="isolated-recovery-worker", lease_seconds=1,
                clock=lambda: _NOW + timedelta(seconds=2), audit_context=_ACTOR,
            )
            with pytest.raises(MoveGenerationMismatchError):
                mover.move("hot", "warm", _BUCKET, _KEY, idempotency_key=_MOVE_ID)
            failed = catalog.get_move_job(_MOVE_ID)
            assert failed is not None and failed.state == MoveJobState.FAILED
            assert failed.idempotency_key == expected["job"].idempotency_key
            assert failed.created_at == expected["job"].created_at
            assert failed.source_metadata == expected["job"].source_metadata
            assert failed.terminal_reason is not None and "generation changed" in failed.terminal_reason
            transitions = catalog.list_move_job_transitions(_MOVE_ID)
            assert transitions[:len(expected["transitions"])] == expected["transitions"]
            assert transitions[-1].to_state == MoveJobState.FAILED
            assert MoveJobState.COMPLETED not in [item.to_state for item in transitions]
            assert catalog.get(_BUCKET, _KEY).tier == "hot"
            assert len(catalog.list_move_jobs()) == 1
            with pytest.raises(MoveJobFailedError):
                mover.move("hot", "warm", _BUCKET, _KEY, idempotency_key=_MOVE_ID)
            assert catalog.list_move_job_transitions(_MOVE_ID) == transitions
            with pytest.raises(LegalHoldError):
                mover.move("hot", "warm", _BUCKET, _HELD_KEY, idempotency_key="held-move")
            assert catalog.list_legal_holds() == [expected["hold"]]
            assert catalog.get_move_job("held-move") is None
            integrity = catalog.verify_audit_integrity(expected["checkpoint"])
            assert integrity.valid and integrity.anchored

    for tier in ("hot", "warm"):
        assert _files(restored / tier) == storage_before_recovery[tier]
    assert _files(backup) == backup_files
    assert _files(live) == original_files
