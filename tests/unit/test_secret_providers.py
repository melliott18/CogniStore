"""Provider protocol contracts, rotation bootstrap, and diagnostic safety."""
from __future__ import annotations

import base64
import io
import json
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from botocore.exceptions import ClientError

from cognistore.secrets.core import (
    KeyReference,
    SecretAccessError,
    SecretConfigurationError,
    SecretReference,
    SecretUnavailableError,
)
from cognistore.secrets.diagnostics import secret_operation
from cognistore.secrets.providers import (
    AWSKMSKeyProvider,
    AWSSecretsManagerProvider,
    VaultKVProvider,
    VaultTransitKeyProvider,
)


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "vault-token"
    path.write_text("bootstrap-token-one\n")
    return path


class FakeAWS:
    def __init__(self, response: dict[str, Any] | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        logging.getLogger("botocore.endpoint").debug("Wire response: %r", self.response)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    decrypt = get_secret_value


def test_vault_kv_uses_version_namespace_and_rotated_token(token_file: Path) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "data": {"data": {"secret": "plaintext"}, "metadata": {"version": 4}},
            "lease_duration": 20,
        })

    provider = VaultKVProvider(
        "https://vault.example", token_file=token_file, mount="team/kv", namespace="team",
        transport=httpx.MockTransport(handle),
    )
    reference = SecretReference("vault", "storage/team key", version="4", field="secret")
    first = provider.fetch(reference)
    token_file.write_text("bootstrap-token-two")
    second = provider.fetch(reference)
    provider.close()
    assert first.text() == second.text() == "plaintext"
    assert first.version == "4" and first.ttl_seconds == 20
    assert requests[0].url.raw_path == b"/v1/team/kv/data/storage/team%20key?version=4"
    assert requests[0].headers["X-Vault-Namespace"] == "team"
    assert [request.headers["X-Vault-Token"] for request in requests] == [
        "bootstrap-token-one", "bootstrap-token-two",
    ]


def test_vault_kv_returns_atomic_json_object_without_field(token_file: Path) -> None:
    provider = VaultKVProvider(
        "https://vault.example", token_file=token_file,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "data": {"data": {"b": "second", "a": "first"}, "metadata": {"version": 1}},
            "lease_duration": 0,
        })),
    )
    value = provider.fetch(SecretReference("vault", "storage"))
    assert value.text() == '{"a":"first","b":"second"}'
    assert value.ttl_seconds is None
    provider.close()


@pytest.mark.parametrize("address", [
    "http://vault.example", "https://user:secret@vault.example", "https://vault.example?token=x",
    "https://vault.example#secret", "https://", "https://vault.example:invalid",
])
def test_vault_rejects_insecure_or_credentialed_addresses(token_file: Path, address: str) -> None:
    with pytest.raises(SecretConfigurationError) as error:
        VaultKVProvider(address, token_file=token_file)
    assert address not in str(error.value)


@pytest.mark.parametrize("mount", ["..", "/secret", "secret/../other", "secret//other"])
def test_vault_rejects_escaping_mount(token_file: Path, mount: str) -> None:
    with pytest.raises(SecretConfigurationError):
        VaultKVProvider("https://vault.example", token_file=token_file, mount=mount)


