"""Server-owned tenant membership and operation-local isolation context.

Tenant ownership is never selected by a request header or a token claim. The
trusted membership policy maps an authenticated issuer/subject to one tenant.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import MappingProxyType

from .principal import Principal

DEFAULT_TENANT_ID = "default"
_TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", re.ASCII)
_tenant: ContextVar[str | None] = ContextVar("cognistore_tenant", default=None)
_MAX_POLICY_BYTES = 1024 * 1024


class TenantIsolationError(PermissionError):
    """A resource-independent failure at a tenant boundary."""

    def __init__(self) -> None:
        super().__init__("Operation not permitted")


def validate_tenant_id(value: object) -> str:
    if not isinstance(value, str) or _TENANT_ID.fullmatch(value) is None:
        raise ValueError("Invalid tenant identity")
    return value


def tenant_namespace(tenant_id: str) -> str:
    """A collision-resistant, path- and SQL-safe namespace for an exact identity."""
    return hashlib.sha256(validate_tenant_id(tenant_id).encode("ascii")).hexdigest()


def current_tenant_id() -> str | None:
    return _tenant.get()


def require_tenant(tenant_id: str) -> None:
    """Prevent a bound dependency from being reused by a different operation."""
    active = current_tenant_id()
    if active is not None and active != tenant_id:
        raise TenantIsolationError()


@contextmanager
def tenant_context(tenant_id: str | None) -> Iterator[None]:
    token = _tenant.set(None if tenant_id is None else validate_tenant_id(tenant_id))
    try:
        yield
    finally:
        _tenant.reset(token)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Invalid tenant policy")
        result[key] = value
    return result


class TenantResolver:
    """Immutable membership, or a policy file reread on every authorization.

    Missing, duplicate, or malformed bindings fail closed. A policy replacement
    revokes queued work as well as subsequent API requests.
    """

    def __init__(
        self,
        bindings: Mapping[tuple[str, str], str] | None = None,
        *,
        policy_path: str | Path | None = None,
    ) -> None:
        if bindings is not None and policy_path is not None:
            raise ValueError("Configure either tenant bindings or a tenant policy path")
        normalized: dict[tuple[str, str], str] = {}
        if bindings is not None:
            if not isinstance(bindings, Mapping):
                raise ValueError("Invalid tenant policy")
            for identity, tenant_id in bindings.items():
                if not isinstance(identity, tuple) or len(identity) != 2:
                    raise ValueError("Invalid tenant policy")
                Principal(issuer=identity[0], subject=identity[1])
                normalized[identity] = validate_tenant_id(tenant_id)
        self._bindings = MappingProxyType(normalized)
        self._policy_path = None if policy_path is None else Path(policy_path).absolute()
        if self._policy_path is not None:
            self._read_policy()

    @classmethod
    def from_dict(cls, value: object) -> TenantResolver:
        if not isinstance(value, dict) or set(value) != {"bindings"}:
            raise ValueError("Invalid tenant policy")
        entries = value["bindings"]
        if not isinstance(entries, list):
            raise ValueError("Invalid tenant policy")
        bindings: dict[tuple[str, str], str] = {}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"issuer", "subject", "tenant_id"}:
                raise ValueError("Invalid tenant policy")
            principal = Principal(issuer=entry["issuer"], subject=entry["subject"])
            identity = (principal.issuer, principal.subject)
            if identity in bindings:
                raise ValueError("Invalid tenant policy")
            bindings[identity] = validate_tenant_id(entry["tenant_id"])
        return cls(bindings)

    def _read_policy(self) -> TenantResolver:
        assert self._policy_path is not None
        try:
            with self._policy_path.open("rb") as source:
                encoded = source.read(_MAX_POLICY_BYTES + 1)
            if len(encoded) > _MAX_POLICY_BYTES:
                raise ValueError("Invalid tenant policy")
            return self.from_dict(json.loads(encoded, object_pairs_hook=_unique_object))
        except (OSError, TypeError, ValueError, RecursionError):
            raise ValueError("Invalid tenant policy") from None

    def resolve(self, principal: Principal | None) -> str:
        if not isinstance(principal, Principal):
            raise TenantIsolationError()
        try:
            bindings = (
                self._bindings if self._policy_path is None else self._read_policy()._bindings
            )
            return bindings[(principal.issuer, principal.subject)]
        except (KeyError, ValueError):
            raise TenantIsolationError() from None
