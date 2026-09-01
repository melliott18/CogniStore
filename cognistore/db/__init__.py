"""Migration-managed control-plane persistence."""

from .catalog import (
    CatalogSchemaError,
    CatalogSchemaNotInstalledError,
    CatalogSchemaOutdatedError,
    SQLCatalog,
)
from .embeddings import PgVectorEmbeddingStore
from .factory import (
    catalog_locator_is_persistent,
    catalog_locator_is_postgres,
    catalog_locator_is_sqlite,
    open_catalog,
    sqlite_catalog_path,
)
from .migrations import MigrationManager

__all__ = [
    "CatalogSchemaError",
    "CatalogSchemaNotInstalledError",
    "CatalogSchemaOutdatedError",
    "MigrationManager",
    "PgVectorEmbeddingStore",
    "SQLCatalog",
    "catalog_locator_is_persistent",
    "catalog_locator_is_postgres",
    "catalog_locator_is_sqlite",
    "open_catalog",
    "sqlite_catalog_path",
]
