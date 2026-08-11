from __future__ import annotations

from pathlib import Path
from typing import Dict

import yaml

from .posix_driver import PosixDriver
from .storage_driver import StorageDriver


def load_drivers(config_path: str = "drivers.yaml") -> Dict[str, StorageDriver]:
    """Load driver instances per tier from a YAML config.

    Example YAML structure:
        tiers:
          hot:
            driver: posix
            path: /tmp/hot
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
        else:
            raise ValueError(f"Unknown or unsupported driver type: {driver_type}")

    return out
