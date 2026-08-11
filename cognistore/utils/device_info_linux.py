from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Dict, Optional


def _run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return out.stdout.strip()
    except Exception:
        return ""


def _find_lsblk_node(root: dict, name: str) -> Optional[dict]:
    def find(nodes: list[dict]) -> Optional[dict]:
        for n in nodes:
            if n.get("name") == name:
                return n
            ch = n.get("children") or []
            r = find(ch)
            if r:
                return r
        return None

    return find(root.get("blockdevices", []) or [])


def inspect_device_linux(base_dev: str) -> Dict[str, Any]:
    name = os.path.basename(base_dev)
    out = _run(["lsblk", "-J", "-o", "NAME,TYPE,ROTA,MODEL,TRAN,SIZE"]) or "{}"
    try:
        data = json.loads(out)
    except Exception:
        data = {}
    node = _find_lsblk_node(data, name) or {}

    rota = node.get("rota")
    rotational = None
    if rota is not None:
        if isinstance(rota, (int, float)):
            rotational = bool(int(rota))
        elif isinstance(rota, str) and rota.isdigit():
            rotational = bool(int(rota))
        else:
            rotational = bool(rota)

    trans = (node.get("tran") or "").lower() or None
    model = node.get("model")
    # SIZE is a human string like '477G' in lsblk; leave None for now.
    size_bytes = None

    if trans == "nvme":
        media_type = "nvme"
        solid_state = True
    elif rotational is True:
        media_type = "hdd"
        solid_state = False
    elif rotational is False:
        media_type = "ssd"
        solid_state = True
    else:
        media_type = "unknown"
        solid_state = None

    return {
        "model": model,
        "transport": trans,
        "rotational": rotational,
        "solid_state": solid_state,
        "size_bytes": size_bytes,
        "media_type": media_type,
    }
