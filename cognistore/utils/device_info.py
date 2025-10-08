from __future__ import annotations

import json
import platform
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class DeviceInfo:
    tier: str
    base_path: str
    device: str | None
    base_device: str | None
    model: str | None
    transport: str | None
    rotational: Optional[bool]
    solid_state: Optional[bool]
    size_bytes: Optional[int]
    media_type: str  # 'nvme' | 'ssd' | 'hdd' | 'unknown'

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return out.stdout.strip()
    except Exception:
        return ""


def _get_device_for_path(p: Path) -> str | None:
    # Portable: use POSIX df -P to resolve the backing device for this mount
    out = _run(["df", "-P", str(p)])
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if len(lines) >= 2:
        # header + row
        row = lines[1].split()
        if row:
            return row[0]
    return None


def _base_device(dev: str) -> str:
    # macOS: /dev/disk3s1 -> /dev/disk3
    m = re.match(r"^(/dev/disk\d+)s\d+", dev)
    if m:
        return m.group(1)
    # Linux: /dev/nvme0n1p1 -> /dev/nvme0n1; /dev/sda1 -> /dev/sda
    m = re.match(r"^(/dev/nvme\d+n\d+)p\d+$", dev)
    if m:
        return m.group(1)
    m = re.match(r"^(/dev/[a-zA-Z]+)\d+$", dev)
    if m:
        return m.group(1)
    return dev


def discover_device_for_tier(tier: str, base_path: str | Path) -> DeviceInfo:
    """Orchestrate device discovery and OS-specific inspection.

    Keeps a stable public API while delegating OS-specific work to dedicated modules.
    """
    p = Path(base_path)
    dev = _get_device_for_path(p)
    base_dev = _base_device(dev) if dev else None
    sysname = platform.system().lower()

    model: str | None = None
    transport: str | None = None
    rotational: Optional[bool] = None
    solid_state: Optional[bool] = None
    size_bytes: Optional[int] = None
    media_type: str = "unknown"

    if base_dev:
        try:
            if sysname == "darwin":
                from .device_info_macos import inspect_device_macos

                info = inspect_device_macos(base_dev)
            elif sysname == "linux":
                from .device_info_linux import inspect_device_linux

                info = inspect_device_linux(base_dev)
            elif sysname == "windows":
                from .device_info_windows import inspect_device_windows

                info = inspect_device_windows(base_dev)
            else:
                info = {}
        except Exception:
            info = {}

        model = info.get("model")
        transport = info.get("transport")
        rotational = info.get("rotational")
        solid_state = info.get("solid_state")
        size_bytes = info.get("size_bytes")
        media_type = info.get("media_type", "unknown")

    return DeviceInfo(
        tier=tier,
        base_path=str(p.resolve()),
        device=dev,
        base_device=base_dev,
        model=model,
        transport=transport,
        rotational=rotational,
        solid_state=solid_state,
        size_bytes=size_bytes,
        media_type=media_type,
    )


def save_hardware_json(info: Dict[str, DeviceInfo], out_path: str | Path) -> None:
    data = {k: v.to_dict() for k, v in info.items()}
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_hardware_json(in_path: str | Path) -> Dict[str, DeviceInfo]:
    with open(in_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    out: Dict[str, DeviceInfo] = {}
    for tier, d in (raw or {}).items():
        out[tier] = DeviceInfo(**d)
    return out
