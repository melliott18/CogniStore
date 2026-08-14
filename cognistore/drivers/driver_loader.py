from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

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
        "list_page_size",
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
    info: Dict[str, Any], tier: str, field: str
) -> Optional[str]:
    env_field = f"{field}_env"
    if field in info and env_field in info:
        raise ValueError(
            f"S3 tier '{tier}' must use only one of '{field}' or '{env_field}'"
        )
    if env_field not in info:
        return info.get(field)

    variable_name = info[env_field]
    if not isinstance(variable_name, str) or not variable_name.strip():
        raise ValueError(
            f"S3 tier '{tier}' field '{env_field}' must name an environment variable"
        )
    value = os.environ.get(variable_name)
    if not value:
        # Report only the variable name.  Credential values must never appear
        # in configuration errors or logs.
        raise ValueError(
            f"S3 tier '{tier}' requires environment variable '{variable_name}'"
        )
    return value


def load_drivers(config_path: str = "drivers.yaml") -> Dict[str, StorageDriver]:
    """Load driver instances per tier from a YAML config.

    Example YAML structure:
        tiers:
          hot:
            driver: posix
            path: /tmp/hot
          warm:
            driver: s3
            endpoint_url: http://127.0.0.1:9000
            access_key_env: MINIO_ACCESS_KEY
            secret_key_env: MINIO_SECRET_KEY
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
            out[tier] = S3Driver(
                endpoint_url=endpoint_url,
                region_name=region_name,
                access_key=_credential_value(info, tier, "access_key"),
                secret_key=_credential_value(info, tier, "secret_key"),
                session_token=_credential_value(info, tier, "session_token"),
                profile_name=profile_name,
                addressing_style=info.get("addressing_style", "auto"),
                auto_create_bucket=info.get("auto_create_bucket", False),
                list_page_size=info.get("list_page_size"),
            )
        else:
            raise ValueError(f"Unknown or unsupported driver type: {driver_type}")

    return out
