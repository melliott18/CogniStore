from __future__ import annotations

import sqlalchemy as sa

from .types import NulSafeText, PortableVector

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

json_type = sa.JSON()
uuid_type = sa.Uuid(as_uuid=True)

catalog_tenant = sa.Table(
    "catalog_tenant", metadata,
    sa.Column("singleton", sa.Integer(), primary_key=True),
    sa.Column("tenant_id", sa.Text(), nullable=False),
    sa.CheckConstraint("singleton = 1", name="catalog_tenant_singleton"),
)

legal_holds = sa.Table(
    "legal_holds", metadata,
    sa.Column("hold_id", uuid_type, primary_key=True),
    sa.Column("tenant_id", NulSafeText(), nullable=False),
    sa.Column("bucket", NulSafeText(), nullable=False),
    sa.Column("object_key", NulSafeText(), nullable=True),
    sa.Column("prefix", NulSafeText(), nullable=True),
    sa.Column("evidence", json_type, nullable=False),
    sa.Column("created_at", sa.Text(), nullable=False),
    sa.Column("released_at", sa.Text(), nullable=True),
    sa.CheckConstraint("object_key IS NULL OR prefix IS NULL", name="legal_hold_scope"),
)
sa.Index("legal_holds_active_scope_idx", legal_holds.c.bucket, legal_holds.c.released_at)

budget_definitions = sa.Table(
    "budget_definitions", metadata,
    sa.Column("budget_id", NulSafeText(), primary_key=True),
    sa.Column("definition", json_type, nullable=False),
    sa.Column("created_at", sa.Text(), nullable=False),
)

budget_reservations = sa.Table(
    "budget_reservations", metadata,
    sa.Column("budget_id", NulSafeText(), sa.ForeignKey("budget_definitions.budget_id"), primary_key=True),
    sa.Column("move_id", NulSafeText(), primary_key=True),
    sa.Column("attempt", sa.Integer(), primary_key=True),
    sa.Column("reservation", json_type, nullable=False),
    sa.Column("created_at", sa.Text(), nullable=False),
)

