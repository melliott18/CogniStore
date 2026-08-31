from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from cognistore.core.catalog import Catalog
from cognistore.core.move_jobs import MoveJobState
from cognistore.core.mover import Mover, MoveVerificationError
from cognistore.core.scanner import scan_catalog
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver

_COORDINATION_TIMEOUT = 10.0
CatalogHandles = tuple[str, Catalog, Catalog]


def _close_catalogs(*catalogs: Catalog) -> None:
    closed: set[int] = set()
    for catalog in catalogs:
        if id(catalog) in closed:
            continue
        closed.add(id(catalog))
        if isinstance(catalog, SQLCatalog):
            catalog.close()


@pytest.fixture(
    params=[
        "memory",
        "sqlite",
        pytest.param("postgres", marks=pytest.mark.integration),
    ]
)
def catalog_handles(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> Iterator[CatalogHandles]:
    """Provide independently connected move and scan catalog handles."""

    catalog_kind = str(request.param)
    if catalog_kind == "memory":
        move_catalog = Catalog()
        scan_catalog_store = move_catalog
    elif catalog_kind == "sqlite":
        database = tmp_path / "catalog.db"
        move_catalog = SQLiteCatalog(database)
        scan_catalog_store = SQLiteCatalog(database)
    else:
        postgres_dsn = request.getfixturevalue("postgres_dsn")
        move_catalog = SQLCatalog(postgres_dsn)
        scan_catalog_store = SQLCatalog(postgres_dsn, migrate=False)

    try:
        yield catalog_kind, move_catalog, scan_catalog_store
    finally:
        _close_catalogs(scan_catalog_store, move_catalog)


def _record_failed_prepared_move(
    catalog: Catalog,
    source: PosixDriver,
    *,
    idempotency_key: str,
) -> None:
    now = "2020-01-01T00:00:00.000000Z"
    lease = "2020-01-01T00:01:00.000000Z"
    metadata = source.stat_object("bucket", "object")
    catalog.claim_move_job(
        idempotency_key,
        src_tier="hot",
        dst_tier="warm",
        bucket="bucket",
        key="object",
        expected_size=int(metadata["size"]),
        source_metadata=metadata,
        owner_id="failed-owner",
        now=now,
        lease_expires_at=lease,
    )
    catalog.transition_move_job(
        idempotency_key,
        owner_id="failed-owner",
        expected_state=MoveJobState.PREPARED,
        to_state=MoveJobState.FAILED,
        reason="injected prepared failure",
        now=now,
        lease_expires_at=lease,
        updates={"terminal_reason": "injected prepared failure"},
    )


def test_scans_do_not_change_placement_at_any_active_move_transition(
    tmp_path: Path,
    catalog_handles: CatalogHandles,
) -> None:
    catalog_kind, move_catalog, scan_catalog_store = catalog_handles
    hot = PosixDriver(str(tmp_path / f"{catalog_kind}-hot"))
    warm = PosixDriver(str(tmp_path / f"{catalog_kind}-warm"))
    data = b"one logical object"
    hot.put_object("bucket", "object", data)
    move_catalog.upsert("bucket", "object", len(data), "hot", {"revision": 1})
    observed_states: list[MoveJobState] = []

    def scan_at_transition(job) -> None:
        if job.state.terminal:
            return
        before = scan_catalog_store.get(job.bucket, job.key)
        assert before is not None
        expected = (before.tier, before.size, dict(before.metadata))

        # Both physical copies are transient while a move job is live. Scans
        # may observe them, but neither may publish placement.
        assert scan_catalog(
            tier="hot", bucket=job.bucket, driver=hot, catalog=scan_catalog_store
        ) == []
        assert scan_catalog(
            tier="warm", bucket=job.bucket, driver=warm, catalog=scan_catalog_store
        ) == []

        after = scan_catalog_store.get(job.bucket, job.key)
        assert after is not None
        assert (after.tier, after.size, after.metadata) == expected
        observed_states.append(job.state)

    result = Mover(
        {"hot": hot, "warm": warm},
        move_catalog,
        transition_hook=scan_at_transition,
    ).move(
        "hot",
        "warm",
        "bucket",
        "object",
        idempotency_key=f"{catalog_kind}-state-scan",
    )

    assert result.verified is True
    assert observed_states == [
        MoveJobState.PREPARED,
        MoveJobState.TRANSFERRED,
        MoveJobState.VERIFIED,
        MoveJobState.COMMITTED,
        MoveJobState.CLEANUP,
    ]
    placement = move_catalog.get("bucket", "object")
    assert placement is not None
    assert placement.tier == "warm"


def test_failed_prepared_destination_without_generation_is_not_authoritative(
    tmp_path: Path,
    catalog_handles: CatalogHandles,
) -> None:
    catalog_kind, move_catalog, scan_catalog_store = catalog_handles
    hot = PosixDriver(str(tmp_path / f"{catalog_kind}-hot"))
    warm = PosixDriver(str(tmp_path / f"{catalog_kind}-warm"))
    source = b"valid source"
    hot.put_object("bucket", "object", source)
    warm.put_object("bucket", "object", b"unverified destination")
    move_catalog.upsert("bucket", "object", len(source), "hot")
    _record_failed_prepared_move(
        move_catalog, hot, idempotency_key=f"{catalog_kind}-failed-prepared"
    )

    failed = move_catalog.get_move_job(f"{catalog_kind}-failed-prepared")
    assert failed is not None
    assert failed.state == MoveJobState.FAILED
    assert failed.destination_generation is None
    assert scan_catalog(
        tier="warm", bucket="bucket", driver=warm, catalog=scan_catalog_store
    ) == []
    placement = move_catalog.get("bucket", "object")
    assert placement is not None
    assert placement.tier == "hot"


def test_newer_completed_move_supersedes_older_failed_scan_authority(
    tmp_path: Path,
    catalog_handles: CatalogHandles,
) -> None:
    catalog_kind, move_catalog, scan_catalog_store = catalog_handles
    hot = PosixDriver(str(tmp_path / f"{catalog_kind}-hot"))
    warm = PosixDriver(str(tmp_path / f"{catalog_kind}-warm"))
    source = b"later verified destination"
    hot.put_object("bucket", "object", source)
    move_catalog.upsert("bucket", "object", len(source), "hot")
    _record_failed_prepared_move(
        move_catalog, hot, idempotency_key=f"{catalog_kind}-old-failure"
    )

    Mover({"hot": hot, "warm": warm}, move_catalog).move(
        "hot",
        "warm",
        "bucket",
        "object",
        idempotency_key=f"{catalog_kind}-new-success",
    )
    results = scan_catalog(
        tier="warm", bucket="bucket", driver=warm, catalog=scan_catalog_store
    )

    assert [result.key for result in results] == ["object"]
    placement = move_catalog.get("bucket", "object")
    assert placement is not None
    assert placement.tier == "warm"


def test_source_scan_write_after_completed_cannot_restore_deleted_placement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalog_handles: CatalogHandles,
) -> None:
    catalog_kind, move_catalog, scan_catalog_store = catalog_handles
    hot = PosixDriver(str(tmp_path / f"{catalog_kind}-hot"))
    warm = PosixDriver(str(tmp_path / f"{catalog_kind}-warm"))
    data = b"source bytes observed before cleanup"
    hot.put_object("bucket", "object", data)
    move_catalog.upsert("bucket", "object", len(data), "hot")
    scan_write_ready = threading.Event()
    release_scan_write = threading.Event()
    original_upsert = scan_catalog_store.upsert_scan_observation

    def delayed_scan_write(*args, **kwargs):
        scan_write_ready.set()
        assert release_scan_write.wait(timeout=_COORDINATION_TIMEOUT)
        return original_upsert(*args, **kwargs)

    monkeypatch.setattr(
        scan_catalog_store, "upsert_scan_observation", delayed_scan_write
    )
    scan_results: list[object] = []
    scan_errors: list[BaseException] = []

    def run_scan() -> None:
        try:
            scan_results.append(
                scan_catalog(
                    tier="hot",
                    bucket="bucket",
                    driver=hot,
                    catalog=scan_catalog_store,
                )
            )
        except BaseException as error:
            scan_errors.append(error)

    scanner = threading.Thread(target=run_scan)
    scanner.start()
    try:
        assert scan_write_ready.wait(timeout=_COORDINATION_TIMEOUT)

        Mover({"hot": hot, "warm": warm}, move_catalog).move(
            "hot",
            "warm",
            "bucket",
            "object",
            idempotency_key=f"{catalog_kind}-source-overlap",
        )
        job = move_catalog.get_move_job(f"{catalog_kind}-source-overlap")
        assert job is not None
        assert job.state == MoveJobState.COMPLETED

        release_scan_write.set()
        scanner.join(timeout=_COORDINATION_TIMEOUT)
        assert scanner.is_alive() is False
        assert scan_errors == []
        assert scan_results == [[]]
        placement = move_catalog.get("bucket", "object")
        assert placement is not None
        assert placement.tier == "warm"
        with pytest.raises(FileNotFoundError):
            hot.stat_object("bucket", "object")
    finally:
        release_scan_write.set()
        scanner.join(timeout=_COORDINATION_TIMEOUT)


