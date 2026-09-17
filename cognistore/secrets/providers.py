"""Read-only Vault and AWS adapters for runtime secrets and wrapped keys.

Vault authentication uses an externally maintained token file (for example a
Vault Agent sink); AWS authentication uses the standard SDK credential chain.
Neither adapter accepts embedded bootstrap credentials or persists plaintext.
"""
from __future__ import annotations

import base64
import json
import math
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError

from .core import (
    KeyReference,
    SecretAccessError,
    SecretConfigurationError,
    SecretError,
    SecretReference,
    SecretUnavailableError,
    SecretValue,
)
from .diagnostics import secret_operation


def _timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SecretConfigurationError("Provider timeout must be positive and finite.")
    if not math.isfinite(value) or value <= 0:
        raise SecretConfigurationError("Provider timeout must be positive and finite.")
    return float(value)


def _vault_path(value: str) -> str:
    # Encode each segment independently; disallow traversal even after URL
    # normalization. Literal '%' is encoded rather than interpreted twice.
    if not isinstance(value, str) or any(part in ("", ".", "..") for part in value.split("/")):
        raise SecretConfigurationError("Vault paths must contain valid relative segments.")
    return "/".join(quote(part, safe="") for part in value.split("/"))


def _lease_ttl(response: dict[str, Any]) -> float | None:
    duration = response.get("lease_duration", 0)
    if isinstance(duration, bool) or not isinstance(duration, (float, int)):
        raise ValueError("Invalid lease duration")
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("Invalid lease duration")
    # KV v2 uses zero to mean no lease. Runtime policy still caps cache TTL.
    return float(duration) if duration else None


