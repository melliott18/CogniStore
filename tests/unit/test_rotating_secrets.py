"""Offline rotation contracts: operation leases, credentials, cursors and failures."""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Generator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
import yaml

from cognistore.drivers import driver_loader
from cognistore.drivers.rotating import RotatingStorageDriver
from cognistore.drivers.storage_driver import (
    DriverCapabilities,
    ObjectGenerationMismatchError,
    StorageDriver,
    StorageListingPage,
    decode_listing_cursor,
    encode_listing_cursor,
)
from cognistore.secrets import (
    SecretAccessError,
    SecretReference,
    SecretResolver,
    SecretUnavailableError,
    SecretValue,
)


class MutableProvider:
    def __init__(self) -> None:
        self.value = '{"access":"access-one","secret":"secret-one"}'
        self.calls: list[SecretReference] = []
        self.fail = False

    def fetch(self, reference: SecretReference) -> SecretValue:
        self.calls.append(reference)
        if self.fail:
            raise ConnectionError("provider echoed " + self.value)
        return SecretValue(self.value)


class RecordingDriver(StorageDriver):
    capabilities = DriverCapabilities(True, False, True, True)

    def __init__(self, *, objects: dict[str, bytes], **options: Any) -> None:
        self.objects = objects
        self.options = options
        self.closed = 0
        self.calls: list[str] = []

    def close(self) -> None:
        self.closed += 1

    def _call(self, method: str) -> None:
        assert self.closed == 0
        self.calls.append(method)

    def put_object(self, bucket: str, key: str, data: bytes, **options: Any) -> None:
        self._call("put_object")
        if not options.get("overwrite", True) and key in self.objects:
            raise FileExistsError("provider-sensitive-data")
        self.objects[key] = data

    def get_object(self, bucket: str, key: str, range: str | None = None) -> bytes:
        self._call("get_object")
        if key not in self.objects:
            raise FileNotFoundError("provider-sensitive-data")
        return self.objects[key]

    @contextmanager
    def open_object_reader(self, bucket: str, key: str, range: str | None = None) -> Iterator[BytesIO]:
        self._call("open_object_reader")
        with BytesIO(self.objects[key]) as stream:
            yield stream
            assert self.closed == 0

    def open_object_reader_if_generation(
        self, bucket: str, key: str, generation: str, range: str | None = None,
    ) -> Any:
        self._call("open_object_reader_if_generation")
        if generation != "generation":
            raise ObjectGenerationMismatchError("provider-sensitive-data")
        return self.open_object_reader(bucket, key, range)

    def put_object_stream(self, bucket: str, key: str, source: Any, *, size: int, **opts: Any) -> int:
        self._call("put_object_stream")
        self.objects[key] = source.read(size)
        assert self.closed == 0
        return size

    def delete_object(self, bucket: str, key: str) -> None:
        self._call("delete_object")
        self.objects.pop(key, None)

    def delete_object_if_generation(self, bucket: str, key: str, generation: str) -> bool:
        self._call("delete_object_if_generation")
        if generation != "generation":
            raise ObjectGenerationMismatchError("provider-sensitive-data")
        return self.objects.pop(key, None) is not None

    def list_objects(self, bucket: str, prefix: str = "") -> Generator[str, None, None]:
        for key in sorted(self.objects):
            self._call("list_objects")
            if key.startswith(prefix):
                yield key

    def list_objects_page(
        self, bucket: str, prefix: str = "", *, cursor: str | None = None, limit: int = 1000,
    ) -> StorageListingPage:
        self._call("list_objects_page")
        after = decode_listing_cursor(cursor, "recording", bucket, prefix) if cursor else ""
        keys = tuple(key for key in sorted(self.objects) if key.startswith(prefix) and key > after)
        page = keys[:limit]
        next_cursor = (
            encode_listing_cursor("recording", bucket, prefix, page[-1])
            if len(keys) > limit else None
        )
        return StorageListingPage(page, next_cursor)

    def stat_object(self, bucket: str, key: str) -> dict[str, Any]:
        self._call("stat_object")
        return {"size": len(self.objects[key]), "mtime": 1.0, "generation": "generation"}

    def ensure_object_durable(self, bucket: str, key: str) -> None:
        self._call("ensure_object_durable")

    def same_backend(self, other: StorageDriver) -> bool:
        return isinstance(other, RecordingDriver) and other.objects is self.objects


