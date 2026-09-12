from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cognistore.core.catalog import Catalog, CatalogStore
from cognistore.core.move_jobs import MoveJobState
from cognistore.core.sqlite_catalog import SQLiteCatalog


@pytest.fixture(params=["memory", "sqlite"])
def catalog(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[CatalogStore]:
    if request.param == "memory":
        yield Catalog()
    else:
        with SQLiteCatalog(tmp_path / "catalog.db") as store:
            yield store


def _claim(catalog: CatalogStore, identity: str, bucket: str, key: str) -> None:
    catalog.claim_move_job(
        identity,
        src_tier="hot",
        dst_tier="warm",
        bucket=bucket,
        key=key,
        expected_size=1,
        source_metadata={"nested": {"labels": ["source"]}},
        owner_id="owner",
        now="2026-09-01T00:00:00.000000Z",
        lease_expires_at="2026-09-01T01:00:00.000000Z",
    )


def test_scoped_jobs_page_uses_stable_exclusive_id_cursor(catalog: CatalogStore) -> None:
    identities = ["move:é", "move:a", "move:\0z", "move:中", "move:Z", "move:🦆"]
    for index, identity in enumerate(identities):
        _claim(catalog, identity, "tenant-a", f"reports/{index}")
    _claim(catalog, "move:other-tenant", "tenant-b", "reports/0")
    _claim(catalog, "move:other-prefix", "tenant-a", "private/0")
    # Terminal jobs remain evidence and participate in the same stable order.
    catalog.transition_move_job(
        "move:a",
        owner_id="owner",
        expected_state=MoveJobState.PREPARED,
        to_state=MoveJobState.FAILED,
        now="2026-09-01T00:01:00.000000Z",
        lease_expires_at=None,
        reason="test failure",
    )

    seen: list[str] = []
    after_id = None
    while page := catalog.list_move_jobs_page(
        "tenant-a", "reports/", after_id=after_id, limit=2
    ):
        assert len(page) <= 2
        seen.extend(job.idempotency_key for job in page)
        assert all(job.bucket == "tenant-a" and job.key.startswith("reports/") for job in page)
        after_id = page[-1].idempotency_key

    assert seen == sorted(identities)
    assert catalog.list_move_jobs_page("absent") == []


def test_job_page_prefix_is_literal_and_snapshots_are_detached(catalog: CatalogStore) -> None:
    _claim(catalog, "included", "tenant", "literal%_\\\0/object")
    _claim(catalog, "excluded", "tenant", "literalZZ/object")
    page = catalog.list_move_jobs_page("tenant", "literal%_\\\0", limit=1)
    assert [job.idempotency_key for job in page] == ["included"]
    page[0].source_metadata["nested"]["labels"].append("mutated")
    assert catalog.list_move_jobs_page("tenant", "literal%_\\\0")[0].source_metadata == {
        "nested": {"labels": ["source"]}
    }


@pytest.mark.parametrize(
    ("bucket", "kwargs", "message"),
    [
        ("", {}, "bucket must be a non-empty string"),
        (None, {}, "bucket must be a non-empty string"),
        ("tenant", {"prefix": None}, "prefix must be a string"),
        ("tenant", {"after_id": 7}, "after_id must be a string or null"),
        ("tenant", {"limit": 0}, "limit must be a positive integer"),
        ("tenant", {"limit": True}, "limit must be a positive integer"),
        ("tenant", {"limit": 1.5}, "limit must be a positive integer"),
    ],
)
def test_job_page_rejects_invalid_scope_and_pagination(
    catalog: CatalogStore, bucket: Any, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        catalog.list_move_jobs_page(bucket, **kwargs)
