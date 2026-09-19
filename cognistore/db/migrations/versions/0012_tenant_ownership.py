"""Persist the owner of each physically isolated tenant catalog.

Revision ID: 0012_tenant_ownership
Revises: 0011_policy_budgets
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0012_tenant_ownership"
down_revision = "0011_policy_budgets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = op.create_table(
        "catalog_tenant",
        sa.Column("singleton", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.CheckConstraint("singleton = 1", name="catalog_tenant_singleton"),
    )
    op.get_bind().execute(table.insert().values(
        singleton=1, tenant_id=context.config.attributes.get("tenant_id", "default")
    ))


def downgrade() -> None:
    op.drop_table("catalog_tenant")
