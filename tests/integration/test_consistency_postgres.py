from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from cognistore.core.consistency import ConsistencyScanner, ScanScope, export_report
from cognistore.core.move_jobs import MoveJobState
from cognistore.db import SQLCatalog
from cognistore.drivers.observed import ObservedStorageDriver
from cognistore.drivers.posix_driver import PosixDriver

pytestmark = pytest.mark.integration


def _claim(catalog: SQLCatalog, identity: str, bucket: str, key: str) -> None:
    catalog.claim_move_job(
        identity, src_tier="hot", dst_tier="warm", bucket=bucket, key=key,
        expected_size=4, source_metadata={"labels": ["fixture"]}, owner_id="owner",
        now="2020-01-01T00:00:00.000000Z", lease_expires_at="2020-01-01T00:01:00.000000Z",
    )


def _snapshot(catalog: SQLCatalog) -> dict[str, list[str]]:
    """Snapshot every catalog table, including audit/access and fence rows."""
    with catalog.engine.connect() as connection:
        quote = connection.dialect.identifier_preparer.quote
        return {
            table: list(connection.exec_driver_sql(
                f"SELECT to_jsonb(entry)::text FROM {quote(table)} AS entry ORDER BY 1"
            ).scalars())
            for table in sa.inspect(connection).get_table_names()
        }


def test_postgres_move_job_pages_preserve_binary_order_and_namespace_scope(postgres_dsn: str) -> None:
    identities = ["move:é", "move:中", "move:\0a", "move:a", "move:Z", "move:🦆"]
    prefix = "literal%_\\\0/"
    with SQLCatalog(postgres_dsn) as catalog:
        for index, identity in enumerate(identities):
            _claim(catalog, identity, "tenant-a", prefix + str(index))
        _claim(catalog, "other-bucket", "tenant-b", prefix + "hidden")
        _claim(catalog, "other-prefix", "tenant-a", "literalZZ/hidden")

    with SQLCatalog(postgres_dsn, read_only=True) as catalog:
        seen: list[str] = []
        cursor = None
        while page := catalog.list_move_jobs_page("tenant-a", prefix, after_id=cursor, limit=2):
            assert len(page) <= 2
            assert all(job.bucket == "tenant-a" and job.key.startswith(prefix) for job in page)
            seen.extend(job.idempotency_key for job in page)
            cursor = page[-1].idempotency_key
        assert seen == sorted(identities)
        assert catalog.list_move_jobs_page("unknown") == []


def test_postgres_consistency_resume_and_export_leave_source_database_unchanged(
    postgres_dsn: str, tmp_path: Path
) -> None:
    bucket, prefix, tenant = "tenant-a-bucket", "objects/", "tenant-a"
    drivers = {tier: PosixDriver(str(tmp_path / tier)) for tier in ("hot", "warm")}
    digest = hashlib.sha256(b"data").hexdigest()
    with SQLCatalog(postgres_dsn) as catalog:
        for index in range(5):
            key = prefix + str(index)
            catalog.upsert(bucket, key, 4, "hot", {"sha256": digest, "sample_len": 4})
            drivers["hot"].put_object(bucket, key, b"evil" if index == 1 else b"data")
        _claim(catalog, "move:destination-missing", bucket, prefix + "0")
        catalog.transition_move_job(
            "move:destination-missing", owner_id="owner", expected_state=MoveJobState.PREPARED,
            to_state=MoveJobState.TRANSFERRED, reason="fixture transfer",
            now="2020-01-01T00:00:01.000000Z", lease_expires_at="2020-01-01T00:01:00.000000Z",
            updates={"source_size": 4, "source_checksum": digest, "transferred_size": 4},
        )
        catalog.upsert("tenant-b-bucket", prefix + "secret", 4, "hot", {"sha256": digest})
        catalog.upsert(bucket, "outside/secret", 4, "hot", {"sha256": digest})
        before = _snapshot(catalog)
    drivers["warm"].put_object(bucket, prefix + "untracked", b"data")

    class NoAccessWrites:
        def append_access_event(self, event: Any) -> Any:
            raise AssertionError("a maintenance scan must not persist access events")

    observed = {tier: ObservedStorageDriver(driver, NoAccessWrites(), tier=tier)
                for tier, driver in drivers.items()}
    report = tmp_path / "report.sqlite3"
    with SQLCatalog(postgres_dsn, read_only=True) as catalog:
        scanner = ConsistencyScanner(
            catalog, observed, ScanScope(tenant, bucket, prefix, ("hot", "warm")),
            binding_id="postgres-fixture", requests_per_second=1e9,
            bytes_per_second=1e12, page_size=2,
        )
        summary = scanner.run(report, max_items=2)
        assert not summary["complete"]
        for _ in range(100):
            summary = scanner.run(report, resume=True, max_items=2)
            if summary["complete"]:
                break
        assert summary["complete"] and summary["read_only"]
        assert summary["checked"] == summary["inventory_keys"] == 6
        assert {"missing_destination", "checksum_mismatch", "partial_job", "untracked_object"} <= {
            *summary["reason_counts"]
        }
        assert _snapshot(catalog) == before

    output = io.StringIO()
    export_report(report, output, tenant_id=tenant, binding_id="postgres-fixture")
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert "secret" not in output.getvalue()
    assert {"consistency.started", "consistency.resumed", "consistency.completed",
            "consistency.exported"} <= {
        row["event_type"] for row in rows if row["type"] == "audit"
    }
    with SQLCatalog(postgres_dsn, read_only=True) as catalog:
        assert _snapshot(catalog) == before