def test_destination_scan_write_after_failed_cannot_publish_corrupt_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalog_handles: CatalogHandles,
) -> None:
    catalog_kind, move_catalog, scan_catalog_store = catalog_handles
    hot = PosixDriver(str(tmp_path / f"{catalog_kind}-hot"))
    warm = PosixDriver(str(tmp_path / f"{catalog_kind}-warm"))
    original = b"valid source bytes"
    corrupt = bytes([original[0] ^ 0xFF]) + original[1:]
    hot.put_object("bucket", "object", original)
    move_catalog.upsert("bucket", "object", len(original), "hot")
    destination_corrupted = threading.Event()
    release_verification = threading.Event()

    def pause_after_corrupting_destination(job) -> None:
        if job.state == MoveJobState.TRANSFERRED:
            warm.put_object("bucket", "object", corrupt)
            destination_corrupted.set()
            assert release_verification.wait(timeout=_COORDINATION_TIMEOUT)

    move_errors: list[BaseException] = []
    mover = Mover(
        {"hot": hot, "warm": warm},
        move_catalog,
        transition_hook=pause_after_corrupting_destination,
    )

    def run_move() -> None:
        try:
            mover.move(
                "hot",
                "warm",
                "bucket",
                "object",
                idempotency_key=f"{catalog_kind}-destination-overlap",
            )
        except BaseException as error:
            move_errors.append(error)

    movement = threading.Thread(target=run_move)
    movement.start()
    scan_write_ready = threading.Event()
    release_scan_write = threading.Event()
    scanner: threading.Thread | None = None
    try:
        assert destination_corrupted.wait(timeout=_COORDINATION_TIMEOUT)
        original_upsert = scan_catalog_store.upsert_scan_observation

        def delayed_scan_write(*args, **kwargs):
            scan_write_ready.set()
            assert release_scan_write.wait(timeout=_COORDINATION_TIMEOUT)
            return original_upsert(*args, **kwargs)

        monkeypatch.setattr(
            scan_catalog_store, "upsert_scan_observation", delayed_scan_write
        )
        scan_results: list[object] = []
        scan_errors: list[BaseException] = []

        def run_scan() -> None:
            try:
                scan_results.append(
                    scan_catalog(
                        tier="warm",
                        bucket="bucket",
                        driver=warm,
                        catalog=scan_catalog_store,
                    )
                )
            except BaseException as error:
                scan_errors.append(error)

        scanner = threading.Thread(target=run_scan)
        scanner.start()
        assert scan_write_ready.wait(timeout=_COORDINATION_TIMEOUT)

        release_verification.set()
        movement.join(timeout=_COORDINATION_TIMEOUT)
        assert movement.is_alive() is False
        assert len(move_errors) == 1
        assert isinstance(move_errors[0], MoveVerificationError)
        failed = move_catalog.get_move_job(
            f"{catalog_kind}-destination-overlap"
        )
        assert failed is not None
        assert failed.state == MoveJobState.FAILED

        release_scan_write.set()
        scanner.join(timeout=_COORDINATION_TIMEOUT)
        assert scanner.is_alive() is False
        assert scan_errors == []
        assert scan_results == [[]]
        assert warm.get_object("bucket", "object") == corrupt
        assert hot.get_object("bucket", "object") == original
        placement = move_catalog.get("bucket", "object")
        assert placement is not None
        assert placement.tier == "hot"
    finally:
        release_verification.set()
        release_scan_write.set()
        movement.join(timeout=_COORDINATION_TIMEOUT)
        if scanner is not None:
            scanner.join(timeout=_COORDINATION_TIMEOUT)
