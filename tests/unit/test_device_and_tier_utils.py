from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from cognistore.utils import device_info, tier_profiler
from cognistore.utils import device_info_linux as linux
from cognistore.utils import device_info_macos as macos
from cognistore.utils import device_info_windows as windows


@pytest.mark.parametrize(
    ("module", "command"),
    [
        (device_info, ["df", "-P", "/data"]),
        (linux, ["lsblk", "-J"]),
        (macos, ["diskutil", "info"]),
        (windows, ["powershell", "-Command", "Get-Disk"]),
    ],
    ids=["device-info", "linux", "macos", "windows"],
)
def test_run_returns_stripped_stdout(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    command: list[str],
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((cmd, kwargs))
        return SimpleNamespace(stdout="  result\n")

    monkeypatch.setattr(module.subprocess, "run", run)

    assert module._run(command) == "result"
    assert calls == [
        (
            command,
            {
                "check": False,
                "stdout": module.subprocess.PIPE,
                "stderr": module.subprocess.PIPE,
                "text": True,
            },
        )
    ]


@pytest.mark.parametrize(
    "module",
    [device_info, linux, macos, windows],
    ids=["device-info", "linux", "macos", "windows"],
)
def test_run_returns_empty_string_when_process_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("command is unavailable")

    monkeypatch.setattr(module.subprocess, "run", fail)

    assert module._run(["missing-command"]) == ""


@pytest.mark.parametrize(
    ("device", "expected"),
    [
        ("/dev/disk3s1", "/dev/disk3"),
        ("/dev/nvme0n1p12", "/dev/nvme0n1"),
        ("/dev/sda10", "/dev/sda"),
        ("/dev/mapper/data", "/dev/mapper/data"),
    ],
)
def test_base_device_removes_known_partition_suffixes(device: str, expected: str) -> None:
    assert device_info._base_device(device) == expected


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "/dev/nvme0n1p2 100 20 80 20% /data\n",
            "/dev/nvme0n1p2",
        ),
        ("Filesystem 1024-blocks Used Available Capacity Mounted on\n", None),
        ("", None),
    ],
)
def test_get_device_for_path_parses_df_output(
    monkeypatch: pytest.MonkeyPatch,
    output: str,
    expected: str | None,
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str]) -> str:
        commands.append(command)
        return output

    monkeypatch.setattr(device_info, "_run", run)

    assert device_info._get_device_for_path(Path("/data")) == expected
    assert commands == [["df", "-P", "/data"]]


@pytest.mark.parametrize(
    ("system_name", "module", "inspector_name"),
    [
        ("Darwin", macos, "inspect_device_macos"),
        ("Linux", linux, "inspect_device_linux"),
        ("Windows", windows, "inspect_device_windows"),
    ],
)
def test_discover_device_dispatches_to_platform_inspector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system_name: str,
    module: ModuleType,
    inspector_name: str,
) -> None:
    inspected: list[str] = []

    def inspect(base_device: str) -> dict[str, Any]:
        inspected.append(base_device)
        return {
            "model": "Test disk",
            "transport": "nvme",
            "rotational": False,
            "solid_state": True,
            "size_bytes": 4096,
            "media_type": "nvme",
        }

    monkeypatch.setattr(device_info, "_get_device_for_path", lambda _path: "/dev/disk4s2")
    monkeypatch.setattr(device_info.platform, "system", lambda: system_name)
    monkeypatch.setattr(module, inspector_name, inspect)

    result = device_info.discover_device_for_tier("hot", tmp_path)

    assert inspected == ["/dev/disk4"]
    assert result == device_info.DeviceInfo(
        tier="hot",
        base_path=str(tmp_path.resolve()),
        device="/dev/disk4s2",
        base_device="/dev/disk4",
        model="Test disk",
        transport="nvme",
        rotational=False,
        solid_state=True,
        size_bytes=4096,
        media_type="nvme",
    )


def test_discover_device_handles_unknown_platform_and_missing_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(device_info, "_get_device_for_path", lambda _path: None)
    monkeypatch.setattr(device_info.platform, "system", lambda: "Plan 9")

    result = device_info.discover_device_for_tier("archive", tmp_path)

    assert result == device_info.DeviceInfo(
        tier="archive",
        base_path=str(tmp_path.resolve()),
        device=None,
        base_device=None,
        model=None,
        transport=None,
        rotational=None,
        solid_state=None,
        size_bytes=None,
        media_type="unknown",
    )


