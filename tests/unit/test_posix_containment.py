"""Platform failures and deployment boundaries for descriptor-contained storage."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from cognistore.drivers.posix_driver import PosixDriver

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires POSIX descriptors")

_PAYLOAD = b"original stored content"
_REPLACEMENT = b"replacement content"


def _stored_object(tmp_path: Path) -> tuple[PosixDriver, Path]:
    root = tmp_path / "tier"
    path = root / "bucket" / "object.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(_PAYLOAD)
    return PosixDriver(str(root)), path


def _operate(driver: PosixDriver, operation: str):
    if operation == "read":
        return driver.get_object("bucket", "object.bin")
    if operation == "range_read":
        return driver.get_object("bucket", "object.bin", range="bytes=0-2")
    if operation == "stat":
        return driver.stat_object("bucket", "object.bin")
    if operation == "list_page":
        return driver.list_objects_page("bucket")
    if operation == "write":
        return driver.put_object("bucket", "object.bin", _REPLACEMENT)
    if operation == "range_write":
        return driver.put_object("bucket", "object.bin", b"new", range="bytes=0-2")
    if operation == "delete":
        return driver.delete_object("bucket", "object.bin")
    if operation == "durability":
        return driver.ensure_object_durable("bucket", "object.bin")
    raise AssertionError(operation)


def _fingerprint(path: Path) -> tuple[int, ...]:
    st = path.stat(follow_symlinks=False)
    return (
        st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_uid, st.st_gid,
        st.st_size, st.st_mtime_ns, st.st_ctime_ns,
    )


@pytest.mark.parametrize(
    "capability",
    [
        "O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK", "fchmod",
        "dir_fd:open", "dir_fd:mkdir", "dir_fd:stat", "dir_fd:unlink",
        "dir_fd:rename", "dir_fd:link", "fd:listdir", "fd:scandir",
        "follow_symlinks:stat", "follow_symlinks:link",
    ],
)
def test_missing_containment_capability_rejects_constructor_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capability: str,
) -> None:
    root = tmp_path / "missing" / "tier"
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(_PAYLOAD)
    before = _fingerprint(sentinel)

    with monkeypatch.context() as patch:
        if ":" in capability:
            support, operation = capability.split(":")
            attribute = f"supports_{support}"
            patch.setattr(os, attribute, getattr(os, attribute) - {getattr(os, operation)})
        else:
            patch.delattr(os, capability)
        with pytest.raises(NotImplementedError, match="containment requires"):
            PosixDriver(str(root))

    assert not root.parent.exists()
    assert _fingerprint(sentinel) == before
    assert sentinel.read_bytes() == _PAYLOAD
    assert set(tmp_path.iterdir()) == {sentinel}


@pytest.mark.parametrize("syscall", ["open", "replace", "link"])
def test_runtime_unsupported_relative_syscall_fails_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, syscall: str,
) -> None:
    driver, object_path = _stored_object(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "object.bin"
    sentinel.write_bytes(b"outside must remain untouched")
    original_fingerprint = _fingerprint(object_path)
    outside_fingerprint = _fingerprint(sentinel)
    original = getattr(os, syscall)
    attempts = []

    def unsupported(path, *args, **kwargs):
        if syscall == "open" and os.fspath(path) != "object.bin":
            return original(path, *args, **kwargs)
        attempts.append((path, args, kwargs))
        if syscall == "open":
            assert kwargs.get("dir_fd") is not None
        else:
            assert kwargs.get("src_dir_fd") is not None
            assert kwargs.get("dst_dir_fd") is not None
            assert not Path(path).is_absolute()
            assert args[0] == "object.bin"
            if syscall == "link":
                assert kwargs["follow_symlinks"] is False
        raise OSError(errno.ENOTSUP, "injected descriptor operation unsupported")

    monkeypatch.setattr(os, syscall, unsupported)
    with pytest.raises(OSError) as error:
        if syscall == "open":
            driver.get_object("bucket", "object.bin")
        else:
            driver.put_object(
                "bucket", "object.bin", _REPLACEMENT, overwrite=syscall == "replace",
            )
    assert error.value.errno == errno.ENOTSUP
    assert len(attempts) == 1
    assert _fingerprint(object_path) == original_fingerprint
    assert object_path.read_bytes() == _PAYLOAD
    assert _fingerprint(sentinel) == outside_fingerprint
    assert sentinel.read_bytes() == b"outside must remain untouched"
    staging = driver.base / ".cognistore-staging"
    assert not staging.exists() or list(staging.iterdir()) == []


@pytest.mark.parametrize("location", ["ancestor", "root", "descendant"])
@pytest.mark.parametrize("mode", [0o770, 0o707])
@pytest.mark.parametrize("operation", ["read", "write", "list_page"])
def test_untrusted_directory_permissions_reject_access(
    tmp_path: Path, location: str, mode: int, operation: str,
) -> None:
    driver, object_path = _stored_object(tmp_path)
    directory = {
        "ancestor": tmp_path,
        "root": driver.base,
        "descendant": object_path.parent,
    }[location]
    old_mode = stat.S_IMODE(directory.stat().st_mode)
    before = _fingerprint(object_path)
    directory.chmod(mode)
    try:
        with pytest.raises(PermissionError, match="trusted directory"):
            _operate(driver, operation)
        assert _fingerprint(object_path) == before
        assert object_path.read_bytes() == _PAYLOAD
        assert not (driver.base / ".cognistore-staging").exists()
    finally:
        directory.chmod(old_mode)


@pytest.mark.parametrize("location", ["ancestor", "root"])
def test_constructor_rejects_unsafe_existing_directory_without_mutation(
    tmp_path: Path, location: str,
) -> None:
    root = tmp_path / "tier"
    root.mkdir()
    directory = tmp_path if location == "ancestor" else root
    old_mode = stat.S_IMODE(directory.stat().st_mode)
    directory.chmod(0o777)
    try:
        before = _fingerprint(directory)
        with pytest.raises(PermissionError, match="trusted directory"):
            PosixDriver(str(root))
        assert _fingerprint(directory) == before
        assert list(root.iterdir()) == []
    finally:
        directory.chmod(old_mode)


@pytest.mark.parametrize("location", ["ancestor", "root", "descendant"])
@pytest.mark.parametrize("operation", ["read", "list_page"])
def test_foreign_directory_owner_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str, operation: str,
) -> None:
    driver, object_path = _stored_object(tmp_path)
    directory = {
        "ancestor": tmp_path,
        "root": driver.base,
        "descendant": object_path.parent,
    }[location]
    target = directory.stat()
    original_fstat = os.fstat
    checked = []

    def foreign_owner(descriptor):
        result = original_fstat(descriptor)
        if (result.st_dev, result.st_ino) == (target.st_dev, target.st_ino):
            values = list(result)
            values[4] = os.geteuid() + 10000
            checked.append(descriptor)
            return os.stat_result(values)
        return result

    monkeypatch.setattr(os, "fstat", foreign_owner)
    with pytest.raises(PermissionError, match="trusted directory"):
        _operate(driver, operation)
    assert checked
    assert object_path.read_bytes() == _PAYLOAD


def test_sticky_shared_ancestor_allows_protected_root(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o1777)
    shared.chmod(0o1777)
    driver = PosixDriver(str(shared / "private-tier"))

    driver.put_object("bucket", "object.bin", _PAYLOAD)

    assert driver.get_object("bucket", "object.bin") == _PAYLOAD
    assert stat.S_IMODE(driver.base.stat().st_mode) == 0o700
    assert stat.S_IMODE((driver.base / "bucket").stat().st_mode) == 0o700


@pytest.mark.parametrize("location", ["root", "descendant"])
@pytest.mark.parametrize("operation", ["read", "list_page"])
def test_sticky_bit_does_not_allow_shared_object_directories(
    tmp_path: Path, location: str, operation: str,
) -> None:
    driver, object_path = _stored_object(tmp_path)
    directory = driver.base if location == "root" else object_path.parent
    directory.chmod(0o1777)
    with pytest.raises(PermissionError, match="trusted directory"):
        _operate(driver, operation)


@pytest.mark.parametrize("operation", ["read", "write", "delete", "list_page"])
def test_replaced_tier_root_is_rejected(tmp_path: Path, operation: str) -> None:
    driver, original_object = _stored_object(tmp_path)
    retained = tmp_path / "retained-tier"
    driver.base.rename(retained)
    replacement = original_object
    replacement.parent.mkdir(parents=True)
    replacement.write_bytes(b"replacement root contents")
    before = _fingerprint(replacement)

    with pytest.raises(ValueError, match="root directory was replaced"):
        _operate(driver, operation)

    assert _fingerprint(replacement) == before
    assert replacement.read_bytes() == b"replacement root contents"
    assert (retained / "bucket" / "object.bin").read_bytes() == _PAYLOAD
    assert not (driver.base / ".cognistore-staging").exists()


@pytest.mark.parametrize("kind", ["hardlink", "fifo", "directory"])
@pytest.mark.parametrize(
    "operation", ["read", "range_read", "stat", "write", "range_write", "delete", "durability"],
)
def test_non_regular_or_aliased_objects_are_rejected(
    tmp_path: Path, kind: str, operation: str,
) -> None:
    driver, object_path = _stored_object(tmp_path)
    object_path.unlink()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside must remain untouched")
    if kind == "hardlink":
        os.link(outside, object_path)
    elif kind == "fifo":
        os.mkfifo(object_path)
    else:
        object_path.mkdir()
    before = _fingerprint(outside)
    object_before = _fingerprint(object_path)

    with pytest.raises(ValueError, match="regular files|multiple hard links"):
        _operate(driver, operation)

    assert _fingerprint(outside) == before
    assert outside.read_bytes() == b"outside must remain untouched"
    assert _fingerprint(object_path) == object_before
    staging = driver.base / ".cognistore-staging"
    assert not staging.exists() or list(staging.iterdir()) == []


def test_paged_listing_rejects_outside_hardlink_without_mutation(tmp_path: Path) -> None:
    driver, object_path = _stored_object(tmp_path)
    outside = tmp_path / "outside.bin"
    os.link(object_path, outside)
    before = _fingerprint(outside)

    with pytest.raises(ValueError, match="multiple hard links"):
        driver.list_objects_page("bucket")

    assert _fingerprint(outside) == before
    assert outside.read_bytes() == object_path.read_bytes() == _PAYLOAD


@pytest.mark.parametrize("mode", [0o770, 0o707])
def test_paged_listing_rejects_unsafe_nested_directory(tmp_path: Path, mode: int) -> None:
    driver, object_path = _stored_object(tmp_path)
    nested = object_path.parent / "nested"
    nested.mkdir()
    object_path.rename(nested / object_path.name)
    nested.chmod(mode)
    before = _fingerprint(nested / object_path.name)

    with pytest.raises(PermissionError, match="trusted directory"):
        driver.list_objects_page("bucket")

    assert _fingerprint(nested / object_path.name) == before


@pytest.mark.parametrize("location", ["directory", "object"])
@pytest.mark.parametrize("operation", ["read", "range_read", "stat", "range_write", "delete"])
def test_descriptor_device_mismatch_rejects_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str, operation: str,
) -> None:
    driver, object_path = _stored_object(tmp_path)
    target = (object_path.parent if location == "directory" else object_path).stat()
    before = _fingerprint(object_path)
    original_fstat = os.fstat
    original_stat = os.stat
    checked = []

    def different_device(result):
        if (result.st_dev, result.st_ino) == (target.st_dev, target.st_ino):
            checked.append(result.st_ino)
            values = list(result)
            values[2] = result.st_dev + 1
            return os.stat_result(values)
        return result

    def fstat(descriptor):
        return different_device(original_fstat(descriptor))

    def relative_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        return different_device(result) if kwargs.get("dir_fd") is not None else result

    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(os, "stat", relative_stat)
    with pytest.raises(ValueError, match="tier root filesystem"):
        _operate(driver, operation)
    assert checked
    assert _fingerprint(object_path) == before
    assert object_path.read_bytes() == _PAYLOAD


def _track_open_descriptors(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    original_open = os.open
    descriptors = []

    def track(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "open", track)
    return descriptors


def _assert_closed(descriptors: list[int]) -> None:
    assert descriptors, "the operation must have acquired filesystem descriptors"
    for descriptor in set(descriptors):
        with pytest.raises(OSError) as error:
            os.fstat(descriptor)
        assert error.value.errno == errno.EBADF


@pytest.mark.parametrize("failure", ["consumer", "invalid_range", "fdopen"])
def test_read_context_failure_closes_all_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    driver, object_path = _stored_object(tmp_path)
    descriptors = _track_open_descriptors(monkeypatch)
    if failure == "fdopen":
        def fail_fdopen(*args, **kwargs):
            raise RuntimeError("injected fdopen failure")

        monkeypatch.setattr(os, "fdopen", fail_fdopen)
    expected_error = ValueError if failure == "invalid_range" else RuntimeError
    with pytest.raises(expected_error):
        with driver.open_object_reader(
            "bucket", "object.bin", range="invalid" if failure == "invalid_range" else None,
        ) as reader:
            assert failure == "consumer"
            assert reader.read(3) == _PAYLOAD[:3]
            raise RuntimeError("injected consumer failure")

    _assert_closed(descriptors)
    assert object_path.read_bytes() == _PAYLOAD


def test_staging_fdopen_failure_closes_descriptors_and_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver, object_path = _stored_object(tmp_path)
    descriptors = _track_open_descriptors(monkeypatch)
    wrapped = []

    def fail_fdopen(descriptor, mode):
        assert mode == "wb"
        wrapped.append(descriptor)
        raise RuntimeError("injected staging fdopen failure")

    monkeypatch.setattr(os, "fdopen", fail_fdopen)
    with pytest.raises(RuntimeError, match="injected staging fdopen failure"):
        driver.put_object("bucket", "object.bin", _REPLACEMENT)

    assert len(wrapped) == 1
    _assert_closed(descriptors)
    assert list((driver.base / ".cognistore-staging").iterdir()) == []
    assert object_path.read_bytes() == _PAYLOAD
