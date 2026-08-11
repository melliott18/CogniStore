from __future__ import annotations

import os
import time
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Any


@dataclass
class TierMetrics:
    path: str
    filesystem_block_size: int
    total_bytes: int
    free_bytes: int
    seq_write_MBps: float
    seq_read_MBps: float
    random_read_IOPS: float
    first_byte_latency_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _statvfs(path: Path) -> tuple[int, int, int]:
    st = os.statvfs(str(path))
    # f_frsize may be 0 on some platforms; fall back to f_bsize
    blksize = st.f_frsize or st.f_bsize or 4096
    total = st.f_blocks * blksize
    free = st.f_bavail * blksize
    return int(blksize), int(total), int(free)


def profile_path(
    path: str | Path,
    *,
    file_size_mb: int = 64,
    block_size: int = 4 * 1024 * 1024,
    random_ops: int = 1024,
    random_block_size: int = 4096,
) -> TierMetrics:
    """Profile a filesystem path with light-touch IO.

    Notes:
      - Results are influenced by OS page cache; we report cached performance.
      - Uses a temporary file under the provided path and cleans it up.
      - Keep sizes modest to avoid wearing SSDs or filling disks.
    """
    base = Path(path)
    base.mkdir(parents=True, exist_ok=True)
    blksize, total, free = _statvfs(base)

    tmp = base / ".cognistore_profile.tmp"
    total_bytes_to_write = file_size_mb * 1024 * 1024

    # Prepare a deterministic buffer to avoid CPU overhead of os.urandom
    buf = (b"\0" * block_size)
    written = 0
    t0 = time.perf_counter()
    with open(tmp, "wb", buffering=0) as f:
        while written < total_bytes_to_write:
            to_write = min(block_size, total_bytes_to_write - written)
            f.write(buf[:to_write])
            written += to_write
        f.flush()
        os.fsync(f.fileno())
    t1 = time.perf_counter()
    seq_write_MBps = (total_bytes_to_write / (1024 * 1024)) / max(t1 - t0, 1e-9)

    # Estimated first-byte latency: open + read 1 byte
    t2 = time.perf_counter()
    with open(tmp, "rb", buffering=0) as f:
        _ = f.read(1)
    t3 = time.perf_counter()
    first_byte_latency_ms = (t3 - t2) * 1000.0

    # Sequential read throughput
    read = 0
    t4 = time.perf_counter()
    with open(tmp, "rb", buffering=0) as f:
        while True:
            data = f.read(block_size)
            if not data:
                break
            read += len(data)
    t5 = time.perf_counter()
    seq_read_MBps = (read / (1024 * 1024)) / max(t5 - t4, 1e-9)

    # Random small-block reads (cached); use many offsets
    file_size = tmp.stat().st_size
    if file_size < random_block_size:
        random_read_IOPS = 0.0
    else:
        offsets = [
            random.randint(0, file_size - random_block_size)
            for _ in range(random_ops)
        ]
        t6 = time.perf_counter()
        with open(tmp, "rb", buffering=0) as f:
            for off in offsets:
                f.seek(off)
                _ = f.read(random_block_size)
        t7 = time.perf_counter()
        elapsed = max(t7 - t6, 1e-9)
        random_read_IOPS = random_ops / elapsed

    try:
        tmp.unlink()
    except Exception:
        pass

    return TierMetrics(
        path=str(base.resolve()),
        filesystem_block_size=blksize,
        total_bytes=total,
        free_bytes=free,
        seq_write_MBps=seq_write_MBps,
        seq_read_MBps=seq_read_MBps,
        random_read_IOPS=random_read_IOPS,
        first_byte_latency_ms=first_byte_latency_ms,
    )


def save_metrics_json(metrics: Dict[str, TierMetrics], out_path: str | Path) -> None:
    data: Dict[str, Any] = {tier: m.to_dict() for tier, m in metrics.items()}
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_metrics_json(in_path: str | Path) -> Dict[str, TierMetrics]:
    with open(in_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    out: Dict[str, TierMetrics] = {}
    for tier, d in (raw or {}).items():
        out[tier] = TierMetrics(**d)
    return out
