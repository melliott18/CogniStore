"""Persist the trusted tier movement clock for cooldown enforcement."""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0010_tier_stability"
down_revision = "0009_placement_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "object_placements", sa.Column("last_tier_move_at", sa.Text(), nullable=True)
    )
    placements = sa.table(
        "object_placements",
        sa.column("placement_started_at", sa.Text()),
        sa.column("last_tier_move_at", sa.Text()),
    )
    # Old catalogs cannot distinguish initial publication from movement. Use
    # the known placement start conservatively; unknown history starts its
    # cooldown at migration, rather than being mistaken for "never moved".
    migrated_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    op.get_bind().execute(sa.update(placements).values(
        last_tier_move_at=sa.func.coalesce(placements.c.placement_started_at, migrated_at)
    ))


def downgrade() -> None:
    op.drop_column("object_placements", "last_tier_move_at")
