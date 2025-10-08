from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass
class DriveProfile:
    name: str
    first_byte_latency_ms: float
    seq_read_MBps: float
    seq_write_MBps: float
    random_read_IOPS: float


DEFAULT_PROFILES: Dict[str, DriveProfile] = {
    # Conservative, order-of-magnitude defaults; tune as needed
    "nvme": DriveProfile("nvme", first_byte_latency_ms=0.10, seq_read_MBps=2000.0, seq_write_MBps=1500.0, random_read_IOPS=200000.0),
    "ssd": DriveProfile("ssd", first_byte_latency_ms=0.20, seq_read_MBps=500.0, seq_write_MBps=450.0, random_read_IOPS=50000.0),
    "hdd": DriveProfile("hdd", first_byte_latency_ms=6.00, seq_read_MBps=180.0, seq_write_MBps=160.0, random_read_IOPS=150.0),
    "unknown": DriveProfile("unknown", first_byte_latency_ms=2.00, seq_read_MBps=250.0, seq_write_MBps=200.0, random_read_IOPS=1000.0),
}
