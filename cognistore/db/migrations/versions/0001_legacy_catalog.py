"""Establish the prototype catalog as a migration baseline.

Revision ID: 0001_legacy_catalog
Revises: None
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from cognistore.db.types import NulSafeText

revision = "0001_legacy_catalog"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "objects" not in tables:
        op.create_table(
            "objects",
            sa.Column("bucket", NulSafeText(), nullable=False),
            sa.Column("key", NulSafeText(), nullable=False),
            sa.Column("size", sa.BigInteger(), nullable=False),
            sa.Column("tier", NulSafeText(), nullable=False),
            sa.Column("metadata", sa.Text()),
            sa.PrimaryKeyConstraint("bucket", "key", name="pk_objects_legacy"),
        )
    if "move_jobs" not in tables:
        op.create_table(
            "move_jobs",
            sa.Column("idempotency_key", NulSafeText(), primary_key=True),
            sa.Column("src_tier", NulSafeText(), nullable=False),
            sa.Column("dst_tier", NulSafeText(), nullable=False),
            sa.Column("bucket", NulSafeText(), nullable=False),
            sa.Column("object_key", NulSafeText(), nullable=False),
            sa.Column("expected_size", sa.BigInteger(), nullable=False),
            sa.Column("source_metadata", sa.Text(), nullable=False),
            sa.Column("state", sa.Text(), nullable=False),
            sa.Column("owner_id", NulSafeText()),
            sa.Column("lease_expires_at", sa.Text()),
            sa.Column("transferred_size", sa.BigInteger()),
            sa.Column("source_size", sa.BigInteger()),
            sa.Column("source_checksum", sa.Text()),
            sa.Column("destination_size", sa.BigInteger()),
            sa.Column("destination_checksum", sa.Text()),
            sa.Column("destination_generation", NulSafeText()),
            sa.Column("verification_details", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("terminal_reason", NulSafeText()),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
        )
        op.create_index("move_jobs_state_idx", "move_jobs", ["state", "updated_at"])
        op.create_index("move_jobs_object_idx", "move_jobs", ["bucket", "object_key"])
    if "move_job_transitions" not in tables:
        op.create_table(
            "move_job_transitions",
            sa.Column("idempotency_key", NulSafeText(), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("from_state", sa.Text()),
            sa.Column("to_state", sa.Text(), nullable=False),
            sa.Column("reason", NulSafeText(), nullable=False),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.ForeignKeyConstraint(
                ["idempotency_key"],
                ["move_jobs.idempotency_key"],
                name="fk_move_job_transitions_idempotency_key_move_jobs",
            ),
            sa.PrimaryKeyConstraint("idempotency_key", "sequence", name="pk_move_job_transitions"),
        )


def downgrade() -> None:
    op.drop_table("move_job_transitions")
    op.drop_index("move_jobs_object_idx", table_name="move_jobs")
    op.drop_index("move_jobs_state_idx", table_name="move_jobs")
    op.drop_table("move_jobs")
    op.drop_table("objects")
