"""Deterministic namespace races against the POSIX containment boundary.

Every rename keeps the original directory inside the configured root. The
attacker controls names below that root, but cannot relocate the trusted tree.
"""

from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path

import pytest

from cognistore.drivers.posix_driver import PosixDriver

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires POSIX descriptors")

_INSIDE = b"original object inside the tier"
_OUTSIDE = b"outside sentinel must remain untouched"
_NEW = b"new published object"
_OPERATIONS = (
    "read",
    "range_read",
    "generation_read",
    "stat",
    "list",
    "publish_overwrite",
    "publish_create",
    "range_write",
    "delete",
    "conditional_delete",
    "durability",
)


def _identity(st: os.stat_result) -> tuple[int, int]:
    return st.st_dev, st.st_ino


def _fingerprint(path: Path) -> tuple[int, ...]:
    st = path.stat(follow_symlinks=False)
    return (
        st.st_dev,
        st.st_ino,
        st.st_mode,
        st.st_uid,
        st.st_gid,
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
        st.st_nlink,
    )


class _DirectorySwap:
    """Replace a checked name at one exact directory-open syscall boundary."""

    bucket = "bucket"
    key = "nested/object.bin"

    def __init__(
        self,
        tmp_path: Path,
        component: str,
        replacement: str = "symlink",
        *,
        existing: bool = True,
    ) -> None:
        self.driver = PosixDriver(str(tmp_path / "objects"))
        self.object = self.driver.base / self.bucket / self.key
        self.object.parent.mkdir(parents=True)
        if existing:
            self.object.write_bytes(_INSIDE)
        self.generation = (
            self.driver.stat_object(self.bucket, self.key)["generation"]
            if existing
            else None
        )
        self.target = (
            self.driver.base / self.bucket if component == "bucket" else self.object.parent
        )
        self.target_identity = _identity(self.target.stat())
        self.parent_identity = _identity(self.target.parent.stat())
        self.relative_object = self.object.relative_to(self.target)
        self.retained = self.driver.base / "retained-original"
        self.outside = tmp_path / "outside"
        self.outside_object = self.outside / self.relative_object
        self.outside_object.parent.mkdir(parents=True)
        self.outside_object.write_bytes(_OUTSIDE)
        self.outside_object.chmod(0o640)
        self.outside_secret = self.outside / "outside-only.bin"
        self.outside_secret.write_bytes(b"private outside filename and contents")
        self.outside_fingerprints = {
            path: _fingerprint(path) for path in (self.outside_object, self.outside_secret)
        }
        self.replacement = replacement
        if replacement == "directory":
            self.new_directory = self.driver.base / "replacement-directory"
            replacement_object = self.new_directory / self.relative_object
            replacement_object.parent.mkdir(parents=True)
            replacement_object.write_bytes(_OUTSIDE)
            self.replacement_fingerprint = _fingerprint(replacement_object)
        self.swapped = False

    @property
    def retained_object(self) -> Path:
        return self.retained / self.relative_object

    def swap(self) -> None:
        assert not self.swapped
        self.swapped = True
        self.target.rename(self.retained)
        if self.replacement == "symlink":
            self.target.symlink_to(self.outside, target_is_directory=True)
        else:
            self.new_directory.rename(self.target)

    def swap_on_open(self, monkeypatch: pytest.MonkeyPatch, *, when: str) -> None:
        original_open = os.open

        def targets_directory(path, flags, kwargs) -> bool:
            if not flags & os.O_DIRECTORY:
                return False
            name = os.fspath(path)
            if name == str(self.target):
                return True
            parent_fd = kwargs.get("dir_fd")
            return (
                name == self.target.name
                and parent_fd is not None
                and _identity(os.fstat(parent_fd)) == self.parent_identity
            )

        def racing_open(path, flags, *args, **kwargs):
            if not self.swapped and when == "before" and targets_directory(path, flags, kwargs):
                self.swap()
            descriptor = original_open(path, flags, *args, **kwargs)
            if (
                not self.swapped
                and when == "after"
                and _identity(os.fstat(descriptor)) == self.target_identity
            ):
                self.swap()
            return descriptor

        monkeypatch.setattr(os, "open", racing_open)

    def assert_untouched(self) -> None:
        assert self.swapped, "the test must exercise its intended syscall race"
        for path, fingerprint in self.outside_fingerprints.items():
            assert _fingerprint(path) == fingerprint
        assert self.outside_object.read_bytes() == _OUTSIDE
        assert self.outside_secret.read_bytes() == b"private outside filename and contents"
        if self.replacement == "directory":
            replacement_object = self.target / self.relative_object
            assert _fingerprint(replacement_object) == self.replacement_fingerprint
            assert replacement_object.read_bytes() == _OUTSIDE