def test_discover_device_uses_defaults_on_unknown_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(device_info, "_get_device_for_path", lambda _path: "/dev/custom0")
    monkeypatch.setattr(device_info.platform, "system", lambda: "FreeBSD")

    result = device_info.discover_device_for_tier("warm", tmp_path)

    assert result.base_device == "/dev/custom"
    assert result.model is None
    assert result.media_type == "unknown"


def test_discover_device_tolerates_inspector_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_base_device: str) -> dict[str, Any]:
        raise RuntimeError("inspection failed")

    monkeypatch.setattr(device_info, "_get_device_for_path", lambda _path: "/dev/sda1")
    monkeypatch.setattr(device_info.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linux, "inspect_device_linux", fail)

    result = device_info.discover_device_for_tier("warm", tmp_path)

    assert result.base_device == "/dev/sda"
    assert result.to_dict()["media_type"] == "unknown"


def test_discover_device_defaults_omitted_inspector_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(device_info, "_get_device_for_path", lambda _path: "/dev/sda")
    monkeypatch.setattr(device_info.platform, "system", lambda: "Linux")
    monkeypatch.setattr(linux, "inspect_device_linux", lambda _device: {"model": "Partial"})

    result = device_info.discover_device_for_tier("warm", tmp_path)

    assert result.model == "Partial"
    assert result.transport is None
    assert result.media_type == "unknown"


def test_hardware_json_round_trip(tmp_path: Path) -> None:
    source = {
        "hot": device_info.DeviceInfo(
            tier="hot",
            base_path="/fast",
            device="/dev/nvme0n1p1",
            base_device="/dev/nvme0n1",
            model="Fast disk",
            transport="nvme",
            rotational=False,
            solid_state=True,
            size_bytes=10_000,
            media_type="nvme",
        )
    }
    output = tmp_path / "nested" / "hardware.json"

    device_info.save_hardware_json(source, output)

    assert json.loads(output.read_text(encoding="utf-8")) == {
        "hot": source["hot"].to_dict()
    }
    assert device_info.load_hardware_json(output) == source


def test_load_hardware_json_accepts_null_document(tmp_path: Path) -> None:
    source = tmp_path / "hardware.json"
    source.write_text("null", encoding="utf-8")

    assert device_info.load_hardware_json(source) == {}


def test_find_lsblk_node_searches_nested_nodes_and_handles_missing_lists() -> None:
    child = {"name": "sda", "children": None}
    root = {
        "blockdevices": [
            {"name": "controller", "children": [{"name": "other"}, child]},
        ]
    }

    assert linux._find_lsblk_node(root, "controller") == root["blockdevices"][0]
    assert linux._find_lsblk_node(root, "sda") == child
    assert linux._find_lsblk_node(root, "absent") is None
    assert linux._find_lsblk_node({}, "absent") is None


@pytest.mark.parametrize(
    ("tree", "device", "expected"),
    [
        (
            {
                "blockdevices": [
                    {
                        "name": "nvme0n1",
                        "rota": 1,
                        "model": "NVMe disk",
                        "tran": "NVME",
                    }
                ]
            },
            "/dev/nvme0n1",
            (True, True, "nvme", "nvme"),
        ),
        (
            {
                "blockdevices": [
                    {
                        "name": "host",
                        "children": [
                            {"name": "sda", "rota": "1", "model": "Disk", "tran": "SATA"}
                        ],
                    }
                ]
            },
            "/dev/sda",
            (True, False, "hdd", "sata"),
        ),
        (
            {"blockdevices": [{"name": "sdb", "rota": 0, "tran": "SATA"}]},
            "/dev/sdb",
            (False, True, "ssd", "sata"),
        ),
        (
            {"blockdevices": [{"name": "sdc", "rota": "yes", "tran": None}]},
            "/dev/sdc",
            (True, False, "hdd", None),
        ),
        (
            {"blockdevices": [{"name": "sdd", "rota": "", "tran": "USB"}]},
            "/dev/sdd",
            (False, True, "ssd", "usb"),
        ),
        (
            {"blockdevices": [{"name": "sde", "rota": None}]},
            "/dev/sde",
            (None, None, "unknown", None),
        ),
    ],
    ids=["nvme", "nested-hdd", "ssd", "truthy-rota", "falsey-rota", "unknown"],
)
def test_inspect_device_linux_classifies_lsblk_nodes(
    monkeypatch: pytest.MonkeyPatch,
    tree: dict[str, Any],
    device: str,
    expected: tuple[bool | None, bool | None, str, str | None],
) -> None:
    monkeypatch.setattr(linux, "_run", lambda _command: json.dumps(tree))

    result = linux.inspect_device_linux(device)

    rotational, solid_state, media_type, transport = expected
    assert result["rotational"] is rotational
    assert result["solid_state"] is solid_state
    assert result["media_type"] == media_type
    assert result["transport"] == transport
    assert result["size_bytes"] is None


