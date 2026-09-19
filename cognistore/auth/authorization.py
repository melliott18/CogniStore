"""Default-deny, issuer-scoped authorization shared by public boundaries.

Grants come only from the server-owned policy, never from token claims or job
payloads. File policies are read on every decision so revocation also applies
to queued work; a missing or malformed replacement cannot retain stale grants.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import uuid4

from cognistore.auth.principal import Principal, current_principal
from cognistore.core.audit import AuditContext, AuditEvent, AuditEventType, AuditOutcome
from cognistore.observability import current_audit_correlation_id

if TYPE_CHECKING:
    from cognistore.core.catalog import CatalogStore

LOGGER = logging.getLogger(__name__)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_POLICY_BYTES = 1024 * 1024


class Permission(str, Enum):
    """The stable permission vocabulary, independent of transport routes."""

    READ = "read"
    WRITE = "write"
    POLICY = "policy"
    MOVEMENT = "movement"
    ADMIN = "administration"
    AUDIT = "audit"
    LEGAL_HOLD_INSPECT = "legal_hold_inspect"
    LEGAL_HOLD_MANAGE = "legal_hold_manage"
    LEGAL_HOLD_RELEASE = "legal_hold_release"


ROLE_PERMISSIONS: Mapping[str, frozenset[Permission]] = MappingProxyType(
    {
        "reader": frozenset({Permission.READ, Permission.LEGAL_HOLD_INSPECT}),
        "writer": frozenset({Permission.READ, Permission.WRITE, Permission.LEGAL_HOLD_INSPECT}),
        "policy_manager": frozenset({
            Permission.READ, Permission.POLICY, Permission.LEGAL_HOLD_INSPECT,
        }),
        "operator": frozenset({
            Permission.READ, Permission.MOVEMENT, Permission.ADMIN, Permission.LEGAL_HOLD_INSPECT,
        }),
        "auditor": frozenset({Permission.AUDIT, Permission.LEGAL_HOLD_INSPECT}),
        "hold_manager": frozenset({Permission.LEGAL_HOLD_INSPECT, Permission.LEGAL_HOLD_MANAGE}),
        "hold_releaser": frozenset({Permission.LEGAL_HOLD_INSPECT, Permission.LEGAL_HOLD_RELEASE}),
        "admin": frozenset(Permission),
    }
)


class AuthorizationError(PermissionError):
    """A deliberately resource- and policy-independent public rejection."""

    def __init__(self) -> None:
        super().__init__("Operation not permitted")


@dataclass(frozen=True, init=False)
class RBACPolicy:
    """Immutable bindings keyed by the exact (issuer, subject) identity pair.

    The constructor accepts a mapping of identity pairs to role names. JSON
    deployments should use ``from_dict`` for strict schema validation.
    """

    bindings: Mapping[tuple[str, str], frozenset[str]]

    def __init__(self, bindings: Mapping[tuple[str, str], Iterable[str]] | None = None):
        normalized: dict[tuple[str, str], frozenset[str]] = {}
        if bindings is not None:
            if not isinstance(bindings, Mapping):
                raise ValueError("Invalid RBAC policy")
            for identity, roles in bindings.items():
                if not isinstance(identity, tuple) or len(identity) != 2:
                    raise ValueError("Invalid RBAC policy")
                try:
                    Principal(issuer=identity[0], subject=identity[1])
                    if isinstance(roles, str):
                        raise ValueError("Invalid RBAC policy")
                    checked_roles = frozenset(roles)
                    if any(
                        not isinstance(role, str) or role not in ROLE_PERMISSIONS
                        for role in checked_roles
                    ):
                        raise ValueError("Invalid RBAC policy")
                except (TypeError, ValueError):
                    raise ValueError("Invalid RBAC policy") from None
                normalized[identity] = checked_roles
        object.__setattr__(self, "bindings", MappingProxyType(normalized))

    @classmethod
    def from_dict(cls, value: object) -> RBACPolicy:
        """Reject unknown fields, duplicate bindings, and unknown role names."""
        if not isinstance(value, dict) or set(value) != {"bindings"}:
            raise ValueError("Invalid RBAC policy")
        if not isinstance(value["bindings"], list):
            raise ValueError("Invalid RBAC policy")
        bindings: dict[tuple[str, str], list[str]] = {}
        for binding in value["bindings"]:
            if not isinstance(binding, dict) or set(binding) != {"issuer", "subject", "roles"}:
                raise ValueError("Invalid RBAC policy")
            if not isinstance(binding["issuer"], str) or not isinstance(binding["subject"], str):
                raise ValueError("Invalid RBAC policy")
            identity = (binding["issuer"], binding["subject"])
            roles = binding["roles"]
            if (
                identity in bindings
                or not isinstance(roles, list)
                or any(not isinstance(role, str) for role in roles)
            ):
                raise ValueError("Invalid RBAC policy")
            if len(set(roles)) != len(roles):
                raise ValueError("Invalid RBAC policy")
            bindings[identity] = roles
        return cls(bindings)

    def permissions_for(self, principal: Principal) -> frozenset[Permission]:
        roles = self.bindings.get((principal.issuer, principal.subject), frozenset())
        return frozenset(permission for role in roles for permission in ROLE_PERMISSIONS[role])


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for name, value in pairs:
        if name in output:
            raise ValueError("Invalid RBAC policy")
        output[name] = value
    return output


def _read_policy(path: Path) -> RBACPolicy:
    try:
        with path.open("rb") as policy_file:
            encoded = policy_file.read(_MAX_POLICY_BYTES + 1)
        if len(encoded) > _MAX_POLICY_BYTES:
            raise ValueError("Invalid RBAC policy")
        decoded = json.loads(encoded, object_pairs_hook=_unique_json_object)
        return RBACPolicy.from_dict(decoded)
    except (OSError, TypeError, ValueError, RecursionError):
        raise ValueError("Invalid RBAC policy") from None


def _safe_reference(value: str | None, *, prefix: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
        return value
    # Correlation and job identifiers may originate outside this process. Keep
    # them useful for correlation without persisting URLs, paths, or credentials.
    if not isinstance(value, str):
        return prefix + ":invalid"
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
    return prefix + ":sha256:" + digest


class RBACAuthorizer:
    """Evaluate current grants and durably audit security-sensitive decisions."""

    def __init__(
        self,
        policy: RBACPolicy | None = None,
        *,
        policy_path: str | Path | None = None,
    ) -> None:
        if policy is not None and policy_path is not None:
            raise ValueError("Configure either an RBAC policy or a policy path")
        if policy is not None and not isinstance(policy, RBACPolicy):
            raise ValueError("Invalid RBAC policy")
        # Anchor relative paths without pinning a symlink's current target;
        # rotating a policy symlink must revoke grants at the next check too.
        self._policy_path = None if policy_path is None else Path(policy_path).absolute()
        self._policy = RBACPolicy() if policy is None else policy
        if self._policy_path is not None:
            _read_policy(self._policy_path)

    def require(
        self,
        principal: Principal | None,
        permissions: Iterable[Permission | str],
        *,
        operation: str,
        boundary: str,
        catalog: CatalogStore | None = None,
        correlation_id: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Require every named permission, failing closed before side effects."""
        valid = True
        required: set[Permission] = set()
        try:
            if isinstance(permissions, (str, bytes)):
                raise ValueError("Invalid permissions")
            for permission in permissions:
                if not isinstance(permission, str):
                    raise ValueError("Invalid permissions")
                required.add(Permission(permission))
        except (TypeError, ValueError):
            valid = False
        if not required:
            valid = False
        if not isinstance(operation, str) or not _IDENTIFIER.fullmatch(operation):
            operation, valid = "unknown", False
        if not isinstance(boundary, str) or not _IDENTIFIER.fullmatch(boundary):
            boundary, valid = "unknown", False
        if not isinstance(principal, Principal):
            principal, valid = None, False
        try:
            policy = self._policy if self._policy_path is None else _read_policy(self._policy_path)
        except ValueError:
            policy, valid = RBACPolicy(), False
        allowed = valid and principal is not None and required <= policy.permissions_for(principal)
        actor_id = "anonymous" if principal is None else principal.actor_id
        safe_correlation = _safe_reference(
            correlation_id or current_audit_correlation_id(), prefix="correlation"
        ) or str(uuid4())
        event = AuditEvent.create(
            AuditEventType.AUTHORIZATION_DECISION,
            AuditOutcome.ALLOWED if allowed else AuditOutcome.DENIED,
            AuditContext(
                correlation_id=safe_correlation,
                actor_type="anonymous" if principal is None else principal.actor_type,
                actor_id=actor_id,
                job_id=_safe_reference(job_id, prefix="job"),
            ),
            details={
                "operation": operation,
                "boundary": boundary,
                "required_permissions": sorted(permission.value for permission in required),
            },
        )
        if catalog is None:
            self._log_decision(event)
        else:
            try:
                catalog.append_audit_event(event)
            except Exception:
                # An unavailable audit store cannot turn a grant into an
                # unaudited side effect. Do not expose persistence diagnostics.
                allowed = False
                self._log_decision(event, persistence_failed=True)
        if not allowed:
            raise AuthorizationError()

    @staticmethod
    def _log_decision(event: AuditEvent, *, persistence_failed: bool = False) -> None:
        decision: dict[str, object] = {
            "event_type": event.event_type,
            "outcome": AuditOutcome.DENIED.value if persistence_failed else event.outcome,
            "actor_type": event.actor_type,
            "actor_id": event.actor_id,
            "correlation_id": event.correlation_id,
            "job_id": event.job_id,
            "details": dict(event.details),
        }
        if persistence_failed:
            decision["audit_persistence_failed"] = True
        try:
            log = (
                LOGGER.warning if decision["outcome"] == AuditOutcome.DENIED.value else LOGGER.info
            )
            log("authorization_decision %s", json.dumps(decision, sort_keys=True))
        except Exception:
            raise AuthorizationError() from None


