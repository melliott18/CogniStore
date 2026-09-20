"""Shared REST mutation regression harness for SQLite and PostgreSQL workers."""

from __future__ import annotations

import json
import multiprocessing
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.auth.authorization import RBACAuthorizer, RBACPolicy
from cognistore.auth.jwt import JWTAuthConfig, JWTAuthenticator
from cognistore.auth.principal import Principal
from cognistore.core.audit import AuditQuery
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver

_ISSUER = "https://identity.example.test/worker"
_AUDIENCE = "cognistore-worker-test"
_BUCKET = "documents"
_KEY = "reports/current.txt"
_PATH = f"/v1/objects/hot/{_BUCKET}/{_KEY}"
_ORIGINAL = b"original object"
_REPLACEMENT = b"replacement object in an independent worker"


@contextmanager
def _authenticated_client(catalog, driver, token, jwks):
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=jwks),
    )) as identity_client:
        authenticator = JWTAuthenticator(
            JWTAuthConfig(issuer=_ISSUER, audience=_AUDIENCE, jwks_uri=_ISSUER + "/keys"),
            http_client=identity_client,
        )
        authorizer = RBACAuthorizer(RBACPolicy.from_dict({"bindings": [{
            "issuer": _ISSUER, "subject": "worker", "roles": ["writer"],
        }]}))
        try:
            with TestClient(create_app(
                CogniStoreGateway(catalog, {"hot": driver}),
                authentication=authenticator, authorization=authorizer,
            ), headers={"Authorization": "Bearer " + token}) as client:
                yield client
        finally:
            authenticator.close()


def _paused_mutation_worker(dsn, root, first_method, token, jwks, entered, release, result):
    """A spawned process imports and opens its own catalog, driver and HTTP app."""
    try:
        with SQLCatalog(dsn) as catalog:
            driver = PosixDriver(root)
            method = "delete_object_if_generation" if first_method == "DELETE" else "put_object"
            mutate = getattr(driver, method)

            def pause_after_backend(*args, **kwargs):
                value = mutate(*args, **kwargs)
                entered.set()
                if not release.wait(20):
                    raise AssertionError("parent did not release completed backend mutation")
                return value

            setattr(driver, method, pause_after_backend)
            with _authenticated_client(catalog, driver, token, jwks) as client:
                response = client.request(
                    first_method, _PATH,
                    content=_REPLACEMENT if first_method == "PUT" else None,
                    headers={"Content-Type": "text/plain", "X-Request-ID": "worker-mutation"},
                )
                result.put((response.status_code, response.json() if response.content else None))
    except BaseException as error:
        result.put(("worker_error", type(error).__name__))
        raise


def assert_process_mutation_serialization(dsn: str, tmp_path: Path, first_method: str):
    """Run two authenticated gateway processes through the exact post-I/O seam."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="worker-key", alg="RS256", use="sig")
    jwks = {"keys": [public]}
    now = int(time.time())
    token = jwt.encode(
        {"iss": _ISSUER, "aud": _AUDIENCE, "sub": "worker", "iat": now, "exp": now + 300},
        key, algorithm="RS256", headers={"kid": "worker-key"},
    )
    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Event()
    result = context.Queue()
    root = str(tmp_path / "hot")
    with SQLCatalog(dsn) as catalog:
        driver = PosixDriver(root)
        with _authenticated_client(catalog, driver, token, jwks) as client:
            seeded = client.put(_PATH, content=_ORIGINAL, headers={"Content-Type": "text/plain"})
            assert seeded.status_code == 201, seeded.text
            original_record = catalog.get(_BUCKET, _KEY)
            process = context.Process(
                target=_paused_mutation_worker,
                args=(dsn, root, first_method, token, jwks, entered, release, result),
            )
            process.start()
            try:
                assert entered.wait(15), "child never reached the post-backend seam"
                assert catalog.get(_BUCKET, _KEY) == original_record
                if first_method == "DELETE":
                    with pytest.raises(FileNotFoundError):
                        driver.stat_object(_BUCKET, _KEY)
                    competing = client.put(_PATH, content=_REPLACEMENT, headers={
                        "Content-Type": "text/plain", "X-Request-ID": "competing-mutation",
                    })
                else:
                    assert driver.get_object(_BUCKET, _KEY) == _REPLACEMENT
                    competing = client.delete(_PATH, headers={"X-Request-ID": "competing-mutation"})
                assert competing.status_code == 409, competing.text
                assert competing.json()["error"]["code"] == "resource_conflict"
                assert competing.json()["error"]["retryable"] is True
                assert catalog.get(_BUCKET, _KEY) == original_record
                assert process.is_alive()
            finally:
                release.set()
                process.join(timeout=15)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            assert process.exitcode == 0
            status, body = result.get(timeout=5)
            assert status == (204 if first_method == "DELETE" else 201), body
            for request_id, outcome in (
                ("worker-mutation", "succeeded"), ("competing-mutation", "failed"),
            ):
                events = catalog.list_audit_events(AuditQuery(
                    event_types={"storage.operation"}, correlation_id=request_id,
                ))
                assert [event.outcome for event in events] == ["started", outcome]
                assert all(event.actor_type == "authenticated" for event in events)
                assert all(event.actor_id == Principal(_ISSUER, "worker").actor_id for event in events)
            if first_method == "DELETE":
                assert catalog.get(_BUCKET, _KEY) is None
                retry = client.put(_PATH, content=_REPLACEMENT, headers={"Content-Type": "text/plain"})
                assert retry.status_code == 201, retry.text
                assert client.get(_PATH).content == _REPLACEMENT
                assert client.head(_PATH).headers["ETag"] == retry.headers["ETag"]
                record = catalog.get(_BUCKET, _KEY)
                assert record is not None and record.size == len(_REPLACEMENT)
            else:
                assert client.get(_PATH).content == _REPLACEMENT
                assert client.delete(_PATH).status_code == 204
                assert client.delete(_PATH).status_code == 404
                assert catalog.get(_BUCKET, _KEY) is None
    result.close()
    result.join_thread()