class _VaultProvider:
    def __init__(
        self,
        address: str,
        *,
        token_file: str | Path,
        mount: str,
        namespace: str | None = None,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        try:
            parsed = urlsplit(address)
            valid = (parsed.scheme == "https" and bool(parsed.hostname)
                     and parsed.username is None and parsed.password is None
                     and not parsed.query and not parsed.fragment)
            parsed.port  # Validate an explicitly supplied port.
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise SecretConfigurationError("Vault requires an HTTPS address without credentials.")
        if not isinstance(token_file, (str, Path)) or not str(token_file):
            raise SecretConfigurationError("Vault requires a bootstrap token file.")
        if namespace is not None and (
            not isinstance(namespace, str) or not namespace or "\n" in namespace or "\r" in namespace
        ):
            raise SecretConfigurationError("Vault namespace is invalid.")
        self._address = address.rstrip("/")
        self._token_file = Path(token_file)
        self._mount = _vault_path(mount)
        self._namespace = namespace
        timeout = _timeout(timeout_seconds)
        failed = False
        with secret_operation():
            try:
                self._client = httpx.Client(
                    timeout=timeout, follow_redirects=False, transport=transport,
                )
            except Exception:
                failed = True
        if failed:
            raise SecretConfigurationError("Vault client configuration failed.")

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        # Re-read the file for every fetch so a Vault Agent can rotate its token.
        token = self._token_file.read_text(encoding="utf-8").strip()
        if not token or "\n" in token or "\r" in token:
            raise ValueError("Invalid bootstrap token")
        headers = {"X-Vault-Token": token}
        if self._namespace is not None:
            headers["X-Vault-Namespace"] = self._namespace
        response = self._client.request(
            method, f"{self._address}/v1/{self._mount}/{path}", headers=headers, **kwargs,
        )
        # Do not include the URL, response body, or HTTP exception in errors.
        if response.status_code != 200:
            if response.status_code in (408, 429) or response.status_code >= 500:
                raise SecretUnavailableError("Secret provider is unavailable.")
            raise SecretAccessError("Secret provider access failed.")
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Invalid provider response")
        return result

    def close(self) -> None:
        failed = False
        with secret_operation():
            try:
                self._client.close()
            except Exception:
                failed = True
        if failed:
            raise SecretUnavailableError("Secret provider close failed.")


class VaultKVProvider(_VaultProvider):
    """Read Vault KV v2 secrets. No field returns the entire JSON secret object."""

    def __init__(
        self,
        address: str,
        *,
        token_file: str | Path,
        mount: str = "secret",
        namespace: str | None = None,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(address, token_file=token_file, mount=mount, namespace=namespace,
                         timeout_seconds=timeout_seconds, transport=transport)

    def fetch(self, reference: SecretReference) -> SecretValue:
        with secret_operation():
            try:
                if reference.version is not None and (
                    not reference.version.isdecimal() or int(reference.version) < 1
                ):
                    raise ValueError("Invalid pinned Vault version")
                params = {} if reference.version is None else {"version": reference.version}
                result = self._request("GET", f"data/{_vault_path(reference.name)}", params=params)
                data = result["data"]["data"]
                metadata = result["data"]["metadata"]
                if not isinstance(data, dict) or not isinstance(metadata, dict):
                    raise ValueError("Invalid secret response")
                version = metadata["version"]
                if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                    raise ValueError("Invalid secret version")
                if reference.version is not None and version != int(reference.version):
                    raise ValueError("Secret version did not match")
                value = (json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
                         if reference.field is None else data[reference.field])
                if not isinstance(value, str):
                    raise ValueError("Secret field must be text")
                return SecretValue(value, version=str(version), ttl_seconds=_lease_ttl(result))
            except SecretAccessError:
                failure: SecretError = SecretAccessError("Secret provider access failed.")
            except (ValueError, KeyError, TypeError):
                failure = SecretAccessError("Secret provider response is invalid.")
            except Exception:
                failure = SecretUnavailableError("Secret provider is unavailable.")
        raise failure


class VaultTransitKeyProvider(_VaultProvider):
    """Unwrap Vault Transit ciphertext; optional context is canonical JSON bytes."""

    def __init__(
        self,
        address: str,
        *,
        token_file: str | Path,
        mount: str = "transit",
        namespace: str | None = None,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(address, token_file=token_file, mount=mount, namespace=namespace,
                         timeout_seconds=timeout_seconds, transport=transport)

    def unwrap(self, reference: KeyReference) -> SecretValue:
        with secret_operation():
            try:
                ciphertext = reference.ciphertext.decode("ascii")
                payload = {"ciphertext": ciphertext}
                if reference.context:
                    context = json.dumps(dict(reference.context), sort_keys=True,
                                         separators=(",", ":")).encode("utf-8")
                    payload["context"] = base64.b64encode(context).decode("ascii")
                result = self._request("POST", f"decrypt/{_vault_path(reference.name)}", json=payload)
                value = base64.b64decode(result["data"]["plaintext"], validate=True)
                # The version is carried by the ciphertext, e.g. vault:v2:...
                parts = ciphertext.split(":", 2)
                version = parts[1] if len(parts) == 3 and parts[0] == "vault" else None
                return SecretValue(value, version=version, ttl_seconds=_lease_ttl(result))
            except SecretAccessError:
                failure: SecretError = SecretAccessError("Key provider access failed.")
            except (ValueError, KeyError, TypeError):
                failure = SecretAccessError("Key provider response is invalid.")
            except Exception:
                failure = SecretUnavailableError("Key provider is unavailable.")
        raise failure


def _aws_error(error: ClientError) -> SecretError:
    response = error.response
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = response.get("Error", {}).get("Code", "")
    transient_codes = {
        "Throttling", "ThrottlingException", "TooManyRequestsException",
        "RequestLimitExceeded", "RequestTimeout", "RequestTimeoutException",
        "InternalServiceError", "InternalServiceErrorException", "InternalFailure",
        "KMSInternalException", "DependencyTimeoutException", "KeyUnavailableException",
        "ServiceUnavailable", "ServiceUnavailableException",
    }
    if code in transient_codes or status in (408, 429) or (
        isinstance(status, int) and status >= 500
    ):
        return SecretUnavailableError("Secret provider is unavailable.")
    return SecretAccessError("Secret provider access failed.")


class _AWSProvider:
    def __init__(
        self,
        service: Literal["kms", "secretsmanager"],
        *,
        region_name: str | None = None,
        client: Any = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if region_name is not None and (
            not isinstance(region_name, str) or not region_name.strip()
        ):
            raise SecretConfigurationError("AWS region must be nonempty text.")
        self._region_name = region_name
        self._client = client
        self._owns_client = client is None
        self._service = service
        self._timeout_seconds = _timeout(timeout_seconds)
        self._client_lock = Lock()

    def _get_client(self) -> Any:
        # Lazily resolve credentials, then reuse this thread-safe SDK client.
        with self._client_lock:
            if self._client is None:
                self._client = boto3.session.Session(region_name=self._region_name).client(
                    self._service,
                    config=Config(
                        connect_timeout=self._timeout_seconds,
                        read_timeout=self._timeout_seconds,
                        retries={"total_max_attempts": 2, "mode": "standard"},
                    ),
                )
        return self._client


    def close(self) -> None:
        failed = False
        with secret_operation(), self._client_lock:
            try:
                if self._owns_client and self._client is not None:
                    self._client.close()
            except Exception:
                failed = True
        if failed:
            raise SecretUnavailableError("Secret provider close failed.")


class AWSSecretsManagerProvider(_AWSProvider):
    """Fetch AWSCURRENT by default; version references select an exact VersionId."""

    def __init__(
        self, *, region_name: str | None = None, client: Any = None, timeout_seconds: float = 5.0,
    ) -> None:
        super().__init__("secretsmanager", region_name=region_name, client=client,
                         timeout_seconds=timeout_seconds)

    def fetch(self, reference: SecretReference) -> SecretValue:
        with secret_operation():
            try:
                params = {"SecretId": reference.name}
                if reference.version is not None:
                    params["VersionId"] = reference.version
                result = self._get_client().get_secret_value(**params)
                value = result["SecretString"] if "SecretString" in result else result["SecretBinary"]
                # Botocore already decodes blob-shaped response fields to bytes.
                if not isinstance(value, (str, bytes)):
                    raise ValueError("Invalid secret value")
                if reference.field is not None:
                    data = json.loads(value)
                    if not isinstance(data, dict):
                        raise ValueError("Secret must be a JSON object")
                    value = data[reference.field]
                    if not isinstance(value, str):
                        raise ValueError("Secret field must be text")
                version = result.get("VersionId")
                if version is not None and not isinstance(version, str):
                    raise ValueError("Invalid secret version")
                if reference.version is not None and version != reference.version:
                    raise ValueError("Secret version did not match")
                return SecretValue(value, version=version)
            except SecretAccessError:
                failure: SecretError = SecretAccessError("Secret provider access failed.")
            except (ValueError, KeyError, TypeError):
                failure = SecretAccessError("Secret provider response is invalid.")
            except ClientError as error:
                failure = _aws_error(error)
            except (NoCredentialsError, PartialCredentialsError):
                failure = SecretAccessError("Secret provider authentication failed.")
            except Exception:
                failure = SecretUnavailableError("Secret provider is unavailable.")
        raise failure


class AWSKMSKeyProvider(_AWSProvider):
    """Unwrap a symmetric KMS ciphertext with an explicit key and optional context."""

    def __init__(
        self, *, region_name: str | None = None, client: Any = None, timeout_seconds: float = 5.0,
    ) -> None:
        super().__init__("kms", region_name=region_name, client=client,
                         timeout_seconds=timeout_seconds)

    def unwrap(self, reference: KeyReference) -> SecretValue:
        with secret_operation():
            try:
                params: dict[str, Any] = {
                    "CiphertextBlob": reference.ciphertext, "KeyId": reference.name,
                    "EncryptionAlgorithm": "SYMMETRIC_DEFAULT",
                }
                if reference.context:
                    params["EncryptionContext"] = dict(reference.context)
                result = self._get_client().decrypt(**params)
                value = result["Plaintext"]
                if not isinstance(value, bytes):
                    raise ValueError("Invalid key value")
                return SecretValue(value)
            except SecretAccessError:
                failure: SecretError = SecretAccessError("Key provider access failed.")
            except (ValueError, KeyError, TypeError):
                failure = SecretAccessError("Key provider response is invalid.")
            except ClientError as error:
                failure = _aws_error(error)
            except (NoCredentialsError, PartialCredentialsError):
                failure = SecretAccessError("Key provider authentication failed.")
            except Exception:
                failure = SecretUnavailableError("Key provider is unavailable.")
        raise failure
