from __future__ import annotations

import threading
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection, Engine

from cognistore.core.audit import AuditRetentionPolicy

# Alembic installs module-level proxy objects while a command runs, so command
# environments cannot safely overlap within one Python process even when they
# target different databases.
_ALEMBIC_COMMAND_LOCK = threading.RLock()


class MigrationManager:
    """Run and inspect CogniStore's owned catalog migration chain."""

    def __init__(
        self,
        *,
        audit_retention: AuditRetentionPolicy | None = None,
    ) -> None:
        self.script_location = Path(__file__).with_name("migrations")
        self.audit_retention = audit_retention

    def config(self, connection: Connection | None = None) -> Config:
        config = Config()
        config.set_main_option("script_location", str(self.script_location))
        config.set_main_option("prepend_sys_path", str(self.script_location.parent.parent.parent))
        if self.audit_retention is not None:
            config.attributes["audit_retention"] = self.audit_retention
        if connection is not None:
            config.attributes["connection"] = connection
        return config

    def upgrade(self, bind: Engine | Connection, revision: str = "head") -> None:
        self._run(bind, command.upgrade, revision)

    def downgrade(self, bind: Engine | Connection, revision: str = "base") -> None:
        self._run(bind, command.downgrade, revision)

    def stamp(self, bind: Engine | Connection, revision: str) -> None:
        self._run(bind, command.stamp, revision)

    def current(self, bind: Engine | Connection) -> str | None:
        if isinstance(bind, Connection):
            return MigrationContext.configure(bind).get_current_revision()
        with bind.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()

    def heads(self) -> tuple[str, ...]:
        return tuple(ScriptDirectory.from_config(self.config()).get_heads())

    def is_at_head(self, bind: Engine | Connection) -> bool:
        return self.current(bind) in self.heads()

    def _run(self, bind: Engine | Connection, operation: object, revision: str) -> None:
        with _ALEMBIC_COMMAND_LOCK:
            if isinstance(bind, Connection):
                if bind.in_transaction():
                    self._run_on_connection(bind, operation, revision)
                elif bind.dialect.name == "sqlite":
                    self._run_in_sqlite_transaction(bind, operation, revision)
                else:
                    with bind.begin():
                        self._run_on_connection(bind, operation, revision)
                return
            if bind.dialect.name == "sqlite":
                with bind.connect() as connection:
                    self._run_in_sqlite_transaction(connection, operation, revision)
                return
            with bind.begin() as connection:
                self._run_on_connection(connection, operation, revision)

    def _run_in_sqlite_transaction(
        self,
        connection: Connection,
        operation: object,
        revision: str,
    ) -> None:
        # A normal SQLite BEGIN is deferred: two fresh processes can both
        # inspect an empty schema before either one takes the writer lock. Take
        # that lock up front so the second initializer re-checks the revision
        # only after the first transaction commits.
        connection.exec_driver_sql("PRAGMA busy_timeout = 30000")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            self._run_on_connection(connection, operation, revision)
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    def _run_on_connection(
        self,
        connection: Connection,
        operation: object,
        revision: str,
    ) -> None:
        if connection.dialect.name == "postgresql":
            # Serialize schema ownership across concurrently starting workers.
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(1129270868)")
        operation(self.config(connection), revision)  # type: ignore[operator]


def catalog_schema_exists(bind: Engine | Connection) -> bool:
    inspector = sa.inspect(bind)
    return all(
        inspector.has_table(table)
        for table in (
            "audit_events",
            "audit_move_heads",
            "audit_event_tombstones",
            "catalog_schema_features",
            "content_blobs",
            "content_manifest_chunks",
            "content_manifests",
            "embedding_document_spaces",
            "embedding_documents",
            "embedding_passages",
            "embedding_spaces",
            "embedding_vectors",
            "move_job_claim_fences",
            "move_job_transitions",
            "move_jobs",
            "object_contents",
            "object_embedding_documents",
            "object_mutation_fences",
            "object_placements",
            "objects",
            "pools",
            "tiers",
        )
    )
