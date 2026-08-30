from __future__ import annotations

from pathlib import Path

from cognistore.core.audit import AuditRetentionPolicy
from cognistore.db.catalog import SQLCatalog


class SQLiteCatalog(SQLCatalog):
	"""Backward-compatible SQLite name backed by the shared SQL DAL."""

	def __init__(
		self,
		db_path: str | Path,
		*,
		read_only: bool = False,
		audit_retention: AuditRetentionPolicy | None = None,
	) -> None:
		super().__init__(
			db_path,
			read_only=read_only,
			audit_retention=audit_retention,
		)
