from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Dict


def _run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return out.stdout.strip()
    except Exception:
        return ""


def _powershell_json(ps_command: str) -> Any:
    # Execute a PowerShell command that outputs JSON
    return _run(["powershell", "-NoProfile", "-Command", ps_command])


def inspect_device_windows(base_dev: str) -> Dict[str, Any]:
    """Return a dict with model, transport, rotational/solid_state, size_bytes, media_type for Windows.

    Note: `base_dev` may be like \\?\Volume{GUID} or a DOS device path; mapping to PhysicalDisk
    is best-effort here. We fallback to transport/media type heuristics.
    """
    # Try PowerShell Get-PhysicalDisk to list disks and pick heuristics
    ps = (
        "Get-PhysicalDisk | Select-Object FriendlyName,MediaType,BusType,Size | ConvertTo-Json -Depth 2"
    )
    out = _powershell_json(ps)
    model = None
    transport = None
    size_bytes = None
    media_type = "unknown"
    solid_state = None
    rotational = None

    try:
        data = json.loads(out) if out else []
        disks = data if isinstance(data, list) else [data]
        # Take the fastest-looking disk as a proxy; mapping volumes to disks is non-trivial here.
        # Prefer NVMe > SSD > HDD
        priority = {"nvme": 3, "ssd": 2, "hdd": 1, "unknown": 0}
        best = None
        best_score = -1
        for d in disks:
            bus = (d.get("BusType") or "").lower()
            media = (d.get("MediaType") or "").lower()
            if "nvme" in bus:
                mt = "nvme"
            elif media == "ssd" or "solid" in media:
                mt = "ssd"
            elif media == "hdd" or "rotational" in media or media == "" and "scsi" in bus:
                mt = "hdd"
            else:
                mt = "unknown"
            score = priority.get(mt, 0)
            if score > best_score:
                best_score = score
                best = d.copy()
                best["_media_type"] = mt
        if best:
            model = best.get("FriendlyName")
            transport = (best.get("BusType") or "").lower() or None
            size_bytes = int(best.get("Size")) if best.get("Size") is not None else None
            media_type = best.get("_media_type", "unknown")
            solid_state = True if media_type in ("nvme", "ssd") else False if media_type == "hdd" else None
            rotational = False if media_type in ("nvme", "ssd") else True if media_type == "hdd" else None
    except Exception:
        pass

    if model is None and transport is None:
        # Fallback to WMIC (deprecated but sometimes available)
        raw = _run(["wmic", "diskdrive", "get", "Model,InterfaceType,Size", "/format:csv"]) or ""
        # Very rough parse
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        # Skip header; pick the first device
        if len(lines) >= 2:
            parts = [p.strip() for p in lines[1].split(",")]
            # CSV columns: Node, InterfaceType, Model, Size
            if len(parts) >= 4:
                transport = (parts[1] or "").lower() or None
                model = parts[2] or None
                try:
                    size_bytes = int(parts[3])
                except Exception:
                    size_bytes = None
                if transport and "nvme" in transport:
                    media_type = "nvme"
                elif transport and transport in ("sata", "sas", "usb"):
                    media_type = "ssd"  # assume SSD if non-rotational not known
                else:
                    media_type = "unknown"
                solid_state = True if media_type in ("nvme", "ssd") else None
                rotational = False if solid_state else None

    return {
        "model": model,
        "transport": transport,
        "rotational": rotational,
        "solid_state": solid_state,
        "size_bytes": size_bytes,
        "media_type": media_type,
    }
