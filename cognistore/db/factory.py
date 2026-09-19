from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

from cognistore.core.audit import AuditRetentionPolicy
from cognistore.core.catalog import CatalogStore

from .catalog import SQLCatalog
from .engine import is_database_url, normalize_database_url


def catalog_locator_is_persistent(locator: str | Path | None) -> bool:
    if locator is None:
        return False
    url = normalize_database_url(locator)
    if not url.startswith("sqlite+pysqlite://"):
        return True
    parsed = sa.engine.make_url(url)
    database = parsed.database
    if database is None or database in ("", ":memory:"):
        return False
    lowered_database = database.lower()
    return not (
        lowered_database.startswith("file::memory:")
        or str(parsed.query.get("mode", "")).lower() == "memory"
    )


def open_catalog(
    locator: str | Path,
    *,
    read_only: bool = False,
    migrate: bool = True,
    audit_retention: AuditRetentionPolicy | None = None,
    tenant_id: str = "default",
) -> CatalogStore:
    return SQLCatalog(
        locator,
        read_only=read_only,
        migrate=migrate,
        audit_retention=audit_retention,
        tenant_id=tenant_id,
    )


def catalog_locator_is_postgres(locator: str | Path) -> bool:
    return is_database_url(locator) and normalize_database_url(locator).startswith(
        "postgresql+psycopg://"
    )


def catalog_locator_is_sqlite(locator: str | Path) -> bool:
    return normalize_database_url(locator).startswith("sqlite+pysqlite://")


def sqlite_catalog_path(locator: str | Path) -> Path | None:
    url = normalize_database_url(locator)
    if not url.startswith("sqlite+pysqlite://") or not catalog_locator_is_persistent(locator):
        return None
    database = sa.engine.make_url(url).database
    return None if database is None else Path(database)
