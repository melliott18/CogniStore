"""Issuer-scoped grants, revocation, audit safety, and local trust boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cognistore.auth.authorization import (
    ROLE_PERMISSIONS,
    AuthorizationError,
    Permission,
    RBACAuthorizer,
    RBACPolicy,
    _read_allow_sampled,
    audit_authorization_denial,
    authorization_context,
    authorize_operation,
    current_authorizer,
)
from cognistore.auth.principal import Principal, principal_context
from cognistore.core.audit import AuditEventType, AuditOutcome
from cognistore.core.catalog import Catalog
from cognistore.observability import current_correlation_id, request_context

PRINCIPAL = Principal("https://issuer.example", "subject-57", client_id="service-client")


def _configuration(*roles: str) -> dict:
    return {
        "bindings": [
            {"issuer": PRINCIPAL.issuer, "subject": PRINCIPAL.subject, "roles": list(roles)}
        ]
    }


def _authorizer(*roles: str) -> RBACAuthorizer:
    return RBACAuthorizer(RBACPolicy.from_dict(_configuration(*roles)))


@pytest.mark.parametrize("role", ROLE_PERMISSIONS)
@pytest.mark.parametrize("permission", list(Permission))
def test_role_permission_matrix(role: str, permission: Permission) -> None:
    authorizer = _authorizer(role)
    catalog = Catalog()
    kwargs = {"operation": "matrix.check", "boundary": "test", "catalog": catalog}
    if permission in ROLE_PERMISSIONS[role]:
        authorizer.require(PRINCIPAL, [permission], **kwargs)
    else:
        with pytest.raises(AuthorizationError, match="^Operation not permitted$"):
            authorizer.require(PRINCIPAL, [permission], **kwargs)
        event = catalog.list_audit_events()[0]
        assert event.outcome == AuditOutcome.DENIED
        assert event.details["required_permissions"] == [permission.value]


def test_role_composition_requires_all_permissions() -> None:
    required = [Permission.POLICY, Permission.MOVEMENT]
    with pytest.raises(AuthorizationError):
        _authorizer("policy_manager").require(
            PRINCIPAL, required, operation="policy.run", boundary="worker"
        )
    _authorizer("policy_manager", "operator").require(
        PRINCIPAL, required, operation="policy.run", boundary="worker"
    )


@pytest.mark.parametrize(
    "principal",
    [
        None,
        Principal("https://other-issuer.example", PRINCIPAL.subject),
        Principal(PRINCIPAL.issuer, "unknown-subject"),
        Principal(PRINCIPAL.issuer, PRINCIPAL.client_id),
    ],
)
def test_missing_and_unbound_principals_cannot_inherit_grants(principal) -> None:
    with pytest.raises(AuthorizationError):
        _authorizer("admin").require(
            principal, [Permission.WRITE], operation="object.put", boundary="sdk"
        )


@pytest.mark.parametrize(
    "permissions", [[], ["unknown"], [Permission.READ, "unknown"], "read", [True], None]
)
def test_empty_or_unknown_permissions_deny_even_admin(permissions) -> None:
    catalog = Catalog()
    with pytest.raises(AuthorizationError):
        _authorizer("admin").require(
            PRINCIPAL, permissions, operation="unknown.operation", boundary="api", catalog=catalog
        )
    assert catalog.list_audit_events()[0].outcome == AuditOutcome.DENIED


@pytest.mark.parametrize(
    "configuration",
    [
        None,
        [],
        {},
        {"bindings": {}},
        {"bindings": [], "default_role": "admin"},
        {"bindings": [{"issuer": "issuer", "subject": "subject", "roles": "admin"}]},
        {"bindings": [{"issuer": "issuer", "subject": "subject", "roles": ["unknown"]}]},
        {"bindings": [{"issuer": "issuer", "subject": "subject", "roles": ["admin", "admin"]}]},
        {"bindings": [{"issuer": "issuer", "subject": "subject", "roles": [True]}]},
        {"bindings": [{"issuer": "issuer", "subject": "subject", "roles": [], "scope": "*"}]},
        {"bindings": [{"issuer": "", "subject": "subject", "roles": []}]},
        {"bindings": [{"issuer": [], "subject": "subject", "roles": []}]},
        {"bindings": [{"issuer": "issuer", "subject": "subject", "roles": []}] * 2},
    ],
)
def test_strict_policy_schema_rejects_ambiguous_configuration(configuration) -> None:
    with pytest.raises(ValueError, match="^Invalid RBAC policy$"):
        RBACPolicy.from_dict(configuration)


def test_policy_copies_and_freezes_server_owned_bindings() -> None:
    configuration = _configuration("reader")
    policy = RBACPolicy.from_dict(configuration)
    configuration["bindings"][0]["roles"].append("admin")
    assert policy.permissions_for(PRINCIPAL) == frozenset({
        Permission.READ, Permission.LEGAL_HOLD_INSPECT,
    })
    with pytest.raises(TypeError):
        policy.bindings[(PRINCIPAL.issuer, PRINCIPAL.subject)] = frozenset({"admin"})
    with pytest.raises(TypeError):
        ROLE_PERMISSIONS["reader"] = frozenset(Permission)


@pytest.mark.parametrize("replacement", [json.dumps({"bindings": []}), "{invalid", None])
def test_file_policy_reloads_without_retaining_stale_grants(tmp_path: Path, replacement) -> None:
    policy_file = tmp_path / "rbac.json"
    policy_file.write_text(json.dumps(_configuration("admin")), encoding="utf-8")
    authorizer = RBACAuthorizer(policy_path=policy_file)
    kwargs = {"operation": "object.put", "boundary": "worker"}
    authorizer.require(PRINCIPAL, [Permission.WRITE], **kwargs)
    if replacement is None:
        policy_file.unlink()
    else:
        policy_file.write_text(replacement, encoding="utf-8")
    with pytest.raises(AuthorizationError):
        authorizer.require(PRINCIPAL, [Permission.WRITE], **kwargs)
    policy_file.write_text(json.dumps(_configuration("writer")), encoding="utf-8")
    authorizer.require(PRINCIPAL, [Permission.WRITE], **kwargs)


@pytest.mark.parametrize(
    "encoded",
    [b'{"bindings":[],"bindings":[]}', b"\xff", b"[]", b" " * (1024 * 1024 + 1)],
    ids=["duplicate-keys", "invalid-utf8", "wrong-schema", "oversized"],
)
def test_file_policy_validates_before_startup(tmp_path: Path, encoded: bytes) -> None:
    policy_file = tmp_path / "rbac.json"
    policy_file.write_bytes(encoded)
    with pytest.raises(ValueError, match="Invalid RBAC policy"):
        RBACAuthorizer(policy_path=policy_file)


def test_denials_and_sensitive_allows_are_audited_without_identity_claims() -> None:
    catalog = Catalog()
    authorizer = _authorizer("writer")
    kwargs = {
        "operation": "object.put",
        "boundary": "api",
        "catalog": catalog,
        "correlation_id": "request-57",
        "job_id": "job-57",
    }
    authorizer.require(PRINCIPAL, [Permission.WRITE], **kwargs)
    with pytest.raises(AuthorizationError):
        authorizer.require(PRINCIPAL, [Permission.ADMIN], **kwargs)
    events = catalog.list_audit_events()
    assert len(events) == 2
    assert [event.outcome for event in events] == [AuditOutcome.ALLOWED, AuditOutcome.DENIED]
    for event in events:
        assert event.event_type == AuditEventType.AUTHORIZATION_DECISION
        assert event.actor_id == PRINCIPAL.actor_id
        assert event.actor_type == "authenticated"
        assert event.correlation_id == "request-57"
        assert event.job_id == "job-57"
        assert event.bucket is None and event.object_key is None
        assert set(event.details) == {"operation", "boundary", "required_permissions"}
        serialized = str(event)
        for sensitive in [PRINCIPAL.issuer, PRINCIPAL.subject, PRINCIPAL.client_id, "writer"]:
            assert sensitive not in serialized


def test_read_allow_sampling_is_deterministic_and_denials_are_never_sampled() -> None:
    sampled = [
        i
        for i in range(10000)
        if _read_allow_sampled(PRINCIPAL.actor_id, f"request-{i}", "object.get", "sdk")
    ]
    assert 50 < len(sampled) < 150
    assert sampled == [
        i
        for i in range(10000)
        if _read_allow_sampled(PRINCIPAL.actor_id, f"request-{i}", "object.get", "sdk")
    ]
    catalog = Catalog()
    authorizer = _authorizer("reader")
    for i in range(100):
        authorizer.require(
            PRINCIPAL,
            [Permission.READ],
            operation="object.get",
            boundary="sdk",
            catalog=catalog,
            correlation_id=f"request-{i}",
        )
    assert len(catalog.list_audit_events()) == len([i for i in sampled if i < 100])
    for i in range(100):
        with pytest.raises(AuthorizationError):
            authorizer.require(
                None,
                [Permission.READ],
                operation="object.get",
                boundary="sdk",
                catalog=catalog,
                correlation_id=f"request-{i}",
            )
    assert sum(event.outcome == AuditOutcome.DENIED for event in catalog.list_audit_events()) == 100


def test_audit_store_failure_prevents_sensitive_allow_and_logs_safe_denial(caplog) -> None:
    class BrokenCatalog:
        def append_audit_event(self, event):
            raise RuntimeError("password=must-not-escape")

    with (
        caplog.at_level("INFO"),
        pytest.raises(AuthorizationError, match="Operation not permitted"),
    ):
        _authorizer("admin").require(
            PRINCIPAL,
            [Permission.WRITE],
            operation="object.put",
            boundary="sdk",
            catalog=BrokenCatalog(),
            correlation_id="https://storage/secret-key",
            job_id="token=secret-token",
        )
    assert '"outcome": "denied"' in caplog.text
    assert '"audit_persistence_failed": true' in caplog.text
    for sensitive in ["must-not-escape", "secret-key", "secret-token", PRINCIPAL.subject]:
        assert sensitive not in caplog.text


def test_unsafe_operation_vocabulary_denies_without_echoing_input(caplog) -> None:
    with caplog.at_level("INFO"), pytest.raises(AuthorizationError):
        _authorizer("admin").require(
            PRINCIPAL,
            [Permission.WRITE],
            operation="https://storage/secret-key",
            boundary="token=secret-token",
        )
    assert '"operation": "unknown"' in caplog.text
    assert "secret-key" not in caplog.text
    assert "secret-token" not in caplog.text


def test_contexts_preserve_local_trust_and_authenticated_default_deny() -> None:
    kwargs = {"operation": "object.put", "boundary": "sdk"}
    authorize_operation(None, [Permission.WRITE], **kwargs)
    with principal_context(PRINCIPAL), pytest.raises(AuthorizationError):
        authorize_operation(None, [Permission.WRITE], **kwargs)
    admin = _authorizer("admin")
    reader = _authorizer("reader")
    assert current_authorizer() is None
    with authorization_context(admin), principal_context(PRINCIPAL):
        assert current_authorizer() is admin
        authorize_operation(None, [Permission.WRITE], **kwargs)
        with authorization_context(reader), pytest.raises(AuthorizationError):
            authorize_operation(None, [Permission.WRITE], **kwargs)
        assert current_authorizer() is admin
        with pytest.raises(AuthorizationError):
            authorize_operation(None, [Permission.WRITE], principal=None, **kwargs)
    assert current_authorizer() is None
    with authorization_context(reader), pytest.raises(AuthorizationError):
        authorize_operation(None, [Permission.READ], **kwargs)


def test_additional_hook_denial_uses_ambient_correlation() -> None:
    catalog = Catalog()
    with request_context("request-57"):
        correlation_id = current_correlation_id()
        audit_authorization_denial(
            PRINCIPAL, [Permission.WRITE], operation="object.put", boundary="api", catalog=catalog
        )
    event = catalog.list_audit_events()[0]
    assert event.outcome == AuditOutcome.DENIED
    assert event.correlation_id == correlation_id


def test_policy_path_does_not_follow_process_directory_changes(tmp_path: Path, monkeypatch) -> None:
    original = tmp_path / "original"
    replacement = tmp_path / "replacement"
    original.mkdir()
    replacement.mkdir()
    (original / "rbac.json").write_text(json.dumps(_configuration("reader")), encoding="utf-8")
    (replacement / "rbac.json").write_text(json.dumps(_configuration("admin")), encoding="utf-8")
    monkeypatch.chdir(original)
    authorizer = RBACAuthorizer(policy_path="rbac.json")
    monkeypatch.chdir(replacement)
    with pytest.raises(AuthorizationError):
        authorizer.require(PRINCIPAL, [Permission.WRITE], operation="object.put", boundary="sdk")


def test_policy_symlink_rotation_revokes_existing_grants(tmp_path: Path) -> None:
    granted = tmp_path / "granted.json"
    revoked = tmp_path / "revoked.json"
    policy_link = tmp_path / "rbac.json"
    granted.write_text(json.dumps(_configuration("writer")), encoding="utf-8")
    revoked.write_text(json.dumps({"bindings": []}), encoding="utf-8")
    policy_link.symlink_to(granted)
    authorizer = RBACAuthorizer(policy_path=policy_link)
    authorizer.require(PRINCIPAL, [Permission.WRITE], operation="object.put", boundary="sdk")
    replacement_link = tmp_path / "replacement.json"
    replacement_link.symlink_to(revoked)
    replacement_link.replace(policy_link)
    with pytest.raises(AuthorizationError):
        authorizer.require(PRINCIPAL, [Permission.WRITE], operation="object.put", boundary="sdk")
