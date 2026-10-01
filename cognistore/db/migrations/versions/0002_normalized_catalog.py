"""Normalize catalog objects and placements and provision pgvector.

Revision ID: 0002_normalized_catalog
Revises: 0001_legacy_catalog
"""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, UUID, uuid5

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import TypeEngine

from cognistore.db.types import NulSafeText

revision = "0002_normalized_catalog"
down_revision = "0001_legacy_catalog"
branch_labels = None
depends_on = None

_MIGRATED_AT = "1970-01-01T00:00:00.000000Z"
_BATCH_SIZE = 1000


def _uuid(kind: str, *values: str) -> UUID:
    identity = json.dumps([kind, *values], ensure_ascii=True, separators=(",", ":"))
    return uuid5(NAMESPACE_URL, identity)


def _json_type() -> TypeEngine[object]:
    return sa.JSON()


def _create_normalized_tables() -> None:
    json_type = _json_type()
    op.create_table(
        "tiers",
        sa.Column("name", NulSafeText(), primary_key=True),
        sa.Column("metadata", json_type, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_table(
        "pools",
        sa.Column("pool_id", NulSafeText(), primary_key=True),
        sa.Column("tier_name", NulSafeText(), nullable=False),
        sa.Column("metadata", json_type, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["tier_name"], ["tiers.name"], ondelete="RESTRICT"),
        sa.UniqueConstraint("pool_id", "tier_name", name="uq_pools_pool_tier"),
    )
    op.create_table(
        "objects",
        sa.Column("object_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("bucket", NulSafeText(), nullable=False),
        sa.Column("object_key", NulSafeText(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("metadata", json_type, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint("size >= 0", name="object_size_nonnegative"),
        sa.UniqueConstraint("bucket", "object_key", name="uq_objects_bucket_key"),
    )
    op.create_table(
        "object_placements",
        sa.Column("placement_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("object_id", sa.Uuid(as_uuid=True), nullable=False, unique=True),
        sa.Column("tier_name", NulSafeText(), nullable=False),
        sa.Column("pool_id", NulSafeText()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["object_id"], ["objects.object_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tier_name"], ["tiers.name"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["pool_id", "tier_name"],
            ["pools.pool_id", "pools.tier_name"],
            name="fk_object_placements_pool_tier",
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "object_mutation_fences",
        sa.Column("bucket", NulSafeText(), primary_key=True),
        sa.Column("object_key", NulSafeText(), primary_key=True),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_table(
        "move_job_claim_fences",
        sa.Column("idempotency_key", NulSafeText(), primary_key=True),
    )
    op.create_table(
        "catalog_schema_features",
        sa.Column("feature", sa.Text(), primary_key=True),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("owned", sa.Boolean(), nullable=False),
    )


def _copy_legacy_objects() -> None:
    connection = op.get_bind()
    legacy = sa.table(
        "objects_legacy",
        sa.column("bucket", NulSafeText()),
        sa.column("key", NulSafeText()),
        sa.column("size", sa.BigInteger()),
        sa.column("tier", NulSafeText()),
        sa.column("metadata", sa.Text()),
    )
    tiers_table = sa.table(
        "tiers",
        sa.column("name", NulSafeText()),
        sa.column("metadata", _json_type()),
        sa.column("created_at", sa.Text()),
        sa.column("updated_at", sa.Text()),
    )
    objects_table = sa.table(
        "objects",
        sa.column("object_id", sa.Uuid(as_uuid=True)),
        sa.column("bucket", NulSafeText()),
        sa.column("object_key", NulSafeText()),
        sa.column("size", sa.BigInteger()),
        sa.column("metadata", _json_type()),
        sa.column("created_at", sa.Text()),
        sa.column("updated_at", sa.Text()),
    )
    placements_table = sa.table(
        "object_placements",
        sa.column("placement_id", sa.Uuid(as_uuid=True)),
        sa.column("object_id", sa.Uuid(as_uuid=True)),
        sa.column("tier_name", NulSafeText()),
        sa.column("pool_id", NulSafeText()),
        sa.column("created_at", sa.Text()),
        sa.column("updated_at", sa.Text()),
    )
    fences_table = sa.table(
        "object_mutation_fences",
        sa.column("bucket", NulSafeText()),
        sa.column("object_key", NulSafeText()),
        sa.column("generation", sa.BigInteger()),
    )
    legacy_move_jobs = sa.table(
        "move_jobs",
        sa.column("src_tier", NulSafeText()),
        sa.column("dst_tier", NulSafeText()),
    )
    tier_names: set[str] = set(
        connection.execute(sa.select(legacy.c.tier).distinct().order_by(legacy.c.tier)).scalars()
    )
    tier_names.update(
        connection.execute(sa.select(legacy_move_jobs.c.src_tier).distinct()).scalars()
    )
    tier_names.update(
        connection.execute(sa.select(legacy_move_jobs.c.dst_tier).distinct()).scalars()
    )
    for tier in sorted(tier_names):
        connection.execute(
            sa.insert(tiers_table).values(
                name=tier,
                metadata={},
                created_at=_MIGRATED_AT,
                updated_at=_MIGRATED_AT,
            )
        )

    rows = connection.execute(
        sa.select(
            legacy.c.bucket,
            legacy.c.key,
            legacy.c.size,
            legacy.c.tier,
            legacy.c.metadata,
        ).order_by(legacy.c.bucket, legacy.c.key)
    )
    while batch := rows.fetchmany(_BATCH_SIZE):
        object_values = []
        placement_values = []
        fence_values = []
        for row in batch:
            object_id = _uuid("object", row.bucket, row.key)
            raw_metadata = row.metadata
            metadata = json.loads(raw_metadata) if raw_metadata else {}
            object_values.append(
                {
                    "object_id": object_id,
                    "bucket": row.bucket,
                    "object_key": row.key,
                    "size": row.size,
                    "metadata": metadata,
                    "created_at": _MIGRATED_AT,
                    "updated_at": _MIGRATED_AT,
                }
            )
            placement_values.append(
                {
                    "placement_id": _uuid("placement", str(object_id)),
                    "object_id": object_id,
                    "tier_name": row.tier,
                    "pool_id": None,
                    "created_at": _MIGRATED_AT,
                    "updated_at": _MIGRATED_AT,
                }
            )
            fence_values.append({"bucket": row.bucket, "object_key": row.key, "generation": 0})
        connection.execute(sa.insert(objects_table), object_values)
        connection.execute(sa.insert(placements_table), placement_values)
        connection.execute(sa.insert(fences_table), fence_values)


def _normalize_sqlite_legacy_move_schema() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return
    idempotency_column = next(
        column
        for column in sa.inspect(connection).get_columns("move_jobs")
        if column["name"] == "idempotency_key"
    )
    if not idempotency_column["nullable"]:
        return
    if connection.execute(
        sa.text("SELECT count(*) FROM move_jobs WHERE idempotency_key IS NULL")
    ).scalar_one():
        raise RuntimeError("cannot migrate move jobs with a NULL idempotency key")

    # Prototype SQLite declared ``TEXT PRIMARY KEY`` without ``NOT NULL``.
    # Rebuild the parent only after copying and dropping the child table so
    # SQLite's foreign-key actions cannot erase transition history.
    op.create_table(
        "move_job_transitions_0002_backup",
        sa.Column("idempotency_key", NulSafeText(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.Text()),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column("reason", NulSafeText(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.execute(
        "INSERT INTO move_job_transitions_0002_backup "
        "SELECT idempotency_key, sequence, from_state, to_state, reason, created_at "
        "FROM move_job_transitions"
    )
    op.drop_table("move_job_transitions")
    with op.batch_alter_table("move_jobs", recreate="always") as batch_op:
        batch_op.alter_column(
            "idempotency_key",
            existing_type=NulSafeText(),
            nullable=False,
        )
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
        sa.PrimaryKeyConstraint(
            "idempotency_key",
            "sequence",
            name="pk_move_job_transitions",
        ),
    )
    op.execute(
        "INSERT INTO move_job_transitions "
        "SELECT idempotency_key, sequence, from_state, to_state, reason, created_at "
        "FROM move_job_transitions_0002_backup"
    )
    op.drop_table("move_job_transitions_0002_backup")


def upgrade() -> None:
    connection = op.get_bind()
    _normalize_sqlite_legacy_move_schema()
    columns = {column["name"] for column in sa.inspect(connection).get_columns("move_jobs")}
    if "destination_generation" not in columns:
        op.add_column("move_jobs", sa.Column("destination_generation", NulSafeText()))

    vector_available = False
    vector_owned = False
    if connection.dialect.name == "postgresql":
        vector_available = bool(
            connection.execute(
                sa.text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            ).scalar()
        )
        if not vector_available:
            op.execute("CREATE EXTENSION vector")
            vector_available = True
            vector_owned = True
        op.alter_column(
            "move_jobs",
            "source_metadata",
            existing_type=sa.Text(),
            type_=postgresql.JSON(),
            postgresql_using="source_metadata::json",
        )
        op.execute("ALTER TABLE move_jobs ALTER COLUMN verification_details DROP DEFAULT")
        op.alter_column(
            "move_jobs",
            "verification_details",
            existing_type=sa.Text(),
            type_=postgresql.JSON(),
            postgresql_using="verification_details::json",
        )
        op.execute("ALTER TABLE move_jobs ALTER COLUMN verification_details SET DEFAULT '[]'::json")

    op.rename_table("objects", "objects_legacy")
    _create_normalized_tables()
    connection.execute(
        sa.text(
            "INSERT INTO catalog_schema_features(feature, available, owned) "
            "VALUES ('pgvector', :available, :owned)"
        ),
        {"available": vector_available, "owned": vector_owned},
    )
    _copy_legacy_objects()
    op.drop_table("objects_legacy")


def downgrade() -> None:
    connection = op.get_bind()
    feature = (
        connection.execute(
            sa.text(
                "SELECT available, owned FROM catalog_schema_features WHERE feature = 'pgvector'"
            )
        )
        .mappings()
        .first()
    )
    normalized_objects = sa.table(
        "objects",
        sa.column("object_id", sa.Uuid(as_uuid=True)),
        sa.column("bucket", NulSafeText()),
        sa.column("object_key", NulSafeText()),
        sa.column("size", sa.BigInteger()),
        sa.column("metadata", _json_type()),
    )
    normalized_placements = sa.table(
        "object_placements",
        sa.column("placement_id", sa.Uuid(as_uuid=True)),
        sa.column("object_id", sa.Uuid(as_uuid=True)),
        sa.column("tier_name", NulSafeText()),
        sa.column("pool_id", NulSafeText()),
    )
    normalized_tiers = sa.table(
        "tiers",
        sa.column("name", NulSafeText()),
        sa.column("metadata", _json_type()),
    )
    normalized_pools = sa.table(
        "pools",
        sa.column("pool_id", NulSafeText()),
    )
    normalized_move_jobs = sa.table(
        "move_jobs",
        sa.column("src_tier", NulSafeText()),
        sa.column("dst_tier", NulSafeText()),
    )
    missing_placements = connection.execute(
        sa.select(sa.func.count())
        .select_from(
            normalized_objects.outerjoin(
                normalized_placements,
                normalized_placements.c.object_id == normalized_objects.c.object_id,
            )
        )
        .where(normalized_placements.c.object_id.is_(None))
    ).scalar_one()
    if missing_placements:
        raise RuntimeError("cannot downgrade a catalog containing objects without placements")

    # The legacy objects table can encode only an object's current tier. Fail
    # before any DDL when normalized domain state would otherwise be discarded
    # silently. The transaction remains at the current migration head.
    incompatible_state: list[str] = []
    pool_count = connection.execute(
        sa.select(sa.func.count()).select_from(normalized_pools)
    ).scalar_one()
    if pool_count:
        incompatible_state.append("storage pools")
    tier_rows = connection.execute(
        sa.select(normalized_tiers.c.name, normalized_tiers.c.metadata)
    ).mappings()
    tier_names: set[str] = set()
    tiers_with_metadata = False
    for row in tier_rows:
        tier_names.add(row["name"])
        tiers_with_metadata = tiers_with_metadata or bool(row["metadata"])
    if tiers_with_metadata:
        incompatible_state.append("tier metadata")
    referenced_tiers: set[str] = set(
        connection.execute(sa.select(normalized_placements.c.tier_name).distinct()).scalars()
    )
    referenced_tiers.update(
        connection.execute(sa.select(normalized_move_jobs.c.src_tier).distinct()).scalars()
    )
    referenced_tiers.update(
        connection.execute(sa.select(normalized_move_jobs.c.dst_tier).distinct()).scalars()
    )
    if tier_names - referenced_tiers:
        incompatible_state.append("tiers absent from placements and move journals")
    identity_rows = connection.execute(
        sa.select(
            normalized_objects.c.object_id,
            normalized_objects.c.bucket,
            normalized_objects.c.object_key,
            normalized_placements.c.placement_id,
        ).select_from(
            normalized_objects.join(
                normalized_placements,
                normalized_placements.c.object_id == normalized_objects.c.object_id,
            )
        )
    ).mappings()
    if any(
        row["object_id"] != _uuid("object", row["bucket"], row["object_key"])
        or row["placement_id"] != _uuid("placement", str(row["object_id"]))
        for row in identity_rows
    ):
        incompatible_state.append("noncanonical object or placement identities")
    if incompatible_state:
        raise RuntimeError(
            "cannot downgrade normalized catalog state into the legacy schema: "
            + ", ".join(incompatible_state)
        )
    op.create_table(
        "objects_legacy",
        sa.Column("bucket", NulSafeText(), nullable=False),
        sa.Column("key", NulSafeText(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("tier", NulSafeText(), nullable=False),
        sa.Column("metadata", sa.Text()),
        sa.PrimaryKeyConstraint("bucket", "key", name="pk_objects_legacy"),
    )
    rows = connection.execute(
        sa.select(
            normalized_objects.c.bucket,
            normalized_objects.c.object_key,
            normalized_objects.c.size,
            normalized_objects.c.metadata,
            normalized_placements.c.tier_name,
        )
        .select_from(
            normalized_objects.join(
                normalized_placements,
                normalized_placements.c.object_id == normalized_objects.c.object_id,
            )
        )
        .order_by(normalized_objects.c.bucket, normalized_objects.c.object_key)
    ).mappings()
    legacy = sa.table(
        "objects_legacy",
        sa.column("bucket", NulSafeText()),
        sa.column("key", NulSafeText()),
        sa.column("size", sa.BigInteger()),
        sa.column("tier", NulSafeText()),
        sa.column("metadata", sa.Text()),
    )
    while batch := rows.fetchmany(_BATCH_SIZE):
        connection.execute(
            sa.insert(legacy),
            [
                {
                    "bucket": row["bucket"],
                    "key": row["object_key"],
                    "size": row["size"],
                    "tier": row["tier_name"],
                    "metadata": json.dumps(row["metadata"] or {}),
                }
                for row in batch
            ],
        )
    op.drop_table("object_placements")
    op.drop_table("object_mutation_fences")
    op.drop_table("move_job_claim_fences")
    op.drop_table("pools")
    op.drop_table("objects")
    op.drop_table("tiers")
    op.drop_table("catalog_schema_features")
    op.rename_table("objects_legacy", "objects")
    if connection.dialect.name == "postgresql":
        op.alter_column(
            "move_jobs",
            "source_metadata",
            existing_type=postgresql.JSON(),
            type_=sa.Text(),
            postgresql_using="source_metadata::text",
        )
        op.execute("ALTER TABLE move_jobs ALTER COLUMN verification_details DROP DEFAULT")
        op.alter_column(
            "move_jobs",
            "verification_details",
            existing_type=postgresql.JSON(),
            type_=sa.Text(),
            postgresql_using="verification_details::text",
        )
        op.execute("ALTER TABLE move_jobs ALTER COLUMN verification_details SET DEFAULT '[]'::text")
        if feature and feature["owned"]:
            op.execute("DROP EXTENSION IF EXISTS vector")