tiers = sa.Table(
    "tiers",
    metadata,
    sa.Column("name", NulSafeText(), primary_key=True),
    sa.Column("metadata", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
    sa.Column("created_at", sa.Text(), nullable=False),
    sa.Column("updated_at", sa.Text(), nullable=False),
)

pools = sa.Table(
    "pools",
    metadata,
    sa.Column("pool_id", NulSafeText(), primary_key=True),
    sa.Column(
        "tier_name",
        NulSafeText(),
        sa.ForeignKey("tiers.name", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("metadata", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("region", NulSafeText(), nullable=True),
    sa.Column("members", json_type, nullable=False, server_default=sa.text("'[]'")),
    sa.Column("localities", json_type, nullable=False, server_default=sa.text("'[]'")),
    sa.Column("attributes", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("created_at", sa.Text(), nullable=False),
    sa.Column("updated_at", sa.Text(), nullable=False),
    sa.UniqueConstraint("pool_id", "tier_name", name="uq_pools_pool_tier"),
)

objects = sa.Table(
    "objects",
    metadata,
    sa.Column("object_id", uuid_type, primary_key=True),
    sa.Column("bucket", NulSafeText(), nullable=False),
    sa.Column("object_key", NulSafeText(), nullable=False),
    sa.Column("size", sa.BigInteger(), nullable=False),
    sa.Column("metadata", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.Column("importance", json_type, nullable=True),
    sa.Column("importance_revision", sa.BigInteger(), nullable=False, server_default="0"),
    sa.Column("created_at", sa.Text(), nullable=False),
    sa.Column("updated_at", sa.Text(), nullable=False),
    sa.CheckConstraint("size >= 0", name="object_size_nonnegative"),
    sa.UniqueConstraint("bucket", "object_key", name="uq_objects_bucket_key"),
)

content_blobs = sa.Table(
    "content_blobs",
    metadata,
    sa.Column("sha256", sa.Text(), primary_key=True),
    sa.Column("digest_algorithm", sa.Text(), nullable=False),
    sa.Column("size", sa.BigInteger(), nullable=False),
    sa.Column("cas_key", sa.Text(), nullable=False, unique=True),
    sa.Column("created_at", sa.Text(), nullable=False),
    sa.Column(
        "reference_count",
        sa.BigInteger(),
        nullable=False,
        server_default=sa.text("0"),
    ),
    sa.Column("unreferenced_at", sa.Text(), nullable=True),
    sa.CheckConstraint(
        "length(sha256) = 64 AND sha256 = lower(sha256)",
        name="content_blob_sha256_canonical",
    ),
    sa.CheckConstraint("size >= 0", name="content_blob_size_nonnegative"),
    sa.CheckConstraint(
        "reference_count >= 0",
        name="content_blob_reference_count_nonnegative",
    ),
)
sa.Index(
    "content_blobs_reclamation_idx",
    content_blobs.c.reference_count,
    content_blobs.c.unreferenced_at,
)

content_manifests = sa.Table(
    "content_manifests",
    metadata,
    sa.Column("manifest_id", uuid_type, primary_key=True),
    sa.Column(
        "content_sha256",
        sa.Text(),
        sa.ForeignKey("content_blobs.sha256", ondelete="RESTRICT"),
        nullable=False,
    ),
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

content_manifest_chunks = sa.Table(
    "content_manifest_chunks",
    metadata,
    sa.Column(
        "manifest_id",
        uuid_type,
        sa.ForeignKey("content_manifests.manifest_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("chunk_index", sa.BigInteger(), primary_key=True),
    sa.Column(
        "chunk_sha256",
        sa.Text(),
        sa.ForeignKey("content_blobs.sha256", ondelete="RESTRICT"),
        nullable=False,
    ),
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
    sa.UniqueConstraint(
        "manifest_id",
        "byte_offset",
        name="uq_content_manifest_chunks_offset",
    ),
)
sa.Index(
    "content_manifest_chunks_blob_idx",
    content_manifest_chunks.c.chunk_sha256,
)

object_contents = sa.Table(
    "object_contents",
    metadata,
    sa.Column(
        "object_id",
        uuid_type,
        sa.ForeignKey("objects.object_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "manifest_id",
        uuid_type,
        sa.ForeignKey("content_manifests.manifest_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("created_at", sa.Text(), nullable=False),
    sa.Column("updated_at", sa.Text(), nullable=False),
)
sa.Index("object_contents_manifest_idx", object_contents.c.manifest_id)

embedding_spaces = sa.Table(
    "embedding_spaces",
    metadata,
    sa.Column("space_id", uuid_type, primary_key=True),
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
    sa.Column("fingerprint", sa.Text(), nullable=False, unique=True),
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
    sa.CheckConstraint("dimensions > 0", name="embedding_space_dimensions_positive"),
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
    sa.CheckConstraint("hnsw_m > 0", name="embedding_space_hnsw_m_positive"),
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
        "space_id",
        "dimensions",
        name="uq_embedding_spaces_id_dimensions",
    ),
)

embedding_documents = sa.Table(
    "embedding_documents",
    metadata,
    sa.Column("document_id", uuid_type, primary_key=True),
    sa.Column("source_document_id", uuid_type, nullable=False),
    sa.Column("source_fingerprint", sa.Text(), nullable=False),
    sa.Column("fingerprint", sa.Text(), nullable=False, unique=True),
    sa.Column(
        "manifest_id",
        uuid_type,
        sa.ForeignKey("content_manifests.manifest_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "source_sha256",
        sa.Text(),
        sa.ForeignKey("content_blobs.sha256", ondelete="RESTRICT"),
        nullable=False,
    ),
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
sa.Index("embedding_documents_manifest_idx", embedding_documents.c.manifest_id)
sa.Index(
    "embedding_documents_source_idx",
    embedding_documents.c.source_document_id,
)

object_embedding_documents = sa.Table(
    "object_embedding_documents",
    metadata,
    sa.Column(
        "object_id",
        uuid_type,
        sa.ForeignKey("objects.object_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "space_id",
        uuid_type,
        sa.ForeignKey("embedding_spaces.space_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "document_id",
        uuid_type,
        sa.ForeignKey("embedding_documents.document_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("created_at", sa.Text(), nullable=False),
    sa.Column("updated_at", sa.Text(), nullable=False),
)
sa.Index(
    "object_embedding_documents_document_idx",
    object_embedding_documents.c.document_id,
    object_embedding_documents.c.space_id,
)

embedding_passages = sa.Table(
    "embedding_passages",
    metadata,
    sa.Column("passage_id", uuid_type, primary_key=True),
    sa.Column(
        "document_id",
        uuid_type,
        sa.ForeignKey("embedding_documents.document_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("passage_index", sa.BigInteger(), nullable=False),
    sa.Column("start_codepoint", sa.BigInteger(), nullable=False),
    sa.Column("end_codepoint", sa.BigInteger(), nullable=False),
    sa.Column("text", NulSafeText(), nullable=False),
    sa.Column("text_sha256", sa.Text(), nullable=False),
    sa.Column("fingerprint", sa.Text(), nullable=False, unique=True),
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
sa.Index("embedding_passages_document_idx", embedding_passages.c.document_id)

embedding_vectors = sa.Table(
    "embedding_vectors",
    metadata,
    sa.Column("space_id", uuid_type, primary_key=True),
    sa.Column("passage_id", uuid_type, primary_key=True),
    sa.Column("dimensions", sa.Integer(), nullable=False),
    sa.Column("input_text_sha256", sa.Text(), nullable=False),
    sa.Column("embedding", PortableVector(), nullable=False),
    sa.Column("created_at", sa.Text(), nullable=False),
    sa.Column("updated_at", sa.Text(), nullable=False),
    sa.CheckConstraint(
        "dimensions > 0",
        name="embedding_vector_dimensions_positive",
    ),
    sa.CheckConstraint(
        "length(input_text_sha256) = 64 AND input_text_sha256 = lower(input_text_sha256)",
        name="embedding_vector_input_sha256_canonical",
    ),
    sa.CheckConstraint(
        "vector_dims(embedding) = dimensions",
        name="embedding_vector_dimensions_match",
    ).ddl_if(dialect="postgresql"),
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
)
sa.Index(
    "embedding_vectors_passage_idx",
    embedding_vectors.c.passage_id,
    embedding_vectors.c.input_text_sha256,
)

embedding_document_spaces = sa.Table(
    "embedding_document_spaces",
    metadata,
    sa.Column(
        "document_id",
        uuid_type,
        sa.ForeignKey("embedding_documents.document_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "space_id",
        uuid_type,
        sa.ForeignKey("embedding_spaces.space_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("completed_at", sa.Text(), nullable=False),
)
sa.Index(
    "embedding_document_spaces_space_idx",
    embedding_document_spaces.c.space_id,
)

object_placements = sa.Table(
    "object_placements",
    metadata,
    sa.Column("placement_id", uuid_type, primary_key=True),
    sa.Column(
        "object_id",
        uuid_type,
        sa.ForeignKey("objects.object_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    sa.Column(
        "tier_name",
        NulSafeText(),
        sa.ForeignKey("tiers.name", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("pool_id", NulSafeText(), nullable=True),
    sa.Column("placement_started_at", sa.Text(), nullable=True),
    sa.Column("last_tier_move_at", sa.Text(), nullable=True),
    sa.Column("created_at", sa.Text(), nullable=False),
    sa.Column("updated_at", sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(
        ["pool_id", "tier_name"],
        ["pools.pool_id", "pools.tier_name"],
        name="fk_object_placements_pool_tier",
        ondelete="RESTRICT",
    ),
)

object_mutation_fences = sa.Table(
    "object_mutation_fences",
    metadata,
    sa.Column("bucket", NulSafeText(), primary_key=True),
    sa.Column("object_key", NulSafeText(), primary_key=True),
    sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
)

move_job_claim_fences = sa.Table(
    "move_job_claim_fences",
    metadata,
    sa.Column("idempotency_key", NulSafeText(), primary_key=True),
)

catalog_schema_features = sa.Table(
    "catalog_schema_features",
    metadata,
    sa.Column("feature", sa.Text(), primary_key=True),
    sa.Column("available", sa.Boolean(), nullable=False),
    sa.Column("owned", sa.Boolean(), nullable=False),
)

move_jobs = sa.Table(
    "move_jobs",
    metadata,
    sa.Column("idempotency_key", NulSafeText(), primary_key=True),
    sa.Column("src_tier", NulSafeText(), nullable=False),
    sa.Column("dst_tier", NulSafeText(), nullable=False),
    sa.Column("bucket", NulSafeText(), nullable=False),
    sa.Column("object_key", NulSafeText(), nullable=False),
    sa.Column("expected_size", sa.BigInteger(), nullable=False),
    sa.Column("source_metadata", json_type, nullable=False),
    sa.Column("state", sa.Text(), nullable=False),
    sa.Column("owner_id", NulSafeText()),
    sa.Column("lease_expires_at", sa.Text()),
    sa.Column("transferred_size", sa.BigInteger()),
    sa.Column("source_size", sa.BigInteger()),
    sa.Column("source_checksum", sa.Text()),
    sa.Column("destination_size", sa.BigInteger()),
    sa.Column("destination_checksum", sa.Text()),
    sa.Column("destination_generation", NulSafeText()),
    sa.Column("verification_details", json_type, nullable=False, server_default=sa.text("'[]'")),
    sa.Column("terminal_reason", NulSafeText()),
    sa.Column("created_at", sa.Text(), nullable=False),
    sa.Column("updated_at", sa.Text(), nullable=False),
)
sa.Index("move_jobs_state_idx", move_jobs.c.state, move_jobs.c.updated_at)
sa.Index("move_jobs_object_idx", move_jobs.c.bucket, move_jobs.c.object_key)

move_job_transitions = sa.Table(
    "move_job_transitions",
    metadata,
    sa.Column(
        "idempotency_key",
        NulSafeText(),
        sa.ForeignKey("move_jobs.idempotency_key"),
        primary_key=True,
    ),
    sa.Column("sequence", sa.Integer(), primary_key=True),
    sa.Column("from_state", sa.Text()),
    sa.Column("to_state", sa.Text(), nullable=False),
    sa.Column("reason", NulSafeText(), nullable=False),
    sa.Column("created_at", sa.Text(), nullable=False),
)

audit_events = sa.Table(
    "audit_events",
    metadata,
    sa.Column("event_id", uuid_type, primary_key=True),
    sa.Column("schema_version", sa.Integer(), nullable=False),
    sa.Column("event_type", sa.Text(), nullable=False),
    sa.Column("outcome", sa.Text(), nullable=False),
    sa.Column("occurred_at", sa.Text(), nullable=False),
    sa.Column("recorded_at", sa.Text(), nullable=False),
    sa.Column("expires_at", sa.Text()),
    sa.Column("correlation_id", NulSafeText(), nullable=False),
    sa.Column("causation_id", uuid_type),
    sa.Column("actor_type", sa.Text(), nullable=False),
    sa.Column("actor_id", NulSafeText(), nullable=False),
    sa.Column("bucket", NulSafeText()),
    sa.Column("object_key", NulSafeText()),
    sa.Column("job_id", NulSafeText()),
    sa.Column("move_id", NulSafeText()),
    sa.Column("move_sequence", sa.BigInteger()),
    sa.Column("policy_name", NulSafeText()),
    sa.Column("policy_version", NulSafeText()),
    sa.Column("details", json_type, nullable=False, server_default=sa.text("'{}'")),
    sa.CheckConstraint(
        "schema_version > 0",
        name="audit_event_schema_version_positive",
    ),
    sa.CheckConstraint(
        "(bucket IS NULL AND object_key IS NULL) OR "
        "(bucket IS NOT NULL AND object_key IS NOT NULL)",
        name="audit_event_object_coordinates_complete",
    ),
    sa.CheckConstraint(
        "move_sequence IS NULL OR move_sequence > 0",
        name="audit_event_move_sequence_positive",
    ),
    sa.UniqueConstraint(
        "move_id",
        "move_sequence",
        name="uq_audit_events_move_sequence",
    ),
)
audit_move_heads = sa.Table(
    "audit_move_heads",
    metadata,
    sa.Column("move_id", NulSafeText(), primary_key=True),
    sa.Column("last_sequence", sa.BigInteger(), nullable=False),
    sa.Column("last_event_id", uuid_type, nullable=False),
    sa.CheckConstraint(
        "last_sequence > 0",
        name="audit_move_head_sequence_positive",
    ),
)
audit_event_tombstones = sa.Table(
    "audit_event_tombstones",
    metadata,
    sa.Column("event_id", uuid_type, primary_key=True),
    sa.Column("replay_digest", sa.Text(), nullable=False),
    sa.Column("causation_id", uuid_type),
    sa.Column("expires_at", sa.Text()),
    sa.CheckConstraint(
        "length(replay_digest) = 64",
        name="audit_event_tombstone_digest_length",
    ),
)
sa.Index(
    "audit_events_correlation_idx",
    audit_events.c.correlation_id,
    audit_events.c.occurred_at,
    audit_events.c.event_id,
)
sa.Index(
    "audit_events_job_idx",
    audit_events.c.job_id,
    audit_events.c.occurred_at,
)
sa.Index(
    "audit_events_move_idx",
    audit_events.c.move_id,
    audit_events.c.occurred_at,
)
sa.Index(
    "audit_events_object_idx",
    audit_events.c.bucket,
    audit_events.c.object_key,
    audit_events.c.occurred_at,
)
sa.Index(
    "audit_events_type_idx",
    audit_events.c.event_type,
    audit_events.c.outcome,
    audit_events.c.occurred_at,
)
sa.Index("audit_events_expiry_idx", audit_events.c.expires_at)

# Access telemetry is independent of object lifecycle and the immutable audit log.
# Expired rows remain bounded retry tombstones until the additional dedup horizon.
access_events = sa.Table(
    "access_events",
    metadata,
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
    sa.CheckConstraint("object_key IS NOT NULL OR kind = 'list'", name="access_event_coordinate"),
)
sa.Index(
    "access_events_object_time_idx",
    access_events.c.bucket,
    access_events.c.object_key,
    access_events.c.expired,
    access_events.c.occurred_at,
)
sa.Index(
    "access_events_retention_idx",
    access_events.c.expired,
    access_events.c.occurred_at,
    access_events.c.event_id,
)
