from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .gcs_driver import GCSDriver
from .posix_driver import PosixDriver
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
        "secret_key",
        "secret_key_env",
        "session_token",
        "session_token_env",
        "addressing_style",
        "auto_create_bucket",
        "chunk_size",
        "list_page_size",
        "multipart_threshold",
    }
)

_AZURE_BLOB_FIELDS = frozenset(
    {
        "driver",
        "account_url",
        "connection_string",
        "connection_string_env",
        "credential",
        "credential_env",
        "auto_create_container",
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
        "emulator_endpoint",
        "auto_create_bucket",
        "chunk_size",
        "list_page_size",
        "max_retries",
        "timeout",
    }
)


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
    if field in info and env_field in info:
        raise ValueError(
            f"{provider} tier '{tier}' must use only one of '{field}' or '{env_field}'"
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


def load_drivers(config_path: str = "drivers.yaml") -> Dict[str, StorageDriver]:
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
            for transfer_field in ("chunk_size", "multipart_threshold"):
                if transfer_field in info:
                    driver_options[transfer_field] = info[transfer_field]
            out[tier] = S3Driver(**driver_options)
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
            out[tier] = AzureBlobDriver(**azure_options)
        elif driver_type == "gcs":
            unknown_fields = sorted(str(field) for field in set(info) - _GCS_FIELDS)
            if unknown_fields:
                raise ValueError(
                    f"GCS tier '{tier}' has unsupported configuration fields: "
                    + ", ".join(unknown_fields)
                )
            if info.get("emulator_endpoint") is not None and (
                "credentials_file" in info or "credentials_file_env" in info
            ):
                raise ValueError(
                    f"GCS tier '{tier}' cannot combine emulator_endpoint with credentials"
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
            ):
                if field in info:
                    gcs_options[field] = info[field]
            out[tier] = GCSDriver(**gcs_options)
        else:
            raise ValueError(f"Unknown or unsupported driver type: {driver_type}")

    return out
