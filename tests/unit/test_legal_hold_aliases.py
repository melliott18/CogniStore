"""Hold aliases cannot bypass the preserved logical object or corrupt lookup state."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from cognistore.core.audit import AuditContext
from cognistore.core.catalog import Catalog
from cognistore.core.legal_holds import LegalHoldError
from cognistore.db import SQLCatalog
from cognistore.db.catalog import CatalogSchemaError
from cognistore.db.schema import legal_holds


@pytest.fixture(params=["memory", "sqlite"])
def catalog(request, tmp_path):
    store = Catalog() if request.param == "memory" else SQLCatalog(tmp_path / "catalog.db")
    yield store
    if isinstance(store, SQLCatalog):
        store.close()


def actor():
    return AuditContext("alias-case", "user", "hold-manager")


@pytest.mark.parametrize("bucket,key", [
    ("docs", "records/evidence.txt"),
    ("DOCS", "Records/Evidence.txt"),
    ("docs", "Records//Evidence.txt"),
    ("docs", "Records/./Evidence.txt"),
    ("docs", "other/../Records/Evidence.txt"),
    ("docs", "Records\\Evidence.txt"),
    ("docs/.", "Records/Evidence.txt"),
])
def test_exact_hold_rejects_case_and_path_aliases(catalog, bucket, key):
    hold = catalog.place_legal_hold(
        "docs", key="Records/Evidence.txt", reason="Preserve", context=actor(),
    )
    assert catalog.list_legal_holds(bucket=bucket, key=key, active_only=True) == [hold]
    with pytest.raises(LegalHoldError):
        catalog.upsert(bucket, key, size=1, tier="hot")
    with pytest.raises(LegalHoldError):
        catalog.delete(bucket, key)
    assert hold.bucket == "docs" and hold.key == "Records/Evidence.txt"
    assert catalog.list_legal_holds(bucket="other", key=key, active_only=True) == []


@pytest.mark.parametrize("scope", [{"key": "Récords/Évidence.txt"}, {"prefix": "Récords/"}])
def test_unicode_aliases_and_prefix_boundaries_are_protected(catalog, scope):
    hold = catalog.place_legal_hold("DÓCS", reason="Preserve", context=actor(), **scope)
    bucket, key = "do\u0301cs", "re\u0301cords/e\u0301vidence.txt"
    assert catalog.list_legal_holds(bucket=bucket, key=key) == [hold]
    with pytest.raises(LegalHoldError):
        catalog.upsert(bucket, key, size=1, tier="hot")
    assert catalog.list_legal_holds(bucket=bucket, key="récords-other/évidence.txt") == []
    assert catalog.list_legal_holds()[0].to_dict() == hold.to_dict()


def test_prefix_keeps_literal_dot_component_coverage(catalog):
    # Cloud object keys can contain dot components even though a filesystem
    # cannot. Alias comparison must add protection, never narrow the literal
    # prefix a caller requested.
    hold = catalog.place_legal_hold("docs", prefix="records/", reason="Preserve", context=actor())
    assert catalog.list_legal_holds(bucket="docs", key="records/../elsewhere") == [hold]
    assert catalog.list_legal_holds(bucket="DOCS", key="RECORDS//item") == [hold]
    assert catalog.list_legal_holds(bucket="docs", key="records-old/item") == []


@pytest.mark.parametrize("field,value", [
    ("hold_id", uuid4()),
    ("tenant_id", "other"),
    ("bucket", "other"),
    ("object_key", "other"),
    ("prefix", "other/"),
    ("created_at", "2000-01-01T00:00:00.000000Z"),
    ("released_at", "2000-01-01T00:00:00.000000Z"),
])
def test_inconsistent_sql_hold_columns_fail_closed(tmp_path, field, value):
    with SQLCatalog(tmp_path / "catalog.db") as catalog:
        catalog.upsert("docs", "evidence.txt", size=8, tier="hot")
        hold = catalog.place_legal_hold(
            "docs", reason="Preserve", context=actor(),
            **({"prefix": "evidence"} if field == "prefix" else {"key": "evidence.txt"}),
        )
        with catalog.engine.begin() as connection:
            connection.execute(sa.update(legal_holds).where(
                legal_holds.c.hold_id == UUID(hold.hold_id),
            ).values(**{field: value}))
        with pytest.raises(CatalogSchemaError, match="inconsistent"):
            catalog.list_legal_holds(bucket="docs", key="evidence.txt", active_only=True)
        with pytest.raises(CatalogSchemaError, match="inconsistent"):
            catalog.delete("docs", "evidence.txt")
        assert catalog.get("docs", "evidence.txt").size == 8
