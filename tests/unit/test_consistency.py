from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from cognistore.core.catalog import Catalog, CatalogStore
from cognistore.core.consistency import (
    ConsistencyScanner,
    ScanScope,
    _RateLimiter,
    export_report,
    report_summary,
)
from cognistore.core.move_jobs import MoveJobState
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.observed import ObservedStorageDriver
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.drivers.storage_driver import ObjectGenerationMismatchError, StorageDriver

BUCKET = "tenant-bucket"
PREFIX = "objects/"
TENANT = "tenant-a"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _drivers(tmp_path: Path, *tiers: str) -> dict[str, StorageDriver]:
    return {tier: PosixDriver(str(tmp_path / tier), chunk_size=64 * 1024) for tier in tiers}


def _scanner(
    catalog: CatalogStore,
    drivers: dict[str, StorageDriver],
    *,
    tenant: str = TENANT,
    prefix: str = PREFIX,
    binding: str = "test",
) -> ConsistencyScanner:
    return ConsistencyScanner(
        catalog,
        drivers,
        ScanScope(tenant, BUCKET, prefix, tuple(drivers)),
        binding_id=binding,
        requests_per_second=1e9,
        bytes_per_second=1e12,
        page_size=2,
    )


def _record(catalog: CatalogStore, key: str, data: bytes, tier: str = "hot") -> None:
    catalog.upsert(BUCKET, key, size=len(data), tier=tier,
                   metadata={"sha256": _digest(data), "sample_len": len(data)})


def _job(
    catalog: CatalogStore,
    key: str,
    *,
    state: MoveJobState = MoveJobState.PREPARED,
    live: bool = False,
) -> None:
    lease = "2999-01-01T00:00:00.000000Z" if live else "2020-01-01T00:00:00.000000Z"
    catalog.claim_move_job(
        f"move:{key}", src_tier="hot", dst_tier="warm", bucket=BUCKET, key=key,
        expected_size=4, source_metadata={}, owner_id="owner",
        now="2019-01-01T00:00:00.000000Z", lease_expires_at=lease,
    )
    if state == MoveJobState.PREPARED:
        return
    catalog.transition_move_job(
        f"move:{key}", owner_id="owner", expected_state=MoveJobState.PREPARED,
        to_state=state, reason="fixture transfer", now="2019-01-01T00:01:00.000000Z",
        lease_expires_at=lease,
        updates={"transferred_size": 4, "source_size": 4, "source_checksum": _digest(b"data")},
    )


