"""Scope configured stateful extensions without silently sharing tenant data."""

from __future__ import annotations

from typing import TypeVar, cast

from cognistore.auth.tenancy import (
    DEFAULT_TENANT_ID,
    TenantIsolationError,
    require_tenant,
    validate_tenant_id,
)

_T = TypeVar("_T")


def for_tenant(dependency: _T, tenant_id: str) -> _T:
    tenant_id = validate_tenant_id(tenant_id)
    require_tenant(tenant_id)
    if dependency is None:
        return dependency
    owner = getattr(dependency, "tenant_id", DEFAULT_TENANT_ID)
    if owner == tenant_id:
        return dependency
    scope = getattr(dependency, "for_tenant", None)
    if not callable(scope):
        raise TenantIsolationError()
    scoped = scope(tenant_id)
    if getattr(scoped, "tenant_id", None) != tenant_id:
        raise TenantIsolationError()
    return cast(_T, scoped)
