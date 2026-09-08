"""Add independently retained access observations and bounded retry tombstones."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from cognistore.db.types import NulSafeText

revision = "0007_access_events"
down_revision = "0006_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_events",
        sa.Column("event_id", NulSafeText(), primary_key=True),
        sa.Column("operation_id", NulSafeText(), nullable=False),
        sa.Column("correlation_id", NulSafeText(), nullable=False),
        sa.Column("occurred_at", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("bucket", NulSafeText(), nullable=False),
        sa.Column("object_key", NulSafeText(), nullable=True),
        sa.Column("tier", NulSafeText(), nullable=True),
        sa.Column("source", NulSafeText(), nullable=False),
        sa.Column("sample_rate", sa.Float(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("expired", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("kind IN ('read', 'write', 'list', 'touch')", name="access_event_kind"),
        sa.CheckConstraint("sample_rate > 0 AND sample_rate <= 1", name="access_event_sample_rate"),
        sa.CheckConstraint("schema_version = 1", name="access_event_schema_version"),
        sa.CheckConstraint(
            "object_key IS NOT NULL OR kind = 'list'", name="access_event_coordinate"
        ),
    )
    op.create_index(
        "access_events_object_time_idx",
        "access_events",
        ["bucket", "object_key", "expired", "occurred_at"],
    )
    op.create_index(
        "access_events_retention_idx", "access_events", ["expired", "occurred_at", "event_id"]
    )


def downgrade() -> None:
    op.drop_index("access_events_retention_idx", table_name="access_events")
    op.drop_index("access_events_object_time_idx", table_name="access_events")
    op.drop_table("access_events")
