"""Add canonical content manifests and object-to-chunk mappings.

Revision ID: 0004_content_identity
Revises: 0003_audit_events
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_content_identity"
down_revision = "0003_audit_events"
branch_labels = None
depends_on = None

_BATCH_SIZE = 1000


def _strip_unbacked_content_identity_metadata() -> None:
    """Remove the normalized-manifest projection when no mapping can survive.

    Revision 0004 deliberately does not infer manifests from legacy metadata,
    and its downgrade drops every manifest mapping. In both directions a
    ``content_identity`` JSON member would therefore be an unbacked,
    caller-controlled claim rather than the catalog-owned projection used by
    the current schema.
    """

    connection = op.get_bind()
    objects = sa.table(
        "objects",
        sa.column("object_id", sa.Uuid(as_uuid=True)),
        sa.column("metadata", sa.JSON()),
    )
    rows = connection.execute(
        sa.select(objects.c.object_id, objects.c.metadata).order_by(objects.c.object_id)
    ).mappings()
    while batch := rows.fetchmany(_BATCH_SIZE):
        updates = []
        for row in batch:
            metadata = dict(row["metadata"] or {})
            if "content_identity" not in metadata:
                continue
            metadata.pop("content_identity")
            updates.append(
                {
                    "content_identity_object_id": row["object_id"],
                    "content_identity_metadata": metadata,
                }
            )
        if updates:
            connection.execute(
                sa.update(objects)
                .where(
                    objects.c.object_id
                    == sa.bindparam("content_identity_object_id")
                )
                .values(
                    metadata=sa.bindparam("content_identity_metadata")
                ),
                updates,
            )


def upgrade() -> None:
    _strip_unbacked_content_identity_metadata()
    op.create_table(
        "content_blobs",
        sa.Column("sha256", sa.Text(), primary_key=True),
        sa.Column("digest_algorithm", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("cas_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(sha256) = 64 AND sha256 = lower(sha256)",
            name="content_blob_sha256_canonical",
        ),
        sa.CheckConstraint(
            "size >= 0",
            name="content_blob_size_nonnegative",
        ),
        sa.UniqueConstraint("cas_key", name="uq_content_blobs_cas_key"),
    )
    op.create_table(
        "content_manifests",
        sa.Column("manifest_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("representation", sa.Text(), nullable=False),
        sa.Column("chunking_algorithm", sa.Text(), nullable=False),
        sa.Column("chunking_version", sa.Integer(), nullable=False),
        sa.Column("chunk_size", sa.BigInteger(), nullable=False),
        sa.Column("chunk_count", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "schema_version > 0",
            name="content_manifest_schema_version_positive",
        ),
        sa.CheckConstraint(
            "chunking_version > 0",
            name="content_manifest_chunking_version_positive",
        ),
        sa.CheckConstraint(
            "chunk_size > 0",
            name="content_manifest_chunk_size_positive",
        ),
        sa.CheckConstraint(
            "chunk_count >= 0",
            name="content_manifest_chunk_count_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["content_sha256"],
            ["content_blobs.sha256"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "content_sha256",
            "schema_version",
            "representation",
            "chunking_algorithm",
            "chunking_version",
            "chunk_size",
            name="uq_content_manifests_layout",
        ),
    )
    op.create_table(
        "content_manifest_chunks",
        sa.Column("manifest_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.BigInteger(), nullable=False),
        sa.Column("chunk_sha256", sa.Text(), nullable=False),
        sa.Column("byte_offset", sa.BigInteger(), nullable=False),
        sa.Column("byte_length", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "chunk_index >= 0",
            name="content_manifest_chunk_index_nonnegative",
        ),
        sa.CheckConstraint(
            "byte_offset >= 0",
            name="content_manifest_chunk_offset_nonnegative",
        ),
        sa.CheckConstraint(
            "byte_length > 0",
            name="content_manifest_chunk_length_positive",
        ),
        sa.ForeignKeyConstraint(
            ["manifest_id"],
            ["content_manifests.manifest_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_sha256"],
            ["content_blobs.sha256"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "manifest_id",
            "chunk_index",
            name="pk_content_manifest_chunks",
        ),
        sa.UniqueConstraint(
            "manifest_id",
            "byte_offset",
            name="uq_content_manifest_chunks_offset",
        ),
    )
    op.create_index(
        "content_manifest_chunks_blob_idx",
        "content_manifest_chunks",
        ["chunk_sha256"],
    )
    op.create_table(
        "object_contents",
        sa.Column("object_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("manifest_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["object_id"],
            ["objects.object_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["manifest_id"],
            ["content_manifests.manifest_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "object_contents_manifest_idx",
        "object_contents",
        ["manifest_id"],
    )


def downgrade() -> None:
    _strip_unbacked_content_identity_metadata()
    op.drop_index("object_contents_manifest_idx", table_name="object_contents")
    op.drop_table("object_contents")
    op.drop_index(
        "content_manifest_chunks_blob_idx",
        table_name="content_manifest_chunks",
    )
    op.drop_table("content_manifest_chunks")
    op.drop_table("content_manifests")
    op.drop_table("content_blobs")
