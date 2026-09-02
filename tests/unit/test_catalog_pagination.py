from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from cognistore.core.catalog import Catalog, CatalogStore, ObjectRecord
from cognistore.core.sqlite_catalog import SQLiteCatalog


@pytest.fixture(params=["memory", "sqlite"])
def paged_catalog(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[CatalogStore]:
    if request.param == "memory":
        yield Catalog()
        return

    with SQLiteCatalog(tmp_path / "catalog.db") as catalog:
        yield catalog


def _seed(catalog: CatalogStore) -> None:
    for bucket, key, tier in (
        ("bucket", "report:04", "warm"),
        ("bucket", "report:01", "hot"),
        ("other", "report:00", "hot"),
        ("bucket", "report:03", "hot"),
        ("bucket", "report:02", "warm"),
        ("bucket", "literal%_\\:01", "hot"),
        ("bucket", "literalXX:02", "hot"),
    ):
        catalog.upsert(
            bucket,
            key,
            size=len(key),
            tier=tier,
            metadata={"nested": {"labels": [key]}},
        )


def test_list_page_has_backend_neutral_keyset_and_tier_semantics(
    paged_catalog: CatalogStore,
) -> None:
    _seed(paged_catalog)

    first = paged_catalog.list_page("bucket", "report:", limit=2)
    second = paged_catalog.list_page(
        "bucket",
        "report:",
        after_key=first[-1].key,
        limit=2,
    )
    exhausted = paged_catalog.list_page(
        "bucket",
        "report:",
        after_key=second[-1].key,
        limit=2,
    )

    assert [record.key for record in first] == ["report:01", "report:02"]
    assert [record.key for record in second] == ["report:03", "report:04"]
    assert exhausted == []
    assert [
        record.key
        for record in paged_catalog.list_page(
            "bucket",
            "report:",
            limit=2,
            tier="hot",
        )
    ] == ["report:01", "report:03"]
    assert paged_catalog.list_page(
        "bucket",
        "report:",
        after_key="report:03",
        limit=2,
        tier="hot",
    ) == []


def test_list_page_treats_prefix_as_literal_and_returns_detached_records(
    paged_catalog: CatalogStore,
) -> None:
    _seed(paged_catalog)

    records = paged_catalog.list_page("bucket", "literal%_\\:", limit=10)
    expected = ObjectRecord(
        bucket="bucket",
        key="literal%_\\:01",
        size=len("literal%_\\:01"),
        tier="hot",
        metadata={"nested": {"labels": ["literal%_\\:01"]}},
    )
    assert records == [expected]

    records[0].tier = "cold"
    records[0].metadata["nested"]["labels"].append("mutated")  # type: ignore[index,union-attr]
    assert paged_catalog.list_page("bucket", "literal%_\\:", limit=10) == [
        expected
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"after_key": 1}, "after_key must be a string or null"),
        ({"limit": 0}, "limit must be a positive integer"),
        ({"limit": True}, "limit must be a positive integer"),
        ({"tier": 1}, "tier must be a string or null"),
    ],
)
def test_list_page_rejects_invalid_keyset_arguments_consistently(
    paged_catalog: CatalogStore,
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=f"^{message}$"):
        paged_catalog.list_page("bucket", **kwargs)  # type: ignore[arg-type]
