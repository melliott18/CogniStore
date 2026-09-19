"""Automatic repair stays conservative and reuses durable move recovery."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import cognistore.core.consistency_repair as repair_module
from cognistore.core.audit import AuditContext
from cognistore.core.catalog import Catalog, CatalogStore
from cognistore.core.consistency import ConsistencyScanner, ScanScope
from cognistore.core.consistency_repair import ConsistencyRepairer
from cognistore.core.move_jobs import MoveJobState
from cognistore.core.mover import Mover
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver

BUCKET = "tenant-bucket"
KEY = "objects/repair.bin"
PAYLOAD = b"one verified object survives every interrupted repair"
JOB_ID = "original-move:repair.bin"
BINDING = "trusted-test-binding"
SCOPE = ScanScope("default", BUCKET, "objects/", ("hot", "warm"))
STATES = [
    MoveJobState.PREPARED, MoveJobState.TRANSFERRED, MoveJobState.VERIFIED,
    MoveJobState.COMMITTED, MoveJobState.CLEANUP, MoveJobState.COMPLETED,
]


class InjectedCrash(BaseException):
    pass


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int = 120) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class System:
    catalog: CatalogStore
    drivers: dict[str, PosixDriver]
    clock: Clock
    root: Path

    def scanner(self) -> ConsistencyScanner:
        return ConsistencyScanner(
            self.catalog, self.drivers, SCOPE, binding_id=BINDING,
            requests_per_second=1e9, bytes_per_second=1e12,
        )

    def report(self, name: str = "report.sqlite3") -> Path:
        path = self.root / name
        assert self.scanner().run(path)["complete"]
        return path

    def repairer(self, **kwargs: Any) -> ConsistencyRepairer:
        return ConsistencyRepairer(
            self.catalog, self.drivers, SCOPE, binding_id=BINDING,
            requests_per_second=1e9, bytes_per_second=1e12,
            clock=self.clock, **kwargs,
        )

    def interrupted_move(self, state: MoveJobState = MoveJobState.PREPARED) -> None:
        def crash(job: Any) -> None:
            if job.state == state:
                raise InjectedCrash(state.value)

        with pytest.raises(InjectedCrash, match=state.value):
            Mover(
                self.drivers, self.catalog, owner_id="original-worker",
                clock=self.clock, lease_seconds=1, transition_hook=crash,
            ).move("hot", "warm", BUCKET, KEY, idempotency_key=JOB_ID)
        self.clock.advance()

    def source_snapshot(self) -> dict[str, Any]:
        return {
            "record": self.catalog.get(BUCKET, KEY),
            "job": self.catalog.get_move_job(JOB_ID),
            "transitions": self.catalog.list_move_job_transitions(JOB_ID),
            "audit": self.catalog.list_audit_events(),
            "storage": {
                str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for tier in self.drivers
                for path in (self.root / tier).rglob("*") if path.is_file()
            },
        }


@pytest.fixture(params=["memory", "sqlite"])
def system(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("COGNISTORE_LOCALITY_CONFIG", raising=False)
    catalog = Catalog() if request.param == "memory" else SQLiteCatalog(tmp_path / "catalog.db")
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in SCOPE.tiers}
    drivers["hot"].put_object(BUCKET, KEY, PAYLOAD)
    catalog.upsert(BUCKET, KEY, len(PAYLOAD), "hot", {
        "sha256": hashlib.sha256(PAYLOAD).hexdigest(), "sample_len": len(PAYLOAD),
    })
    try:
        yield System(catalog, drivers, Clock(), tmp_path)
    finally:
        if isinstance(catalog, SQLiteCatalog):
            catalog.close()


def _action(summary: dict[str, Any], key: str = KEY) -> dict[str, Any]:
    actions = [action for action in summary["actions"] if action["key"] == key]
    assert len(actions) == 1
    return actions[0]


def _assert_completed(system: System) -> None:
    assert system.drivers["warm"].get_object(BUCKET, KEY) == PAYLOAD
    with pytest.raises(FileNotFoundError):
        system.drivers["hot"].stat_object(BUCKET, KEY)
    assert system.catalog.get(BUCKET, KEY).tier == "warm"
    assert system.catalog.get_move_job(JOB_ID).state == MoveJobState.COMPLETED
    assert [transition.to_state for transition in system.catalog.list_move_job_transitions(JOB_ID)] == STATES
    assert len(system.catalog.list_move_jobs()) == 1


def test_repair_defaults_to_plan_without_source_mutations(system: System) -> None:
    system.interrupted_move()
    report = system.report()
    before = system.source_snapshot()

    summary = system.repairer().run(report)

    assert summary["plan_only"] is True
    assert _action(summary)["status"] == "planned"
    assert _action(summary)["job_id"] == JOB_ID
    assert summary["counts"]["planned"] == 1
    assert system.source_snapshot() == before


@pytest.mark.parametrize("enabled", [False, True])
def test_dry_run_preserves_report_and_source(system: System, enabled: bool) -> None:
    system.interrupted_move()
    report = system.report()
    before = system.source_snapshot()
    report_bytes = report.read_bytes()
    entries = sorted(str(path.relative_to(system.root)) for path in system.root.rglob("*"))

    summary = system.repairer().run(report, enabled=enabled, dry_run=True)

    assert summary["plan_only"] is True
    assert _action(summary)["status"] == "planned"
    assert system.source_snapshot() == before
    assert report.read_bytes() == report_bytes
    assert sorted(str(path.relative_to(system.root)) for path in system.root.rglob("*")) == entries


@pytest.mark.parametrize("phase", STATES[:-1])
def test_existing_job_recovery_converges_from_every_phase(system: System, phase: MoveJobState) -> None:
    system.interrupted_move(phase)
    report = system.report()

    result = system.repairer().run(report, enabled=True)

    assert result["plan_only"] is False
    assert _action(result)["status"] == "completed"
    _assert_completed(system)
    before = system.source_snapshot()
    repeated = system.repairer().run(report, enabled=True)
    assert _action(repeated)["status"] in {"completed", "resolved"}
    assert system.source_snapshot() == before
    clean_report = system.report("after-repair.sqlite3")
    assert system.repairer().run(clean_report, enabled=True)["actions"] == []


@pytest.mark.parametrize("phase", STATES[1:])
def test_repair_interruption_recovers_without_duplicate_transitions(system: System, phase: MoveJobState) -> None:
    system.interrupted_move()
    report = system.report()

    def crash(job: Any) -> None:
        if job.state == phase:
            raise InjectedCrash(phase.value)

    with pytest.raises(InjectedCrash, match=phase.value):
        system.repairer(transition_hook=crash).run(report, enabled=True)
    assert system.catalog.get_move_job(JOB_ID).state == phase
    system.clock.advance()

    result = system.repairer().run(report, enabled=True)

    assert _action(result)["status"] in {"completed", "resolved"}
    _assert_completed(system)


def test_repair_transfer_interruption_before_checkpoint_preserves_source(system: System, monkeypatch: pytest.MonkeyPatch) -> None:
    system.interrupted_move()
    report = system.report()
    original_put = system.drivers["warm"].put_object_stream

    def publish_then_crash(*args: Any, **kwargs: Any) -> None:
        original_put(*args, **kwargs)
        raise InjectedCrash("published before transferred checkpoint")

    monkeypatch.setattr(system.drivers["warm"], "put_object_stream", publish_then_crash)
    with pytest.raises(InjectedCrash, match="published"):
        system.repairer().run(report, enabled=True)
    assert system.drivers["hot"].get_object(BUCKET, KEY) == PAYLOAD
    assert system.drivers["warm"].get_object(BUCKET, KEY) == PAYLOAD
    assert system.catalog.get_move_job(JOB_ID).state == MoveJobState.PREPARED
    monkeypatch.setattr(system.drivers["warm"], "put_object_stream", original_put)
    system.clock.advance()

    result = system.repairer().run(report, enabled=True)

    assert _action(result)["status"] == "completed"
    _assert_completed(system)


@pytest.mark.parametrize("condition", [
    "hold", "failed", "live_lease", "source_generation", "destination_generation",
    "missing_catalog", "missing_source", "missing_destination", "extra_copy",
])
def test_unsafe_repair_candidates_are_quarantined_without_source_writes(system: System, condition: str) -> None:
    phase = MoveJobState.TRANSFERRED if condition in {"destination_generation", "missing_destination"} else MoveJobState.PREPARED
    system.interrupted_move(phase)
    report = system.report()
    if condition == "hold":
        system.catalog.place_legal_hold(
            BUCKET, key=KEY, reason="preserve evidence",
            context=AuditContext("hold-repair", "user", "operator"),
        )
    elif condition == "failed":
        system.catalog.transition_move_job(
            JOB_ID, owner_id="original-worker", expected_state=phase,
            to_state=MoveJobState.FAILED, reason="failed verification",
            now=system.clock().isoformat(), lease_expires_at=system.clock().isoformat(),
        )
    elif condition == "live_lease":
        system.catalog.renew_move_job_lease(
            JOB_ID, owner_id="original-worker", expected_state=phase,
            now=system.clock().isoformat(),
            lease_expires_at=(system.clock() + timedelta(hours=1)).isoformat(),
        )
    elif condition == "source_generation":
        system.drivers["hot"].put_object(BUCKET, KEY, PAYLOAD)
    elif condition == "destination_generation":
        system.drivers["warm"].put_object(BUCKET, KEY, b"corrupted destination")
    elif condition == "missing_catalog":
        system.catalog.delete(BUCKET, KEY)
    elif condition == "missing_source":
        system.drivers["hot"].delete_object(BUCKET, KEY)
    elif condition == "missing_destination":
        system.drivers["warm"].delete_object(BUCKET, KEY)
    elif condition == "extra_copy":
        system.drivers["warm"].put_object(BUCKET, KEY, b"unrelated destination")
    before = system.source_snapshot()

    summary = system.repairer().run(report, enabled=True)

    assert _action(summary)["status"] == "quarantined"
    assert _action(summary)["reason"]
    assert system.source_snapshot() == before


def test_configured_locality_is_quarantined_even_for_allowed_destination(system: System, monkeypatch: pytest.MonkeyPatch) -> None:
    system.interrupted_move()
    report = system.report()
    for tier in SCOPE.tiers:
        system.catalog.register_tier(tier)
        system.catalog.register_pool(
            tier + "-eu", tier, region="eu-west-1", members=(tier + "-storage",),
            localities=("EU",), metadata={"locality_evidence": {
                "observed_at": system.clock().isoformat(), "max_age_seconds": 3600,
                "source": "operator-attestation",
            }},
        )
    path = system.root / "locality.json"
    path.write_text(json.dumps({"version": 1, "tenants": {"default": {
        "allowed_regions": ["eu-west-1"], "required_localities": ["EU"],
        "tier_pools": {tier: tier + "-eu" for tier in SCOPE.tiers},
        "objects": [], "exceptions": [],
    }}}), encoding="utf-8")
    monkeypatch.setenv("COGNISTORE_LOCALITY_CONFIG", str(path))
    before = system.source_snapshot()

    result = system.repairer().run(report, enabled=True)

    assert _action(result)["status"] == "quarantined"
    assert "locality" in _action(result)["reason"].lower()
    assert system.source_snapshot() == before


def test_untracked_content_and_unexplained_duplicates_are_never_deleted(system: System) -> None:
    orphan = "objects/orphan.bin"
    system.drivers["warm"].put_object(BUCKET, orphan, b"retain orphan")
    system.drivers["warm"].put_object(BUCKET, KEY, PAYLOAD)
    report = system.report()
    before = system.source_snapshot()

    result = system.repairer().run(report, enabled=True)

    assert _action(result)["status"] == "quarantined"
    assert _action(result, orphan)["status"] == "quarantined"
    assert system.source_snapshot() == before


@pytest.mark.parametrize("binding,scope", [
    ("other-source", SCOPE),
    (BINDING, ScanScope("other-tenant", BUCKET, "objects/", SCOPE.tiers)),
    (BINDING, ScanScope("default", BUCKET, "other/", SCOPE.tiers)),
])
def test_report_scope_and_source_binding_are_required(system: System, binding: str, scope: ScanScope) -> None:
    system.interrupted_move()
    report = system.report()
    before = system.source_snapshot()

    with pytest.raises(ValueError):
        ConsistencyRepairer(system.catalog, system.drivers, scope, binding_id=binding).run(report, enabled=True)

    assert system.source_snapshot() == before


def test_forged_report_job_cannot_redirect_recovery(system: System) -> None:
    system.interrupted_move()
    report = system.report()
    with sqlite3.connect(report) as connection:
        for sequence, payload in connection.execute("SELECT sequence,payload FROM findings").fetchall():
            finding = json.loads(payload)
            if finding["reason_code"] == "partial_job":
                finding["details"]["job_id"] = "another-tenants-move"
                connection.execute("UPDATE findings SET payload=? WHERE sequence=?", (json.dumps(finding), sequence))
    summary = system.repairer().run(report, enabled=True)

    # Finding details are only hints. Fresh durable evidence still identifies
    # the original job, and the forged target is never claimed or created.
    assert _action(summary)["job_id"] == JOB_ID
    assert _action(summary)["status"] == "completed"
    assert system.catalog.get_move_job("another-tenants-move") is None
    _assert_completed(system)


@pytest.mark.parametrize("phase", STATES[1:-1])
@pytest.mark.parametrize("change", ["hold", "locality", "source_generation", "catalog_deleted"])
def test_new_uncertainty_between_repair_phases_stops_all_following_effects(
    system: System, monkeypatch: pytest.MonkeyPatch, phase: MoveJobState, change: str,
) -> None:
    system.interrupted_move()
    report = system.report()
    stopped: list[dict[str, Any]] = []

    def change_after_transition(job: Any) -> None:
        if job.state != phase:
            return
        if change == "hold":
            system.catalog.place_legal_hold(
                BUCKET, key=KEY, reason="hold arriving during repair",
                context=AuditContext("phase-hold", "user", "operator"),
            )
        elif change == "locality":
            path = system.root / "new-locality.json"
            path.write_text("{}", encoding="utf-8")
            monkeypatch.setenv("COGNISTORE_LOCALITY_CONFIG", str(path))
        elif change == "source_generation":
            system.drivers["hot"].put_object(BUCKET, KEY, b"new source must survive")
        else:
            system.catalog.delete(BUCKET, KEY)
        stopped.append(system.source_snapshot())

    result = system.repairer(transition_hook=change_after_transition).run(report, enabled=True)

    assert len(stopped) == 1
    assert _action(result)["status"] == "quarantined"
    assert system.catalog.get_move_job(JOB_ID).state == phase
    assert system.source_snapshot() == stopped[0]
    assert system.drivers["warm"].get_object(BUCKET, KEY) == PAYLOAD


def test_missing_full_checksum_evidence_requires_review(system: System) -> None:
    system.interrupted_move()
    system.catalog.upsert(BUCKET, KEY, len(PAYLOAD), "hot", {"sha256": hashlib.sha256(PAYLOAD).hexdigest()})
    report = system.report()
    before = system.source_snapshot()

    result = system.repairer().run(report, enabled=True)

    assert _action(result)["status"] == "quarantined"
    assert "checksum" in _action(result)["reason"]
    assert system.source_snapshot() == before


def test_cleanup_recovers_after_source_deleted_before_completion_checkpoint(
    system: System, monkeypatch: pytest.MonkeyPatch,
) -> None:
    system.interrupted_move(MoveJobState.CLEANUP)
    report = system.report()
    original_delete = system.drivers["hot"].delete_object_if_generation

    def delete_then_crash(*args: Any, **kwargs: Any) -> None:
        original_delete(*args, **kwargs)
        raise InjectedCrash("deleted before completion checkpoint")

    monkeypatch.setattr(system.drivers["hot"], "delete_object_if_generation", delete_then_crash)
    with pytest.raises(InjectedCrash, match="deleted"):
        system.repairer().run(report, enabled=True)
    assert system.catalog.get_move_job(JOB_ID).state == MoveJobState.CLEANUP
    with pytest.raises(FileNotFoundError):
        system.drivers["hot"].stat_object(BUCKET, KEY)
    monkeypatch.setattr(system.drivers["hot"], "delete_object_if_generation", original_delete)
    system.clock.advance()

    result = system.repairer().run(report, enabled=True)

    assert _action(result)["status"] == "completed"
    _assert_completed(system)


@pytest.mark.parametrize("field,value", [
    ("transferred_size", len(PAYLOAD) + 1),
    ("source_size", len(PAYLOAD) + 1),
    ("destination_size", len(PAYLOAD) + 1),
    ("destination_size", None),
    ("source_checksum", None),
    ("destination_checksum", None),
    ("destination_generation", None),
    ("verification_details", ("previous verification failed",)),
])
def test_incomplete_or_conflicting_verified_journal_requires_review(
    system: System, field: str, value: Any,
) -> None:
    system.interrupted_move(MoveJobState.TRANSFERRED)
    updates = {
        "destination_size": len(PAYLOAD),
        "destination_checksum": hashlib.sha256(PAYLOAD).hexdigest(),
        "destination_generation": system.drivers["warm"].stat_object(BUCKET, KEY)["generation"],
        field: value,
    }
    system.catalog.transition_move_job(
        JOB_ID, owner_id="original-worker", expected_state=MoveJobState.TRANSFERRED,
        to_state=MoveJobState.VERIFIED, reason="malformed verification checkpoint",
        now=system.clock().isoformat(), lease_expires_at=system.clock().isoformat(), updates=updates,
    )
    report = system.report()
    before = system.source_snapshot()

    result = system.repairer().run(report, enabled=True)

    assert _action(result)["status"] == "quarantined"
    assert system.source_snapshot() == before


def test_catalog_audit_failure_prevents_backend_or_job_changes(system: System, monkeypatch: pytest.MonkeyPatch) -> None:
    system.interrupted_move()
    report = system.report()
    before = system.source_snapshot()

    def reject_audit(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("catalog audit unavailable")

    monkeypatch.setattr(system.catalog, "append_audit_event", reject_audit)

    result = system.repairer().run(report, enabled=True)

    assert _action(result)["status"] == "quarantined"
    assert system.source_snapshot() == before


def test_report_audit_failure_prevents_catalog_or_backend_changes(system: System, monkeypatch: pytest.MonkeyPatch) -> None:
    system.interrupted_move()
    report = system.report()
    before = system.source_snapshot()
    report_bytes = report.read_bytes()

    def reject_audit(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("report audit unavailable")

    monkeypatch.setattr(repair_module, "append_event", reject_audit)

    with pytest.raises(RuntimeError, match="report audit unavailable"):
        system.repairer().run(report, enabled=True)

    assert system.source_snapshot() == before
    assert report.read_bytes() == report_bytes


def test_new_owner_in_same_phase_stops_repair_before_commit(system: System) -> None:
    system.interrupted_move()
    report = system.report()
    stopped: list[dict[str, Any]] = []

    def take_ownership(job: Any) -> None:
        if job.state != MoveJobState.TRANSFERRED:
            return
        now = system.clock() + timedelta(hours=1)
        system.catalog.claim_move_job(
            JOB_ID, src_tier=job.src_tier, dst_tier=job.dst_tier,
            bucket=job.bucket, key=job.key, expected_size=job.expected_size,
            source_metadata=job.source_metadata, owner_id="replacement-worker",
            now=now.isoformat(), lease_expires_at=(now + timedelta(hours=1)).isoformat(),
        )
        stopped.append(system.source_snapshot())

    result = system.repairer(transition_hook=take_ownership).run(report, enabled=True)

    assert len(stopped) == 1
    assert _action(result)["status"] == "quarantined"
    assert system.catalog.get_move_job(JOB_ID).owner_id == "replacement-worker"
    assert system.catalog.get_move_job(JOB_ID).state == MoveJobState.TRANSFERRED
    assert system.source_snapshot() == stopped[0]


@pytest.mark.parametrize("change", ["delete", "replace"])
def test_catalog_commit_atomically_rejects_record_changed_after_final_guard(
    system: System, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    system.interrupted_move(MoveJobState.VERIFIED)
    report = system.report()
    original_commit = system.catalog.commit_move_job_placement
    stopped: list[dict[str, Any]] = []

    def change_before_commit(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["expected_record"] == system.catalog.get(BUCKET, KEY)
        if change == "delete":
            system.catalog.delete(BUCKET, KEY)
        else:
            # Keep size, placement and full content evidence unchanged. The
            # commit must compare the complete record, including fresh metadata.
            record = system.catalog.get(BUCKET, KEY)
            system.catalog.upsert(BUCKET, KEY, record.size, record.tier, {
                **record.metadata, "revision": "new operator record",
            })
        stopped.append(system.source_snapshot())
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(system.catalog, "commit_move_job_placement", change_before_commit)

    result = system.repairer().run(report, enabled=True)

    assert len(stopped) == 1
    assert _action(result)["status"] == "quarantined"
    assert system.catalog.get_move_job(JOB_ID).state == MoveJobState.VERIFIED
    assert system.source_snapshot() == stopped[0]
    assert system.drivers["hot"].get_object(BUCKET, KEY) == PAYLOAD
    assert system.drivers["warm"].get_object(BUCKET, KEY) == PAYLOAD
    assert MoveJobState.COMMITTED not in [
        transition.to_state for transition in system.catalog.list_move_job_transitions(JOB_ID)
    ]
    if change == "delete":
        assert system.catalog.get(BUCKET, KEY) is None
    else:
        assert system.catalog.get(BUCKET, KEY).metadata["revision"] == "new operator record"


@pytest.mark.parametrize("change", ["delete", "replace"])
def test_cleanup_with_absent_source_rechecks_catalog_after_destination_verification(
    system: System, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    system.interrupted_move(MoveJobState.CLEANUP)
    system.drivers["hot"].delete_object(BUCKET, KEY)
    report = system.report()
    original_verify = Mover._verify_committed_destination
    stopped: list[dict[str, Any]] = []

    def verify_then_change(mover: Mover, *args: Any, **kwargs: Any) -> None:
        original_verify(mover, *args, **kwargs)
        if change == "delete":
            system.catalog.delete(BUCKET, KEY)
        else:
            system.catalog.upsert(BUCKET, KEY, len(PAYLOAD) + 1, "warm", {
                "revision": "new operator record",
            })
        stopped.append(system.source_snapshot())

    monkeypatch.setattr(Mover, "_verify_committed_destination", verify_then_change)

    result = system.repairer().run(report, enabled=True)

    assert len(stopped) == 1
    assert _action(result)["status"] == "quarantined"
    assert system.catalog.get_move_job(JOB_ID).state == MoveJobState.CLEANUP
    assert system.source_snapshot() == stopped[0]
    assert system.drivers["warm"].get_object(BUCKET, KEY) == PAYLOAD
    with pytest.raises(FileNotFoundError):
        system.drivers["hot"].stat_object(BUCKET, KEY)
    assert MoveJobState.COMPLETED not in [
        transition.to_state for transition in system.catalog.list_move_job_transitions(JOB_ID)
    ]
