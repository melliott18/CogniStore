from __future__ import annotations

import hashlib
import io
import os
from contextlib import contextmanager
from dataclasses import replace

import pytest

from cognistore.core.catalog import Catalog
from cognistore.core.consistency import ConsistencyScanner, ScanScope, _RateLimiter
from cognistore.core.consistency_report import open_report, read_state
from cognistore.core.move_jobs import MoveJobState
from cognistore.drivers.observed import ObservedStorageDriver
from cognistore.drivers.posix_driver import PosixDriver


def scanner(tmp_path, catalog=None, driver=None):
    catalog = catalog or Catalog()
    driver = driver or PosixDriver(str(tmp_path / "hot"))
    return ConsistencyScanner(
        catalog, {"hot": driver}, ScanScope("tenant", "bucket", "", ("hot",)),
        binding_id="binding", requests_per_second=1e9, bytes_per_second=1e12,
    )


def test_report_hardlinked_into_storage_cannot_be_resumed(tmp_path):
    scan = scanner(tmp_path)
    report = tmp_path / "report.sqlite"
    scan.run(report, max_items=1)
    data_root = tmp_path / "hot" / "bucket"
    data_root.mkdir(parents=True)
    os.link(report, data_root / "stored-object")
    before = report.read_bytes()
    with pytest.raises(ValueError, match="alias"):
        scan.run(report, resume=True)
    assert report.read_bytes() == before


def test_unknown_existing_journal_is_never_overwritten(tmp_path):
    report = tmp_path / "report.sqlite"
    journal = tmp_path / "report.sqlite-journal"
    journal.write_bytes(b"preexisting user data")
    with pytest.raises(FileExistsError, match="sidecars"):
        scanner(tmp_path).run(report)
    assert not report.exists()
    assert journal.read_bytes() == b"preexisting user data"


def test_short_reads_are_throttled_by_actual_bytes(tmp_path):
    payload = b"a" * (1024 * 32)

    class ShortReader(io.BytesIO):
        def read(self, size=-1):
            return super().read(min(size, 1024))

    class ShortDriver(PosixDriver):
        @contextmanager
        def open_object_reader_if_generation(self, bucket, key, generation, range=None):
            yield ShortReader(payload)

    driver = ShortDriver(str(tmp_path / "hot"))
    driver.put_object("bucket", "data", payload)
    scan = scanner(tmp_path, driver=driver)
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    scan.bytes = _RateLimiter(1024, clock=lambda: now[0], sleep=sleep)
    result = scan._observe(driver, "data")
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert sum(sleeps) == pytest.approx(len(payload) / 1024)
    assert max(sleeps) <= 1


def test_failed_job_digest_is_not_reused_for_new_generation(tmp_path):
    scan = scanner(tmp_path)
    driver = scan.drivers["hot"]
    driver.put_object("bucket", "data", b"old")
    scan.catalog.upsert("bucket", "data", 3, "hot")
    job = scan.catalog.claim_move_job(
        "old-job", src_tier="hot", dst_tier="cold", bucket="bucket", key="data",
        expected_size=3, source_metadata=driver.stat_object("bucket", "data"),
        owner_id="test", now="2026-09-12T00:00:00Z",
        lease_expires_at="2999-01-01T00:00:00Z",
    )
    scan.catalog.transition_move_job(
        job.idempotency_key, expected_state=MoveJobState.PREPARED, to_state=MoveJobState.FAILED,
        owner_id="test", reason="test failure", now="2026-09-12T00:00:01Z",
        lease_expires_at=None,
        updates={"source_checksum": hashlib.sha256(b"old").hexdigest()},
    )
    driver.put_object("bucket", "data", b"new")
    findings = scan._inspect("data", {"hot"})
    assert not any(row["reason_code"] == "checksum_mismatch" for row in findings)
    assert any(row["reason_code"] == "checksum_unavailable" for row in findings)


def test_completed_job_digest_is_used_only_for_its_generation(tmp_path, monkeypatch):
    scan = scanner(tmp_path)
    driver = scan.drivers["hot"]
    driver.put_object("bucket", "data", b"actual")
    scan.catalog.upsert("bucket", "data", 6, "cold")
    job = scan.catalog.claim_move_job(
        "old-job", src_tier="cold", dst_tier="hot", bucket="bucket", key="data",
        expected_size=6, source_metadata={}, owner_id="test", now="2026-09-12T00:00:00Z",
        lease_expires_at="2999-01-01T00:00:00Z",
    )
    scan.catalog.upsert("bucket", "data", 6, "hot")
    completed = replace(job, state=MoveJobState.COMPLETED,
                        destination_generation=driver.object_generation("bucket", "data"),
                        destination_checksum=hashlib.sha256(b"wanted").hexdigest())
    monkeypatch.setattr(scan.catalog, "get_move_job", lambda _id: completed)
    findings = scan._inspect("data", {"hot"})
    assert any(row["reason_code"] == "checksum_mismatch" for row in findings)
    driver.put_object("bucket", "data", b"newest")
    findings = scan._inspect("data", {"hot"})
    assert not any(row["reason_code"] == "checksum_mismatch" for row in findings)
    assert any(row["reason_code"] == "checksum_unavailable" for row in findings)


def test_report_state_records_rate_settings(tmp_path):
    report = tmp_path / "scan.sqlite"
    scan = scanner(tmp_path)
    scan.run(report, max_items=1)
    with open_report(report, read_only=True) as connection:
        state = read_state(connection)
    assert state["requests_per_second"] == scan.requests_per_second
    different = ConsistencyScanner(
        scan.catalog, scan.drivers, scan.scope, binding_id="binding", requests_per_second=15,
    )
    with pytest.raises(ValueError, match="options"):
        different.run(report, resume=True)


def test_mixed_observed_and_raw_aliases_are_one_physical_placement(tmp_path):
    catalog = Catalog()
    raw = PosixDriver(str(tmp_path / "hot"))
    data = b"data"
    raw.put_object("bucket", "key", data)
    catalog.upsert("bucket", "key", len(data), "hot",
                   {"sha256": hashlib.sha256(data).hexdigest(), "sample_len": len(data)})
    observed = ObservedStorageDriver(raw, catalog, tier="hot")
    scan = ConsistencyScanner(
        catalog, {"hot": observed, "warm": PosixDriver(str(tmp_path / "hot"))},
        ScanScope("tenant", "bucket", "", ("hot", "warm")), binding_id="binding",
        requests_per_second=1e9, bytes_per_second=1e12,
    )
    result = scan.run(tmp_path / "scan.sqlite")
    assert result["consistent"]
    assert scan.aliases == {"hot": "hot", "warm": "hot"}


def test_consistency_rejects_nested_posix_roots(tmp_path):
    from cognistore.cli.consistency_commands import _driver_roots

    config = tmp_path / "drivers.json"
    import json
    config.write_text(json.dumps({"tiers": {
        "hot": {"driver": "posix", "path": str(tmp_path / "data")},
        "warm": {"driver": "posix", "path": str(tmp_path / "data" / "nested")},
    }}))
    with pytest.raises(ValueError, match="disjoint"):
        _driver_roots(config)