_authorizer: ContextVar[RBACAuthorizer | None] = ContextVar("cognistore_authorizer", default=None)
_CURRENT_PRINCIPAL = object()


def current_authorizer() -> RBACAuthorizer | None:
    """Return the authorizer scoped to this request or execution boundary."""
    return _authorizer.get()


@contextmanager
def authorization_context(authorizer: RBACAuthorizer | None) -> Iterator[None]:
    """Scope authorization across nested synchronous and asynchronous calls."""
    token = _authorizer.set(authorizer)
    try:
        yield
    finally:
        _authorizer.reset(token)


def authorize_operation(
    authorizer: RBACAuthorizer | None,
    permissions: Iterable[Permission | str],
    *,
    operation: str,
    boundary: str,
    catalog: CatalogStore | None = None,
    correlation_id: str | None = None,
    job_id: str | None = None,
    principal: Principal | None | object = _CURRENT_PRINCIPAL,
    require_authenticated: bool = False,
) -> None:
    """Authorize an operation, preserving explicit trusted local use.

    Legacy in-process calls with neither an authorizer nor an authenticated
    principal remain trusted. An authenticated caller without configured grants
    always receives the empty policy; it cannot inherit local process trust.
    """
    effective = authorizer if authorizer is not None else current_authorizer()
    identity = current_principal() if principal is _CURRENT_PRINCIPAL else principal
    if effective is None and identity is None and not require_authenticated:
        return
    if effective is None:
        effective = RBACAuthorizer()
    effective.require(
        identity if isinstance(identity, Principal) else None,
        permissions,
        operation=operation,
        boundary=boundary,
        catalog=catalog,
        correlation_id=correlation_id,
        job_id=job_id,
    )


def audit_authorization_denial(
    principal: Principal | None,
    permissions: Iterable[Permission | str],
    *,
    operation: str,
    boundary: str,
    catalog: CatalogStore | None = None,
    correlation_id: str | None = None,
    job_id: str | None = None,
) -> None:
    """Record a rejection by an additional policy hook using the same safe audit contract."""
    try:
        RBACAuthorizer().require(
            principal,
            permissions,
            operation=operation,
            boundary=boundary,
            catalog=catalog,
            correlation_id=correlation_id,
            job_id=job_id,
        )
    except AuthorizationError:
        pass
