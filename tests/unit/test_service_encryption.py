from __future__ import annotations

import asyncio
import json
import ssl
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.request import HTTPSHandler

import httpx
import pytest

from cognistore.auth.jwt import JWTAuthConfig, JWTAuthenticator
from cognistore.core.embeddings import (
    EmbeddingProviderConfigurationError,
    OpenAICompatibleEmbeddingProvider,
    SentenceTransformersEmbeddingProvider,
)
from cognistore.core.placement_llm_http import HTTPPlacementProvider
from cognistore.db.engine import create_catalog_engine
from cognistore.encryption import require_verified_httpx
from cognistore.jobs.nats_queue import NatsJetStreamConfig, NatsJetStreamQueue
from cognistore.sdk.client import CogniStoreClient
from cognistore.sdk.errors import APIError
from cognistore.secrets.providers import (
    AWSKMSKeyProvider,
    AWSSecretsManagerProvider,
    VaultKVProvider,
)


@pytest.fixture(autouse=True)
def production(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "production")
    monkeypatch.delenv("COGNISTORE_TLS_CA_FILE", raising=False)
    config = tmp_path / "encryption.json"
    config.write_text(json.dumps({
        "version": 1,
        "components": {
            component: {
                "mode": "encrypted-volume",
                "owner": "platform",
                "evidence": "deployment/encrypted-volumes",
                "expires_at": "2099-01-01T00:00:00Z",
                "rotation_days": 90,
            }
            for component in ("catalog", "queue")
        },
    }))
    monkeypatch.setenv("COGNISTORE_ENCRYPTION_CONFIG", str(config))


@pytest.mark.parametrize("url", [
    "postgresql://db.example/catalog?sslmode=disable",
    "postgresql://db.example/catalog?sslmode=prefer",
    "postgresql://db.example/catalog?sslmode=require",
    "postgresql://db.example/catalog?sslmode=verify-ca",
    "postgresql://db.example/catalog?sslmode=verify-full&sslmode=disable",
    "postgresql:///catalog",
    "postgresql://%2Fvar%2Frun%2Fpostgresql/catalog",
    "postgresql://%40abstract-socket/catalog",
    "postgresql://db.example/catalog?host=/tmp",
    "postgresql://db.example/catalog?host=other.example",
    "postgresql://db.example/catalog?hostaddr=127.0.0.1",
    "postgresql://db.example/catalog?service=insecure",
    "postgresql://db.example/dbname%3Dcatalog%20sslmode%3Ddisable",
])
def test_postgres_rejects_transport_downgrades_before_connect(url: str) -> None:
    with pytest.raises(ValueError, match="Production catalog"):
        create_catalog_engine(url)


@pytest.mark.parametrize("schema_name", [None, "cognistore_t_" + "a" * 48])
def test_postgres_enforces_verified_tls_and_readonly(
    monkeypatch: pytest.MonkeyPatch, schema_name: str | None,
) -> None:
    options: dict[str, Any] = {}
    marker = object()

    def create_engine(url: str, **kwargs: Any) -> object:
        options.update(kwargs)
        return marker

    monkeypatch.setattr("cognistore.db.engine.sa.create_engine", create_engine)
    engine, connection = create_catalog_engine(
        "postgresql://db.example/catalog?gssencmode=prefer&ssl_min_protocol_version=TLSv1",
        read_only=True,
        schema_name=schema_name,
    )
    assert engine is marker
    assert connection is None
    assert options["connect_args"] == {
        "sslmode": "verify-full",
        "gssencmode": "disable",
        "ssl_min_protocol_version": "TLSv1.2",
        "options": "-c default_transaction_read_only=on" + (
            f" -c search_path={schema_name},public" if schema_name is not None else ""
        ),
    }


def test_persistent_catalog_requires_current_at_rest_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("COGNISTORE_ENCRYPTION_CONFIG")
    for locator in (tmp_path / "catalog.db", "postgresql://db.example/catalog"):
        with pytest.raises(ValueError, match="catalog requires a current"):
            create_catalog_engine(locator)
    assert not (tmp_path / "catalog.db").exists()
    engine, connection = create_catalog_engine(":memory:")
    engine.dispose()
    assert connection is not None
    connection.close()