def _operate(case: _DirectorySwap, operation: str):
    driver, bucket, key = case.driver, case.bucket, case.key
    if operation == "read":
        return driver.get_object(bucket, key)
    if operation == "range_read":
        return driver.get_object(bucket, key, range="bytes=2-6")
    if operation == "generation_read":
        with driver.open_object_reader_if_generation(bucket, key, case.generation) as source:
            return source.read()
    if operation == "stat":
        return driver.stat_object(bucket, key)
    if operation == "list":
        return list(driver.list_objects(bucket))
    if operation in {"publish_overwrite", "publish_create"}:
        return driver.put_object_stream(
            bucket,
            key,
            BytesIO(_NEW),
            size=len(_NEW),
            overwrite=operation == "publish_overwrite",
        )
    if operation == "range_write":
        return driver.put_object(bucket, key, b"new", range="bytes=0-2")
    if operation == "delete":
        return driver.delete_object(bucket, key)
    if operation == "conditional_delete":
        return driver.delete_object_if_generation(bucket, key, case.generation)
    if operation == "durability":
        return driver.ensure_object_durable(bucket, key)
    raise AssertionError(operation)


@pytest.mark.parametrize("component", ["bucket", "parent"])
@pytest.mark.parametrize("operation", _OPERATIONS)
def test_symlink_swap_before_directory_acquisition_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
    operation: str,
) -> None:
    case = _DirectorySwap(tmp_path, component, existing=operation != "publish_create")
    case.swap_on_open(monkeypatch, when="before")

    if operation == "list":
        # Listings can skip a child that becomes unsafe during traversal, or
        # reject the bucket itself. Neither response may reveal outside keys.
        try:
            assert _operate(case, operation) == []
        except (ValueError, OSError):
            pass
    else:
        with pytest.raises((ValueError, OSError)):
            _operate(case, operation)

    case.assert_untouched()
    if operation == "publish_create":
        assert not case.retained_object.exists()
    else:
        assert case.retained_object.read_bytes() == _INSIDE


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
@pytest.mark.parametrize("component", ["bucket", "parent"])
@pytest.mark.parametrize("operation", _OPERATIONS)
def test_acquired_directory_remains_bound_during_namespace_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
    component: str,
    operation: str,
) -> None:
    case = _DirectorySwap(
        tmp_path, component, replacement, existing=operation != "publish_create"
    )
    case.swap_on_open(monkeypatch, when="after")

    result = _operate(case, operation)

    case.assert_untouched()
    if operation in {"read", "generation_read"}:
        assert result == _INSIDE
    elif operation == "range_read":
        assert result == _INSIDE[2:7]
    elif operation == "stat":
        assert (result["size"], result["generation"]) == (len(_INSIDE), case.generation)
    elif operation == "list":
        assert result == [case.key]
    elif operation in {"publish_overwrite", "publish_create"}:
        assert result == len(_NEW)
        assert case.retained_object.read_bytes() == _NEW
    elif operation == "range_write":
        assert case.retained_object.read_bytes() == b"new" + _INSIDE[3:]
    elif operation in {"delete", "conditional_delete"}:
        assert not case.retained_object.exists()
        if operation == "conditional_delete":
            assert result is True
    elif operation == "durability":
        assert case.retained_object.read_bytes() == _INSIDE


@pytest.mark.parametrize("overwrite", [True, False])
def test_publication_uses_acquired_parent_at_the_publish_syscall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overwrite: bool
) -> None:
    case = _DirectorySwap(tmp_path, "parent", existing=overwrite)
    syscall = "replace" if overwrite else "link"
    original_publish = getattr(os, syscall)

    def racing_publish(source, destination, *args, **kwargs):
        if not case.swapped and os.fspath(destination) in {"object.bin", str(case.object)}:
            case.swap()
        return original_publish(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, syscall, racing_publish)

    assert case.driver.put_object_stream(
        case.bucket, case.key, BytesIO(_NEW), size=len(_NEW), overwrite=overwrite
    ) == len(_NEW)

    case.assert_untouched()
    assert case.retained_object.read_bytes() == _NEW
    staging = case.driver.base / ".cognistore-staging"
    assert list(staging.iterdir()) == []


def test_listing_rechecks_child_at_open_after_nofollow_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _DirectorySwap(tmp_path, "parent")
    original_stat = os.stat

    def racing_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if not case.swapped and _identity(result) == case.target_identity:
            case.swap()
        return result

    monkeypatch.setattr(os, "stat", racing_stat)

    try:
        assert list(case.driver.list_objects(case.bucket)) == []
    except (ValueError, OSError):
        pass
    case.assert_untouched()
    assert case.retained_object.read_bytes() == _INSIDE


