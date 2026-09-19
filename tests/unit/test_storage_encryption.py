"""Production transport rejection and provider encryption write contracts."""

from __future__ import annotations

import json
import ssl
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest

from cognistore.auth.tenancy import TenantIsolationError, tenant_context
from cognistore.drivers import driver_loader, gcs_driver, s3_driver
from cognistore.drivers.gcs_driver import GCSDriver
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.drivers.s3_driver import S3Driver
from cognistore.drivers.tenancy import TenantStorageDriver
from tests.gcs_fake import GCSFake
from tests.unit.test_s3_driver import _FakeS3Client


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "production")
    monkeypatch.delenv("COGNISTORE_ENCRYPTION_CONFIG", raising=False)


def _native_s3() -> _FakeS3Client:
    client = _FakeS3Client()
    client.meta = SimpleNamespace(endpoint_url="https://s3.us-east-1.amazonaws.com")
    return client


@pytest.mark.parametrize("tenant_id", ["default", "alpha"])
def test_tenant_encryption_status_preserves_safe_provider_details_and_isolation(
    production, monkeypatch, tenant_id,
) -> None:
    driver = S3Driver(
        client=_native_s3(), server_side_encryption="aws:kms", kms_key_id="private-key-id",
    )
    scoped = TenantStorageDriver(driver, tenant_id)
    with tenant_context(tenant_id):
        status = scoped.encryption_status()
    assert status == {"source": "provider", "mode": "aws:kms", "key_configured": True}
    assert "private-key-id" not in json.dumps(status)

    def unexpected_status():
        pytest.fail("Backend encryption status accessed by a different tenant")

    monkeypatch.setattr(driver, "encryption_status", unexpected_status)
    with tenant_context("other"), pytest.raises(TenantIsolationError):
        scoped.encryption_status()


@pytest.mark.parametrize("injected", [False, True])
def test_s3_rejects_plaintext_explicit_and_effective_endpoints(production, injected) -> None:
    client = _native_s3()
    options = {"client": client}
    if injected:
        client.meta.endpoint_url = "http://s3.us-east-1.amazonaws.com"
    else:
        options["endpoint_url"] = "http://localhost:9000"
    with pytest.raises(ValueError, match="TLS"):
        S3Driver(**options)


def test_s3_custom_endpoint_requires_attestation_even_with_sse(production) -> None:
    client = _native_s3()
    client.meta.endpoint_url = "https://minio.example.test"
    with pytest.raises(ValueError, match="attestation"):
        S3Driver(client=client, server_side_encryption="AES256")


def test_s3_injected_client_cannot_disable_verification() -> None:
    client = _native_s3()
    client._endpoint = SimpleNamespace(http_session=SimpleNamespace(_verify=False))
    with pytest.raises(ValueError, match="verification"):
        S3Driver(client=client)


@pytest.mark.parametrize("mode,key", [("AES256", None), ("aws:kms", "private-key-id")])
def test_s3_applies_encryption_to_bytes_single_stream_and_multipart(production, mode, key) -> None:
    client = _native_s3()
    driver = S3Driver(
        client=client, server_side_encryption=mode, kms_key_id=key,
        multipart_threshold=2,
    )
    driver.put_object("bucket", "bytes", b"a", overwrite=False)
    driver.put_object_stream("bucket", "small", BytesIO(b"a"), size=1)
    driver.put_object_stream("bucket", "large", BytesIO(b"abc"), size=3)
    for request in [*client.put_calls, *client.create_multipart_calls]:
        assert request["ServerSideEncryption"] == mode
        assert request.get("SSEKMSKeyId") == key
    assert client.put_calls[0]["IfNoneMatch"] == "*"
    assert driver.encryption_status()["key_configured"] is (key is not None)
    assert "private-key-id" not in json.dumps(driver.encryption_status())


