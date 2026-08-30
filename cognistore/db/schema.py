from __future__ import annotations

import sqlalchemy as sa

from .types import NulSafeText

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

tiers = sa.Table(
    "tiers",
    metadata,
    sa.Column("name", NulSafeText(), primary_key=True),
    sa.Column("metadata", json_type, nullable=False, server_default=sa.text("'{}'")),
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
    sa.Column("created_at", sa.Text(), nullable=False),
    sa.Column("updated_at", sa.Text(), nullable=False),
    sa.CheckConstraint("size >= 0", name="object_size_nonnegative"),
    sa.UniqueConstraint("bucket", "object_key", name="uq_objects_bucket_key"),
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