@pytest.mark.parametrize("status,error_type", [
    (302, SecretAccessError), (400, SecretAccessError), (403, SecretAccessError),
    (404, SecretAccessError), (408, SecretUnavailableError), (429, SecretUnavailableError),
    (500, SecretUnavailableError), (503, SecretUnavailableError),
])
def test_vault_fails_closed_and_redacts_response(
    token_file: Path, status: int, error_type: type[Exception], caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        logging.getLogger("httpcore.new_lazy_logger").error("bootstrap-token-one plaintext")
        return httpx.Response(status, text="plaintext", headers={"location": "https://elsewhere"})

    provider = VaultKVProvider("https://vault.example", token_file=token_file,
                               transport=httpx.MockTransport(handle))
    with caplog.at_level(logging.DEBUG), pytest.raises(error_type) as error:
        provider.fetch(SecretReference("vault", "storage"))
    diagnostics = "".join(traceback.format_exception(error.value)) + caplog.text
    assert "plaintext" not in diagnostics
    assert "bootstrap-token-one" not in diagnostics
    assert len(requests) == 1
    provider.close()


def test_vault_timeout_and_missing_bootstrap_are_unavailable(token_file: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("plaintext token", request=request)

    provider = VaultKVProvider("https://vault.example", token_file=token_file,
                               transport=httpx.MockTransport(handle))
    with pytest.raises(SecretUnavailableError) as error:
        provider.fetch(SecretReference("vault", "storage"))
    assert "plaintext" not in "".join(traceback.format_exception(error.value))
    token_file.unlink()
    with pytest.raises(SecretUnavailableError):
        provider.fetch(SecretReference("vault", "storage"))
    provider.close()


@pytest.mark.parametrize("payload", [
    {}, {"data": {"data": None, "metadata": {"version": 1}}},
    {"data": {"data": {"secret": "plaintext"}, "metadata": {"version": 0}}},
    {"data": {"data": {"secret": 123}, "metadata": {"version": 1}}},
    {"data": {"data": {"other": "plaintext"}, "metadata": {"version": 1}}},
])
def test_vault_invalid_payload_is_permanent_failure(token_file: Path, payload: dict) -> None:
    provider = VaultKVProvider("https://vault.example", token_file=token_file,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    with pytest.raises(SecretAccessError):
        provider.fetch(SecretReference("vault", "storage", field="secret"))
    provider.close()


def test_vault_transit_decrypts_with_canonical_context(token_file: Path) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {
            "plaintext": base64.b64encode(b"unwrapped-key\0").decode("ascii"),
        }})

    provider = VaultTransitKeyProvider("https://vault.example", token_file=token_file,
                                      transport=httpx.MockTransport(handle))
    value = provider.unwrap(KeyReference("vault", "backup", b"vault:v2:wrapped",
                                        context=(("tenant", "team"), ("app", "cognistore"))))
    assert value.reveal() == b"unwrapped-key\0"
    assert value.version == "v2"
    assert requests[0].url.path == "/v1/transit/decrypt/backup"
    payload = json.loads(requests[0].content)
    assert payload["ciphertext"] == "vault:v2:wrapped"
    assert base64.b64decode(payload["context"]) == b'{"app":"cognistore","tenant":"team"}'
    provider.close()


def test_vault_transit_rejects_invalid_plaintext(token_file: Path) -> None:
    provider = VaultTransitKeyProvider("https://vault.example", token_file=token_file,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "data": {"plaintext": "not base64"},
        })))
    with pytest.raises(SecretAccessError):
        provider.unwrap(KeyReference("vault", "backup", b"vault:v1:wrapped"))
    provider.close()


def test_aws_secret_version_and_json_field() -> None:
    client = FakeAWS({"SecretString": '{"password":"plaintext"}', "VersionId": "version-one"})
    provider = AWSSecretsManagerProvider(client=client)
    value = provider.fetch(SecretReference("aws", "storage", version="version-one", field="password"))
    assert value.text() == "plaintext" and value.version == "version-one"
    assert client.calls == [{"SecretId": "storage", "VersionId": "version-one"}]


def test_aws_secret_binary_is_not_base64_decoded_again() -> None:
    client = FakeAWS({"SecretBinary": b"already decoded\xff"})
    value = AWSSecretsManagerProvider(client=client).fetch(SecretReference("aws", "storage"))
    assert value.reveal() == b"already decoded\xff"
    assert client.calls == [{"SecretId": "storage"}]


def test_aws_kms_uses_key_and_encryption_context() -> None:
    client = FakeAWS({"Plaintext": b"unwrapped-key", "KeyId": "arn:aws:kms:key"})
    value = AWSKMSKeyProvider(client=client).unwrap(KeyReference(
        "aws", "arn:aws:kms:key", b"wrapped", context=(("tenant", "one"),),
    ))
    assert value.reveal() == b"unwrapped-key"
    assert client.calls == [{"CiphertextBlob": b"wrapped", "KeyId": "arn:aws:kms:key",
                            "EncryptionAlgorithm": "SYMMETRIC_DEFAULT",
                            "EncryptionContext": {"tenant": "one"}}]