@pytest.mark.parametrize("options", [
    {"server_side_encryption": "none"},
    {"server_side_encryption": "aws:kms"},
    {"kms_key_id": "key"},
    {"server_side_encryption": "AES256", "kms_key_id": "key"},
    {"server_side_encryption": "aws:kms", "kms_key_id": ""},
])
def test_s3_rejects_invalid_encryption_configuration(options) -> None:
    with pytest.raises(ValueError):
        S3Driver(client=_native_s3(), **options)


@pytest.mark.parametrize("override", [
    {"ServerSideEncryption": "AES256"},
    {"SSEKMSKeyId": "other-key"},
    {"SSECustomerKey": "secret-key"},
])
def test_s3_cannot_override_tier_key(override) -> None:
    client = _native_s3()
    driver = S3Driver(client=client, server_side_encryption="aws:kms", kms_key_id="tier-key")
    with pytest.raises(ValueError, match="override"):
        driver.put_object("bucket", "key", b"data", **override)
    assert not client.put_calls


def test_s3_owned_transport_explicitly_verifies_tls(monkeypatch) -> None:
    captured = {}
    client = _native_s3()

    class Session:
        region_name = "us-east-1"

        def __init__(self, **kwargs):
            pass

        def get_partition_for_region(self, region):
            return "aws"

        def client(self, service, **kwargs):
            captured.update(kwargs)
            return client

    monkeypatch.setattr(s3_driver.boto3, "Session", Session)
    monkeypatch.setattr(s3_driver, "ca_bundle", lambda: "/trusted/ca.pem")
    S3Driver()
    assert captured["verify"] == "/trusted/ca.pem"


def test_posix_requires_current_external_encryption_evidence(production, tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="attestation"):
        PosixDriver(str(tmp_path / "objects"))
    config = tmp_path / "encryption.json"
    config.write_text(json.dumps({"version": 1, "components": {"storage": {
        "mode": "encrypted-volume", "owner": "operations", "evidence": "ticket-123",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        "rotation_days": 90,
    }}}))
    monkeypatch.setenv("COGNISTORE_ENCRYPTION_CONFIG", str(config))
    driver = PosixDriver(str(tmp_path / "objects"))
    driver.put_object("bucket", "key", b"value")
    assert driver.get_object("bucket", "key") == b"value"


def test_gcs_rejects_plaintext_and_unattested_emulators(production):
    with pytest.raises(ValueError, match="TLS"):
        GCSDriver(emulator_endpoint="http://localhost:4443")
    with pytest.raises(ValueError, match="attestation"):
        GCSDriver(emulator_endpoint="https://localhost:4443")


def test_gcs_rejects_injected_client_without_certificate_verification():
    with httpx.Client(verify=False) as client:
        with pytest.raises(ValueError, match="verification"):
            GCSDriver(client=client, emulator_endpoint="http://gcs.test")


def test_gcs_kms_key_is_bound_when_resumable_upload_starts() -> None:
    server = GCSFake()
    key = "projects/private-project/locations/us/keyRings/private-ring/cryptoKeys/private-key"
    with server.client() as client:
        driver = GCSDriver(client=client, emulator_endpoint=server.endpoint, kms_key_name=key)
        driver.put_object_stream("bucket", "key", BytesIO(b"payload"), size=7)
    start = next(request for request in server.requests if request.method == "POST")
    assert start.url.params["kmsKeyName"] == key
    assert driver.encryption_status()["key_configured"] is True
    assert "private-project" not in json.dumps(driver.encryption_status())


