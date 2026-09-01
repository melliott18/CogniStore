"""Add durable CAS reference counts and reclamation timestamps.

Revision ID: 0005_content_references
Revises: 0004_content_identity
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0005_content_references"
down_revision = "0004_content_identity"
branch_labels = None
depends_on = None


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _backfill_reference_state() -> None:
    """Derive materialized counts from active object-to-manifest ownership."""

    connection = op.get_bind()
    blobs = sa.table(
        "content_blobs",
        sa.column("sha256", sa.Text()),
        sa.column("reference_count", sa.BigInteger()),
        sa.column("unreferenced_at", sa.Text()),
    )
    manifests = sa.table(
        "content_manifests",
        sa.column("manifest_id", sa.Uuid(as_uuid=True)),
        sa.column("content_sha256", sa.Text()),
    )
    chunks = sa.table(
        "content_manifest_chunks",
        sa.column("manifest_id", sa.Uuid(as_uuid=True)),
        sa.column("chunk_sha256", sa.Text()),
    )
    ownership = sa.table(
        "object_contents",
        sa.column("object_id", sa.Uuid(as_uuid=True)),
        sa.column("manifest_id", sa.Uuid(as_uuid=True)),
    )

    root_edges = sa.select(manifests.c.content_sha256.label("sha256")).select_from(
        ownership.join(
            manifests,
            manifests.c.manifest_id == ownership.c.manifest_id,
        )
    )
    chunk_edges = sa.select(chunks.c.chunk_sha256.label("sha256")).select_from(
        ownership.join(
            chunks,
            chunks.c.manifest_id == ownership.c.manifest_id,
        )
    )
    active_edges = root_edges.union_all(chunk_edges).subquery("active_content_edges")
    counts = (
        sa.select(
            active_edges.c.sha256,
            sa.func.count().label("reference_count"),
        )
        .group_by(active_edges.c.sha256)
        .subquery("active_content_reference_counts")
    )
    expected_count = (
        sa.select(counts.c.reference_count)
        .where(counts.c.sha256 == blobs.c.sha256)
        .scalar_subquery()
    )
    resolved_count = sa.func.coalesce(expected_count, 0)
    migrated_at = _timestamp()
    connection.execute(
        sa.update(blobs).values(
            reference_count=resolved_count,
            unreferenced_at=sa.case(
                (resolved_count == 0, migrated_at),
                else_=None,
            ),
        )
    )


def upgrade() -> None:
    op.add_column(
        "content_blobs",
        sa.Column(
            "reference_count",
            sa.BigInteger(),
            sa.CheckConstraint(
                "reference_count >= 0",
                name="content_blob_reference_count_nonnegative",
            ),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "content_blobs",
        sa.Column("unreferenced_at", sa.Text(), nullable=True),
    )

    _backfill_reference_state()

    op.create_index(
        "content_blobs_reclamation_idx",
        "content_blobs",
        ["reference_count", "unreferenced_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "content_blobs_reclamation_idx",
        table_name="content_blobs",
    )
    op.drop_column("content_blobs", "unreferenced_at")
    op.drop_column("content_blobs", "reference_count")