@pytest.mark.parametrize("output", ["", "not-json"])
def test_inspect_device_linux_tolerates_empty_or_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    monkeypatch.setattr(linux, "_run", lambda _command: output)

    assert linux.inspect_device_linux("/dev/missing") == {
        "model": None,
        "transport": None,
        "rotational": None,
        "solid_state": None,
        "size_bytes": None,
        "media_type": "unknown",
    }


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "ignored line\n"
            "Device Identifier: disk0\n"
            "Device Location: PCI-Express\n"
            "Solid State: Yes\n",
            ("disk0", "nvme", None, True, "nvme"),
        ),
        (
            "Device / Media Name: Vendor: Model\n"
            "Protocol: SATA\n"
            "Medium Type: Rotational\n"
            "Solid State: No\n",
            ("Vendor: Model", "sata", True, False, "hdd"),
        ),
        (
            "Device / Media Name: SSD\n"
            "Protocol: SATA\n"
            "Medium Type: Solid State\n"
            "Solid State: Yes\n",
            ("SSD", "sata", False, True, "ssd"),
        ),
        (
            "Device / Media Name: Inferred SSD\n"
            "Medium Type: Solid State\n"
            "Solid State: No\n",
            ("Inferred SSD", "sata", False, False, "ssd"),
        ),
        (
            "Device / Media Name: External\nProtocol: USB\nSolid State: No\n",
            ("External", "usb", None, False, "unknown"),
        ),
        ("", (None, None, None, None, "unknown")),
    ],
    ids=["nvme", "hdd", "ssd-field", "ssd-medium", "unknown", "empty"],
)
def test_inspect_device_macos_parses_diskutil_output(
    monkeypatch: pytest.MonkeyPatch,
    output: str,
    expected: tuple[str | None, str | None, bool | None, bool | None, str],
) -> None:
    monkeypatch.setattr(macos, "_run", lambda _command: output)

    result = macos.inspect_device_macos("/dev/disk0")

    model, transport, rotational, solid_state, media_type = expected
    assert result == {
        "model": model,
        "transport": transport,
        "rotational": rotational,
        "solid_state": solid_state,
        "size_bytes": None,
        "media_type": media_type,
    }


