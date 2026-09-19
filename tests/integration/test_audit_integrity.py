from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
import sqlalchemy as sa

from cognistore.db import SQLCatalog
from cognistore.db.audit_integrity import remove_guards
from cognistore.db.schema import audit_events, audit_integrity_entries
from tests.unit.test_audit_integrity import event

pytestmark = pytest.mark.integration


def test_postgres_serializes_concurrent_appends_and_preserves_retention(postgres_dsn):
    with SQLCatalog(postgres_dsn) as catalog:

        def append(number):
            catalog.append_audit_event(replace(event(number), move_id="move"))
            catalog.append_audit_event(replace(event(number), move_id="move"))

        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(append, range(1, 25)))
        checkpoint = catalog.audit_checkpoint()
        result = catalog.verify_audit_integrity(checkpoint)
        assert result.valid and result.checked_entries == 24
        assert catalog.prune_expired_audit_events("2025-01-01T00:00:00Z", limit=100) == 24
        assert catalog.verify_audit_integrity(checkpoint).valid
        assert catalog.prune_audit_events("2999-01-01T00:00:00Z") == 0


def test_postgres_runtime_cannot_mutate_history_or_spoof_retention(postgres_dsn):
    role = "audit_runtime_" + uuid4().hex
    with SQLCatalog(postgres_dsn) as catalog:
        catalog.append_audit_event(event())
        with catalog.engine.begin() as connection:
            connection.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
            connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
            # Deliberately over-grant DML to demonstrate trigger defense too.
            connection.exec_driver_sql(
                f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public TO {role}"
            )
        try:
            for statement in (
                "UPDATE audit_events SET outcome='failed'",
                "DELETE FROM audit_events",
                "TRUNCATE audit_events",
                "DELETE FROM audit_integrity_entries",
                "UPDATE audit_integrity_entries SET payload_digest='tampered'",
                "DELETE FROM audit_integrity_head",
                "TRUNCATE audit_integrity_head",
                "SELECT cognistore_prune_audit_events(ARRAY[]::uuid[])",
            ):
                with pytest.raises(sa.exc.DBAPIError):
                    with catalog.engine.begin() as connection:
                        connection.exec_driver_sql(f"SET LOCAL ROLE {role}")
                        connection.exec_driver_sql(
                            "SET LOCAL cognistore.audit_retention='authorized'"
                        )
                        connection.exec_driver_sql(statement)
            assert catalog.verify_audit_integrity().valid
        finally:
            with catalog.engine.begin() as connection:
                connection.exec_driver_sql(f"DROP OWNED BY {role}")
                connection.exec_driver_sql(f"DROP ROLE {role}")


@pytest.mark.parametrize("attack", ["payload", "middle", "tail"])
def test_postgres_detects_privileged_payload_or_evidence_corruption(postgres_dsn, attack):
    with SQLCatalog(postgres_dsn) as catalog:
        for number in range(1, 4):
            catalog.append_audit_event(event(number))
        checkpoint = catalog.audit_checkpoint()
        with catalog.engine.begin() as connection:
            remove_guards(connection)
            if attack == "payload":
                connection.execute(sa.update(audit_events).values(outcome="failed"))
            else:
                connection.execute(
                    sa.delete(audit_integrity_entries).where(
                        audit_integrity_entries.c.sequence == (2 if attack == "middle" else 3)
                    )
                )
        assert not catalog.verify_audit_integrity(checkpoint).valid