@pytest.mark.parametrize("overwrite", [True, False])
@pytest.mark.parametrize("stream_fails", [True, False])
def test_staging_namespace_swap_cannot_redirect_publication_or_failure_cleanup(
    tmp_path: Path,
    overwrite: bool,
    stream_fails: bool,
) -> None:
    driver = PosixDriver(str(tmp_path / "objects"))
    object_path = driver.base / "bucket" / "object.bin"
    object_path.parent.mkdir(parents=True)
    if overwrite:
        object_path.write_bytes(_INSIDE)
    staging = driver.base / ".cognistore-staging"
    retained_staging = driver.base / "retained-staging"
    outside = tmp_path / "outside"
    outside.mkdir()

    class SwappingStream(BytesIO):
        sentinel: Path | None = None
        fingerprint: tuple[int, ...] | None = None

        def read(self, size: int = -1) -> bytes:
            if self.sentinel is None:
                temporary_files = list(staging.iterdir())
                assert len(temporary_files) == 1
                self.sentinel = outside / temporary_files[0].name
                self.sentinel.write_bytes(_OUTSIDE)
                self.sentinel.chmod(0o640)
                self.fingerprint = _fingerprint(self.sentinel)
                staging.rename(retained_staging)
                staging.symlink_to(outside, target_is_directory=True)
                if stream_fails:
                    raise OSError("injected source stream failure")
            return super().read(size)

    source = SwappingStream(_NEW)
    if stream_fails:
        with pytest.raises(OSError, match="injected source stream failure"):
            driver.put_object_stream(
                "bucket", "object.bin", source, size=len(_NEW), overwrite=overwrite
            )
        if overwrite:
            assert object_path.read_bytes() == _INSIDE
        else:
            assert not object_path.exists()
    else:
        assert driver.put_object_stream(
            "bucket", "object.bin", source, size=len(_NEW), overwrite=overwrite
        ) == len(_NEW)
        assert object_path.read_bytes() == _NEW

    assert source.sentinel is not None, "the stream must trigger the staging swap"
    assert _fingerprint(source.sentinel) == source.fingerprint
    assert source.sentinel.read_bytes() == _OUTSIDE
    assert list(retained_staging.iterdir()) == []


@pytest.mark.parametrize(
    ("operation", "syscall"),
    [
        ("read", "open"),
        ("range_read", "open"),
        ("generation_read", "open"),
        ("range_write", "open"),
        ("stat", "stat"),
        ("delete", "unlink"),
        ("conditional_delete", "unlink"),
    ],
)
def test_final_file_symlink_swap_cannot_redirect_the_object_syscall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    syscall: str,
) -> None:
    case = _DirectorySwap(tmp_path, "parent")
    retained_object = case.driver.base / "retained-object.bin"
    parent_identity = _identity(case.object.parent.stat())
    original_syscall = getattr(os, syscall)

    def racing_syscall(path, *args, **kwargs):
        parent_fd = kwargs.get("dir_fd")
        is_object = os.fspath(path) == str(case.object) or (
            os.fspath(path) == case.object.name
            and parent_fd is not None
            and _identity(os.fstat(parent_fd)) == parent_identity
        )
        if not case.swapped and is_object:
            # All path validation and any earlier generation comparison have
            # finished. The next kernel call sees a symlink at the final name.
            case.swapped = True
            case.object.rename(retained_object)
            case.object.symlink_to(case.outside_object)
        return original_syscall(path, *args, **kwargs)

    monkeypatch.setattr(os, syscall, racing_syscall)

    if syscall == "unlink":
        result = _operate(case, operation)
        assert not case.object.is_symlink()
        assert not case.object.exists()
        if operation == "conditional_delete":
            assert result is True
    else:
        with pytest.raises((ValueError, OSError)):
            _operate(case, operation)
        assert case.object.is_symlink()

    case.assert_untouched()
    assert retained_object.read_bytes() == _INSIDE


def test_new_parent_creation_stays_bound_when_bucket_is_swapped_at_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _DirectorySwap(tmp_path, "bucket", existing=False)
    case.object.parent.rmdir()
    outside_inventory = {
        path.relative_to(case.outside): _fingerprint(path)
        for path in (case.outside, *case.outside.rglob("*"))
    }
    original_mkdir = os.mkdir

    def racing_mkdir(path, *args, **kwargs):
        parent_fd = kwargs.get("dir_fd")
        creates_parent = os.fspath(path) == str(case.object.parent) or (
            os.fspath(path) == "nested"
            and parent_fd is not None
            and _identity(os.fstat(parent_fd)) == case.target_identity
        )
        if not case.swapped and creates_parent:
            case.swap()
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", racing_mkdir)

    assert case.driver.put_object_stream(
        case.bucket, case.key, BytesIO(_NEW), size=len(_NEW), overwrite=False
    ) == len(_NEW)

    case.assert_untouched()
    assert case.retained_object.read_bytes() == _NEW
    assert {
        path.relative_to(case.outside): _fingerprint(path)
        for path in (case.outside, *case.outside.rglob("*"))
    } == outside_inventory
