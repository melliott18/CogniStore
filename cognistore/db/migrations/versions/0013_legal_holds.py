"""Persist tenant-owned legal holds separately from mutable object metadata."""

from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op

from cognistore.db.types import NulSafeText

revision = "0013_legal_holds"
down_revision = "0012_tenant_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "legal_holds",
        sa.Column("hold_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", NulSafeText(), nullable=False),
        sa.Column("bucket", NulSafeText(), nullable=False),
        sa.Column("object_key", NulSafeText(), nullable=True),
        sa.Column("prefix", NulSafeText(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("released_at", sa.Text(), nullable=True),
        sa.CheckConstraint("object_key IS NULL OR prefix IS NULL", name="legal_hold_scope"),
    )
    op.create_index("legal_holds_active_scope_idx", "legal_holds", ["bucket", "released_at"])


def downgrade() -> None:
    # Releasing a hold does not authorize erasing its lifecycle evidence.
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        schema = connection.exec_driver_sql("SELECT current_schema()").scalar_one()
        lock_key = int.from_bytes(hashlib.sha256(
            ("cognistore-legal-holds:" + schema).encode()
        ).digest()[:8], "big", signed=True)
        connection.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    table = sa.table("legal_holds", sa.column("hold_id", sa.Uuid()))
    if op.get_bind().execute(sa.select(table.c.hold_id).limit(1)).first() is not None:
        raise RuntimeError("cannot downgrade while legal hold history exists")
    op.drop_index("legal_holds_active_scope_idx", table_name="legal_holds")
    op.drop_table("legal_holds")