def _findings(path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(path) as connection:
        return [json.loads(row[0]) for row in connection.execute(
            "SELECT payload FROM findings ORDER BY sequence"
        )]


def _reasons(path: Path) -> set[str]:
    return {finding["reason_code"] for finding in _findings(path)}


def _audit(path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(path) as connection:
        return [json.loads(row[0]) for row in connection.execute(
            "SELECT payload FROM audit ORDER BY sequence"
        )]


def test_fixtures_classify_ticket_discrepancies_and_export_audited_report(tmp_path: Path) -> None:
    catalog = Catalog()
    drivers = _drivers(tmp_path, "hot", "warm")
    for leaf in ("missing-source", "missing-destination", "checksum", "size", "duplicate"):
        _record(catalog, PREFIX + leaf, b"data")
    _job(catalog, PREFIX + "missing-source")
    _job(catalog, PREFIX + "missing-destination", state=MoveJobState.TRANSFERRED)
    drivers["hot"].put_object(BUCKET, PREFIX + "missing-destination", b"data")
    drivers["hot"].put_object(BUCKET, PREFIX + "checksum", b"evil")
    drivers["hot"].put_object(BUCKET, PREFIX + "size", b"longer")
    for tier in drivers:
        drivers[tier].put_object(BUCKET, PREFIX + "duplicate", b"data")
    drivers["warm"].put_object(BUCKET, PREFIX + "untracked", b"data")

    report = tmp_path / "report.sqlite3"
    result = _scanner(catalog, drivers).run(report)
    assert result["complete"] and result["read_only"]
    assert {
        "missing_source", "missing_destination", "checksum_mismatch", "size_mismatch",
        "duplicate_placement", "partial_job", "untracked_object",
    } <= _reasons(report)
    assert all(finding["severity"] in {"info", "warning", "error"} for finding in _findings(report))
    assert not result["consistent"]

    output = io.StringIO()
    exported = export_report(report, output, tenant_id=TENANT, binding_id="test")
    assert exported["finding_count"] == result["finding_count"]
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[0]["type"] == "report"
    assert sum(row["type"] == "finding" for row in rows) == result["finding_count"]
    events = [row for row in rows if row["type"] == "audit"]
    assert {"consistency.started", "consistency.completed", "consistency.exported"} <= {
        event["event_type"] for event in events
    }
    assert all(event["details"]["tenant_id"] == TENANT for event in events)
    assert all(event["correlation_id"] == result["scan_id"] for event in events)


def test_scan_and_export_preserve_sql_catalog_storage_and_access_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "catalog.sqlite3"
    drivers = _drivers(tmp_path, "hot", "warm")
    key = PREFIX + "clean"
    drivers["hot"].put_object(BUCKET, key, b"data")
    with SQLiteCatalog(database) as catalog:
        _record(catalog, key, b"data")
    before = database.read_bytes()
    storage = {tier: {
        path.relative_to(tmp_path / tier): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (tmp_path / tier).rglob("*") if path.is_file()
    } for tier in drivers}
    forbidden_calls: list[str] = []

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        forbidden_calls.append("write")
        raise AssertionError("consistency scans must not mutate source state")

    class NoAccessWrites:
        append_access_event = staticmethod(forbidden)

    for driver in drivers.values():
        for method in ("put_object", "put_object_stream", "delete_object",
                       "delete_object_if_generation", "ensure_object_durable"):
            monkeypatch.setattr(driver, method, forbidden)
    observed = {tier: ObservedStorageDriver(driver, NoAccessWrites(), tier=tier)
                for tier, driver in drivers.items()}
    report = tmp_path / "report.sqlite3"
    with SQLiteCatalog(database, read_only=True) as catalog:
        result = _scanner(catalog, observed).run(report)
    assert result["complete"] and result["consistent"]
    export_report(report, io.StringIO(), tenant_id=TENANT)
    assert not forbidden_calls
    assert database.read_bytes() == before
    assert storage == {tier: {
        path.relative_to(tmp_path / tier): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (tmp_path / tier).rglob("*") if path.is_file()
    } for tier in drivers}


def test_bounded_resume_matches_uninterrupted_scan_and_excludes_other_scopes(tmp_path: Path) -> None:
    catalog = Catalog()
    drivers = _drivers(tmp_path, "hot", "warm")
    for index in range(9):
        key = PREFIX + str(index)
        _record(catalog, key, b"data")
        if index % 2:
            drivers["hot"].put_object(BUCKET, key, b"evil")
    catalog.upsert("another-bucket", PREFIX + "secret", 1, "hot")
    catalog.upsert(BUCKET, "outside/secret", 1, "hot")
    drivers["hot"].put_object("another-bucket", PREFIX + "secret", b"x")
    drivers["hot"].put_object(BUCKET, "outside/secret", b"x")
    whole = tmp_path / "whole.sqlite3"
    resumed = tmp_path / "resumed.sqlite3"
    expected = _scanner(catalog, drivers).run(whole)
    scanner = _scanner(catalog, drivers)
    result = scanner.run(resumed, max_items=1)
    assert not result["complete"]
    runs = 1
    while not result["complete"]:
        previous_checked = result["checked"]
        result = scanner.run(resumed, resume=True, max_items=1)
        assert result["checked"] - previous_checked <= 1
        runs += 1
        assert runs < 100
    assert result["reason_counts"] == expected["reason_counts"]
    assert result["checked"] == expected["checked"] == 9
    assert result["inventory_keys"] == 9
    assert len(_findings(resumed)) == len(_findings(whole))
    assert "secret" not in json.dumps(_findings(resumed))
    assert scanner.run(resumed, resume=True)["finding_count"] == result["finding_count"]


def test_scope_and_source_binding_fail_closed_for_resume_read_and_export(tmp_path: Path) -> None:
    catalog = Catalog()
    drivers = _drivers(tmp_path, "hot", "warm")
    _record(catalog, PREFIX + "missing", b"data")
    report = tmp_path / "report.sqlite3"
    _scanner(catalog, drivers).run(report, max_items=1)
    before = report.read_bytes()
    for scanner in (
        _scanner(catalog, drivers, tenant="tenant-b"),
        _scanner(catalog, drivers, prefix="another/"),
        _scanner(catalog, drivers, binding="different-source"),
    ):
        with pytest.raises(ValueError):
            scanner.run(report, resume=True)
    with pytest.raises(ValueError):
        report_summary(report, tenant_id="tenant-b")
    with pytest.raises(ValueError):
        report_summary(report, tenant_id=TENANT, binding_id="different-source")
    output = io.StringIO()
    with pytest.raises(ValueError):
        export_report(report, output, tenant_id="tenant-b")
    assert output.getvalue() == ""
    assert report.read_bytes() == before


@pytest.mark.parametrize("bucket", ["shared/tenant-b", "shared\\tenant-b", ".", "..", "shared\0b"])
def test_standalone_scanner_scope_rejects_bucket_namespace_aliases(
    tmp_path: Path, bucket: str
) -> None:
    with pytest.raises(ValueError, match="single path component"):
        ConsistencyScanner(
            Catalog(), _drivers(tmp_path, "hot"), ScanScope(TENANT, bucket, "", ("hot",)),
            binding_id="test",
        )


def test_hash_reads_the_complete_object_and_never_substitutes_etag(tmp_path: Path) -> None:
    catalog = Catalog()
    drivers = _drivers(tmp_path, "hot")
    prefix = b"same prefix" * 100_000
    key = PREFIX + "tail-corruption"
    _record(catalog, key, prefix + b"good")
    drivers["hot"].put_object(BUCKET, key, prefix + b"evil")
    etag_key = PREFIX + "etag-only"
    catalog.upsert(BUCKET, etag_key, 4, "hot", {"etag": _digest(b"data")})
    drivers["hot"].put_object(BUCKET, etag_key, b"data")
    legacy_key = PREFIX + "legacy-sample"
    catalog.upsert(BUCKET, legacy_key, 4, "hot", {"sha256": _digest(b"da"), "sample_len": 2})
    drivers["hot"].put_object(BUCKET, legacy_key, b"data")
    report = tmp_path / "report.sqlite3"
    _scanner(catalog, drivers).run(report)
    findings = _findings(report)
    assert any(row["reason_code"] == "checksum_mismatch" and row.get("key", row.get("object_key")) == key
               for row in findings)
    assert "checksum_unavailable" in _reasons(report)
    legacy = [row for row in findings if row["key"] == legacy_key]
    assert any(row["reason_code"] == "checksum_unavailable" for row in legacy)
    assert not any(row["reason_code"] == "checksum_mismatch" for row in legacy)


def test_generation_change_is_uncertain_evidence_not_corruption(tmp_path: Path) -> None:
    class ChangingDriver(PosixDriver):
        @contextmanager
        def open_object_reader_if_generation(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
            raise ObjectGenerationMismatchError("changed during scan")
            yield  # pragma: no cover

    catalog = Catalog()
    driver = ChangingDriver(str(tmp_path / "hot"))
    key = PREFIX + "changing"
    _record(catalog, key, b"data")
    driver.put_object(BUCKET, key, b"evil")
    report = tmp_path / "report.sqlite3"
    _scanner(catalog, {"hot": driver}).run(report)
    assert "object_changed" in _reasons(report)
    assert not {"checksum_mismatch", "missing_placement"} & _reasons(report)


def test_permission_failure_is_never_reported_as_missing(tmp_path: Path) -> None:
    class DeniedDriver(PosixDriver):
        def stat_object(self, bucket: str, key: str) -> dict[str, Any]:
            raise PermissionError("backend access denied")

    catalog = Catalog()
    driver = DeniedDriver(str(tmp_path / "hot"))
    _record(catalog, PREFIX + "denied", b"data")
    report = tmp_path / "report.sqlite3"
    _scanner(catalog, {"hot": driver}).run(report)
    assert "backend_error" in _reasons(report)
    assert not {"missing_placement", "missing_source", "missing_destination"} & _reasons(report)


def test_job_only_inventory_finds_missing_source_without_catalog_or_storage_rows(tmp_path: Path) -> None:
    catalog = Catalog()
    drivers = _drivers(tmp_path, "hot", "warm")
    _job(catalog, PREFIX + "orphan-job")
    report = tmp_path / "report.sqlite3"
    result = _scanner(catalog, drivers).run(report)
    assert result["checked"] == result["inventory_keys"] == 1
    assert {"partial_job", "missing_source"} <= _reasons(report)
    assert all(row["severity"] == "warning" for row in _findings(report)
               if row["reason_code"] == "partial_job")


def test_backend_aliases_do_not_count_as_duplicate_physical_placements(tmp_path: Path) -> None:
    catalog = Catalog()
    driver = PosixDriver(str(tmp_path / "same-root"))
    key = PREFIX + "alias"
    driver.put_object(BUCKET, key, b"data")
    _record(catalog, key, b"data", tier="warm")
    report = tmp_path / "report.sqlite3"
    result = _scanner(catalog, {"hot": driver, "warm": PosixDriver(str(driver.base))}).run(report)
    assert result["consistent"]
    assert "duplicate_placement" not in _reasons(report)


def test_scope_reports_uninspected_catalog_placement_as_incomplete(tmp_path: Path) -> None:
    catalog = Catalog()
    _record(catalog, PREFIX + "outside-tier", b"data", tier="cold")
    report = tmp_path / "report.sqlite3"
    result = _scanner(catalog, _drivers(tmp_path, "hot", "warm")).run(report)
    assert result["complete"] and not result["consistent"]
    assert "scope_incomplete" in _reasons(report)
    assert "missing_placement" not in _reasons(report)


@pytest.mark.parametrize(("extra_copy", "duplicate"), [(False, False), (True, True)])
def test_live_move_only_suppresses_expected_source_destination_duplicates(
    tmp_path: Path, extra_copy: bool, duplicate: bool
) -> None:
    catalog = Catalog()
    drivers = _drivers(tmp_path, "hot", "warm", "cold")
    key = PREFIX + "moving"
    _record(catalog, key, b"data")
    _job(catalog, key, state=MoveJobState.TRANSFERRED, live=True)
    for tier in ("hot", "warm", "cold") if extra_copy else ("hot", "warm"):
        drivers[tier].put_object(BUCKET, key, b"data")
    report = tmp_path / "report.sqlite3"
    _scanner(catalog, drivers).run(report)
    assert ("duplicate_placement" in _reasons(report)) is duplicate
    partial = [row for row in _findings(report) if row["reason_code"] == "partial_job"]
    assert partial and all(row["severity"] == "info" for row in partial)


def test_report_never_overwrites_existing_file_and_failed_export_is_not_audited(tmp_path: Path) -> None:
    catalog = Catalog()
    drivers = _drivers(tmp_path, "hot")
    existing = tmp_path / "existing"
    existing.write_bytes(b"important user data")
    with pytest.raises((ValueError, FileExistsError)):
        _scanner(catalog, drivers).run(existing)
    assert existing.read_bytes() == b"important user data"
    report = tmp_path / "report.sqlite3"
    _scanner(catalog, drivers).run(report)
    before = _audit(report)

    class BrokenOutput(io.StringIO):
        def flush(self) -> None:
            raise OSError("output disk full")

    with pytest.raises(OSError, match="output disk full"):
        export_report(report, BrokenOutput(), tenant_id=TENANT)
    assert _audit(report) == before


def test_report_journal_cannot_alias_source_catalog_before_any_writes(tmp_path: Path) -> None:
    report = tmp_path / "report.sqlite3"
    database = Path(str(report) + "-journal")
    with SQLiteCatalog(database) as catalog:
        _record(catalog, PREFIX + "preserved", b"data")
    before = database.read_bytes()
    with SQLiteCatalog(database, read_only=True) as catalog:
        with pytest.raises(ValueError, match="catalog|journal|alias"):
            _scanner(catalog, _drivers(tmp_path, "hot")).run(report)
    assert not report.exists()
    assert database.read_bytes() == before


def test_interrupted_storage_inventory_resumes_from_last_committed_page(tmp_path: Path) -> None:
    class InterruptedDriver(PosixDriver):
        fail_once = True

        def list_objects_page(self, bucket: str, prefix: str = "", *,
                              cursor: str | None = None, limit: int = 1000) -> Any:
            if cursor is not None and self.fail_once:
                self.fail_once = False
                raise ConnectionError("listing temporarily unavailable")
            return super().list_objects_page(bucket, prefix, cursor=cursor, limit=limit)

    catalog = Catalog()
    driver = InterruptedDriver(str(tmp_path / "hot"))
    for index in range(5):
        key = PREFIX + str(index)
        driver.put_object(BUCKET, key, b"data")
        _record(catalog, key, b"data")
    report = tmp_path / "report.sqlite3"
    scanner = _scanner(catalog, {"hot": driver})
    with pytest.raises(ConnectionError, match="temporarily unavailable"):
        scanner.run(report)
    interrupted = report_summary(report, tenant_id=TENANT)
    assert not interrupted["complete"]
    assert "consistency.failed" in {event["event_type"] for event in _audit(report)}
    resumed = scanner.run(report, resume=True)
    assert resumed["scan_id"] == interrupted["scan_id"]
    assert resumed["complete"] and resumed["consistent"]
    assert resumed["checked"] == resumed["inventory_keys"] == 5


def test_scan_paces_both_backend_operations_and_complete_stream_bytes(tmp_path: Path) -> None:
    class FakeClock:
        now = 0.0

        def __init__(self) -> None:
            self.sleeps: list[float] = []

        def clock(self) -> float:
            return self.now

        def sleep(self, delay: float) -> None:
            assert delay > 0
            self.sleeps.append(delay)
            self.now += delay

    catalog = Catalog()
    drivers = _drivers(tmp_path, "hot")
    key, data = PREFIX + "large", b"x" * (2 * 1024 * 1024 + 5)
    _record(catalog, key, data)
    drivers["hot"].put_object(BUCKET, key, data)
    scanner = _scanner(catalog, drivers)
    operations, reads = FakeClock(), FakeClock()
    scanner.requests = _RateLimiter(2, clock=operations.clock, sleep=operations.sleep)
    scanner.bytes = _RateLimiter(1024, clock=reads.clock, sleep=reads.sleep)
    result = scanner.run(tmp_path / "report.sqlite3")
    assert result["consistent"]
    assert sum(operations.sleeps) >= 1.5
    assert sum(reads.sleeps) >= len(data) / 1024 - 0.01


@pytest.mark.parametrize("rate", [0, -1, float("nan"), float("inf"), True])
def test_rate_limit_rejects_unbounded_or_invalid_values(rate: float) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        _RateLimiter(rate)
