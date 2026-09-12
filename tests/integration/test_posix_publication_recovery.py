"""Recovery from process termination during POSIX no-overwrite publication."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cognistore.core.move_jobs import MoveJobState
from cognistore.core.mover import Mover
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires POSIX descriptors")

_PAYLOAD = b"complete durable object retained across process termination"
_BUCKET = "bucket"
_KEY = "nested/object.bin"
_JOB = "interrupted-no-overwrite-publication"
_CRASH_EXIT_CODE = 91
_CLOCK = datetime(2026, 8, 16, tzinfo=timezone.utc)
_CHILD = """
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from cognistore.core.mover import Mover
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver

root = Path(sys.argv[1])
warm = PosixDriver(str(root / "warm"))
destination = warm.base / "bucket" / "nested" / "object.bin"
original_sync_directory = warm._sync_directory

def crash_after_destination_barrier(directory):
    original_sync_directory(directory)
    if directory.path == destination.parent and destination.exists():
        # Exit without stack unwinding, so the driver's staging finally block
        # cannot hide the second hard link that survives a real process crash.
        assert destination.stat().st_nlink == 2
        os._exit(91)

warm._sync_directory = crash_after_destination_barrier
if sys.argv[2] == "move":
    hot = PosixDriver(str(root / "hot"))
    catalog = SQLiteCatalog(root / "catalog.db")
    mover = Mover(
        {"hot": hot, "warm": warm}, catalog, owner_id="crashing-worker",
        lease_seconds=1, clock=lambda: datetime(2026, 8, 16, tzinfo=timezone.utc),
    )
    mover.move(
        "hot", "warm", "bucket", "nested/object.bin",
        idempotency_key="interrupted-no-overwrite-publication",
    )
else:
    warm.put_object(
        "bucket", "nested/object.bin",
        b"complete durable object retained across process termination", overwrite=False,
    )
raise AssertionError("publication did not reach the intended crash boundary")
"""


def _crash_during_publication(tmp_path: Path, *, move: bool = False) -> tuple[Path, Path]:
    child = subprocess.run(
        [sys.executable, "-c", _CHILD, str(tmp_path), "move" if move else "put"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == _CRASH_EXIT_CODE, child.stdout + child.stderr
    destination = tmp_path / "warm" / _BUCKET / _KEY
    staging = list((tmp_path / "warm" / ".cognistore-staging").iterdir())
    assert len(staging) == 1
    destination_stat = destination.stat()
    staging_stat = staging[0].stat()
    assert destination_stat.st_nlink == staging_stat.st_nlink == 2
    assert (destination_stat.st_dev, destination_stat.st_ino) == (
        staging_stat.st_dev, staging_stat.st_ino,
    )
    assert destination.read_bytes() == _PAYLOAD
    return destination, staging[0]


def _fingerprint(path: Path) -> tuple[int, ...]:
    st = path.stat(follow_symlinks=False)
    return (
        st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_uid, st.st_gid,
        st.st_size, st.st_mtime_ns, st.st_ctime_ns,
    )


def test_fresh_driver_recovers_object_with_private_staging_link_after_process_exit(
    tmp_path: Path,
) -> None:
    destination, _ = _crash_during_publication(tmp_path)
    driver = PosixDriver(str(tmp_path / "warm"))

    assert driver.get_object(_BUCKET, _KEY) == _PAYLOAD
    metadata = driver.stat_object(_BUCKET, _KEY)
    assert metadata["size"] == len(_PAYLOAD)
    assert list(driver.list_objects(_BUCKET)) == [_KEY]
    driver.ensure_object_durable(_BUCKET, _KEY)
    with driver.open_object_reader_if_generation(
        _BUCKET, _KEY, metadata["generation"],
    ) as reader:
        assert reader.read() == _PAYLOAD
    driver.delete_object(_BUCKET, _KEY)
    assert not destination.exists()


@pytest.mark.parametrize("operation", ["read", "stat", "list", "durability", "write", "delete"])
def test_private_staging_link_does_not_authorize_additional_outside_hardlink(
    tmp_path: Path, operation: str,
) -> None:
    destination, staging = _crash_during_publication(tmp_path)
    outside = tmp_path / "outside.bin"
    os.link(destination, outside)
    assert outside.stat().st_nlink == 3
    before = _fingerprint(outside)
    driver = PosixDriver(str(tmp_path / "warm"))

    with pytest.raises(ValueError, match="multiple hard links"):
        if operation == "read":
            driver.get_object(_BUCKET, _KEY)
        elif operation == "stat":
            driver.stat_object(_BUCKET, _KEY)
        elif operation == "list":
            list(driver.list_objects(_BUCKET))
        elif operation == "durability":
            driver.ensure_object_durable(_BUCKET, _KEY)
        elif operation == "write":
            driver.put_object(_BUCKET, _KEY, b"must not replace the aliased object")
        else:
            driver.delete_object(_BUCKET, _KEY)

    assert _fingerprint(outside) == before
    assert destination.read_bytes() == staging.read_bytes() == outside.read_bytes() == _PAYLOAD


def test_prepared_move_recovers_after_process_exit_before_staging_unlink(tmp_path: Path) -> None:
    hot = PosixDriver(str(tmp_path / "hot"))
    hot.put_object(_BUCKET, _KEY, _PAYLOAD)
    database = tmp_path / "catalog.db"
    catalog = SQLiteCatalog(database)
    catalog.upsert(_BUCKET, _KEY, len(_PAYLOAD), "hot")
    catalog.close()

    _crash_during_publication(tmp_path, move=True)

    recovered_catalog = SQLiteCatalog(database)
    try:
        interrupted = recovered_catalog.get_move_job(_JOB)
        assert interrupted is not None
        assert interrupted.state == MoveJobState.PREPARED
        placement = recovered_catalog.get(_BUCKET, _KEY)
        assert placement is not None
        assert placement.tier == "hot"
        hot = PosixDriver(str(tmp_path / "hot"))
        warm = PosixDriver(str(tmp_path / "warm"))
        assert hot.get_object(_BUCKET, _KEY) == _PAYLOAD
        recovered = Mover(
            {"hot": hot, "warm": warm}, recovered_catalog,
            owner_id="recovery-worker", lease_seconds=1,
            clock=lambda: _CLOCK + timedelta(seconds=2),
        )

        result = recovered.move("hot", "warm", _BUCKET, _KEY, idempotency_key=_JOB)

        assert result.verified
        assert warm.get_object(_BUCKET, _KEY) == _PAYLOAD
        with pytest.raises(FileNotFoundError):
            hot.stat_object(_BUCKET, _KEY)
        completed = recovered_catalog.get_move_job(_JOB)
        assert completed is not None
        assert completed.state == MoveJobState.COMPLETED
        placement = recovered_catalog.get(_BUCKET, _KEY)
        assert placement is not None
        assert placement.tier == "warm"
    finally:
        recovered_catalog.close()
