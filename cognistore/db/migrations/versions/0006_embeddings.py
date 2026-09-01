"""Add reproducible document passages and versioned embedding spaces.

Revision ID: 0006_embeddings
Revises: 0005_content_references
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from cognistore.db.types import NulSafeText, PortableVector

revision = "0006_embeddings"
down_revision = "0005_content_references"
branch_labels = None
depends_on = None

_MINIMUM_PGVECTOR_VERSION = (0, 8, 0)


def _require_pgvector_capabilities() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    value = connection.exec_driver_sql(
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
    ).scalar_one_or_none()
    if not isinstance(value, str):
        raise RuntimeError("migration 0006 requires the PostgreSQL vector extension")
    pieces = value.split(".")
    if not 2 <= len(pieces) <= 3 or any(not piece.isdigit() for piece in pieces):
        raise RuntimeError(f"migration 0006 cannot interpret pgvector version {value!r}")
    observed = (
        int(pieces[0]),
        int(pieces[1]),
        int(pieces[2]) if len(pieces) == 3 else 0,
    )
    if observed < _MINIMUM_PGVECTOR_VERSION:
        raise RuntimeError(
            "migration 0006 requires pgvector 0.8.0 or newer for filtered "
            f"HNSW search; found {value}"
        )


def upgrade() -> None:
    _require_pgvector_capabilities()
    op.create_table(
        "embedding_spaces",
        sa.Column("space_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("provider_implementation", sa.Text(), nullable=False),
        sa.Column("provider_implementation_version", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("model_revision", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("distance_metric", sa.Text(), nullable=False),
        sa.Column("normalization", sa.Text(), nullable=False),
        sa.Column("normalization_version", sa.Integer(), nullable=False),
        sa.Column("preprocessing", sa.Text(), nullable=False),
        sa.Column("preprocessing_version", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column(
            "hnsw_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("hnsw_m", sa.Integer(), nullable=False, server_default="16"),
        sa.Column(
            "hnsw_ef_construction",
            sa.Integer(),
            nullable=False,
            server_default="64",
        ),
        sa.Column("hnsw_ef_search", sa.Integer(), nullable=False, server_default="40"),
        sa.Column(
            "hnsw_iterative_scan",
            sa.Text(),
            nullable=False,
            server_default="off",
        ),
        sa.Column("hnsw_index_name", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "dimensions > 0",
            name="embedding_space_dimensions_positive",
        ),
        sa.CheckConstraint(
            "normalization_version > 0",
            name="embedding_space_normalization_version_positive",
        ),
        sa.CheckConstraint(
            "preprocessing_version > 0",
            name="embedding_space_preprocessing_version_positive",
        ),
        sa.CheckConstraint(
            "length(fingerprint) = 64 AND fingerprint = lower(fingerprint)",
            name="embedding_space_fingerprint_canonical",
        ),
        sa.CheckConstraint(
            "hnsw_m > 0",
            name="embedding_space_hnsw_m_positive",
        ),
        sa.CheckConstraint(
            "hnsw_ef_construction > 0",
            name="embedding_space_hnsw_ef_construction_positive",
        ),
        sa.CheckConstraint(
            "hnsw_ef_search > 0",
            name="embedding_space_hnsw_ef_search_positive",
        ),
        sa.CheckConstraint(
            "hnsw_iterative_scan IN ('off', 'relaxed_order', 'strict_order')",
            name="embedding_space_hnsw_iterative_scan_valid",
        ),
        sa.CheckConstraint(
            "NOT hnsw_enabled OR hnsw_index_name IS NOT NULL",
            name="embedding_space_hnsw_index_named_when_enabled",
        ),
        sa.UniqueConstraint(
            "fingerprint",
            name="uq_embedding_spaces_fingerprint",
        ),
        sa.UniqueConstraint(
            "space_id",
            "dimensions",
            name="uq_embedding_spaces_id_dimensions",
        ),
    )

    op.create_table(
        "embedding_documents",
        sa.Column("document_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("source_document_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("source_fingerprint", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("manifest_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("extraction_schema_version", sa.Integer(), nullable=False),
        sa.Column("source_mime", sa.Text(), nullable=False),
        sa.Column("parser_name", sa.Text(), nullable=False),
        sa.Column("parser_implementation_version", sa.Text(), nullable=False),
        sa.Column("parser_runtime_version", sa.Text(), nullable=False),
        sa.Column("normalization_name", sa.Text(), nullable=False),
        sa.Column("normalization_version", sa.Integer(), nullable=False),
        sa.Column("normalized_text", NulSafeText(), nullable=False),
        sa.Column("text_sha256", sa.Text(), nullable=False),
        sa.Column("chunker_algorithm", sa.Text(), nullable=False),
        sa.Column("chunker_version", sa.Integer(), nullable=False),
        sa.Column("max_codepoints", sa.BigInteger(), nullable=False),
        sa.Column("overlap_codepoints", sa.BigInteger(), nullable=False),
        sa.Column("passage_count", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(source_fingerprint) = 64 AND source_fingerprint = lower(source_fingerprint)",
            name="embedding_document_source_fingerprint_canonical",
        ),
        sa.CheckConstraint(
            "length(fingerprint) = 64 AND fingerprint = lower(fingerprint)",
            name="embedding_document_fingerprint_canonical",
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64 AND source_sha256 = lower(source_sha256)",
            name="embedding_document_source_sha256_canonical",
        ),
        sa.CheckConstraint(
            "length(text_sha256) = 64 AND text_sha256 = lower(text_sha256)",
            name="embedding_document_text_sha256_canonical",
        ),
        sa.CheckConstraint(
            "extraction_schema_version > 0",
            name="embedding_document_extraction_version_positive",
        ),
        sa.CheckConstraint(
            "normalization_version > 0",
            name="embedding_document_normalization_version_positive",
        ),
        sa.CheckConstraint(
            "chunker_version > 0",
            name="embedding_document_chunker_version_positive",
        ),
        sa.CheckConstraint(
            "max_codepoints > 0",
            name="embedding_document_max_codepoints_positive",
        ),
        sa.CheckConstraint(
            "overlap_codepoints >= 0 AND overlap_codepoints < max_codepoints",
            name="embedding_document_overlap_valid",
        ),
        sa.CheckConstraint(
            "passage_count >= 0",
            name="embedding_document_passage_count_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["manifest_id"],
            ["content_manifests.manifest_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_sha256"],
            ["content_blobs.sha256"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "fingerprint",
            name="uq_embedding_documents_fingerprint",
        ),
        sa.UniqueConstraint(
            "source_document_id",
            "chunker_algorithm",
            "chunker_version",
            "max_codepoints",
            "overlap_codepoints",
            "text_sha256",
            name="uq_embedding_documents_passage_layout",
        ),
    )
    op.create_index(
        "embedding_documents_manifest_idx",
        "embedding_documents",
        ["manifest_id"],
    )
    op.create_index(
        "embedding_documents_source_idx",
        "embedding_documents",
        ["source_document_id"],
    )

    op.create_table(
        "object_embedding_documents",
        sa.Column("object_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("space_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["object_id"],
            ["objects.object_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["embedding_spaces.space_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["embedding_documents.document_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "object_embedding_documents_document_idx",
        "object_embedding_documents",
        ["document_id", "space_id"],
    )

    op.create_table(
        "embedding_passages",
        sa.Column("passage_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("passage_index", sa.BigInteger(), nullable=False),
        sa.Column("start_codepoint", sa.BigInteger(), nullable=False),
        sa.Column("end_codepoint", sa.BigInteger(), nullable=False),
        sa.Column("text", NulSafeText(), nullable=False),
        sa.Column("text_sha256", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "passage_index >= 0",
            name="embedding_passage_index_nonnegative",
        ),
        sa.CheckConstraint(
            "start_codepoint >= 0",
            name="embedding_passage_start_nonnegative",
        ),
        sa.CheckConstraint(
            "end_codepoint > start_codepoint",
            name="embedding_passage_end_after_start",
        ),
        sa.CheckConstraint(
            "length(text_sha256) = 64 AND text_sha256 = lower(text_sha256)",
            name="embedding_passage_text_sha256_canonical",
        ),
        sa.CheckConstraint(
            "length(fingerprint) = 64 AND fingerprint = lower(fingerprint)",
            name="embedding_passage_fingerprint_canonical",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["embedding_documents.document_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "fingerprint",
            name="uq_embedding_passages_fingerprint",
        ),
        sa.UniqueConstraint(
            "document_id",
            "passage_index",
            name="uq_embedding_passages_document_index",
        ),
        sa.UniqueConstraint(
            "document_id",
            "start_codepoint",
            "end_codepoint",
            name="uq_embedding_passages_document_offsets",
        ),
        sa.UniqueConstraint(
            "passage_id",
            "text_sha256",
            name="uq_embedding_passages_id_text_sha256",
        ),
    )
    op.create_index(
        "embedding_passages_document_idx",
        "embedding_passages",
        ["document_id"],
    )

    vector_constraints: list[sa.CheckConstraint | sa.ForeignKeyConstraint] = [
        sa.CheckConstraint(
            "dimensions > 0",
            name="embedding_vector_dimensions_positive",
        ),
        sa.CheckConstraint(
            "length(input_text_sha256) = 64 AND input_text_sha256 = lower(input_text_sha256)",
            name="embedding_vector_input_sha256_canonical",
        ),
        sa.ForeignKeyConstraint(
            ["space_id", "dimensions"],
            ["embedding_spaces.space_id", "embedding_spaces.dimensions"],
            name="fk_embedding_vectors_space_dimensions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["passage_id", "input_text_sha256"],
            ["embedding_passages.passage_id", "embedding_passages.text_sha256"],
            name="fk_embedding_vectors_passage_text",
            ondelete="CASCADE",
        ),
    ]
    if op.get_bind().dialect.name == "postgresql":
        vector_constraints.append(
            sa.CheckConstraint(
                "vector_dims(embedding) = dimensions",
                name="embedding_vector_dimensions_match",
            )
        )
    op.create_table(
        "embedding_vectors",
        sa.Column("space_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("passage_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("input_text_sha256", sa.Text(), nullable=False),
        sa.Column("embedding", PortableVector(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        *vector_constraints,
    )
    op.create_index(
        "embedding_vectors_passage_idx",
        "embedding_vectors",
        ["passage_id", "input_text_sha256"],
    )

    op.create_table(
        "embedding_document_spaces",
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("space_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("completed_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["embedding_documents.document_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["embedding_spaces.space_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "document_id",
            "space_id",
            name="pk_embedding_document_spaces",
        ),
    )
    op.create_index(
        "embedding_document_spaces_space_idx",
        "embedding_document_spaces",
        ["space_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "embedding_document_spaces_space_idx",
        table_name="embedding_document_spaces",
    )
    op.drop_table("embedding_document_spaces")
    op.drop_index("embedding_vectors_passage_idx", table_name="embedding_vectors")
    op.drop_table("embedding_vectors")
    op.drop_index("embedding_passages_document_idx", table_name="embedding_passages")
    op.drop_table("embedding_passages")
    op.drop_index(
        "object_embedding_documents_document_idx",
        table_name="object_embedding_documents",
    )
    op.drop_table("object_embedding_documents")
    op.drop_index("embedding_documents_source_idx", table_name="embedding_documents")
    op.drop_index("embedding_documents_manifest_idx", table_name="embedding_documents")
    op.drop_table("embedding_documents")
    op.drop_table("embedding_spaces")
