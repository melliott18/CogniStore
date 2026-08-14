from __future__ import annotations

import subprocess
from typing import Any, Dict


def _run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return out.stdout.strip()
    except Exception:
        return ""


def inspect_device_macos(base_dev: str) -> Dict[str, Any]:
    """Return a dict with model, transport, rotational/solid_state, size_bytes, media_type for macOS."""
    info_text = _run(["diskutil", "info", base_dev])
    info: dict[str, str] = {}
    for ln in info_text.splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            info[k.strip()] = v.strip()

    model = info.get("Device / Media Name") or info.get("Device Identifier")
    proto = (info.get("Protocol") or info.get("Device Location") or "").lower()
    medium = (info.get("Medium Type") or "").lower()
    solid_state_field = info.get("Solid State")
    ssd = None if solid_state_field is None else (solid_state_field.lower().startswith("y"))
    rotational = None if medium == "" else ("rotational" in medium)

    transport: str | None
    if "nvme" in proto or "pci" in proto:
        media_type = "nvme"
        transport = "nvme"
    elif rotational is True:
        media_type = "hdd"
        transport = "sata"
    elif ssd is True or "solid state" in medium:
        media_type = "ssd"
        transport = "sata"
    else:
        media_type = "unknown"
        transport = proto or None

    # diskutil text output doesn't easily expose size in bytes here; omit
    size_bytes = None

    return {
        "model": model,
        "transport": transport,
        "rotational": rotational,
        "solid_state": ssd,
        "size_bytes": size_bytes,
        "media_type": media_type,
    }
