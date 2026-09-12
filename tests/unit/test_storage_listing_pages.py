"""Resumable inventory pages must remain bounded, read-only, and fail closed."""

from __future__ import annotations

import os
import stat
import tracemalloc
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from cognistore.drivers import posix_driver
from cognistore.drivers.observed import ObservedStorageDriver
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.drivers.s3_driver import S3Driver
from cognistore.drivers.storage_driver import StorageDriver, StorageListingPage


def _files(root: Path, keys: list[str]) -> None:
    for key in keys:
        target = root / "bucket" / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"content")


@pytest.mark.parametrize("limit", [1, 2, 4, 1000])
@pytest.mark.parametrize("prefix", ["", "a", "a/", "percent%_", "雪/", "missing"])
def test_posix_pages_resume_after_driver_reconstruction(
    tmp_path: Path, limit: int, prefix: str
) -> None:
    keys = [
        "a.txt", "a-early", "a/one", "a/nested/two", "a0", "a!/punctuation",
        "percent%_/one", "percentAA/other", "雪/é", "雪/line\nbreak", "雪/quote'\\",
    ]
    _files(tmp_path, keys)
    cursor = None
    found: list[str] = []
    while True:
        driver = PosixDriver(str(tmp_path))
        page = driver.list_objects_page("bucket", prefix, cursor=cursor, limit=limit)
        assert isinstance(page, StorageListingPage)
        assert isinstance(page.keys, tuple)
        assert len(page.keys) <= limit
        assert list(page.keys) == sorted(page.keys)
        found.extend(page.keys)
        if page.next_cursor is None:
            break
        assert page.next_cursor != cursor
        cursor = page.next_cursor
    assert found == sorted(key for key in keys if key.startswith(prefix))


def test_posix_page_does_not_mutate_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    absent = tmp_path / "absent"

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("inventory listing attempted a write")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(posix_driver, "_sync_descriptor", forbidden)
    assert PosixDriver(str(absent)).list_objects_page("bucket") == StorageListingPage((), None)
    assert not absent.exists()


def test_posix_resuming_a_disappeared_bucket_is_an_error(tmp_path: Path) -> None:
    _files(tmp_path, ["one", "two"])
    driver = PosixDriver(str(tmp_path))
    cursor = driver.list_objects_page("bucket", limit=1).next_cursor
    (tmp_path / "bucket").rename(tmp_path / "moved")
    with pytest.raises(FileNotFoundError):
        driver.list_objects_page("bucket", cursor=cursor)