@pytest.mark.parametrize("code,status,error_type", [
    ("AccessDeniedException", 400, SecretAccessError),
    ("ResourceNotFoundException", 400, SecretAccessError),
    ("InvalidCiphertextException", 400, SecretAccessError),
    ("ThrottlingException", 400, SecretUnavailableError),
    ("KMSInternalException", 500, SecretUnavailableError),
    ("Other", 429, SecretUnavailableError),
])
@pytest.mark.parametrize("is_key", [False, True])
def test_aws_failure_classification_and_diagnostic_safety(
    code: str, status: int, error_type: type[Exception], is_key: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeAWS(ClientError({"Error": {"Code": code, "Message": "plaintext-token"},
                                 "ResponseMetadata": {"HTTPStatusCode": status}}, "GetSecretValue"))
    with caplog.at_level(logging.DEBUG), pytest.raises(error_type) as error:
        if is_key:
            AWSKMSKeyProvider(client=client).unwrap(KeyReference("aws", "key", b"cipher"))
        else:
            AWSSecretsManagerProvider(client=client).fetch(SecretReference("aws", "storage"))
    assert "plaintext-token" not in caplog.text + "".join(traceback.format_exception(error.value))
    assert error.value.__context__ is None
    assert error.value.__cause__ is None


def test_aws_default_chain_client_is_lazy_and_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeAWS({"SecretString": "plaintext"})

    class Session:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(kwargs)

        def client(self, service: str, **kwargs: Any) -> FakeAWS:
            calls.append({"service": service, **kwargs})
            return client

    monkeypatch.setattr("cognistore.secrets.providers.boto3.session.Session", Session)
    provider = AWSSecretsManagerProvider(region_name="us-west-2", timeout_seconds=3)
    assert not calls
    provider.fetch(SecretReference("aws", "storage"))
    provider.fetch(SecretReference("aws", "storage"))
    assert len(calls) == 2
    assert calls[0] == {"region_name": "us-west-2"}
    assert calls[1]["service"] == "secretsmanager"
    assert calls[1]["config"].connect_timeout == 3
    assert calls[1]["config"].retries["total_max_attempts"] == 2


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_provider_timeout_is_bounded(timeout: float) -> None:
    with pytest.raises(SecretConfigurationError):
        AWSSecretsManagerProvider(timeout_seconds=timeout)


def test_diagnostics_protect_new_handlers_exceptions_and_restore_other_threads() -> None:
    output = io.StringIO()
    logger = logging.getLogger("new-secret-sdk")
    handler = logging.StreamHandler(output)
    handler.setFormatter(logging.Formatter("%(message)s %(exc_text)s %(secret)s", defaults={"secret": ""}))
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        with secret_operation():
            logger.addHandler(handler)  # Created after entering the secret operation.
            try:
                raise RuntimeError("plaintext-token")
            except RuntimeError:
                logger.exception("payload %s", "plaintext-token")
            with secret_operation():
                logger.info("plaintext-token", extra={"secret": "plaintext-token"})
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(logger.info, "unrelated-thread").result()
        logger.info("outside-operation")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        handler.close()
    assert "plaintext-token" not in output.getvalue()
    assert "unrelated-thread" in output.getvalue()
    assert "outside-operation" in output.getvalue()


@pytest.mark.parametrize("version", ["0", "-1", "latest"])
def test_vault_rejects_unpinned_version_before_request(token_file: Path, version: str) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    provider = VaultKVProvider("https://vault.example", token_file=token_file,
                               transport=httpx.MockTransport(handle))
    with pytest.raises(SecretAccessError) as error:
        provider.fetch(SecretReference("vault", "storage", version=version))
    assert not requests
    assert error.value.__context__ is None
    provider.close()


def test_vault_rejects_response_for_wrong_pinned_version(token_file: Path) -> None:
    provider = VaultKVProvider("https://vault.example", token_file=token_file,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "data": {"data": {"secret": "plaintext"}, "metadata": {"version": 2}},
        })))
    with pytest.raises(SecretAccessError):
        provider.fetch(SecretReference("vault", "storage", version="1"))
    provider.close()


def test_aws_rejects_response_for_wrong_pinned_version() -> None:
    client = FakeAWS({"SecretString": "plaintext", "VersionId": "two"})
    with pytest.raises(SecretAccessError):
        AWSSecretsManagerProvider(client=client).fetch(SecretReference("aws", "storage", version="one"))


def test_aws_closes_only_owned_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    class Client(FakeAWS):
        closed = False

        def close(self) -> None:
            self.closed = True

    owned = Client({"Plaintext": b"key-material"})
    injected = Client({"Plaintext": b"key-material"})

    class Session:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def client(self, *args: Any, **kwargs: Any) -> Client:
            return owned

    monkeypatch.setattr("cognistore.secrets.providers.boto3.session.Session", Session)
    provider = AWSKMSKeyProvider()
    provider.unwrap(KeyReference("aws", "key", b"wrapped"))
    provider.close()
    AWSKMSKeyProvider(client=injected).close()
    assert owned.closed
    assert not injected.closed


@pytest.mark.parametrize("region", ["", 3, {}, []])
def test_aws_rejects_invalid_region_configuration(region: Any) -> None:
    with pytest.raises(SecretConfigurationError):
        AWSKMSKeyProvider(region_name=region)


def test_vault_client_constructor_failure_has_no_raw_context(
    token_file: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_client(**kwargs: Any) -> None:
        raise RuntimeError("bootstrap-plaintext")

    monkeypatch.setattr("cognistore.secrets.providers.httpx.Client", failed_client)
    with pytest.raises(SecretConfigurationError) as error:
        VaultKVProvider("https://vault.example", token_file=token_file)
    assert error.value.__context__ is None
    assert "bootstrap-plaintext" not in str(error.value)
