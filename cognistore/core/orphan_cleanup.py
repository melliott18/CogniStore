"""Explicit, fenced reclamation of one confirmed orphan generation.

Consistency findings are discovery hints, never deletion authority. Quarantine
and deletion attempts live in the catalog's permanent, verified audit journal.
No backend bytes move during quarantine; a new reference always takes priority.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from cognistore.auth.tenancy import DEFAULT_TENANT_ID, require_tenant, validate_tenant_id
from cognistore.core.audit import (
    ORPHAN_CLEANUP_EVENT_TYPES,
    AuditContext,
    AuditEvent,
    AuditOutcome,
    AuditQuery,
    AuditRetentionPolicy,
    canonical_audit_timestamp,
)
from cognistore.core.audit_integrity import digest
from cognistore.core.catalog import CatalogStore
from cognistore.core.consistency import ScanScope
from cognistore.core.content_references import DEFAULT_RECLAMATION_GRACE_PERIOD_SECONDS
from cognistore.core.legal_holds import _scope_identity
from cognistore.core.move_jobs import MoveJobState
from cognistore.drivers.observed import ObservedStorageDriver
from cognistore.drivers.storage_driver import ObjectGenerationMismatchError, StorageDriver
from cognistore.drivers.tenancy import TENANT_STORAGE_DIRECTORY, TenantStorageDriver

_DEFAULT_SECONDS = DEFAULT_RECLAMATION_GRACE_PERIOD_SECONDS


def _seconds(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{name} must be a finite positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    try:
        if timedelta(seconds=result) <= timedelta(0):
            raise ValueError(f"{name} must be at least one microsecond")
    except OverflowError as exc:
        raise ValueError(f"{name} exceeds the supported timestamp range") from exc
    return result


def _identity(value: str) -> str:
    return _scope_identity(value)


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(canonical_audit_timestamp(value).replace("Z", "+00:00"))


class OrphanCleanup:
    """Operator service with report as the default and no implicit execution.

    ``scope`` and ``binding_id`` must come from trusted operator configuration.
    Nondefault tenants require explicitly scoped catalogs *and* storage drivers.
    Callers must use the normal catalog/storage fences for all publications.
    """

    def __init__(
        self, catalog: CatalogStore, drivers: Mapping[str, StorageDriver],
        scope: ScanScope, *, binding_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(scope, ScanScope):
            raise ValueError("scope must be a ScanScope")
        validate_tenant_id(scope.tenant_id)
        if not isinstance(binding_id, str) or not binding_id.strip():
            raise ValueError("a trusted source/tenant binding_id is required")
        self.catalog, self.scope, self.binding_id = catalog, scope, binding_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.drivers: dict[str, StorageDriver] = {}
        for tier, driver in drivers.items():
            # Access observers may append events on reads. Inspection must stay
            # read-only even when called with ordinary instrumented drivers.
            while isinstance(driver, ObservedStorageDriver):
                driver = driver.raw_driver
            if isinstance(driver, TenantStorageDriver):
                raw = driver.driver
                while isinstance(raw, ObservedStorageDriver):
                    raw = raw.raw_driver
                driver = TenantStorageDriver(raw, driver.tenant_id)
            self.drivers[tier] = driver

    def _validate_target(self, key: str, tier: str) -> None:
        if (
            not isinstance(key, str) or not key or "\\" in key or "\0" in key
            or any(part in {"", ".", ".."} for part in key.split("/"))
            or _identity(key.split("/", 1)[0]) == _identity(TENANT_STORAGE_DIRECTORY)
        ):
            raise ValueError("cleanup key must be a canonical relative object key")
        if not key.startswith(self.scope.prefix):
            raise ValueError("cleanup key is outside the authorized prefix")
        if tier not in self.scope.tiers or tier not in self.drivers:
            raise ValueError("cleanup tier is outside the configured scope")

    def _target(self, key: str, tier: str) -> str:
        return digest([self.scope.to_dict(), self.binding_id, key, tier])

    def _resolved(self, driver: StorageDriver) -> bool:
        require_tenant(self.scope.tenant_id)
        if self.catalog.tenant_id != self.scope.tenant_id:
            return False
        if isinstance(driver, TenantStorageDriver):
            return driver.tenant_id == self.scope.tenant_id
        return self.scope.tenant_id == DEFAULT_TENANT_ID

    def _journal(self, candidate_id: str, key: str, tier: str) -> tuple[AuditEvent, AuditEvent]:
        identifier = str(UUID(candidate_id))
        if not self.catalog.verify_audit_integrity().valid:
            raise ValueError("cleanup requires a valid catalog audit chain")
        quarantine = self.catalog.get_audit_event(identifier)
        if (
            quarantine is None or quarantine.event_type != "orphan.quarantined"
            or quarantine.correlation_id != identifier
            or quarantine.details.get("version") != 1
            or quarantine.details.get("target") != self._target(key, tier)
        ):
            raise ValueError("quarantine does not match this tenant, source, or target")
        latest = self.catalog.list_audit_events(AuditQuery(
            correlation_id=identifier, event_types=ORPHAN_CLEANUP_EVENT_TYPES, ascending=False, limit=1,
        ))
        if not latest:
            raise ValueError("cleanup journal is missing")
        return quarantine, latest[0]

    def _inspect(
        self, key: str, tier: str, *, now: datetime, retention: float,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
        driver = self.drivers[tier]
        conditions: dict[str, Any] = {}
        blockers: list[str] = []
        result: dict[str, Any] = {
            "tenant_id": self.scope.tenant_id, "bucket": self.scope.bucket,
            "key": key, "tier": tier, "conditions": conditions, "blockers": blockers,
        }
        resolved = self._resolved(driver)
        conditions["tenant"] = {"resolved": resolved}
        if not resolved:
            blockers.append("unresolved_tenant")
            return result, None, ""

        integrity = self.catalog.verify_audit_integrity()
        conditions["audit"] = {"valid": integrity.valid, "issues": list(integrity.issues)}
        if not integrity.valid:
            blockers.append("audit_integrity")

        # A reference on any tier protects the logical key. Alias matching is
        # deliberately conservative for case-insensitive/normalizing filesystems.
        references = self.catalog.orphan_cleanup_references(self.scope.bucket, key)
        conditions["catalog"] = {"reference_count": len(references), "clear": not references}
        if references:
            blockers.append("catalog_reference")

        report = self.catalog.reconcile_content_references(grace_period_seconds=retention, now=now)
        entries = [entry for entry in report.entries if _identity(entry.cas_key) == _identity(key)]
        cas = bool(entries) or _identity(key).startswith(_identity("cas") + "/")
        cas_conditions: dict[str, Any] = {
            "applicable": cas, "resolved": bool(entries) if cas else True,
            "entries": [entry.to_dict() for entry in entries],
        }
        conditions["cas"] = cas_conditions
        if cas and not entries:
            blockers.append("unresolved_cas")
        if any(entry.expected_reference_count or entry.stored_reference_count for entry in entries):
            blockers.append("cas_reference")
        if any(entry.issues for entry in entries):
            blockers.append("cas_inconsistent")
        if any(not entry.reclamation_eligible for entry in entries):
            blockers.append("cas_not_reclaimable")

        jobs = [job for job in self.catalog.list_move_jobs() if cas or (
            _identity(job.bucket) == _identity(self.scope.bucket) and _identity(job.key) == _identity(key)
        )]
        # Failed jobs can retain recovery evidence/copies; lease expiry never
        # establishes orphanhood. Only completed jobs cease to protect bytes.
        protected_jobs = [job for job in jobs if job.state != MoveJobState.COMPLETED]
        conditions["jobs"] = {
            "clear": not protected_jobs, "count": len(protected_jobs),
            "states": sorted({job.state.value for job in protected_jobs}),
        }
        if protected_jobs:
            blockers.append("active_job")

        holds = self.catalog.list_legal_holds(active_only=True)
        relevant_holds = [hold for hold in holds if cas or hold.matches(self.scope.bucket, key)]
        conditions["legal_holds"] = {"clear": not relevant_holds, "count": len(relevant_holds)}
        if relevant_holds:
            blockers.append("legal_hold")

        stat: dict[str, Any] | None = None
        conditions["backend"] = {"exists": None, "conditional_delete": driver.capabilities.conditional_delete}
        conditions["retention"] = {"seconds": retention, "satisfied": False}
        if not driver.capabilities.conditional_delete:
            blockers.append("conditional_delete_unavailable")
        try:
            stat = driver.stat_object(self.scope.bucket, key)
            generation, size, mtime = stat.get("generation"), stat.get("size"), stat.get("mtime")
            if (
                not isinstance(generation, str) or not generation
                or isinstance(size, bool) or not isinstance(size, int) or size < 0
                or isinstance(mtime, bool) or not isinstance(mtime, (float, int))
                or not math.isfinite(mtime)
            ):
                raise ValueError("backend returned invalid generation, size, or modification time")
            deadline = datetime.fromtimestamp(mtime, timezone.utc) + timedelta(seconds=retention)
            conditions["backend"].update(exists=True, size=size, generation_digest=digest(generation))
            conditions["retention"].update(
                satisfied=now >= deadline, eligible_at=canonical_audit_timestamp(deadline),
            )
            if now < deadline:
                blockers.append("retention_window")
            if any(entry.size != size for entry in entries):
                blockers.append("cas_size_mismatch")
        except FileNotFoundError:
            conditions["backend"]["exists"] = False
            blockers.append("backend_missing")
        except Exception as exc:
            stat = None
            conditions["backend"]["error_type"] = type(exc).__name__
            blockers.append("backend_error")
        reference_state = digest({
            "catalog_revision": self.catalog.orphan_cleanup_revision(self.scope.bucket, key),
            "cas": [{"sha256": entry.sha256, "unreferenced_at": entry.unreferenced_at,
                     "stored": entry.stored_reference_count, "expected": entry.expected_reference_count}
                    for entry in entries],
            "jobs": [asdict(job) for job in sorted(jobs, key=lambda job: job.idempotency_key)],
        })
        return result, stat, reference_state

    def _append(
        self, kind: str, outcome: AuditOutcome, context: AuditContext,
        candidate_id: str, key: str, *, now: datetime, details: Mapping[str, Any],
        previous: AuditEvent | None = None,
    ) -> AuditEvent:
        # Ordering cannot depend on UUID ties or on a wall clock moving backward.
        if previous is not None:
            now = max(now, _timestamp(previous.occurred_at) + timedelta(microseconds=1))
        return self.catalog.append_audit_event(AuditEvent.create(
            kind, outcome,
            AuditContext(candidate_id, context.actor_type, context.actor_id,
                         causation_id=None if previous is None else previous.event_id),
            event_id=candidate_id if kind == "orphan.quarantined" else None,
            occurred_at=now, retention=AuditRetentionPolicy(max_age_seconds=None),
            bucket=self.scope.bucket, object_key=key, details=details,
        ))

    def run(
        self, key: str, tier: str, *, stage: str = "report", candidate_id: str | None = None,
        grace_period_seconds: float = _DEFAULT_SECONDS, retention_seconds: float = _DEFAULT_SECONDS,
        context: AuditContext | None = None,
    ) -> dict[str, Any]:
        self._validate_target(key, tier)
        if stage not in {"report", "quarantine", "execute"}:
            raise ValueError("stage must be report, quarantine, or execute")
        grace, retention = _seconds(grace_period_seconds, "grace_period_seconds"), _seconds(retention_seconds, "retention_seconds")
        if stage == "execute" and candidate_id is None:
            raise ValueError("execution requires a persisted candidate_id")
        if stage == "quarantine" and candidate_id is not None:
            raise ValueError("quarantine creates a new candidate_id")
        if stage != "report":
            if not isinstance(context, AuditContext) or context.actor_type != "user":
                raise ValueError("cleanup mutation requires an explicit user audit context")
            if getattr(self.catalog, "read_only", False):
                raise PermissionError("cleanup requires a writable catalog")
            with self.catalog.orphan_cleanup_fence():
                return self._run(key, tier, stage, candidate_id, grace, retention, context)
        return self._run(key, tier, stage, candidate_id, grace, retention, context)

    def _run(
        self, key: str, tier: str, stage: str, candidate_id: str | None,
        grace: float, retention: float, context: AuditContext | None,
    ) -> dict[str, Any]:
        now = _timestamp(canonical_audit_timestamp(self.clock()))
        quarantine = latest = None
        if candidate_id is not None:
            quarantine, latest = self._journal(candidate_id, key, tier)
            # Execution cannot shorten the durable observation/grace contract.
            grace = _seconds(quarantine.details["grace_period_seconds"], "stored grace")
            retention = max(retention, _seconds(quarantine.details["retention_seconds"], "stored retention"))
        result, stat, reference_state = self._inspect(key, tier, now=now, retention=retention)
        result.update(stage=stage, dry_run=stage == "report", candidate_id=candidate_id)
        blockers = result["blockers"]
        conditions = result["conditions"]
        conditions["quarantine"] = {
            "present": quarantine is not None, "grace_period_seconds": grace,
            "grace_elapsed": False,
        }
        changed = False
        if quarantine is not None:
            assert latest is not None
            deadline = _timestamp(str(quarantine.details["eligible_at"]))
            conditions["quarantine"].update(eligible_at=canonical_audit_timestamp(deadline), grace_elapsed=now >= deadline,
                                            journal_state=latest.event_type)
            if latest.event_type == "orphan.deleted":
                result["status"] = "already_deleted"
                return result
            if latest.event_type == "orphan.invalidated":
                blockers.append("quarantine_invalidated")
            if now < deadline:
                blockers.append("grace_period")
            changed = reference_state != quarantine.details["references"]
            if stat is not None:
                changed = changed or digest(stat["generation"]) != quarantine.details["generation"]
                changed = changed or digest([stat["size"], stat["mtime"]]) != quarantine.details["observation"]
            if changed:
                blockers.append("observation_changed")

        result["status"] = "blocked" if blockers else "eligible"
        if stage == "report":
            return result
        assert context is not None
        if stage == "quarantine":
            if blockers:
                return result
            assert stat is not None
            identifier = str(uuid4())
            # Inspection may be slow. Its start time must not consume the
            # waiting period before quarantine evidence is ready to persist.
            now = max(now, _timestamp(canonical_audit_timestamp(self.clock())))
            deadline = now + timedelta(seconds=grace)
            self._append("orphan.quarantined", AuditOutcome.SELECTED, context, identifier, key, now=now, details={
                "version": 1, "target": self._target(key, tier), "tier": tier,
                "generation": digest(stat["generation"]), "observation": digest([stat["size"], stat["mtime"]]),
                "references": reference_state, "grace_period_seconds": grace,
                "retention_seconds": retention, "eligible_at": canonical_audit_timestamp(deadline),
            })
            result.update(status="quarantined", candidate_id=identifier)
            conditions["quarantine"].update(present=True, eligible_at=canonical_audit_timestamp(deadline))
            return result

        assert latest is not None and candidate_id is not None
        retry = latest.event_type in {"orphan.delete_started", "orphan.delete_failed"}
        # A missing object is completion evidence only after a durable attempt.
        # Never interpret a failed stat request as absence.
        recovered_absence = retry and conditions.get("backend", {}).get("exists") is False
        effective_blockers = [item for item in blockers if not (recovered_absence and item == "backend_missing")]
        invalidating = {
            "catalog_reference", "cas_reference", "cas_inconsistent", "cas_not_reclaimable",
            "unresolved_cas", "active_job", "legal_hold", "observation_changed", "cas_size_mismatch",
        }
        if effective_blockers:
            if latest.event_type != "orphan.invalidated" and (
                invalidating.intersection(effective_blockers)
                or ("backend_missing" in effective_blockers and not retry)
            ):
                self._append("orphan.invalidated", AuditOutcome.REJECTED, context, candidate_id, key,
                             now=now, previous=latest, details={"blockers": effective_blockers})
                result["status"] = "invalidated"
            return result
        if recovered_absence:
            self._append("orphan.deleted", AuditOutcome.SUCCEEDED, context, candidate_id, key,
                         now=now, previous=latest, details={"tier": tier, "confirmed_absent": True,
                                                         "recovered_attempt": True})
            result.update(status="deleted", recovered_attempt=True, blockers=[])
            return result

        assert stat is not None
        started = self._append("orphan.delete_started", AuditOutcome.STARTED, context, candidate_id, key,
                               now=now, previous=latest, details={"tier": tier,
                               "generation": digest(stat["generation"]), "size": stat["size"]})
        try:
            existed = self.drivers[tier].delete_object_if_generation(self.scope.bucket, key, stat["generation"])
        except ObjectGenerationMismatchError:
            self._append("orphan.invalidated", AuditOutcome.REJECTED, context, candidate_id, key,
                         now=now, previous=started, details={"blockers": ["observation_changed"]})
            result.update(status="invalidated", blockers=["observation_changed"])
            return result
        except Exception as exc:
            self._append("orphan.delete_failed", AuditOutcome.FAILED, context, candidate_id, key,
                         now=now, previous=started, details={"error_type": type(exc).__name__, "retryable": True})
            result.update(status="failed", error_type=type(exc).__name__, retryable=True)
            return result
        # Failure to append this outcome deliberately propagates. The retained
        # started event makes the uncertain physical outcome visible on retry.
        self._append("orphan.deleted", AuditOutcome.SUCCEEDED, context, candidate_id, key,
                     now=now, previous=started, details={"tier": tier, "deleted": existed,
                                                      "confirmed_absent": not existed})
        result.update(status="deleted")
        return result