def test_powershell_json_delegates_to_run(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def run(command: list[str]) -> str:
        commands.append(command)
        return '[{"FriendlyName": "Disk"}]'

    monkeypatch.setattr(windows, "_run", run)

    assert windows._powershell_json("Get-PhysicalDisk") == '[{"FriendlyName": "Disk"}]'
    assert commands == [["powershell", "-NoProfile", "-Command", "Get-PhysicalDisk"]]


def test_inspect_device_windows_prefers_fastest_physical_disk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disks = [
        {
            "FriendlyName": "Mystery",
            "MediaType": "Unspecified",
            "BusType": "USB",
            "Size": 1,
        },
        {"FriendlyName": "Hard disk", "MediaType": "HDD", "BusType": "SAS", "Size": 2},
        {
            "FriendlyName": "Rotational disk",
            "MediaType": "Rotational Disk",
            "BusType": "SATA",
            "Size": 3,
        },
        {
            "FriendlyName": "SCSI disk",
            "MediaType": "",
            "BusType": "SCSI",
            "Size": 4,
        },
        {
            "FriendlyName": "Solid disk",
            "MediaType": "Solid State",
            "BusType": "SATA",
            "Size": 5,
        },
        {
            "FriendlyName": "Winner",
            "MediaType": "Unspecified",
            "BusType": "NVMe",
            "Size": "6000",
        },
        {
            "FriendlyName": "Same score later",
            "MediaType": "SSD",
            "BusType": "NVMe",
            "Size": 7,
        },
    ]
    monkeypatch.setattr(windows, "_powershell_json", lambda _command: json.dumps(disks))

    assert windows.inspect_device_windows(r"\\?\Volume{abc}") == {
        "model": "Winner",
        "transport": "nvme",
        "rotational": False,
        "solid_state": True,
        "size_bytes": 6000,
        "media_type": "nvme",
    }


@pytest.mark.parametrize(
    ("disk", "expected"),
    [
        (
            {"FriendlyName": "SSD", "MediaType": "SSD", "BusType": "SATA", "Size": None},
            ("sata", None, False, True, "ssd"),
        ),
        (
            {"FriendlyName": "HDD", "MediaType": "HDD", "BusType": "SAS", "Size": 20},
            ("sas", 20, True, False, "hdd"),
        ),
        (
            {
                "FriendlyName": "Unknown",
                "MediaType": "Unspecified",
                "BusType": "USB",
                "Size": 30,
            },
            ("usb", 30, None, None, "unknown"),
        ),
    ],
    ids=["ssd", "hdd", "unknown"],
)
def test_inspect_device_windows_accepts_single_disk_document(
    monkeypatch: pytest.MonkeyPatch,
    disk: dict[str, Any],
    expected: tuple[str, int | None, bool | None, bool | None, str],
) -> None:
    monkeypatch.setattr(windows, "_powershell_json", lambda _command: json.dumps(disk))

    result = windows.inspect_device_windows("C:")

    transport, size, rotational, solid_state, media_type = expected
    assert result["model"] == disk["FriendlyName"]
    assert result["transport"] == transport
    assert result["size_bytes"] == size
    assert result["rotational"] is rotational
    assert result["solid_state"] is solid_state
    assert result["media_type"] == media_type


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (
            "HOST,NVMe,Fast disk,2048",
            ("Fast disk", "nvme", 2048, False, True, "nvme"),
        ),
        (
            "HOST,SATA,Solid disk,not-a-number",
            ("Solid disk", "sata", None, False, True, "ssd"),
        ),
        (
            "HOST,IDE,Legacy disk,4096",
            ("Legacy disk", "ide", 4096, None, None, "unknown"),
        ),
        (
            "HOST,,No bus,8",
            ("No bus", None, 8, None, None, "unknown"),
        ),
    ],
    ids=["nvme", "assumed-ssd", "unknown", "blank-interface"],
)
def test_inspect_device_windows_falls_back_to_wmic(
    monkeypatch: pytest.MonkeyPatch,
    row: str,
    expected: tuple[str, str | None, int | None, bool | None, bool | None, str],
) -> None:
    monkeypatch.setattr(windows, "_powershell_json", lambda _command: "")
    monkeypatch.setattr(
        windows,
        "_run",
        lambda _command: "Node,InterfaceType,Model,Size\n" + row,
    )

    result = windows.inspect_device_windows("C:")

    model, transport, size, rotational, solid_state, media_type = expected
    assert result == {
        "model": model,
        "transport": transport,
        "rotational": rotational,
        "solid_state": solid_state,
        "size_bytes": size,
        "media_type": media_type,
    }


@pytest.mark.parametrize(
    ("powershell_output", "wmic_output"),
    [
        ("not-json", "Node,InterfaceType,Model,Size"),
        ("[]", "Node,InterfaceType,Model,Size\nshort,row"),
    ],
    ids=["invalid-powershell", "short-wmic-row"],
)
def test_inspect_device_windows_tolerates_unusable_command_output(
    monkeypatch: pytest.MonkeyPatch,
    powershell_output: str,
    wmic_output: str,
) -> None:
    monkeypatch.setattr(windows, "_powershell_json", lambda _command: powershell_output)
    monkeypatch.setattr(windows, "_run", lambda _command: wmic_output)

    assert windows.inspect_device_windows("C:") == {
        "model": None,
        "transport": None,
        "rotational": None,
        "solid_state": None,
        "size_bytes": None,
        "media_type": "unknown",
    }