@pytest.fixture
def runtime() -> Iterator[Any]:
    now = [0.0]
    provider = MutableProvider()
    resolver = SecretResolver({"vault": provider}, cache_ttl_seconds=10, clock=lambda: now[0])
    clients: list[RecordingDriver] = []
    objects = {"a": b"alpha", "b": b"beta", "c": b"gamma"}

    def factory(**options: Any) -> RecordingDriver:
        client = RecordingDriver(objects=objects, **options)
        clients.append(client)
        return client

    references = {
        "access_key": SecretReference("vault", "storage", field="access"),
        "secret_key": SecretReference("vault", "storage", field="secret"),
    }
    driver = RotatingStorageDriver(factory, {}, references, resolver)
    yield driver, clients, provider, resolver, now
    driver.close()
    resolver.close()


def rotate(provider: MutableProvider, now: list[float]) -> None:
    provider.value = '{"access":"access-two","secret":"secret-two"}'
    now[0] += 11


def test_rotation_keeps_active_reader_and_releases_old_client(runtime: Any) -> None:
    driver, clients, provider, resolver, now = runtime
    with driver.open_object_reader("bucket", "a") as stream:
        assert stream.read(2) == b"al"
        rotate(provider, now)
        assert driver.get_object("bucket", "b") == b"beta"
        assert len(clients) == 2
        assert clients[0].closed == 0
        assert clients[1].options == {"access_key": "access-two", "secret_key": "secret-two"}
        assert stream.read() == b"pha"
    assert clients[0].closed == 1
    assert clients[1].closed == 0
    assert all(reference.field is None for reference in provider.calls)
    assert len(provider.calls) == 2  # One fetch per whole credential bundle.


def test_expired_provider_failure_blocks_new_io_but_open_reader_finishes(runtime: Any) -> None:
    driver, clients, provider, resolver, now = runtime
    with driver.open_object_reader("bucket", "a") as stream:
        provider.fail = True
        now[0] = 11
        with pytest.raises(SecretUnavailableError) as error:
            driver.get_object("bucket", "b")
        assert error.value.__context__ is None
        assert clients[0].calls == ["open_object_reader"]
        assert stream.read() == b"alpha"
        assert len(clients) == 1
    provider.fail = False
    rotate(provider, now)
    assert driver.get_object("bucket", "b") == b"beta"
    assert clients[0].closed == 1


def test_listing_generator_lease_and_cursor_survive_rotation(runtime: Any) -> None:
    driver, clients, provider, resolver, now = runtime
    iterator = driver.list_objects("bucket")
    assert next(iterator) == "a"
    first = driver.list_objects_page("bucket", limit=1)
    rotate(provider, now)
    second = driver.list_objects_page("bucket", cursor=first.next_cursor, limit=1)
    assert first.keys == ("a",)
    assert second.keys == ("b",)
    assert clients[0].closed == 0
    assert next(iterator) == "b"
    iterator.close()
    assert clients[0].closed == 1
    final = driver.list_objects_page("bucket", cursor=second.next_cursor, limit=1)
    assert final == StorageListingPage(("c",), None)


def test_invalidation_refreshes_without_waiting_and_unchanged_values_reuse_client(runtime: Any) -> None:
    driver, clients, provider, resolver, now = runtime
    now[0] = 11
    driver.stat_object("bucket", "a")
    assert len(provider.calls) == 2
    assert len(clients) == 1
    provider.value = '{"access":"access-two","secret":"secret-two"}'
    resolver.invalidate(SecretReference("vault", "storage", field="secret"))
    driver.stat_object("bucket", "a")
    assert len(clients) == 2
    assert clients[0].closed == 1


def test_concurrent_refresh_installs_one_client(runtime: Any) -> None:
    driver, clients, provider, resolver, now = runtime
    rotate(provider, now)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: driver.get_object("bucket", "a"), range(16))) == [b"alpha"] * 16
    assert len(clients) == 2
    assert len(provider.calls) == 2
    assert clients[0].closed == 1


def test_close_defers_release_until_upload_finishes_and_is_idempotent(runtime: Any) -> None:
    driver, clients, provider, resolver, now = runtime

    class Source:
        def read(self, size: int) -> bytes:
            driver.close()
            assert clients[0].closed == 0
            return b"data"

    assert driver.put_object_stream("bucket", "d", Source(), size=4) == 4
    assert clients[0].closed == 1
    driver.close()
    assert clients[0].closed == 1
    with pytest.raises(RuntimeError, match="closed"):
        driver.stat_object("bucket", "a")


