from __future__ import annotations

from typing import Dict

import yaml

from .posix_driver import PosixDriver


def load_drivers(config_path: str = "drivers.yaml") -> Dict[str, object]:
    """Load driver instances per tier from a YAML config.

    Example YAML structure:
        tiers:
          hot:
            driver: posix
            path: /tmp/hot
    """

    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f) or {}

    tiers = cfg.get("tiers", {})
    out: Dict[str, object] = {}

    for tier, info in tiers.items():
        driver_type = info.get("driver")
        if driver_type == "posix":
            out[tier] = PosixDriver(base_path=info["path"])  # type: ignore[index]
        else:
            raise ValueError(f"Unknown or unsupported driver type: {driver_type}")

    return out
