"""Add explicit pool topology, observations, and tier/pool lifecycle state.

Revision ID: 0007_tier_pools
Revises: 0006_embeddings

Legacy pools have no region or member inventory, so they remain visible but
inactive until an operator supplies a complete topology and activates them.
Existing object placement identities and pool/tier foreign keys are preserved.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from cognistore.db.types import NulSafeText

revision = "0007_tier_pools"
down_revision = "0006_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tiers", sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true())
    )
    op.add_column("pools", sa.Column("region", NulSafeText(), nullable=True))
    op.add_column(
        "pools", sa.Column("members", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
    )
    op.add_column(
        "pools", sa.Column("localities", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
    )
    op.add_column(
        "pools", sa.Column("attributes", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
    )
    op.add_column(
        "pools", sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    # Native DROP COLUMN preserves inbound foreign keys on SQLite; rebuilding
    # either table with batch_alter_table would require dropping those keys.
    for column in ("active", "attributes", "localities", "members", "region"):
        op.drop_column("pools", column)
    op.drop_column("tiers", "active")
