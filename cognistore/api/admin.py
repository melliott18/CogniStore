"""Authorized, tenant-scoped operational views with allowlisted configuration."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, TypeAlias

from pydantic import Field

from cognistore.auth.authorization import RBACAuthorizer, current_authorizer
from cognistore.auth.principal import current_principal
from cognistore.auth.tenancy import current_tenant_id
from cognistore.core.audit import AuditEventType, AuditQuery
from cognistore.drivers.storage_driver import StorageDriver
from cognistore.jobs.protocols import JobQueue

from .errors import RequestContractError
from .models import APIModel, JobStatusResponse, PageMetadata
from .pagination import CursorError, decode_cursor, encode_cursor
from .permissions import ANY_PERMISSION_OPERATIONS, OPERATION_PERMISSIONS

if TYPE_CHECKING:
    from cognistore.core.catalog import CatalogStore


class AdminSession(APIModel):
    schema_version: Literal[1] = 1
    tenant_id: str
    actor_id: str
    operations: list[str]


HealthState: TypeAlias = Literal["ready", "unavailable", "not_configured", "unverified"]


class DependencyHealth(APIModel):
    status: HealthState


class DriverCapabilityView(APIModel):
    range_reads: bool = False
    range_writes: bool = False
    atomic_no_overwrite: bool = False
    conditional_delete: bool = False


class DriverEncryptionView(APIModel):
    source: str = "unknown"
    mode: str = "unknown"
    key_configured: bool = False


class PoolView(APIModel):
    pool_id: str
    region: str | None
    localities: list[str]
    member_count: int
    active: bool


class TierView(APIModel):
    name: str
    active: bool | None
    driver: str | None
    capabilities: DriverCapabilityView
    encryption: DriverEncryptionView
    health: DependencyHealth
    pools: list[PoolView] = Field(default_factory=list)


class AdminStorage(APIModel):
    schema_version: Literal[1] = 1
    tenant_id: str
    observed_at: str
    catalog: DependencyHealth
    queue: DependencyHealth
    tiers: list[TierView]


class JobHistoryError(APIModel):
    job_id: str
    message: str


class JobHistoryPage(APIModel):
    schema_version: Literal[1] = 1
    items: list[JobStatusResponse]
    page: PageMetadata
    errors: list[JobHistoryError] = Field(default_factory=list)


_ENCRYPTION_SOURCES = frozenset({"unknown", "none", "provider", "operator-attestation"})
_ENCRYPTION_MODES = frozenset({
    "unknown", "unattested", "provider-default", "provider-managed", "customer-managed",
    "AES256", "aws:kms", "aws:kms:dsse", "encryption-scope", "volume", "filesystem",
    "application", "encrypted-volume", "encrypted-filesystem",
})


class AdminAccessMixin:
    """Use gateway authorization and catalog properties at every read boundary."""

    authorization: RBACAuthorizer | None
    tenant_id: str
    _base_drivers: Mapping[str, StorageDriver]
    queue: JobQueue | None

    @property
    def catalog(self) -> CatalogStore:
        raise NotImplementedError

    def _authorize(self, operation: str) -> None:
        raise NotImplementedError

    def _get_job(self, job_id: str) -> JobStatusResponse:
        raise NotImplementedError

    def get_admin_session(self) -> AdminSession:
        self._authorize("get_admin_session")
        principal = current_principal()
        assert principal is not None
        authorizers = [
            policy for policy in (self.authorization, current_authorizer()) if policy is not None
        ] or [RBACAuthorizer()]
        granted = authorizers[0].permissions_for(principal)
        # An injected HTTP policy may further restrict the gateway policy.
        # Discovery must reflect both boundaries, as every action does.
        for policy in authorizers[1:]:
            granted &= policy.permissions_for(principal)
        return AdminSession(
            tenant_id=current_tenant_id() or self.tenant_id,
            actor_id=principal.actor_id,
            operations=sorted(
                operation for operation, permissions in OPERATION_PERMISSIONS.items()
                if permissions and (
                    bool(set(permissions) & granted)
                    if operation in ANY_PERMISSION_OPERATIONS else set(permissions) <= granted
                )
            ),
        )

    def _storage_view(self) -> AdminStorage:
        # These are deliberately projections, never driver __dict__ or free-form
        # metadata. Secrets, filesystem paths, endpoints and pool members are
        # omitted even for administrators.
        catalog_health: HealthState = "ready"
        definitions = {}
        pools = []
        try:
            definitions = {tier.name: tier for tier in self.catalog.list_tiers()}
            pools = self.catalog.list_pools()
        except Exception:
            catalog_health = "unavailable"
        tiers = []
        for name in sorted(set(definitions) | set(self._base_drivers)):
            driver = self._base_drivers.get(name)
            health: HealthState = "not_configured" if driver is None else "unverified"
            capabilities = DriverCapabilityView()
            encryption = DriverEncryptionView()
            if driver is not None:
                try:
                    capabilities = DriverCapabilityView(**{
                        field: getattr(driver.capabilities, field) is True
                        for field in DriverCapabilityView.model_fields
                    })
                    status = driver.encryption_status()
                    source, mode = status.get("source"), status.get("mode")
                    encryption = DriverEncryptionView(
                        source=source if isinstance(source, str) and source in _ENCRYPTION_SOURCES else "unknown",
                        mode=mode if isinstance(mode, str) and mode in _ENCRYPTION_MODES else "unknown",
                        key_configured=status.get("key_configured") is True,
                    )
                except Exception:
                    health = "unavailable"
            tiers.append(TierView(
                name=name,
                active=definitions[name].active if name in definitions else None,
                driver=type(driver).__name__ if driver is not None else None,
                capabilities=capabilities,
                encryption=encryption,
                health=DependencyHealth(status=health),
                pools=[PoolView(
                    pool_id=pool.pool_id, region=pool.region, localities=list(pool.localities),
                    member_count=len(pool.members), active=pool.active,
                ) for pool in pools if pool.tier == name],
            ))
        return AdminStorage(
            tenant_id=current_tenant_id() or self.tenant_id,
            observed_at=datetime.now(timezone.utc).isoformat(),
            catalog=DependencyHealth(status=catalog_health),
            queue=DependencyHealth(status="not_configured"),
            tiers=tiers,
        )

    async def get_admin_storage(self) -> AdminStorage:
        await asyncio.to_thread(self._authorize, "get_admin_storage")
        result = await asyncio.to_thread(self._storage_view)
        if self.queue is not None:
            try:
                health = await asyncio.wait_for(self.queue.probe(), timeout=2.0)
                result.queue = DependencyHealth(status="ready" if health.ready else "unavailable")
            except Exception:
                result.queue = DependencyHealth(status="unavailable")
        return result

    def list_jobs(self, *, limit: int = 50, cursor: str | None = None) -> JobHistoryPage:
        self._authorize("list_jobs")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise RequestContractError("limit must be between 1 and 200")
        try:
            last_key = decode_cursor(cursor, resource="jobs", filters={})
            boundary = None if last_key is None else json.loads(last_key)
            if boundary is not None and (not isinstance(boundary, list) or len(boundary) != 2):
                raise ValueError("invalid boundary")
            query = AuditQuery(
                event_types=frozenset({AuditEventType.JOB_QUEUED.value}),
                before_event=None if boundary is None else tuple(boundary),
                ascending=False, limit=limit + 1,
            )
        except (CursorError, TypeError, ValueError) as exc:
            raise RequestContractError("cursor is invalid for this job history", code="invalid_cursor") from exc
        events = self.catalog.list_audit_events(query)
        next_cursor = None
        if len(events) > limit:
            last = events[limit - 1]
            next_cursor = encode_cursor(
                resource="jobs", filters={},
                last_key=json.dumps([last.occurred_at, last.event_id]),
            )
        items = []
        errors = []
        seen: set[str] = set()
        for event in events[:limit]:
            if event.job_id is None or event.job_id in seen:
                continue
            seen.add(event.job_id)
            try:
                items.append(self._get_job(event.job_id))
            except Exception:
                # Retention can remove creation evidence between these reads,
                # and a corrupt historical record must not hide healthy jobs.
                # The event boundary still advances past failed status reads.
                errors.append(JobHistoryError(
                    job_id=event.job_id, message="Job status unavailable",
                ))
        return JobHistoryPage(
            items=items, errors=errors,
            page=PageMetadata(limit=limit, next_cursor=next_cursor),
        )
