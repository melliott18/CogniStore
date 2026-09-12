"""POSIX namespace races at the mover's conditional source cleanup."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cognistore.core.catalog import Catalog
from cognistore.core.mover import Mover
from cognistore.drivers.posix_driver import PosixDriver
from tests.conformance.test_posix_containment import _INSIDE, _DirectorySwap, _identity

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires POSIX descriptors")


@pytest.mark.parametrize("component", ["bucket", "parent"])
@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_mover_cleanup_unlinks_only_the_acquired_source_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
    replacement: str,
) -> None:
    case = _DirectorySwap(tmp_path, component, replacement)
    warm = PosixDriver(str(tmp_path / "warm"))
    catalog = Catalog()
    catalog.upsert(case.bucket, case.key, size=len(_INSIDE), tier="hot")
    source_parent_identity = _identity(case.object.parent.stat())
    original_unlink = os.unlink

    def racing_unlink(path, *args, **kwargs):
        parent_fd = kwargs.get("dir_fd")
        is_source = os.fspath(path) == str(case.object) or (
            os.fspath(path) == case.object.name
            and parent_fd is not None
            and _identity(os.fstat(parent_fd)) == source_parent_identity
        )
        if not case.swapped and is_source:
            # Conditional generation comparison and source descriptor opening
            # have completed. Replace the namespace just before the unlink.
            case.swap()
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", racing_unlink)

    result = Mover({"hot": case.driver, "warm": warm}, catalog).move(
        "hot", "warm", case.bucket, case.key
    )

    assert result.verified
    case.assert_untouched()
    assert not case.retained_object.exists()
    assert warm.get_object(case.bucket, case.key) == _INSIDE
    record = catalog.get(case.bucket, case.key)
    assert record is not None
    assert (record.tier, record.size) == ("warm", len(_INSIDE))


@pytest.mark.parametrize("component", ["bucket", "parent"])
def test_mover_rejects_source_symlink_before_cleanup_parent_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
) -> None:
    case = _DirectorySwap(tmp_path, component)
    warm = PosixDriver(str(tmp_path / "warm"))
    catalog = Catalog()
    catalog.upsert(case.bucket, case.key, size=len(_INSIDE), tier="hot")
    original_delete = case.driver.delete_object_if_generation

    def arm_race_at_conditional_cleanup(*args, **kwargs):
        case.swap_on_open(monkeypatch, when="before")
        return original_delete(*args, **kwargs)

    monkeypatch.setattr(case.driver, "delete_object_if_generation", arm_race_at_conditional_cleanup)

    with pytest.raises((ValueError, OSError)):
        Mover({"hot": case.driver, "warm": warm}, catalog).move(
            "hot", "warm", case.bucket, case.key
        )

    case.assert_untouched()
    assert case.retained_object.read_bytes() == _INSIDE
    assert warm.get_object(case.bucket, case.key) == _INSIDE
    record = catalog.get(case.bucket, case.key)
    assert record is not None
    assert (record.tier, record.size) == ("warm", len(_INSIDE))
