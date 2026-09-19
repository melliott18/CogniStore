"""Conservative, opt-in recovery of unambiguous consistency discrepancies.

Reports select keys, never authorize writes. Every attempt uses fresh catalog,
policy and generation-bound full-object evidence. The existing move journal is
also the repair journal: its original key fences retries across reports/processes.
Quarantine is an audited operator-review decision, not a storage mutation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from cognistore.core.audit import AuditContext, AuditEvent, AuditOutcome
from cognistore.core.catalog import CatalogStore, ObjectRecord
from cognistore.core.consistency import ConsistencyScanner, ScanScope
from cognistore.core.consistency_report import (
    append_event,
    encode,
    open_report,
    read_state,
    verify_binding,
)
from cognistore.core.locality import evaluate_locality
from cognistore.core.move_jobs import (
    EXPECTED_SOURCE_SHA256_METADATA_KEY,
    MoveJob,
    MoveJobLeaseError,
    MoveJobState,
)
from cognistore.core.mover import Mover
from cognistore.core.placement_controls import assert_move_allowed
from cognistore.drivers.observed import suppress_access_capture
from cognistore.drivers.storage_driver import StorageDriver

_PRECOMMIT = {MoveJobState.PREPARED, MoveJobState.TRANSFERRED, MoveJobState.VERIFIED}


class RepairQuarantinedError(RuntimeError):
    """Fresh evidence no longer permits automatic recovery."""


class ConsistencyRepairer:
    def __init__(
        self, catalog: CatalogStore, drivers: Mapping[str, StorageDriver], scope: ScanScope,
        *, binding_id: str, requests_per_second: float = 20,
        bytes_per_second: float = 8 * 1024 * 1024,
        clock: Callable[[], datetime] | None = None,
        transition_hook: Callable[[MoveJob], None] | None = None,
    ) -> None:
        self.catalog, self.scope, self.binding_id = catalog, scope, binding_id
        self.scanner = ConsistencyScanner(
            catalog, drivers, scope, binding_id=binding_id,
            requests_per_second=requests_per_second, bytes_per_second=bytes_per_second,
        )
        self.drivers = {tier: drivers[tier] for tier in scope.tiers}
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.transition_hook = transition_hook

    def run(
        self, report_path: Path, *, enabled: bool = False, dry_run: bool = False,
        actor_id: str = "cli",
    ) -> dict[str, Any]:
        if not isinstance(enabled, bool) or not isinstance(dry_run, bool):
            raise ValueError("enabled and dry_run must be booleans")
        report_path = Path(report_path)
        self.scanner._protect_report(report_path)
        plan_only = not enabled or dry_run
        actions: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        with open_report(report_path, read_only=dry_run) as connection, suppress_access_capture():
            state = read_state(connection)
            verify_binding(state, self.scope.tenant_id, self.binding_id)
            if state["scope"] != self.scope.to_dict():
                raise ValueError("repair scope does not match report")
            if state["phase"] != "completed":
                raise ValueError("repair requires a completed consistency report")

            # Findings are not authenticated by the audit chain. Validate their
            # namespace, and use only distinct keys as hints for fresh inspection.
            keys: set[str] = set()
            for (payload,) in connection.execute("SELECT payload FROM findings ORDER BY sequence"):
                finding = json.loads(payload)
                key = finding.get("key")
                if (finding.get("tenant_id") != self.scope.tenant_id
                        or finding.get("bucket") != self.scope.bucket
                        or not isinstance(key, str) or not key.startswith(self.scope.prefix)):
                    raise ValueError("finding is outside the trusted repair scope")
                keys.add(key)

            def record(action: dict[str, Any], event: str | None = None) -> None:
                if not dry_run:
                    outcome = {
                        "quarantined": AuditOutcome.REJECTED,
                        "interrupted": AuditOutcome.FAILED,
                    }.get(action["status"], AuditOutcome.SUCCEEDED)
                    if event == "repair.started":
                        outcome = AuditOutcome.STARTED
                    with connection:
                        append_event(
                            connection, state, event or "repair." + action["status"], actor_id,
                            details={"repair": action, "plan_only": plan_only},
                            outcome=outcome,
                        )

            for key in sorted(keys):
                action: dict[str, Any] = {
                    "bucket": self.scope.bucket, "key": key, "job_id": None,
                    "action": "resume_move", "status": "quarantined", "reason": "uncertain",
                }
                try:
                    job = self._assess(key)
                    if job is None:
                        action.update(action="none", status="resolved", reason="no_discrepancy")
                    else:
                        identity = [self.binding_id, self.scope.bucket, key, job.idempotency_key]
                        action.update(
                            job_id=job.idempotency_key,
                            repair_id=hashlib.sha256(encode(identity).encode()).hexdigest(),
                            status="planned", reason="verified_partial_job", phase=job.state.value,
                            source_tier=job.src_tier, destination_tier=job.dst_tier,
                            expected_size=job.expected_size,
                        )
                        if not plan_only:
                            # Persist intent before any catalog/backend side effect.
                            # Failure to publish audit evidence prevents execution.
                            record(action, "repair.started")
                            context = AuditContext(
                                correlation_id=action["repair_id"], actor_type="operator",
                                actor_id=actor_id,
                            )
                            self.catalog.append_audit_event(AuditEvent.create(
                                "repair.started", AuditOutcome.ALLOWED, context,
                                bucket=self.scope.bucket, object_key=key,
                                move_id=job.idempotency_key,
                                details={"scan_id": state["scan_id"], "repair_id": action["repair_id"],
                                         "tenant_id": self.scope.tenant_id},
                            ))
                            owner = "repair:" + str(uuid4())

                            def guard(current: MoveJob) -> None:
                                observed = self._assess(key, owned_by=owner)
                                if (observed is None or observed.idempotency_key != job.idempotency_key
                                        or self._contract(observed) != self._contract(current)):
                                    raise RepairQuarantinedError("job_changed")

                            # Reassess after intent/audit and before claiming the
                            # durable job. Live leases always belong to another
                            # attempt here, even if a previous attempt crashed.
                            selected = self._assess(key)
                            if selected is None or self._contract(selected) != self._contract(job):
                                raise RepairQuarantinedError("job_changed")
                            mover = Mover(
                                self.drivers, self.catalog, owner_id=owner, clock=self.clock,
                                transition_hook=self.transition_hook, checkpoint_guard=guard,
                            )
                            mover.move(
                                job.src_tier, job.dst_tier, job.bucket, job.key,
                                idempotency_key=job.idempotency_key, audit_context=context,
                            )
                            action.update(status="completed", reason="move_completed")
                except RepairQuarantinedError as exc:
                    action.update(status="quarantined", reason=str(exc))
                except MoveJobLeaseError:
                    action.update(status="quarantined", reason="live_lease")
                except Exception as exc:
                    # Includes backend, policy and audit failures. Never guess
                    # that an inaccessible object is absent or erase its copies.
                    action.update(status="quarantined", reason="verification_or_execution_failed",
                                  exception_type=type(exc).__name__)
                except BaseException as exc:
                    action.update(status="interrupted", reason="interrupted",
                                  exception_type=type(exc).__name__)
                    record(action)
                    raise
                record(action)
                actions.append(action)
                counts[action["status"]] = counts.get(action["status"], 0) + 1
        return {
            "schema": "cognistore.consistency.repair", "version": 1,
            "plan_only": plan_only, "scope": self.scope.to_dict(),
            "counts": counts, "actions": actions,
        }

    @staticmethod
    def _contract(job: MoveJob) -> MoveJob:
        # Heartbeats can refresh these fields while verification streams bytes.
        # State, owner, generations and all frozen content/policy evidence remain
        # authoritative and must match across that observation.
        return replace(job, lease_expires_at=None, updated_at="")

    def _policy(self, record: ObjectRecord, job: MoveJob) -> None:
        if self.catalog.list_legal_holds(bucket=job.bucket, key=job.key, active_only=True):
            raise RepairQuarantinedError("legal_hold")
        locality = evaluate_locality(
            record, tenant_id=self.catalog.tenant_id,
            tiers=self.catalog.list_tiers(), pools=self.catalog.list_pools(), as_of=self.clock(),
        )
        if locality["configured"] or any(name in job.source_metadata for name in (
            "cognistore_locality", "cognistore_locality_exception_id",
            "cognistore_expected_destination_pool_id",
        )):
            raise RepairQuarantinedError("locality_constrained")
        # Before placement commit, use the original move's frozen controls plus
        # the current object and tier policy. After commit, cleanup does not move
        # the placement again; the original move has already started its timers.
        if job.state in _PRECOMMIT:
            tier = self.catalog.get_tier(job.src_tier)
            assert_move_allowed(
                record, job.dst_tier, job.source_metadata.get("cognistore_movement_constraints"),
                tier_metadata=None if tier is None else tier.metadata, as_of=self.clock(),
            )

    def _assess(self, key: str, *, owned_by: str | None = None) -> MoveJob | None:
        bucket = self.scope.bucket
        fence = self.catalog.capture_scan_fence(bucket, key)
        record = self.catalog.get(bucket, key)
        jobs = [self.catalog.get_move_job(row[0]) for row in fence.move_jobs]
        if any(job is None for job in jobs):
            raise RepairQuarantinedError("job_changed")
        present_jobs = [job for job in jobs if job is not None]
        active = [job for job in present_jobs if not job.state.terminal]
        latest = max(present_jobs, key=lambda job: (job.created_at, job.updated_at, job.idempotency_key),
                     default=None)
        if not active:
            # Completed jobs never resurrect keys or demand a historical copy.
            # Fresh consistency evidence also distinguishes resolved old findings
            # from unexplained duplicates, orphans, missing/corrupt placements.
            findings = self.scanner._inspect(key, set())
            if findings:
                reasons = sorted({finding["reason_code"] for finding in findings})
                raise RepairQuarantinedError("unrepairable:" + ",".join(reasons))
            return None
        if len(active) != 1 or latest != active[0]:
            raise RepairQuarantinedError("ambiguous_jobs")
        job = active[0]
        if record is None:
            raise RepairQuarantinedError("missing_catalog_record")
        if job.bucket != bucket or job.key != key:
            raise RepairQuarantinedError("job_outside_scope")
        if job.src_tier not in self.drivers or job.dst_tier not in self.drivers:
            raise RepairQuarantinedError("scope_incomplete")
        if job.src_tier == job.dst_tier or len(self.scanner.drivers) != len(self.drivers):
            raise RepairQuarantinedError("ambiguous_backends")
        if owned_by is not None and job.owner_id != owned_by:
            raise RepairQuarantinedError("job_owner_changed")
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("repair clock must return a timezone-aware datetime")
        if job.owner_id != owned_by and job.lease_expires_at is not None:
            expiry = datetime.fromisoformat(job.lease_expires_at.replace("Z", "+00:00"))
            if expiry > now:
                raise RepairQuarantinedError("live_lease")
        expected_tier = job.src_tier if job.state in _PRECOMMIT else job.dst_tier
        if record.tier != expected_tier or record.size != job.expected_size:
            raise RepairQuarantinedError("catalog_changed")
        self._policy(record, job)
        if job.verification_details:
            raise RepairQuarantinedError("failed_verification_evidence")
        if job.state != MoveJobState.PREPARED and (
            job.source_size != job.expected_size or job.transferred_size != job.expected_size
            or self.scanner._digest(job.source_checksum) is None
        ):
            raise RepairQuarantinedError("incomplete_transfer_evidence")
        if job.state in {MoveJobState.VERIFIED, MoveJobState.COMMITTED, MoveJobState.CLEANUP} and (
            job.destination_size != job.expected_size
        ):
            raise RepairQuarantinedError("incomplete_verification_evidence")

        observations = {tier: self.scanner._observe(driver, key)
                        for tier, driver in self.drivers.items()}
        source, destination = observations[job.src_tier], observations[job.dst_tier]
        if any(observed is not None for tier, observed in observations.items()
               if tier not in {job.src_tier, job.dst_tier}):
            raise RepairQuarantinedError("duplicate_placement")
        if source is None and job.state != MoveJobState.CLEANUP:
            raise RepairQuarantinedError("missing_source")
        if source is not None and source["generation"] != job.source_metadata.get("generation"):
            raise RepairQuarantinedError("source_generation_changed")
        if destination is None and job.state != MoveJobState.PREPARED:
            raise RepairQuarantinedError("missing_destination")
        if job.state in {MoveJobState.VERIFIED, MoveJobState.COMMITTED, MoveJobState.CLEANUP}:
            if (destination is None or destination["generation"] != job.destination_generation
                    or self.scanner._digest(job.destination_checksum) is None):
                raise RepairQuarantinedError("destination_generation_changed")

        digests = {
            digest for digest in (
                self.scanner._catalog_checksum(record), self.scanner._digest(job.source_checksum),
                self.scanner._digest(job.destination_checksum),
                self.scanner._digest(job.source_metadata.get(EXPECTED_SOURCE_SHA256_METADATA_KEY)),
            ) if digest is not None
        }
        if not digests:
            raise RepairQuarantinedError("checksum_unavailable")
        if len(digests) != 1:
            raise RepairQuarantinedError("conflicting_checksums")
        expected_digest = next(iter(digests))
        for observed in observations.values():
            if observed is not None:
                if observed["size"] != job.expected_size:
                    raise RepairQuarantinedError("size_mismatch")
                if observed["sha256"] != expected_digest:
                    raise RepairQuarantinedError("checksum_mismatch")
        # Include absences in the final fence: a racing create invalidates the
        # same evidence as replacement/deletion of an observed copy.
        for tier, observed in observations.items():
            stat = self.scanner._stat(self.drivers[tier], key)
            before = None if observed is None else {name: observed[name]
                                                    for name in ("size", "generation")}
            if stat != before:
                raise RepairQuarantinedError("object_changed")
        current = self.catalog.get_move_job(job.idempotency_key)
        after = self.catalog.capture_scan_fence(bucket, key)
        # Ignore heartbeat timestamps only for our claimed job. Other changes
        # (including another job appearing) invalidate this observation.
        before_rows = tuple(row[:-1] if row[0] == job.idempotency_key and owned_by else row
                            for row in fence.move_jobs)
        after_rows = tuple(row[:-1] if row[0] == job.idempotency_key and owned_by else row
                           for row in after.move_jobs)
        if (current is None or self._contract(current) != self._contract(job)
                or record != self.catalog.get(bucket, key) or before_rows != after_rows):
            raise RepairQuarantinedError("object_changed")
        self._policy(record, current)
        return current
