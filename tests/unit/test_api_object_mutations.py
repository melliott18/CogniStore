"""REST regressions for object mutation ordering and partial completion."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.auth.principal import Principal
from cognistore.core.audit import AuditQuery
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.drivers.storage_driver import ObjectGenerationMismatchError
from tests.object_mutation_helpers import assert_process_mutation_serialization
from tests.unit.test_api_authentication import ISSUER
from tests.unit.test_api_authentication import identity_provider as identity_provider
from tests.unit.test_api_authorization import authorization

BUCKET = "documents"
KEY = "reports/current.txt"
PATH = f"/v1/objects/hot/{BUCKET}/{KEY}"
ORIGINAL = b"original object"
REPLACEMENT = b"replacement object with a different length"


def _client(catalog, driver, identity_provider):
    authenticator, token, _ = identity_provider
    return TestClient(
        create_app(
            CogniStoreGateway(catalog, {"hot": driver}),
            authentication=authenticator,
            authorization=authorization("writer"),
        ),
        headers={"Authorization": "Bearer " + token()},
    )


def _put(client, data=REPLACEMENT, *, request_id="replacement-put"):
    return client.put(PATH, content=data, headers={
        "Content-Type": "text/plain", "X-Request-ID": request_id,
    })


def _assert_error(response, status, code, *, retryable=False):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["schema_version"] == 1
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["error"]["code"] == code
    assert body["error"]["retryable"] is retryable
    assert set(body["error"]) == {"code", "message", "retryable", "details"}
    assert "private-finalization-detail" not in response.text


def _assert_audit(catalog, request_id, operation, outcome):
    events = catalog.list_audit_events(AuditQuery(
        event_types={"storage.operation"}, correlation_id=request_id,
    ))
    assert [event.outcome for event in events] == ["started", outcome]
    assert all(event.details["operation"] == operation for event in events)
    assert all(event.actor_id == Principal(ISSUER, "alice").actor_id for event in events)
    assert all(event.actor_type == "authenticated" for event in events)
    assert events[1].causation_id == events[0].event_id


@pytest.mark.parametrize("first_method", ["DELETE", "PUT"])
def test_independent_gateways_serialize_backend_and_catalog_mutations(
    tmp_path: Path, identity_provider, monkeypatch, first_method,
):
    """Pause after POSIX releases its own lock, before catalog finalization."""
    with ExitStack() as stack:
        first_catalog = stack.enter_context(SQLCatalog(tmp_path / "catalog.db"))
        second_catalog = stack.enter_context(SQLCatalog(tmp_path / "catalog.db"))
        first_driver = PosixDriver(str(tmp_path / "hot"))
        second_driver = PosixDriver(str(tmp_path / "hot"))
        first_client = stack.enter_context(_client(first_catalog, first_driver, identity_provider))
        second_client = stack.enter_context(_client(second_catalog, second_driver, identity_provider))
        assert _put(first_client, ORIGINAL, request_id="seed").status_code == 201
        original_record = second_catalog.get(BUCKET, KEY)
        entered = threading.Event()
        release = threading.Event()
        driver_method = (
            "delete_object_if_generation" if first_method == "DELETE" else "put_object"
        )
        mutate = getattr(first_driver, driver_method)

        def pause_after_mutation(*args, **kwargs):
            result = mutate(*args, **kwargs)
            entered.set()
            if not release.wait(10):
                raise AssertionError("mutation was never released")
            return result

        monkeypatch.setattr(first_driver, driver_method, pause_after_mutation)
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(
                first_client.request, first_method, PATH,
                content=REPLACEMENT if first_method == "PUT" else None,
                headers={"Content-Type": "text/plain", "X-Request-ID": "first-mutation"},
            )
            try:
                assert entered.wait(10), "request did not reach the post-backend seam"
                assert second_catalog.get(BUCKET, KEY) == original_record
                if first_method == "DELETE":
                    with pytest.raises(FileNotFoundError):
                        second_driver.stat_object(BUCKET, KEY)
                    competing = _put(second_client, request_id="competing-mutation")
                    competing_operation = "put_object"
                else:
                    assert second_driver.get_object(BUCKET, KEY) == REPLACEMENT
                    competing = second_client.delete(
                        PATH, headers={"X-Request-ID": "competing-mutation"},
                    )
                    competing_operation = "delete_object"
                _assert_error(competing, 409, "resource_conflict", retryable=True)
                assert not first.done()
                assert second_catalog.get(BUCKET, KEY) == original_record
            finally:
                release.set()
            response = first.result(timeout=10)

        assert response.status_code == (204 if first_method == "DELETE" else 201), response.text
        _assert_audit(first_catalog, "first-mutation", first_method.lower() + "_object", "succeeded")
        _assert_audit(second_catalog, "competing-mutation", competing_operation, "failed")
        if first_method == "DELETE":
            assert second_catalog.get(BUCKET, KEY) is None
            retried = _put(second_client, request_id="retry-mutation")
            assert retried.status_code == 201, retried.text
            assert second_client.get(PATH).content == REPLACEMENT
            assert second_client.head(PATH).headers["ETag"] == retried.headers["ETag"]
            record = first_catalog.get(BUCKET, KEY)
            assert record is not None and record.size == len(REPLACEMENT)
            assert record.metadata["mime"] == "text/plain"
            assert second_driver.stat_object(BUCKET, KEY)["generation"] == retried.json()["generation"]
        else:
            assert second_client.get(PATH).content == REPLACEMENT
            assert second_client.head(PATH).headers["ETag"] == response.headers["ETag"]
            assert second_client.delete(PATH).status_code == 204
            _assert_error(second_client.delete(PATH), 404, "resource_not_found")
            assert first_catalog.get(BUCKET, KEY) is None


@pytest.mark.parametrize("backend_result", ["absent", "generation_changed"])
def test_unsuccessful_conditional_delete_does_not_remove_catalog_record(
    tmp_path: Path, identity_provider, monkeypatch, backend_result,
):
    with SQLCatalog(tmp_path / "catalog.db") as catalog:
        driver = PosixDriver(str(tmp_path / "hot"))
        with _client(catalog, driver, identity_provider) as client:
            assert _put(client, ORIGINAL, request_id="seed").status_code == 201
            original_record = catalog.get(BUCKET, KEY)
            calls = []

            def refuse_delete(bucket, key, generation):
                calls.append((bucket, key, generation))
                if backend_result == "generation_changed":
                    raise ObjectGenerationMismatchError("private-finalization-detail")
                return False

            monkeypatch.setattr(driver, "delete_object_if_generation", refuse_delete)
            response = client.delete(PATH, headers={"X-Request-ID": "refused-delete"})
            _assert_error(
                response,
                404 if backend_result == "absent" else 409,
                "resource_not_found" if backend_result == "absent" else "object_generation_changed",
                retryable=backend_result == "generation_changed",
            )
            assert len(calls) == 1 and calls[0][:2] == (BUCKET, KEY)
            assert calls[0][2] == driver.stat_object(BUCKET, KEY)["generation"]
            assert catalog.get(BUCKET, KEY) == original_record
            assert client.get(PATH).content == ORIGINAL
            _assert_audit(catalog, "refused-delete", "delete_object", "failed")


@pytest.mark.parametrize("commit_before_failure", [False, True])
def test_delete_finalization_failure_is_retryable_without_false_success(
    tmp_path: Path, identity_provider, monkeypatch, commit_before_failure,
):
    with SQLCatalog(tmp_path / "catalog.db") as catalog:
        driver = PosixDriver(str(tmp_path / "hot"))
        with _client(catalog, driver, identity_provider) as client:
            assert _put(client, ORIGINAL, request_id="seed").status_code == 201
            original_record = catalog.get(BUCKET, KEY)
            delete = catalog.delete

            def fail_finalization(*args, **kwargs):
                if commit_before_failure:
                    delete(*args, **kwargs)
                raise RuntimeError("private-finalization-detail")

            monkeypatch.setattr(catalog, "delete", fail_finalization)
            failed = client.delete(PATH, headers={"X-Request-ID": "failed-delete"})
            _assert_error(failed, 503, "backend_unavailable", retryable=True)
            assert failed.headers["Retry-After"] == "1"
            with pytest.raises(FileNotFoundError):
                driver.stat_object(BUCKET, KEY)
            assert catalog.get(BUCKET, KEY) == (None if commit_before_failure else original_record)
            _assert_audit(catalog, "failed-delete", "delete_object", "failed")

            monkeypatch.setattr(catalog, "delete", delete)
            retried = client.delete(PATH, headers={"X-Request-ID": "retry-delete"})
            if commit_before_failure:
                _assert_error(retried, 404, "resource_not_found")
            else:
                assert retried.status_code == 204, retried.text
                _assert_audit(catalog, "retry-delete", "delete_object", "succeeded")
            assert catalog.get(BUCKET, KEY) is None
            _assert_error(client.delete(PATH), 404, "resource_not_found")
            assert _put(client, request_id="recreate-after-failure").status_code == 201
            assert client.get(PATH).content == REPLACEMENT


@pytest.mark.parametrize("commit_before_failure", [False, True])
def test_put_finalization_failure_keeps_written_bytes_and_can_be_retried(
    tmp_path: Path, identity_provider, monkeypatch, commit_before_failure,
):
    with SQLCatalog(tmp_path / "catalog.db") as catalog:
        driver = PosixDriver(str(tmp_path / "hot"))
        with _client(catalog, driver, identity_provider) as client:
            assert _put(client, ORIGINAL, request_id="seed").status_code == 201
            original_record = catalog.get(BUCKET, KEY)
            upsert = catalog.upsert

            def fail_finalization(*args, **kwargs):
                if commit_before_failure:
                    upsert(*args, **kwargs)
                raise RuntimeError("private-finalization-detail")

            monkeypatch.setattr(catalog, "upsert", fail_finalization)
            failed = _put(client, request_id="failed-put")
            _assert_error(failed, 503, "backend_unavailable", retryable=True)
            assert driver.get_object(BUCKET, KEY) == REPLACEMENT
            if not commit_before_failure:
                assert catalog.get(BUCKET, KEY) == original_record
            _assert_audit(catalog, "failed-put", "put_object", "failed")

            monkeypatch.setattr(catalog, "upsert", upsert)
            retried = _put(client, request_id="retry-put")
            assert retried.status_code == 201, retried.text
            assert client.get(PATH).content == REPLACEMENT
            assert client.head(PATH).headers["ETag"] == retried.headers["ETag"]
            record = catalog.get(BUCKET, KEY)
            assert record is not None and record.size == len(REPLACEMENT)
            assert record.metadata["mime"] == "text/plain"
            assert driver.stat_object(BUCKET, KEY)["generation"] == retried.json()["generation"]
            _assert_audit(catalog, "retry-put", "put_object", "succeeded")


@pytest.mark.parametrize("first_method", ["DELETE", "PUT"])
def test_sqlite_gateway_mutation_ordering_across_processes(tmp_path: Path, first_method):
    assert_process_mutation_serialization(str(tmp_path / "catalog.db"), tmp_path, first_method)