def test_full_storage_contract_and_capabilities(runtime: Any) -> None:
    driver, clients, provider, resolver, now = runtime
    assert driver.capabilities == RecordingDriver.capabilities
    assert driver.same_backend(clients[0])
    assert driver.same_backend(driver)
    driver.put_object("bucket", "d", b"delta")
    assert driver.object_generation("bucket", "d") == "generation"
    with driver.open_object_reader_if_generation("bucket", "d", "generation") as stream:
        assert stream.read() == b"delta"
    driver.ensure_object_durable("bucket", "d")
    assert driver.delete_object_if_generation("bucket", "d", "generation") is True
    driver.delete_object("bucket", "c")
    assert "c" not in clients[0].objects
    assert "ensure_object_durable" in clients[0].calls


@pytest.mark.parametrize("operation,category", [
    (lambda d: d.get_object("b", "missing"), FileNotFoundError),
    (lambda d: d.put_object("b", "a", b"x", overwrite=False), FileExistsError),
    (lambda d: d.delete_object_if_generation("b", "a", "old"), ObjectGenerationMismatchError),
    (lambda d: d.open_object_reader_if_generation("b", "a", "old").__enter__(), ObjectGenerationMismatchError),
])
def test_safe_failures_preserve_storage_contract(runtime: Any, operation: Any, category: Any) -> None:
    driver = runtime[0]
    with pytest.raises(category) as captured:
        operation(driver)
    assert captured.value.__context__ is None
    assert "provider-sensitive-data" not in "".join(traceback.format_exception(captured.value))


@pytest.mark.parametrize("payload", ['{"access":"new"}', '{"access":false,"secret":"value"}', 'secret-invalid-json'])
def test_invalid_bundle_is_rejected_before_replacing_client(runtime: Any, payload: str) -> None:
    driver, clients, provider, resolver, now = runtime
    provider.value = payload
    now[0] = 11
    with pytest.raises(SecretAccessError) as captured:
        driver.get_object("bucket", "a")
    assert captured.value.__context__ is None
    assert len(clients) == 1
    assert clients[0].calls == []


def test_constructor_and_sdk_logs_never_echo_material(caplog: pytest.LogCaptureFixture) -> None:
    sentinel = "highly-sensitive-secret"
    provider = MutableProvider()
    provider.value = sentinel
    resolver = SecretResolver({"vault": provider})

    def factory(**options: Any) -> StorageDriver:
        logging.getLogger("mock.sdk").warning("credentials %s", options)
        raise RuntimeError("constructor leaked " + sentinel)

    with pytest.raises(RuntimeError) as captured:
        RotatingStorageDriver(factory, {}, {"secret_key": SecretReference("vault", "key")}, resolver)
    assert captured.value.__context__ is None
    assert sentinel not in "".join(traceback.format_exception(captured.value))
    assert sentinel not in caplog.text


def _config(tmp_path: Path, driver: str, **fields: Any) -> str:
    path = tmp_path / "drivers.yaml"
    path.write_text(yaml.safe_dump({"tiers": {"archive": {"driver": driver, **fields}}}))
    return str(path)


def test_loader_s3_builds_and_rotates_json_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = MutableProvider()
    resolver = SecretResolver({"vault": provider})
    clients: list[RecordingDriver] = []

    def factory(**options: Any) -> RecordingDriver:
        result = RecordingDriver(objects={"a": b"data"}, **options)
        clients.append(result)
        return result

    monkeypatch.setattr(driver_loader, "S3Driver", factory)
    path = _config(
        tmp_path, "s3", access_key_ref={"provider": "vault", "name": "storage", "field": "access"},
        secret_key_ref={"provider": "vault", "name": "storage", "field": "secret"},
    )
    driver = driver_loader.load_drivers(path, secret_resolver=resolver)["archive"]
    try:
        assert isinstance(driver, RotatingStorageDriver)
        assert clients[0].options["access_key"] == "access-one"
        provider.value = '{"access":"access-two","secret":"secret-two"}'
        resolver.invalidate()
        assert driver.get_object("bucket", "a") == b"data"
        assert clients[1].options["secret_key"] == "secret-two"
    finally:
        driver.close()