@pytest.mark.parametrize(
    ("frsize", "bsize", "expected_block_size"),
    [(1024, 512, 1024), (0, 2048, 2048), (0, 0, 4096)],
)
def test_statvfs_uses_portable_block_size_fallbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frsize: int,
    bsize: int,
    expected_block_size: int,
) -> None:
    stat = SimpleNamespace(
        f_frsize=frsize,
        f_bsize=bsize,
        f_blocks=10,
        f_bavail=3,
    )
    paths: list[str] = []

    def statvfs(path: str) -> SimpleNamespace:
        paths.append(path)
        return stat

    monkeypatch.setattr(tier_profiler.os, "statvfs", statvfs)

    assert tier_profiler._statvfs(tmp_path) == (
        expected_block_size,
        10 * expected_block_size,
        3 * expected_block_size,
    )
    assert paths == [str(tmp_path)]


def test_profile_path_measures_io_and_removes_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_dir = tmp_path / "new-tier"
    clock = iter([0.0, 2.0, 10.0, 10.25, 20.0, 24.0, 30.0, 30.5])
    random_ranges: list[tuple[int, int]] = []

    def randint(low: int, high: int) -> int:
        random_ranges.append((low, high))
        return high

    monkeypatch.setattr(tier_profiler, "_statvfs", lambda _path: (4096, 50_000, 20_000))
    monkeypatch.setattr(tier_profiler.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(tier_profiler.random, "randint", randint)
    monkeypatch.setattr(tier_profiler.os, "fsync", lambda _fd: None)

    result = tier_profiler.profile_path(
        profile_dir,
        file_size_mb=1,
        block_size=700_000,
        random_ops=3,
        random_block_size=4096,
    )

    assert result == tier_profiler.TierMetrics(
        path=str(profile_dir.resolve()),
        filesystem_block_size=4096,
        total_bytes=50_000,
        free_bytes=20_000,
        seq_write_MBps=0.5,
        seq_read_MBps=0.25,
        random_read_IOPS=6.0,
        first_byte_latency_ms=250.0,
    )
    assert random_ranges == [(0, 1024 * 1024 - 4096)] * 3
    assert not (profile_dir / ".cognistore_profile.tmp").exists()


def test_profile_path_handles_tiny_file_and_unlink_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter([0.0, 0.0, 1.0, 1.0, 2.0, 2.0])
    unlinks: list[Path] = []

    def unlink(path: Path, *_args: object, **_kwargs: object) -> None:
        unlinks.append(path)
        raise PermissionError("busy")

    monkeypatch.setattr(tier_profiler, "_statvfs", lambda _path: (512, 1000, 500))
    monkeypatch.setattr(tier_profiler.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(tier_profiler.os, "fsync", lambda _fd: None)
    monkeypatch.setattr(Path, "unlink", unlink)

    result = tier_profiler.profile_path(
        tmp_path,
        file_size_mb=0,
        block_size=1024,
        random_ops=4,
        random_block_size=1,
    )

    assert result.seq_write_MBps == 0.0
    assert result.seq_read_MBps == 0.0
    assert result.random_read_IOPS == 0.0
    assert result.first_byte_latency_ms == 0.0
    assert unlinks == [tmp_path / ".cognistore_profile.tmp"]
    assert (tmp_path / ".cognistore_profile.tmp").exists()


def test_metrics_json_round_trip(tmp_path: Path) -> None:
    source = {
        "warm": tier_profiler.TierMetrics(
            path="/warm",
            filesystem_block_size=4096,
            total_bytes=20_000,
            free_bytes=10_000,
            seq_write_MBps=10.5,
            seq_read_MBps=20.5,
            random_read_IOPS=300.0,
            first_byte_latency_ms=1.25,
        )
    }
    output = tmp_path / "nested" / "metrics.json"

    tier_profiler.save_metrics_json(source, output)

    assert json.loads(output.read_text(encoding="utf-8")) == {
        "warm": source["warm"].to_dict()
    }
    assert tier_profiler.load_metrics_json(output) == source


def test_load_metrics_json_accepts_null_document(tmp_path: Path) -> None:
    source = tmp_path / "metrics.json"
    source.write_text("null", encoding="utf-8")

    assert tier_profiler.load_metrics_json(source) == {}