def test_posix_prunes_completed_and_unrelated_subtrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _files(tmp_path, ["scope/a/one", "scope/b/two", "scope/c/three", "unrelated/four"])
    driver = PosixDriver(str(tmp_path))
    first = driver.list_objects_page("bucket", "scope/", limit=2)
    original = os.open
    visited: list[str] = []

    def tracked(path: str, flags: int, *, dir_fd: int | None = None):
        visited.append(path)
        assert path not in {"a", "unrelated"}
        return original(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(posix_driver.os, "open", tracked)
    assert driver.list_objects_page("bucket", "scope/", cursor=first.next_cursor).keys == (
        "scope/c/three",
    )
    assert "c" in visited


def test_posix_propagates_listing_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _files(tmp_path, ["one"])

    def denied(path: Path):
        raise PermissionError("injected listing failure")

    monkeypatch.setattr(posix_driver.os, "scandir", denied)
    with pytest.raises(PermissionError, match="injected"):
        PosixDriver(str(tmp_path)).list_objects_page("bucket")


def test_posix_excludes_symlinks_and_special_files(tmp_path: Path) -> None:
    _files(tmp_path, ["regular"])
    bucket = tmp_path / "bucket"
    (bucket / "symlink").symlink_to(bucket / "regular")
    (bucket / "loop").symlink_to(bucket, target_is_directory=True)
    os.mkfifo(bucket / "pipe")
    assert PosixDriver(str(tmp_path)).list_objects_page("bucket").keys == ("regular",)


@pytest.mark.parametrize("swap_root", [False, True])
def test_posix_rejects_directory_symlink_swapped_in_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap_root: bool
) -> None:
    root = tmp_path / "tier"
    outside = tmp_path / "outside"
    _files(root, ["scope/visible"])
    _files(outside, ["scope/private"])
    driver = PosixDriver(str(root))
    swapped = root if swap_root else root / "bucket" / "scope"
    destination = outside if swap_root else outside / "bucket" / "scope"
    original = os.open
    attempted = False

    def swap_before_open(path: str, flags: int, *, dir_fd: int | None = None):
        nonlocal attempted
        if path == swapped.name and dir_fd is not None and not attempted:
            attempted = True
            swapped.rename(tmp_path / "retired")
            swapped.symlink_to(destination, target_is_directory=True)
        return original(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(posix_driver.os, "open", swap_before_open)
    with pytest.raises(ValueError, match="symbolic links"):
        driver.list_objects_page("bucket", "scope/")
    assert attempted


def test_posix_inventory_preserves_staging_namespace_rules(tmp_path: Path) -> None:
    driver = PosixDriver(str(tmp_path))
    key = ".cognistore-staging/application-object"
    driver.put_object("bucket", key, b"content")
    (tmp_path / ".cognistore-staging" / "unfinished-upload").write_bytes(b"private")
    assert driver.list_objects_page("bucket").keys == tuple(driver.list_objects("bucket")) == (key,)
    with pytest.raises(ValueError, match="reserved namespace"):
        driver.list_objects_page(".cognistore-staging")


def test_posix_flat_listing_memory_does_not_grow_with_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bucket").mkdir()
    regular = os.stat_result((
        stat.S_IFREG | 0o644, 0, tmp_path.stat().st_dev, 1, 0, 0, 0, 0, 0, 0,
    ))

    class Entry:
        def __init__(self, number: int):
            self.name = f"item-{number:06d}-" + "x" * 100

        def stat(self, *, follow_symlinks: bool):
            assert not follow_symlinks
            return regular

    @contextmanager
    def entries(path: Path):
        yield (Entry(number) for number in range(50_000, 0, -1))

    monkeypatch.setattr(posix_driver.os, "scandir", entries)
    tracemalloc.start()
    try:
        page = PosixDriver(str(tmp_path)).list_objects_page("bucket", limit=10)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(page.keys) == 10
    assert page.keys[0].startswith("item-000001-")
    assert page.next_cursor is not None
    assert peak < 500_000


class _S3Client:
    def __init__(self, pages: list[dict[str, Any] | Exception]):
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def list_objects_v2(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def test_s3_one_native_page_per_call_and_opaque_resume() -> None:
    client = _S3Client([
        {"Contents": [{"Key": "雪/%_one"}], "IsTruncated": True,
         "NextContinuationToken": "native+/token="},
        {"Contents": [{"Key": "雪/%_two"}], "IsTruncated": False},
    ])
    first = S3Driver(client=client).list_objects_page("bucket", "雪/%_", limit=1)
    assert first.keys == ("雪/%_one",)
    assert first.next_cursor is not None
    assert len(client.calls) == 1
    second = S3Driver(client=client).list_objects_page(
        "bucket", "雪/%_", cursor=first.next_cursor, limit=1
    )
    assert second == StorageListingPage(("雪/%_two",), None)
    assert client.calls == [
        {"Bucket": "bucket", "Prefix": "雪/%_", "MaxKeys": 1},
        {"Bucket": "bucket", "Prefix": "雪/%_", "MaxKeys": 1,
         "ContinuationToken": "native+/token="},
    ]


def test_s3_empty_truncated_page_preserves_resume() -> None:
    client = _S3Client([{"IsTruncated": True, "NextContinuationToken": "next"}])
    page = S3Driver(client=client).list_objects_page("bucket")
    assert page.keys == ()
    assert page.next_cursor is not None


@pytest.mark.parametrize("response", [
    {},
    {"IsTruncated": True},
    {"IsTruncated": True, "NextContinuationToken": ""},
    {"IsTruncated": False, "NextContinuationToken": "contradiction"},
    {"IsTruncated": False, "Contents": None},
    {"IsTruncated": False, "Contents": [{"Key": 1}]},
    {"IsTruncated": False, "Contents": [{"Key": "outside-prefix"}]},
    {"IsTruncated": False, "Contents": [{"Key": "scope/one"}, {"Key": "scope/two"}]},
])
def test_s3_malformed_response_never_reports_completion(response: dict[str, Any]) -> None:
    client = _S3Client([response])
    with pytest.raises(RuntimeError):
        S3Driver(client=client).list_objects_page("bucket", "scope/", limit=1)


def test_s3_repeated_token_is_an_error() -> None:
    client = _S3Client([{"IsTruncated": True, "NextContinuationToken": "same"}] * 2)
    driver = S3Driver(client=client)
    first = driver.list_objects_page("bucket")
    with pytest.raises(RuntimeError, match="advancing"):
        driver.list_objects_page("bucket", cursor=first.next_cursor)


@pytest.mark.parametrize("code", ["AccessDenied", "NoSuchBucket", "SlowDown"])
def test_s3_propagates_listing_failures(code: str) -> None:
    error = ClientError({"Error": {"Code": code, "Message": code}}, "ListObjectsV2")
    with pytest.raises(ClientError) as raised:
        S3Driver(client=_S3Client([error])).list_objects_page("bucket")
    assert raised.value is error


@pytest.mark.parametrize("backend", ["posix", "s3"])
def test_listing_cursor_is_bound_to_scope(tmp_path: Path, backend: str) -> None:
    _files(tmp_path, ["scope/one", "scope/two"])
    client = _S3Client([{"IsTruncated": True, "NextContinuationToken": "next"}])
    driver = PosixDriver(str(tmp_path)) if backend == "posix" else S3Driver(client=client)
    cursor = driver.list_objects_page("bucket", "scope/", limit=1).next_cursor
    assert cursor is not None
    for bucket, prefix in [("other", "scope/"), ("bucket", "else/")]:
        with pytest.raises(ValueError, match="scope"):
            driver.list_objects_page(bucket, prefix, cursor=cursor)
    other = PosixDriver(str(tmp_path / "elsewhere")) if backend == "posix" else S3Driver(
        endpoint_url="https://different.invalid", client=client
    )
    with pytest.raises(ValueError, match="scope"):
        other.list_objects_page("bucket", "scope/", cursor=cursor)


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 2.5])
def test_listing_rejects_invalid_limits(tmp_path: Path, limit: Any) -> None:
    with pytest.raises(ValueError, match="limit"):
        PosixDriver(str(tmp_path)).list_objects_page("bucket", limit=limit)


@pytest.mark.parametrize("cursor", ["", "!bad!", "é", "bnVsbA==", True])
def test_listing_rejects_invalid_cursors(tmp_path: Path, cursor: Any) -> None:
    with pytest.raises(ValueError, match="cursor"):
        PosixDriver(str(tmp_path)).list_objects_page("bucket", cursor=cursor)


def test_unsupported_listing_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError, match="resumable"):
        StorageDriver.list_objects_page(PosixDriver(str(tmp_path)), "bucket")


def test_observed_inventory_pages_do_not_capture_access(tmp_path: Path) -> None:
    _files(tmp_path, ["one", "two"])

    class ForbiddenSink:
        def append_access_event(self, event: Any):
            pytest.fail("inventory page generated an application access event")

    observed = ObservedStorageDriver(PosixDriver(str(tmp_path)), ForbiddenSink())
    first = observed.list_objects_page("bucket", limit=1)
    assert first.keys == ("one",)
    assert observed.list_objects_page("bucket", cursor=first.next_cursor).keys == ("two",)