@pytest.mark.parametrize("driver,field,alternative", [
    ("s3", "access_key", "access_key"),
    ("s3", "secret_key", "secret_key_env"),
    ("s3", "session_token", "session_token_env"),
    ("azure_blob", "connection_string", "connection_string"),
    ("azure_blob", "credential", "credential_env"),
    ("gcs", "credentials", "credentials_file"),
    ("gcs", "credentials", "credentials_file_env"),
])
def test_loader_rejects_ambiguous_reference_before_fetch(
    tmp_path: Path, driver: str, field: str, alternative: str,
) -> None:
    if driver == "azure_blob":
        pytest.importorskip("azure.storage.blob")
    provider = MutableProvider()
    resolver = SecretResolver({"vault": provider})
    path = _config(tmp_path, driver, **{
        field + "_ref": {"provider": "vault", "name": "secret"}, alternative: "should-not-leak",
    })
    with pytest.raises(ValueError, match="only one of") as captured:
        driver_loader.load_drivers(path, secret_resolver=resolver)
    assert "should-not-leak" not in str(captured.value)
    assert provider.calls == []


def test_loader_gcs_json_material_stays_in_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from google.oauth2 import service_account

    material = {"type": "service_account", "private_key": "secret-private-key", "project_id": "test"}
    provider = MutableProvider()
    provider.value = json.dumps(material)
    resolver = SecretResolver({"vault": provider})
    credential = object()
    captures: list[dict[str, Any]] = []

    def from_info(info: Any, **options: Any) -> object:
        assert info == material
        assert options["scopes"] == ["https://www.googleapis.com/auth/devstorage.read_write"]
        return credential

    def factory(**options: Any) -> RecordingDriver:
        captures.append(options)
        return RecordingDriver(objects={}, **options)

    monkeypatch.setattr(service_account.Credentials, "from_service_account_info", from_info)
    monkeypatch.setattr(driver_loader, "GCSDriver", factory)
    path = _config(tmp_path, "gcs", credentials_ref={"provider": "vault", "name": "key"})
    driver = driver_loader.load_drivers(path, secret_resolver=resolver)["archive"]
    try:
        assert captures == [{"credentials_file": None, "credentials": credential}]
        assert sorted(p.name for p in tmp_path.iterdir()) == ["drivers.yaml"]
    finally:
        driver.close()


def test_cloud_backend_identity_is_symmetric_with_wrapper(runtime: Any) -> None:
    from cognistore.drivers.s3_driver import S3Driver

    resolver = runtime[3]
    client = object()
    raw = S3Driver(endpoint_url="https://example.test", client=client)
    driver = RotatingStorageDriver(
        S3Driver, {"endpoint_url": "https://example.test", "client": client},
        {"secret_key": SecretReference("vault", "storage", field="secret"),
         "access_key": SecretReference("vault", "storage", field="access")}, resolver,
    )
    try:
        assert raw.same_backend(driver)
        assert driver.same_backend(raw)
    finally:
        driver.close()


