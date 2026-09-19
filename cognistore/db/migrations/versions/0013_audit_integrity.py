"""Add append-only audit evidence and baseline the existing retained history."""

from alembic import context, op

from cognistore.core.audit_integrity import GENESIS_HASH
from cognistore.db.audit_integrity import initialize_baseline, install_guards, remove_guards
from cognistore.db.schema import audit_integrity_entries, audit_integrity_head

revision = "0013_audit_integrity"
down_revision = "0012_tenant_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    audit_integrity_entries.create(connection)
    audit_integrity_head.create(connection)
    connection.execute(
        audit_integrity_head.insert().values(singleton=1, sequence=0, entry_hash=GENESIS_HASH)
    )
    initialize_baseline(connection, context.config.attributes.get("tenant_id", "default"))
    install_guards(connection)


def downgrade() -> None:
    connection = op.get_bind()
    remove_guards(connection)
    audit_integrity_entries.drop(connection)
    audit_integrity_head.drop(connection)
