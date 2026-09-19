from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional

import yaml

from cognistore.secrets import SecretReference, SecretResolver
from cognistore.secrets.config import build_secret_resolver, parse_secret_reference

from .gcs_driver import GCSDriver
from .posix_driver import PosixDriver
from .rotating import RotatingStorageDriver
from .s3_driver import S3Driver
from .storage_driver import StorageDriver

_S3_FIELDS = frozenset(
    {
        "driver",
        "endpoint",
        "endpoint_url",
        "region",
        "region_name",
        "profile",
        "profile_name",
        "access_key",
        "access_key_env",
        "access_key_ref",
        "secret_key",
        "secret_key_env",
        "secret_key_ref",
        "session_token",
        "session_token_env",
        "session_token_ref",
        "addressing_style",
        "auto_create_bucket",
        "chunk_size",
        "list_page_size",
        "multipart_threshold",
        "server_side_encryption",
        "kms_key_id",
    }
)

_AZURE_BLOB_FIELDS = frozenset(
    {
        "driver",
        "account_url",
        "connection_string",
        "connection_string_env",
        "connection_string_ref",
        "credential",
        "credential_env",
        "credential_ref",
        "auto_create_container",
        "encryption_scope",
        "chunk_size",
        "list_page_size",
    }
)


_GCS_FIELDS = frozenset(
    {
        "driver",
        "project",
        "credentials_file",
        "credentials_file_env",
        "credentials_ref",
        "emulator_endpoint",
        "kms_key_name",
        "auto_create_bucket",
        "chunk_size",
        "list_page_size",
        "max_retries",
        "timeout",
    }
)


class _OwnedResolver:
    """Close a loader-created provider pool when its final driver closes."""

    def __init__(self, resolver: SecretResolver) -> None:
        self.resolver = resolver
        self.references = 0
        self.lock = Lock()

    def retain(self) -> None:
        with self.lock:
            self.references += 1

    def release(self) -> None:
        with self.lock:
            self.references -= 1
            if self.references == 0:
                self.resolver.close()