@pytest.mark.parametrize("code,status,category,retryable", [
    ("AccessDenied", 403, "authorization", False),
    ("InvalidArgument", 400, "invalid", False),
    ("SlowDown", 503, "throttled", True),
    ("InternalError", 500, "unavailable", True),
    ("ConditionalRequestConflict", 409, "conflict", True),
])
def test_sdk_error_retry_category_survives_safe_boundary(
    runtime: Any, monkeypatch: pytest.MonkeyPatch,
    code: str, status: int, category: str, retryable: bool,
) -> None:
    from botocore.exceptions import ClientError

    from cognistore.jobs.retry import classify_job_error

    driver, clients, provider, resolver, now = runtime

    def broken_get(*args: Any, **kwargs: Any) -> bytes:
        raise ClientError({
            "Error": {"Code": code, "Message": "secret-one"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }, "GetObject")

    monkeypatch.setattr(clients[0], "get_object", broken_get)
    with pytest.raises(Exception) as captured:
        driver.get_object("bucket", "a")
    classified = classify_job_error(captured.value)
    assert classified.category.value == category
    assert classified.retryable == retryable
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "secret-one" not in "".join(traceback.format_exception(captured.value))


def test_reader_failure_is_safe_but_consumer_exception_is_unchanged(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    driver, clients, provider, resolver, now = runtime

    class BrokenReader:
        def read(self, size: int = -1) -> bytes:
            raise ConnectionError("secret-one")

    @contextmanager
    def broken_reader(*args: Any, **kwargs: Any) -> Iterator[BrokenReader]:
        yield BrokenReader()

    with driver.open_object_reader("bucket", "a"):
        pass
    consumer_error = ValueError("a consumer failed")
    with pytest.raises(ValueError) as consumer:
        with driver.open_object_reader("bucket", "a"):
            raise consumer_error
    assert consumer.value is consumer_error
    monkeypatch.setattr(clients[0], "open_object_reader", broken_reader)
    with pytest.raises(ConnectionError) as captured:
        with driver.open_object_reader("bucket", "a") as stream:
            stream.read()
    assert "secret-one" not in "".join(traceback.format_exception(captured.value))
    assert captured.value.__context__ is None


def test_loader_closes_owned_resolver_after_final_driver_and_cleans_failed_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = MutableProvider()
    closes: list[bool] = []
    monkeypatch.setattr(provider, "close", lambda: closes.append(True), raising=False)
    resolver = SecretResolver({"vault": provider})
    captures: list[RecordingDriver] = []
    configs: list[Any] = []

    def build(config: Any) -> SecretResolver:
        configs.append(config)
        return resolver

    def factory(**options: Any) -> RecordingDriver:
        client = RecordingDriver(objects={}, **options)
        captures.append(client)
        return client

    monkeypatch.setattr(driver_loader, "build_secret_resolver", build)
    monkeypatch.setattr(driver_loader, "S3Driver", factory)
    tier = {"driver": "s3", "secret_key_ref": {"provider": "vault", "name": "storage", "field": "secret"}}
    path = tmp_path / "drivers.yaml"
    path.write_text(yaml.safe_dump({"secrets": {"providers": {}}, "tiers": {"one": tier, "two": tier}}))
    drivers = driver_loader.load_drivers(str(path))
    assert configs == [{"providers": {}}]
    drivers["one"].close()
    assert closes == []
    drivers["two"].close()
    assert closes == [True]
    assert all(client.closed == 1 for client in captures)

    resolver = SecretResolver({"vault": provider})
    path.write_text(yaml.safe_dump({"tiers": {"one": tier, "two": {"driver": "unknown"}}}))
    with pytest.raises(ValueError, match="unsupported driver"):
        driver_loader.load_drivers(str(path))
    assert len(closes) == 2
    assert all(client.closed == 1 for client in captures)


def test_loader_keeps_injected_resolver_owned_by_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = MutableProvider()
    closes: list[bool] = []
    monkeypatch.setattr(provider, "close", lambda: closes.append(True), raising=False)
    resolver = SecretResolver({"vault": provider})
    monkeypatch.setattr(driver_loader, "S3Driver", lambda **options: RecordingDriver(objects={}, **options))
    path = _config(tmp_path, "s3", secret_key_ref={"provider": "vault", "name": "storage", "field": "secret"})
    driver_loader.load_drivers(path, secret_resolver=resolver)["archive"].close()
    assert closes == []
    resolver.close()
    assert closes == [True]


@pytest.mark.parametrize("field", ["connection_string", "credential"])
def test_loader_azure_reference_values_rotate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    pytest.importorskip("azure.storage.blob")
    from cognistore.drivers import azure_blob_driver

    provider = MutableProvider()
    provider.value = "azure-first-secret"
    resolver = SecretResolver({"vault": provider})
    captures: list[RecordingDriver] = []

    def factory(**options: Any) -> RecordingDriver:
        client = RecordingDriver(objects={"a": b"value"}, **options)
        captures.append(client)
        return client

    monkeypatch.setattr(azure_blob_driver, "AzureBlobDriver", factory)
    path = _config(tmp_path, "azure_blob", **{field + "_ref": {"provider": "vault", "name": "azure"}})
    driver = driver_loader.load_drivers(path, secret_resolver=resolver)["archive"]
    try:
        assert captures[0].options[field] == "azure-first-secret"
        provider.value = "azure-second-secret"
        resolver.invalidate()
        assert driver.get_object("b", "a") == b"value"
        assert captures[1].options[field] == "azure-second-secret"
        assert captures[0].closed == 1
    finally:
        driver.close()
