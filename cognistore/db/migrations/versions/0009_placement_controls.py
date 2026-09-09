"""Persist trusted importance independently and track actual tier residency."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_placement_controls"
down_revision = "0008_access_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("objects", sa.Column("importance", sa.JSON(), nullable=True))
    op.add_column(
        "objects",
        sa.Column("importance_revision", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "object_placements", sa.Column("placement_started_at", sa.Text(), nullable=True)
    )
    placements = sa.table(
        "object_placements",
        sa.column("updated_at", sa.Text()),
        sa.column("placement_started_at", sa.Text()),
    )
    # The last refresh is a conservative lower bound for residency. Prototype
    # imports used an epoch sentinel, which is unknown history, not evidence.
    op.get_bind().execute(sa.update(placements).values(
        placement_started_at=sa.case(
            (placements.c.updated_at == "1970-01-01T00:00:00.000000Z", None),
            else_=placements.c.updated_at,
        )
    ))


def downgrade() -> None:
    op.drop_column("object_placements", "placement_started_at")
    op.drop_column("objects", "importance_revision")
    op.drop_column("objects", "importance")