def _close_loaded_driver(driver: StorageDriver) -> None:
    close = getattr(driver, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass  # Cleanup must not replace the safe configuration failure.


def _aliased_value(
    info: Dict[str, Any],
    tier: str,
    canonical: str,
    alias: str,
) -> Any:
    present = [name for name in (canonical, alias) if name in info]
    if len(present) > 1:
        raise ValueError(
            f"S3 tier '{tier}' must use only one of '{canonical}' or '{alias}'"
        )
    return info.get(present[0]) if present else None


def _credential_value(
    info: Dict[str, Any], tier: str, field: str, provider: str = "S3"
) -> Optional[str]:
    env_field = f"{field}_env"
    alternatives = (field, env_field, f"{field}_ref")
    if sum(name in info for name in alternatives) > 1:
        raise ValueError(
            f"{provider} tier '{tier}' must use only one of "
            f"'{field}', '{env_field}', or '{field}_ref'"
        )
    if env_field not in info:
        return info.get(field)

    variable_name = info[env_field]
    if not isinstance(variable_name, str) or not variable_name.strip():
        raise ValueError(
            f"{provider} tier '{tier}' field '{env_field}' must name an environment variable"
        )
    value = os.environ.get(variable_name)
    if not value:
        # Report only the variable name.  Credential values must never appear
        # in configuration errors or logs.
        raise ValueError(
            f"{provider} tier '{tier}' requires environment variable '{variable_name}'"
        )
    return value


def _azure_credential_value(info: Dict[str, Any], tier: str, field: str) -> Optional[str]:
    """Resolve Azure secrets without reflecting malformed values in errors."""
    if f"{field}_ref" in info and (field in info or f"{field}_env" in info):
        raise ValueError(
            f"Azure Blob tier '{tier}' must use only one of "
            f"'{field}', '{field}_env', or '{field}_ref'"
        )
    for name in (field, f"{field}_env"):
        if name not in info:
            continue
        value = info[name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Azure Blob tier '{tier}' field '{name}' must be a non-empty string")
        if name.endswith("_env") and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError(
                f"Azure Blob tier '{tier}' field '{name}' must name an environment variable"
            )
    return _credential_value(info, tier, field, provider="Azure Blob")


def _secret_references(info: Dict[str, Any], *fields: str) -> dict[str, SecretReference]:
    return {
        field: parse_secret_reference(info[f"{field}_ref"])
        for field in fields
        if f"{field}_ref" in info
    }


def _gcs_with_secret_credentials(**options: Any) -> StorageDriver:
    import json

    from google.oauth2 import service_account

    material = json.loads(options.pop("credentials"))
    if not isinstance(material, dict) or material.get("type") != "service_account":
        raise ValueError("GCS credentials_ref must contain service-account JSON")
    options["credentials"] = service_account.Credentials.from_service_account_info(
        material, scopes=["https://www.googleapis.com/auth/devstorage.read_write"]
    )
    return GCSDriver(**options)


def load_drivers(
    config_path: str = "drivers.yaml", *, secret_resolver: SecretResolver | None = None,
) -> Dict[str, StorageDriver]:
    """Load driver instances per tier from a YAML config.

    Example YAML structure:
        tiers:
          hot:
            driver: posix
            path: /tmp/hot
            chunk_size: 8388608
          warm:
            driver: s3
            endpoint_url: http://127.0.0.1:9000
            access_key_env: MINIO_ACCESS_KEY
            secret_key_env: MINIO_SECRET_KEY
            chunk_size: 8388608
            multipart_threshold: 8388608
    """

    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f) or {}

    if not isinstance(cfg, dict):
        raise ValueError("Driver configuration must be a mapping")
    tiers = cfg.get("tiers", {})
    if not isinstance(tiers, dict):
        raise ValueError("Driver configuration 'tiers' must be a mapping")

    out: Dict[str, StorageDriver] = {}
    posix_roots: Dict[Path, str] = {}
    owned_resolver: _OwnedResolver | None = None

    def configured_driver(
        factory: Callable[..., StorageDriver], options: dict[str, Any],
        references: dict[str, SecretReference],
    ) -> StorageDriver:
        nonlocal secret_resolver, owned_resolver
        if not references:
            return factory(**options)
        if secret_resolver is None:
            secret_resolver = build_secret_resolver(cfg.get("secrets", {}))
            owned_resolver = _OwnedResolver(secret_resolver)
        driver = RotatingStorageDriver(
            factory, options, references, secret_resolver,
            on_close=owned_resolver.release if owned_resolver is not None else None,
        )
        if owned_resolver is not None:
            owned_resolver.retain()
        return driver

    try:
        for tier, info in tiers.items():
            if not isinstance(tier, str) or not tier.strip():
                raise ValueError("Tier names must be non-empty strings")
            if not isinstance(info, dict):
                raise ValueError(f"Configuration for tier '{tier}' must be a mapping")
            driver_type = info.get("driver")
            if driver_type == "posix":
                path = info.get("path")
                if not isinstance(path, str) or not path.strip():
                    raise ValueError(f"POSIX tier '{tier}' requires a non-empty path")
                if "chunk_size" in info:
                    driver = PosixDriver(
                        base_path=path,
                        chunk_size=info["chunk_size"],
                    )
                else:
                    driver = PosixDriver(base_path=path)
                existing_tier = posix_roots.get(driver.base)
                if existing_tier is not None:
                    raise ValueError(
                        f"POSIX tiers '{existing_tier}' and '{tier}' resolve to the same root: "
                        f"{driver.base}"
                    )
                posix_roots[driver.base] = tier
                out[tier] = driver
            elif driver_type == "s3":
                unknown_fields = sorted(set(info) - _S3_FIELDS)
                if unknown_fields:
                    raise ValueError(
                        f"S3 tier '{tier}' has unsupported configuration fields: "
                        + ", ".join(unknown_fields)
                    )

                endpoint_url = _aliased_value(info, tier, "endpoint_url", "endpoint")
                region_name = _aliased_value(info, tier, "region_name", "region")
                profile_name = _aliased_value(info, tier, "profile_name", "profile")
                driver_options: Dict[str, Any] = {
                    "endpoint_url": endpoint_url,
                    "region_name": region_name,
                    "access_key": _credential_value(info, tier, "access_key"),
                    "secret_key": _credential_value(info, tier, "secret_key"),
                    "session_token": _credential_value(info, tier, "session_token"),
                    "profile_name": profile_name,
                    "addressing_style": info.get("addressing_style", "auto"),
                    "auto_create_bucket": info.get("auto_create_bucket", False),
                    "list_page_size": info.get("list_page_size"),
                }
                for transfer_field in (
                    "chunk_size", "multipart_threshold", "server_side_encryption", "kms_key_id"
                ):
                    if transfer_field in info:
                        driver_options[transfer_field] = info[transfer_field]
                out[tier] = configured_driver(
                    S3Driver, driver_options,
                    _secret_references(info, "access_key", "secret_key", "session_token"),
                )
            elif driver_type == "azure_blob":
                try:
                    from .azure_blob_driver import AzureBlobDriver
                except ImportError:
                    raise ImportError(
                        "Azure Blob storage requires the optional 'azure' dependencies; "
                        "install cognistore[azure]"
                    ) from None

                azure_unknown_fields = set(info) - _AZURE_BLOB_FIELDS
                if azure_unknown_fields:
                    raise ValueError(f"Azure Blob tier '{tier}' has unsupported configuration fields")

                azure_options: Dict[str, Any] = {
                    "account_url": info.get("account_url"),
                    "connection_string": _azure_credential_value(info, tier, "connection_string"),
                    "credential": _azure_credential_value(info, tier, "credential"),
                    "auto_create_container": info.get("auto_create_container", False),
                    "list_page_size": info.get("list_page_size"),
                }
                if "chunk_size" in info:
                    azure_options["chunk_size"] = info["chunk_size"]
                if "encryption_scope" in info:
                    azure_options["encryption_scope"] = info["encryption_scope"]
                out[tier] = configured_driver(
                    AzureBlobDriver, azure_options,
                    _secret_references(info, "connection_string", "credential"),
                )
            elif driver_type == "gcs":
                unknown_fields = sorted(str(field) for field in set(info) - _GCS_FIELDS)
                if unknown_fields:
                    raise ValueError(
                        f"GCS tier '{tier}' has unsupported configuration fields: "
                        + ", ".join(unknown_fields)
                    )
                if info.get("emulator_endpoint") is not None and (
                    "credentials_file" in info or "credentials_file_env" in info
                    or "credentials_ref" in info
                ):
                    raise ValueError(
                        f"GCS tier '{tier}' cannot combine emulator_endpoint with credentials"
                    )
                if "credentials_ref" in info and (
                    "credentials_file" in info or "credentials_file_env" in info
                ):
                    raise ValueError(
                        f"GCS tier '{tier}' must use only one of credentials_file, "
                        "credentials_file_env, or credentials_ref"
                    )
                gcs_options: Dict[str, Any] = {
                    "credentials_file": _credential_value(
                        info, tier, "credentials_file", provider="GCS"
                    ),
                }
                for field in (
                    "project",
                    "emulator_endpoint",
                    "auto_create_bucket",
                    "chunk_size",
                    "list_page_size",
                    "max_retries",
                    "timeout",
                    "kms_key_name",
                ):
                    if field in info:
                        gcs_options[field] = info[field]
                out[tier] = configured_driver(
                    _gcs_with_secret_credentials if "credentials_ref" in info else GCSDriver,
                    gcs_options, _secret_references(info, "credentials"),
                )
            else:
                raise ValueError(f"Unknown or unsupported driver type: {driver_type}")

    except BaseException:
        for loaded in out.values():
            _close_loaded_driver(loaded)
        if owned_resolver is not None:
            owned_resolver.resolver.close()
        raise

    return out