def test_gcs_owned_http_and_auth_sessions_verify_certificates(monkeypatch, production) -> None:
    captured = {}
    real_client = httpx.Client

    def make_client(**kwargs):
        captured.update(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr(gcs_driver.httpx, "Client", make_client)
    driver = GCSDriver(credentials=SimpleNamespace())
    try:
        assert captured["verify"].verify_mode == ssl.CERT_REQUIRED
        assert captured["verify"].check_hostname is True
        assert driver._auth_request.session.verify is True
        assert driver._auth_request.session.trust_env is False
        assert captured["trust_env"] is False
    finally:
        driver.close()


@pytest.mark.parametrize("field", ["_token_uri", "_token_url", "_service_account_impersonation_url"])
def test_gcs_rejects_plaintext_token_exchange_endpoints(production, field):
    with pytest.raises(ValueError, match="TLS"):
        GCSDriver(credentials=SimpleNamespace(**{field: "http://tokens.example.test"}))


def test_native_cloud_uploads_require_runtime_evidence_before_disk_spill(production, monkeypatch):
    def unexpected_spool(**kwargs):
        pytest.fail("Disk spool opened before validating encryption evidence")

    monkeypatch.setattr(s3_driver, "SpooledTemporaryFile", unexpected_spool)
    monkeypatch.setattr(gcs_driver, "SpooledTemporaryFile", unexpected_spool)
    s3 = S3Driver(client=_native_s3(), multipart_threshold=10 * 1024 * 1024)
    with pytest.raises(ValueError, match="runtime.*attestation"):
        s3.put_object_stream("bucket", "key", BytesIO(), size=s3.chunk_size + 1)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
        gcs = GCSDriver(client=client, credentials=SimpleNamespace())
        try:
            with pytest.raises(ValueError, match="runtime.*attestation"):
                gcs.put_object_stream("bucket", "key", BytesIO(), size=gcs.chunk_size + 1)
        finally:
            gcs.close()


def test_azure_rejects_plaintext_urls_and_connection_strings(production):
    pytest.importorskip("azure.storage.blob")
    from cognistore.drivers.azure_blob_driver import AzureBlobDriver

    for options in (
        {"account_url": "http://account.blob.core.windows.net"},
        {"connection_string": "UseDevelopmentStorage=true"},
        {"connection_string": "BlobEndpoint=http://localhost:10000;AccountName=secret-name"},
        {"connection_string": "BlobSecondaryEndpoint=http://localhost:10000"},
    ):
        with pytest.raises(ValueError, match="TLS"):
            AzureBlobDriver(**options)


@pytest.mark.parametrize("payload", [b"", b"abc"])
def test_azure_scope_applies_to_every_block_and_atomic_commit(production, payload):
    pytest.importorskip("azure.storage.blob")
    from cognistore.drivers.azure_blob_driver import AzureBlobDriver
    from tests.azure_blob_fixtures import FakeAzureService

    client = FakeAzureService()
    client.containers.add("container")
    driver = AzureBlobDriver(client=client, chunk_size=2, encryption_scope="private-scope")
    driver.put_object("container", "key", payload)
    for request in [*client.stage_calls, *client.commit_calls]:
        assert request["encryption_scope"] == "private-scope"
    assert "private-scope" not in json.dumps(driver.encryption_status())


def test_azure_rejects_injected_client_without_certificate_verification():
    pytest.importorskip("azure.storage.blob")
    from azure.storage.blob import BlobServiceClient

    from cognistore.drivers.azure_blob_driver import AzureBlobDriver

    with BlobServiceClient(
        "https://account.blob.core.windows.net", connection_verify=False
    ) as client:
        with pytest.raises(ValueError, match="verification"):
            AzureBlobDriver(client=client)


@pytest.mark.parametrize("backend,fields", [
    ("s3", {"server_side_encryption": "aws:kms", "kms_key_id": "key"}),
    ("gcs", {"kms_key_name": "key"}),
    ("azure_blob", {"encryption_scope": "scope"}),
])
def test_loader_passes_encryption_fields(tmp_path, monkeypatch, backend, fields):
    import yaml

    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    if backend == "azure_blob":
        pytest.importorskip("azure.storage.blob")
        from cognistore.drivers import azure_blob_driver
        monkeypatch.setattr(azure_blob_driver, "AzureBlobDriver", factory)
    else:
        monkeypatch.setattr(driver_loader, "S3Driver" if backend == "s3" else "GCSDriver", factory)
    config = tmp_path / "drivers.yaml"
    config.write_text(yaml.safe_dump({"tiers": {"archive": {"driver": backend, **fields}}}))
    driver_loader.load_drivers(str(config))
    assert all(captured[field] == value for field, value in fields.items())