@pytest.mark.parametrize("servers", [
    ("nats://localhost:4222",),
    ("tls://nats.example:4222", "nats://other.example:4222"),
    ("tls://nats.example:4222", "other.example:4222"),
])
def test_every_nats_endpoint_requires_tls(servers: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="NATS requires verified TLS"):
        NatsJetStreamConfig(servers=servers)


def test_queue_requires_at_rest_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COGNISTORE_ENCRYPTION_CONFIG")
    with pytest.raises(ValueError, match="queue requires a current"):
        NatsJetStreamQueue(NatsJetStreamConfig(servers=("tls://nats.example:4222",)))


def _assert_verified(context: ssl.SSLContext) -> None:
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_nats_forces_tls_before_server_info(monkeypatch: pytest.MonkeyPatch) -> None:
    options: dict[str, Any] = {}

    async def connect(**kwargs: Any) -> Any:
        options.update(kwargs)
        return SimpleNamespace(jetstream=lambda **_: object())

    async def ensure_stream(self: NatsJetStreamQueue) -> None:
        pass

    monkeypatch.setattr("cognistore.jobs.nats_queue.nats.connect", connect)
    monkeypatch.setattr(NatsJetStreamQueue, "_ensure_stream", ensure_stream)
    queue = NatsJetStreamQueue(
        NatsJetStreamConfig(servers=("tls://nats.example:4222",)), consume=False,
    )
    asyncio.run(queue.connect())
    assert options["tls_handshake_first"] is True
    _assert_verified(options["tls"])


def test_nats_rejects_broker_attempt_to_disable_tls() -> None:
    """A plaintext INFO must never release CONNECT credentials or job data."""
    async def scenario() -> None:
        received: list[bytes] = []
        handlers: list[asyncio.Task[Any]] = []

        async def plaintext_broker(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
        ) -> None:
            writer.write(b'INFO {"tls_required":false,"auth_required":true}\r\n')
            await writer.drain()
            received.append(await reader.read(4096))
            writer.close()
            await writer.wait_closed()

        def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            handlers.append(asyncio.create_task(plaintext_broker(reader, writer)))

        server = await asyncio.start_server(accept, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            queue = NatsJetStreamQueue(NatsJetStreamConfig(
                servers=(f"tls://credential:private-token@127.0.0.1:{port}",),
                allow_reconnect=False,
                report_connection_errors=False,
                connect_timeout=0.5,
            ), consume=False)
            with pytest.raises(Exception):
                await asyncio.wait_for(queue.connect(), 3)
            await asyncio.gather(*handlers)
        finally:
            server.close()
            await server.wait_closed()
        assert received
        assert all(data.startswith(b"\x16\x03") for data in received)
        assert all(b"CONNECT" not in data and b"private-token" not in data for data in received)

    asyncio.run(scenario())


def _embedding(base_url: str) -> OpenAICompatibleEmbeddingProvider:
    return OpenAICompatibleEmbeddingProvider(
        base_url=base_url, api_key=None, model="embed-v1",
        deployment_version="release-1", dimensions=2,
    )


def test_production_rejects_loopback_http_model_services() -> None:
    with pytest.raises(ValueError, match="LLM requires verified TLS"):
        HTTPPlacementProvider("http://localhost:9000/decide", model="placement-v1")
    with pytest.raises(EmbeddingProviderConfigurationError, match="verified HTTPS"):
        _embedding("http://127.0.0.1:9000")


def test_hub_model_downloads_reject_plaintext_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_ENDPOINT", "http://localhost:9000")
    with pytest.raises(EmbeddingProviderConfigurationError, match="model hub endpoints"):
        SentenceTransformersEmbeddingProvider(model="org/model", revision="a" * 40, dimensions=2)


def test_development_explicitly_allows_local_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "development")
    NatsJetStreamConfig(servers=("nats://localhost:4222",))
    HTTPPlacementProvider("http://localhost:9000/decide", model="placement-v1")
    _embedding("http://127.0.0.1:9000")
    engine, _ = create_catalog_engine("postgresql://localhost/catalog?sslmode=disable")
    engine.dispose()


def test_embedding_uses_verified_https_context() -> None:
    provider = _embedding("https://embedding.example")
    handler = next(item for item in provider._opener.handlers if isinstance(item, HTTPSHandler))
    _assert_verified(handler._context)


