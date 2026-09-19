"""Authenticated bridge to operator-registered, tenant-bound consistency reports.

Only server configuration can select report files and source bindings. A browser
must preview fresh evidence and confirm the exact scope before the existing
repair workflow can resume a durable move. Its audit log also retains outcomes
for status and idempotent HTTP retries across application restarts.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from _thread import LockType
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal
from weakref import WeakValueDictionary

from pydantic import Field

from cognistore.auth.principal import current_principal
from cognistore.auth.tenancy import validate_tenant_id
from cognistore.core.audit import AuditOutcome
from cognistore.core.consistency import ScanScope
from cognistore.core.consistency_repair import ConsistencyRepairer
from cognistore.core.consistency_report import (
    append_event,
    encode,
    open_report,
    read_state,
    verify_binding,
)

from .errors import (
    BackendUnavailableError,
    RequestContractError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from .models import APIModel, Bucket, JSONValue, Tier

if TYPE_CHECKING:
    from cognistore.core.catalog import CatalogStore
    from cognistore.drivers.storage_driver import StorageDriver

RepairIdentifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
RepairState = Literal["ready", "running", "completed", "review_required"]
_REGISTRY_ENV = "COGNISTORE_ADMIN_REPAIR_REPORTS"
_STARTED = "repair.started"
_FINISHED = "repair.completed"
_FAILED = "repair.interrupted"
_SUBMISSIONS: WeakValueDictionary[tuple[str, str], LockType] = WeakValueDictionary()
_SUBMISSION_REGISTRY_LOCK = threading.Lock()


@contextmanager
def _submission_guard(report: _RegisteredReport) -> Iterator[None]:
    # Avoid two requests in this process publishing contradictory attempt
    # status. Across processes the existing durable move leases still fence
    # every storage transition; this lock is only a request/status convenience.
    identity = (report.tenant_id, str(Path(report.path).resolve()))
    with _SUBMISSION_REGISTRY_LOCK:
        lock = _SUBMISSIONS.setdefault(identity, threading.Lock())
    if not lock.acquire(blocking=False):
        raise ResourceConflictError("Repair is already running; inspect its status before retrying")
    try:
        yield
    finally:
        lock.release()


class RepairScope(APIModel):
    tenant_id: Annotated[str, Field(min_length=1, max_length=128)]
    bucket: Bucket
    prefix: Annotated[str, Field(max_length=8192)]
    tiers: Annotated[list[Tier], Field(min_length=1, max_length=256)]


class RepairPreviewRequest(APIModel):
    repair_id: RepairIdentifier


class RepairSubmitRequest(RepairPreviewRequest):
    preview_token: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    confirmation: RepairScope


class RepairPreviewResponse(APIModel):
    repair_id: RepairIdentifier
    scope: RepairScope
    plan_only: Literal[True] = True
    counts: dict[str, int]
    actions: list[dict[str, JSONValue]]
    preview_token: str


class RepairAttempt(APIModel):
    occurred_at: str
    actor_id: str
    status: RepairState
    counts: dict[str, int] = Field(default_factory=dict)


class RepairStatusResponse(APIModel):
    repair_id: RepairIdentifier
    scope: RepairScope
    status: RepairState
    counts: dict[str, int] = Field(default_factory=dict)
    actions: list[dict[str, JSONValue]] = Field(default_factory=list)
    history: list[RepairAttempt] = Field(default_factory=list)


class RepairListError(APIModel):
    repair_id: RepairIdentifier
    message: str


class RepairListResponse(APIModel):
    items: list[RepairStatusResponse]
    errors: list[RepairListError] = Field(default_factory=list)


class _RegisteredReport(APIModel):
    repair_id: RepairIdentifier
    tenant_id: str
    path: str
    binding_id: Annotated[str, Field(min_length=1)]


class AdminRepairMixin:
    @property
    def catalog(self) -> CatalogStore:
        raise NotImplementedError

    @property
    def drivers(self) -> Mapping[str, StorageDriver]:
        raise NotImplementedError

    def _authorize(self, operation: str) -> None:
        raise NotImplementedError

    def _repair_reports(self) -> list[_RegisteredReport]:
        configured = os.environ.get(_REGISTRY_ENV)
        if not configured:
            return []
        try:
            with Path(configured).open("rb") as handle:
                payload = handle.read(1024 * 1024 + 1)
            if len(payload) > 1024 * 1024:
                raise ValueError("oversized registry")
            rows = json.loads(payload)
            if not isinstance(rows, list) or len(rows) > 200:
                raise ValueError("invalid registry")
            reports = [_RegisteredReport.model_validate(row) for row in rows]
            identities = {(row.tenant_id, row.repair_id) for row in reports}
            if len(identities) != len(reports):
                raise ValueError("duplicate report identity")
            for row in reports:
                validate_tenant_id(row.tenant_id)
                if not Path(row.path).is_absolute():
                    raise ValueError("report path must be absolute")
        except (OSError, TypeError, ValueError, RecursionError):
            raise BackendUnavailableError("Repair report configuration is unavailable") from None
        return [row for row in reports if row.tenant_id == self.catalog.tenant_id]

    def _repair_report(self, repair_id: str) -> _RegisteredReport:
        for report in self._repair_reports():
            if report.repair_id == repair_id:
                return report
        raise ResourceNotFoundError("repair", repair_id)

    @staticmethod
    def _repair_scope(report: _RegisteredReport) -> ScanScope:
        try:
            with open_report(Path(report.path), read_only=True) as connection:
                state = read_state(connection)
                verify_binding(state, report.tenant_id, report.binding_id)
                if state["phase"] != "completed":
                    raise ValueError("incomplete report")
                scope = state["scope"]
                first = connection.execute("SELECT payload FROM audit ORDER BY sequence LIMIT 1").fetchone()
                if first is None or json.loads(first[0])["details"]["scope"] != scope:
                    raise ValueError("checkpoint scope differs from audited scope")
                return ScanScope(scope["tenant_id"], scope["bucket"], scope["prefix"], tuple(scope["tiers"]))
        except (OSError, KeyError, TypeError, ValueError, sqlite3.DatabaseError):
            raise BackendUnavailableError("Repair report is unavailable or its binding is invalid") from None

    def _repairer(self, report: _RegisteredReport, scope: ScanScope) -> ConsistencyRepairer:
        try:
            return ConsistencyRepairer(self.catalog, self.drivers, scope, binding_id=report.binding_id)
        except ValueError:
            raise BackendUnavailableError("Repair scope is not configured on this server") from None

    def _repair_preview(self, report: _RegisteredReport, scope: ScanScope) -> RepairPreviewResponse:
        result = self._repairer(report, scope).run(Path(report.path), dry_run=True)
        token = hashlib.sha256(encode({
            "report": report.repair_id, "binding": report.binding_id, "plan": result,
        }).encode()).hexdigest()
        return RepairPreviewResponse(
            repair_id=report.repair_id, scope=RepairScope.model_validate(result["scope"]),
            counts=result["counts"], actions=result["actions"], preview_token=token,
        )

    def preview_repair(self, request: RepairPreviewRequest) -> RepairPreviewResponse:
        self._authorize("preview_repair")
        report = self._repair_report(request.repair_id)
        return self._repair_preview(report, self._repair_scope(report))

    @staticmethod
    def _repair_events(
        report: _RegisteredReport, *, plan_digest: str | None = None,
    ) -> list[dict[str, Any]]:
        with open_report(Path(report.path), read_only=True) as connection:
            verify_binding(read_state(connection), report.tenant_id, report.binding_id)
            events: list[dict[str, Any]] = []
            for (payload,) in connection.execute("SELECT payload FROM audit ORDER BY sequence DESC"):
                event = json.loads(payload)
                if (event["event_type"] not in {_STARTED, _FINISHED, _FAILED}
                        or event["details"].get("admin_submission") is not True):
                    continue
                if plan_digest is not None:
                    # Old receipts remain replayable without loading every
                    # historical action list into memory for each submission.
                    if (event["event_type"] == _FINISHED
                            and event["details"].get("plan_digest") == plan_digest):
                        return [event]
                    continue
                if events and "result" in event["details"]:
                    # Only the latest result needs actions; history renders
                    # counts, actor and time. Keep retained memory bounded.
                    event["details"]["result"].pop("actions", None)
                events.append(event)
                if len(events) == 50:
                    break
            return list(reversed(events))

    @staticmethod
    def _repair_result_status(result: dict[str, Any]) -> RepairState:
        return "review_required" if any(result["counts"].get(name, 0) for name in ("quarantined", "interrupted")) else "completed"

    def _repair_status(self, report: _RegisteredReport) -> RepairStatusResponse:
        scope = self._repair_scope(report)
        events = self._repair_events(report)
        history = [RepairAttempt(
            occurred_at=event["occurred_at"], actor_id=event["actor_id"],
            status="running" if event["event_type"] == _STARTED
            else self._repair_result_status(event["details"]["result"]),
            counts={} if event["event_type"] == _STARTED else event["details"]["result"]["counts"],
        ) for event in events]
        last = events[-1] if events else None
        result = {} if last is None else last["details"].get("result", {})
        return RepairStatusResponse(
            repair_id=report.repair_id, scope=RepairScope.model_validate(scope.to_dict()),
            status=history[-1].status if history else "ready", counts=result.get("counts", {}),
            actions=result.get("actions", []), history=history,
        )

    def get_repair(self, repair_id: str) -> RepairStatusResponse:
        self._authorize("get_repair")
        return self._repair_status(self._repair_report(repair_id))

    def list_repairs(self) -> RepairListResponse:
        self._authorize("list_repairs")
        items, errors = [], []
        for report in self._repair_reports():
            try:
                items.append(self._repair_status(report))
            except Exception:
                errors.append(RepairListError(repair_id=report.repair_id, message="Repair report unavailable"))
        return RepairListResponse(items=items, errors=errors)

    @staticmethod
    def _append_repair_event(report: _RegisteredReport, event_type: str, details: dict[str, Any]) -> None:
        principal = current_principal()
        assert principal is not None  # authenticated authorization precedes any report operation
        with open_report(Path(report.path)) as connection:
            state = read_state(connection)
            verify_binding(state, report.tenant_id, report.binding_id)
            with connection:
                outcome = {_STARTED: AuditOutcome.STARTED, _FAILED: AuditOutcome.FAILED}.get(event_type, AuditOutcome.SUCCEEDED)
                append_event(connection, state, event_type, principal.actor_id,
                             details={**details, "admin_submission": True}, outcome=outcome)

    def submit_repair(self, request: RepairSubmitRequest) -> RepairStatusResponse:
        self._authorize("submit_repair")
        report = self._repair_report(request.repair_id)
        scope = self._repair_scope(report)
        if request.confirmation.model_dump() != scope.to_dict():
            raise RequestContractError("Confirmation must match the complete repair scope")
        with _submission_guard(report):
            return self._submit_repair(request, report, scope)

    def _submit_repair(
        self, request: RepairSubmitRequest, report: _RegisteredReport, scope: ScanScope,
    ) -> RepairStatusResponse:
        # A completed request is replayed without re-running storage effects.
        # Interrupted attempts still reuse the original durable move journal.
        for event in self._repair_events(report, plan_digest=request.preview_token):
            if event["event_type"] == _FINISHED and event["details"].get("plan_digest") == request.preview_token:
                result = event["details"]["result"]
                return RepairStatusResponse(
                    repair_id=report.repair_id, scope=request.confirmation,
                    status=self._repair_result_status(result), counts=result["counts"], actions=result["actions"],
                    history=self._repair_status(report).history,
                )
        preview = self._repair_preview(report, scope)
        if preview.preview_token != request.preview_token:
            raise ResourceConflictError("Repair evidence changed; preview and confirm the scope again")
        principal = current_principal()
        assert principal is not None
        self._append_repair_event(report, _STARTED, {"plan_digest": request.preview_token})
        try:
            result = self._repairer(report, scope).run(Path(report.path), enabled=True, actor_id=principal.actor_id)
        except Exception:
            self._append_repair_event(report, _FAILED, {
                "plan_digest": request.preview_token, "result": {"counts": {"interrupted": 1}, "actions": []},
            })
            raise
        self._append_repair_event(report, _FINISHED, {"plan_digest": request.preview_token, "result": result})
        return self._repair_status(report)
