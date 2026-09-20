"""Authenticated reserved-namespace attacks cannot reach another tenant's bytes."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.auth.authorization import RBACAuthorizer, RBACPolicy
from cognistore.auth.tenancy import TenantResolver
from cognistore.core.catalog import Catalog
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver
from tests.unit.test_api_authentication import ISSUER
from tests.unit.test_api_authentication import identity_provider as identity_provider


class _CaseInsensitivePosixDriver(PosixDriver):
    """Emulate case folding and Unicode ignorables on case-sensitive CI volumes."""

    # Independent Unicode 17 Default_Ignorable_Code_Point fixture, including
    # ext4 casefold aliases beyond the narrower HFS+ ignorable set.
    # https://www.unicode.org/Public/17.0.0/ucd/DerivedCoreProperties.txt
    _ignored = dict.fromkeys(
        point
        for first, last in (
            (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C),
            (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F),
            (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F),
            (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
            (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
            (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
        )
        for point in range(first, last + 1)
    )

    def _path(self, bucket: str, key: str, *, allow_bucket_root: bool = False) -> Path:
        return super()._path(
            bucket.translate(self._ignored).casefold(),
            key.translate(self._ignored).casefold(),
            allow_bucket_root=allow_bucket_root,
        )


@pytest.fixture(params=["casefold-posix", "native-case-insensitive-posix"])
def namespace_backend(request, tmp_path: Path):
    if request.param == "native-case-insensitive-posix":
        probe = tmp_path / "case-insensitive-probe"
        probe.write_bytes(b"probe")
        try:
            if not probe.with_name(probe.name.upper()).exists():
                pytest.skip("native POSIX regression requires a case-insensitive temporary volume")
        finally:
            probe.unlink()
        return PosixDriver(str(tmp_path / "hot"))
    return _CaseInsensitivePosixDriver(str(tmp_path / "hot"))


@pytest.fixture(params=["memory", "sqlite"])
def namespace_api(request, namespace_backend, identity_provider, tmp_path: Path):
    authenticator, issue_token, _ = identity_provider
    catalog = Catalog() if request.param == "memory" else SQLCatalog(tmp_path / "catalog.db")
    gateway = CogniStoreGateway(catalog, {"hot": namespace_backend})
    app = create_app(
        gateway,
        authentication=authenticator,
        authorization=RBACAuthorizer(RBACPolicy({
            (ISSUER, "victim-owner"): ["admin"],
            (ISSUER, "default-writer"): ["writer"],
        })),
        tenancy=TenantResolver({
            (ISSUER, "victim-owner"): "victim",
            (ISSUER, "default-writer"): "default",
        }),
    )
    headers = {
        subject: {"Authorization": "Bearer " + issue_token(subject)}
        for subject in ("victim-owner", "default-writer")
    }
    try:
        with TestClient(app) as client:
            yield client, gateway, namespace_backend, headers
    finally:
        if isinstance(catalog, SQLCatalog):
            catalog.close()


@pytest.mark.parametrize("reserved_alias", [
    ".COGNISTORE-TENANTS", ".CoGnIsToRe-TeNaNtS", ".Cogni\u200cstore-Tenants",
    ".Cogni\u00adstore-Tenants",
])
def test_default_writer_cannot_read_replace_or_delete_held_tenant_object(
    namespace_api, reserved_alias, monkeypatch,
):
    client, gateway, raw, headers = namespace_api
    victim, attacker = headers["victim-owner"], headers["default-writer"]
    target = "/v1/objects/hot/bucket/private.txt"
    original = b"retained victim evidence"
    created = client.put(target, headers=victim, content=original)
    assert created.status_code == 201, created.text
    held = client.post("/v1/legal-holds", headers=victim, json={
        "bucket": "bucket", "key": "private.txt", "reason": "Preserve victim evidence",
    })
    assert held.status_code == 201, held.text
    hold = held.json()
    assert hold["tenant_id"] == "victim"
    denied = client.put(target, headers=victim, content=b"victim replacement")
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "legal_hold"

    # The attacker really has default-tenant write/delete/read grants.
    ordinary = "/v1/objects/hot/bucket/ordinary.txt"
    assert client.put(ordinary, headers=attacker, content=b"ordinary").status_code == 201
    assert client.get(ordinary, headers=attacker).content == b"ordinary"
    assert client.delete(ordinary, headers=attacker).status_code == 204
    assert client.get("/v1/legal-holds", headers=attacker).json()["items"] == []

    digest = sha256(b"victim").hexdigest()
    physical_key = f".cognistore-tenants/{digest}/private.txt"
    alias_key = f"{reserved_alias}/{digest}/private.txt"
    alias_url = "/v1/objects/hot/bucket/" + alias_key
    # Both backends reproduce the reported ASCII aliases. The emulated backend
    # additionally resolves Unicode ignorables; APFS need not share that behavior.
    if isinstance(raw, _CaseInsensitivePosixDriver) or reserved_alias.isascii():
        assert raw.get_object("bucket", alias_key) == original
    before_stat = raw.stat_object("bucket", physical_key)
    victim_catalog = gateway.catalog.for_tenant("victim")
    before_record = victim_catalog.get("bucket", "private.txt")

    # Spy on public raw entry points, before even a filesystem stat/read begins.
    calls = Mock()
    for name in (
        "put_object", "put_object_stream", "get_object", "open_object_reader",
        "open_object_reader_if_generation", "stat_object", "object_generation",
        "delete_object", "delete_object_if_generation", "ensure_object_durable",
    ):
        spy = Mock(wraps=getattr(raw, name))
        calls.attach_mock(spy, name)
        monkeypatch.setattr(raw, name, spy)

    def assert_victim_unchanged():
        response = client.get(target, headers=victim)
        assert response.status_code == 200, response.text
        assert response.content == original
        assert raw.stat_object("bucket", physical_key) == before_stat
        assert victim_catalog.get("bucket", "private.txt") == before_record
        assert client.get(
            "/v1/legal-holds?active_only=true", headers=victim,
        ).json()["items"] == [hold]

    # Reproduce the reported PUT/DELETE sequence without creating attacker metadata.
    for method, expected_status in (("PUT", 422), ("DELETE", 404), ("GET", 404), ("HEAD", 404)):
        calls.reset_mock()
        response = client.request(method, alias_url, headers=attacker, content=b"replacement")
        assert response.status_code == expected_status, response.text
        if expected_status == 422:
            assert response.json()["error"]["code"] == "validation_error"
        assert calls.mock_calls == []
        assert gateway.catalog.get("bucket", alias_key) is None
        assert_victim_unchanged()

    # Even catalog metadata left by an older vulnerable writer cannot authorize
    # access to the reserved storage namespace after the boundary is fixed.
    gateway.catalog.upsert("bucket", alias_key, size=len(original), tier="hot")
    for method in ("GET", "HEAD", "DELETE"):
        calls.reset_mock()
        response = client.request(method, alias_url, headers=attacker)
        assert response.status_code == 422, response.text
        if method != "HEAD":
            assert response.json()["error"]["code"] == "validation_error"
        assert calls.mock_calls == []
        assert_victim_unchanged()