@pytest.mark.parametrize("service", ["jwt", "vault"])
def test_auth_and_secrets_http_clients_verify_certificates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, service: str,
) -> None:
    options: dict[str, Any] = {}

    def client(**kwargs: Any) -> object:
        options.update(kwargs)
        return object()

    monkeypatch.setattr(httpx, "Client", client)
    if service == "jwt":
        JWTAuthenticator(JWTAuthConfig(issuer="https://identity.example", audience="catalog"))
    else:
        VaultKVProvider("https://vault.example", token_file=tmp_path / "token")
    _assert_verified(options["verify"])
    assert options["trust_env"] is False
    assert options["follow_redirects"] is False


def test_placement_client_verifies_certificates(monkeypatch: pytest.MonkeyPatch) -> None:
    options: dict[str, Any] = {}

    def transport(**kwargs: Any) -> httpx.MockTransport:
        options.update(kwargs)
        return httpx.MockTransport(lambda _: httpx.Response(200, content='{"action":"stay"}'))

    monkeypatch.setattr(httpx, "HTTPTransport", transport)
    provider = HTTPPlacementProvider("https://placement.example/decide", model="placement-v1")
    provider.complete("prompt", {}, 1)
    _assert_verified(options["verify"])
    assert options["trust_env"] is False


@pytest.mark.parametrize("provider_class", [AWSSecretsManagerProvider, AWSKMSKeyProvider])
def test_aws_secrets_ignore_plaintext_endpoint_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider_class: Any,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-credentials"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    for key in ("AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_KMS", "AWS_ENDPOINT_URL_SECRETS_MANAGER"):
        monkeypatch.setenv(key, "http://localhost:9999")
    provider = provider_class(region_name="us-west-2")
    try:
        client = provider._get_client()
        assert client.meta.endpoint_url.startswith("https://")
        assert "localhost" not in client.meta.endpoint_url
        assert client._endpoint.http_session._verify is True
    finally:
        provider.close()


@pytest.mark.parametrize("service", ["sdk", "jwt"])
@pytest.mark.parametrize("misconfiguration", ["unverified", "hostname", "mount"])
def test_injected_http_clients_cannot_disable_verification(
    service: str, misconfiguration: str,
) -> None:
    context = ssl.create_default_context()
    if misconfiguration == "hostname":
        context.check_hostname = False
    options: dict[str, Any] = {"verify": context, "trust_env": False}
    if misconfiguration == "unverified":
        options["verify"] = False
    elif misconfiguration == "mount":
        options["mounts"] = {"https://": httpx.HTTPTransport(verify=False)}
    with httpx.Client(**options) as client:
        with pytest.raises(ValueError, match="certificate and hostname verification"):
            if service == "sdk":
                CogniStoreClient("https://api.example", http_client=client)
            else:
                JWTAuthenticator(
                    JWTAuthConfig(issuer="https://identity.example", audience="catalog"),
                    http_client=client,
                )


@pytest.mark.parametrize("service", ["vault", "placement"])
def test_injected_http_transports_cannot_disable_verification(
    tmp_path: Path, service: str,
) -> None:
    with httpx.HTTPTransport(verify=False) as transport:
        with pytest.raises(ValueError):
            if service == "vault":
                VaultKVProvider(
                    "https://vault.example", token_file=tmp_path / "token", transport=transport,
                )
            else:
                HTTPPlacementProvider(
                    "https://placement.example/decide", model="placement-v1", transport=transport,
                )


def test_unknown_production_transport_fails_closed() -> None:
    with pytest.raises(ValueError, match="certificate and hostname verification"):
        require_verified_httpx(httpx.BaseTransport())


def test_development_https_still_requires_certificate_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "development")
    with httpx.Client(verify=False, trust_env=False) as client:
        with pytest.raises(ValueError, match="certificate and hostname verification"):
            require_verified_httpx(client)


def test_sdk_injected_client_cannot_follow_plaintext_redirect() -> None:
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://other.example/healthz"})

    with httpx.Client(
        transport=httpx.MockTransport(redirect), follow_redirects=True,
    ) as client:
        sdk = CogniStoreClient("https://api.example", http_client=client)
        with pytest.raises(APIError):
            sdk.get_health()
    assert [str(request.url) for request in requests] == ["https://api.example/healthz"]


def test_verified_injected_http_client_is_supported() -> None:
    with httpx.Client(trust_env=False) as client:
        require_verified_httpx(client)
        sdk = CogniStoreClient("https://api.example", http_client=client)
        sdk.close()
        assert client.is_closed is False
