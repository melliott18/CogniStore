"""Persist immutable placement budgets and conservative move reservations."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from cognistore.db.types import NulSafeText

revision = "0011_policy_budgets"
down_revision = "0010_tier_stability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "budget_definitions",
        sa.Column("budget_id", NulSafeText(), primary_key=True),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_table(
        "budget_reservations",
        sa.Column("budget_id", NulSafeText(), sa.ForeignKey("budget_definitions.budget_id"), primary_key=True),
        sa.Column("move_id", NulSafeText(), primary_key=True),
        sa.Column("attempt", sa.Integer(), primary_key=True),
        sa.Column("reservation", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        # Configuration and move admission share this fence. Do not allow a
        # definition to appear between the emptiness check and table removal.
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(1129270870)")
    for name in ("budget_reservations", "budget_definitions"):
        table = sa.table(name, sa.column("budget_id", sa.Text()))
        if connection.execute(sa.select(table.c.budget_id).limit(1)).first() is not None:
            raise RuntimeError("cannot downgrade while budget definitions or reservations exist")
    op.drop_table("budget_reservations")
    op.drop_table("budget_definitions")
