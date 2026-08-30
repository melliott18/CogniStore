"""Add append-oriented operational audit events.

Revision ID: 0003_audit_events
Revises: 0002_normalized_catalog
"""

from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa
from alembic import op

from cognistore.core.audit import (
    AuditContext,
    AuditEvent,
    AuditEventType,
    AuditOutcome,
    AuditRetentionPolicy,
    redact_audit_event,
    stable_audit_event_id,
)
from cognistore.db.types import NulSafeText
from cognistore.utils.redaction import redact, redact_text

revision = "0003_audit_events"
down_revision = "0002_normalized_catalog"
branch_labels = None
depends_on = None


def _transition_vocabulary(to_state: str) -> tuple[AuditEventType, AuditOutcome]:
    if to_state == "prepared":
        return AuditEventType.MOVE_PREPARED, AuditOutcome.STARTED
    if to_state == "completed":
        return AuditEventType.MOVE_COMPLETED, AuditOutcome.SUCCEEDED
    if to_state == "failed":
        return AuditEventType.MOVE_FAILED, AuditOutcome.FAILED
    return AuditEventType.MOVE_TRANSITIONED, AuditOutcome.SUCCEEDED


def _redact_move_diagnostics() -> None:
    """Repair catalogs upgraded to 0002 before diagnostic redaction existed."""

    connection = op.get_bind()
    jobs = sa.table(
        "move_jobs",
        sa.column("idempotency_key", NulSafeText()),
        sa.column("verification_details", sa.JSON()),
        sa.column("terminal_reason", NulSafeText()),
    )
    transitions = sa.table(
        "move_job_transitions",
        sa.column("idempotency_key", NulSafeText()),
        sa.column("sequence", sa.Integer()),
        sa.column("reason", NulSafeText()),
    )
    job_rows = connection.execute(
        sa.select(
            jobs.c.idempotency_key,
            jobs.c.verification_details,
            jobs.c.terminal_reason,
        )
    ).mappings().all()
    for row in job_rows:
        raw_details = row["verification_details"]
        safe_details = redact(raw_details)
        terminal_reason = row["terminal_reason"]
        safe_terminal_reason = (
            None
            if terminal_reason is None
            else redact_text(str(terminal_reason))
        )
        if (
            safe_details != raw_details
            or safe_terminal_reason != terminal_reason
        ):
            connection.execute(
                sa.update(jobs)
                .where(jobs.c.idempotency_key == row["idempotency_key"])
                .values(
                    verification_details=safe_details,
                    terminal_reason=safe_terminal_reason,
                )
            )
    transition_rows = connection.execute(
        sa.select(
            transitions.c.idempotency_key,
            transitions.c.sequence,
            transitions.c.reason,
        )
    ).mappings().all()
    for row in transition_rows:
        safe_reason = redact_text(str(row["reason"]))
        if safe_reason != row["reason"]:
            connection.execute(
                sa.update(transitions)
                .where(
                    transitions.c.idempotency_key == row["idempotency_key"],
                    transitions.c.sequence == row["sequence"],
                )
                .values(reason=safe_reason)
            )


def _backfill_move_transitions() -> None:
    """Create a deterministic chain for move history that predates this table."""

    connection = op.get_bind()
    audit_table = sa.table(
        "audit_events",
        sa.column("event_id", sa.Uuid(as_uuid=True)),
        sa.column("schema_version", sa.Integer()),
        sa.column("event_type", sa.Text()),
        sa.column("outcome", sa.Text()),
        sa.column("occurred_at", sa.Text()),
        sa.column("recorded_at", sa.Text()),
        sa.column("expires_at", sa.Text()),
        sa.column("correlation_id", NulSafeText()),
        sa.column("causation_id", sa.Uuid(as_uuid=True)),
        sa.column("actor_type", sa.Text()),
        sa.column("actor_id", NulSafeText()),
        sa.column("bucket", NulSafeText()),
        sa.column("object_key", NulSafeText()),
        sa.column("job_id", NulSafeText()),
        sa.column("move_id", NulSafeText()),
        sa.column("move_sequence", sa.BigInteger()),
        sa.column("policy_name", NulSafeText()),
        sa.column("policy_version", NulSafeText()),
        sa.column("details", sa.JSON()),
    )
    head_table = sa.table(
        "audit_move_heads",
        sa.column("move_id", NulSafeText()),
        sa.column("last_sequence", sa.BigInteger()),
        sa.column("last_event_id", sa.Uuid(as_uuid=True)),
    )
    transitions = sa.table(
        "move_job_transitions",
        sa.column("idempotency_key", NulSafeText()),
        sa.column("sequence", sa.Integer()),
        sa.column("from_state", sa.Text()),
        sa.column("to_state", sa.Text()),
        sa.column("reason", NulSafeText()),
        sa.column("created_at", sa.Text()),
    )
    jobs = sa.table(
        "move_jobs",
        sa.column("idempotency_key", NulSafeText()),
        sa.column("src_tier", NulSafeText()),
        sa.column("dst_tier", NulSafeText()),
        sa.column("bucket", NulSafeText()),
        sa.column("object_key", NulSafeText()),
        sa.column("expected_size", sa.BigInteger()),
    )
    rows = connection.execute(
        sa.select(
            transitions.c.idempotency_key,
            transitions.c.sequence,
            transitions.c.from_state,
            transitions.c.to_state,
            transitions.c.reason,
            transitions.c.created_at,
            jobs.c.src_tier,
            jobs.c.dst_tier,
            jobs.c.bucket,
            jobs.c.object_key,
            jobs.c.expected_size,
        )
        .select_from(
            transitions.join(
                jobs,
                jobs.c.idempotency_key == transitions.c.idempotency_key,
            )
        )
        .order_by(transitions.c.idempotency_key, transitions.c.sequence)
    ).mappings().all()
    migration_config = op.get_context().config
    configured_retention = (
        None
        if migration_config is None
        else migration_config.attributes.get("audit_retention")
    )
    retention = (
        configured_retention
        if isinstance(configured_retention, AuditRetentionPolicy)
        else AuditRetentionPolicy()
    )
    previous_by_move: dict[str, str] = {}
    last_by_move: dict[str, tuple[int, str]] = {}
    for row in rows:
        move_id = str(row["idempotency_key"])
        transition_sequence = int(row["sequence"])
        event_type, outcome = _transition_vocabulary(str(row["to_state"]))
        event = redact_audit_event(
            AuditEvent.create(
                event_type,
                outcome,
                AuditContext(
                    correlation_id=move_id,
                    actor_type="migration",
                    actor_id="0003-audit-events",
                    causation_id=previous_by_move.get(move_id),
                ),
                event_id=stable_audit_event_id(
                    "move-transition",
                    move_id,
                    str(transition_sequence),
                ),
                occurred_at=str(row["created_at"]),
                recorded_at=str(row["created_at"]),
                retention=retention,
                bucket=str(row["bucket"]),
                object_key=str(row["object_key"]),
                move_id=move_id,
                details={
                    "transition_sequence": transition_sequence,
                    "from_state": row["from_state"],
                    "to_state": row["to_state"],
                    "reason": row["reason"],
                    "src_tier": row["src_tier"],
                    "dst_tier": row["dst_tier"],
                    "expected_size": row["expected_size"],
                    "backfilled": True,
                },
            )
        )
        connection.execute(
            sa.insert(audit_table).values(
                event_id=UUID(event.event_id),
                schema_version=event.schema_version,
                event_type=event.event_type,
                outcome=event.outcome,
                occurred_at=event.occurred_at,
                recorded_at=event.recorded_at,
                expires_at=event.expires_at,
                correlation_id=event.correlation_id,
                causation_id=(
                    None if event.causation_id is None else UUID(event.causation_id)
                ),
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                bucket=event.bucket,
                object_key=event.object_key,
                job_id=event.job_id,
                move_id=event.move_id,
                move_sequence=transition_sequence,
                policy_name=event.policy_name,
                policy_version=event.policy_version,
                details=dict(event.details),
            )
        )
        previous_by_move[move_id] = event.event_id
        assert event.move_id is not None
        last_by_move[event.move_id] = (transition_sequence, event.event_id)
    if last_by_move:
        connection.execute(
            sa.insert(head_table),
            [
                {
                    "move_id": move_id,
                    "last_sequence": sequence,
                    "last_event_id": UUID(event_id),
                }
                for move_id, (sequence, event_id) in sorted(last_by_move.items())
            ],
        )


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text()),
        sa.Column("correlation_id", NulSafeText(), nullable=False),
        sa.Column("causation_id", sa.Uuid(as_uuid=True)),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", NulSafeText(), nullable=False),
        sa.Column("bucket", NulSafeText()),
        sa.Column("object_key", NulSafeText()),
        sa.Column("job_id", NulSafeText()),
        sa.Column("move_id", NulSafeText()),
        sa.Column("move_sequence", sa.BigInteger()),
        sa.Column("policy_name", NulSafeText()),
        sa.Column("policy_version", NulSafeText()),
        sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
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
    op.create_index(
        "audit_events_correlation_idx",
        "audit_events",
        ["correlation_id", "occurred_at", "event_id"],
    )
    op.create_index(
        "audit_events_job_idx",
        "audit_events",
        ["job_id", "occurred_at"],
    )
    op.create_index(
        "audit_events_move_idx",
        "audit_events",
        ["move_id", "occurred_at"],
    )
    op.create_index(
        "audit_events_object_idx",
        "audit_events",
        ["bucket", "object_key", "occurred_at"],
    )
    op.create_index(
        "audit_events_type_idx",
        "audit_events",
        ["event_type", "outcome", "occurred_at"],
    )
    op.create_index(
        "audit_events_expiry_idx",
        "audit_events",
        ["expires_at"],
    )
    op.create_table(
        "audit_move_heads",
        sa.Column("move_id", NulSafeText(), primary_key=True),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("last_event_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "last_sequence > 0",
            name="audit_move_head_sequence_positive",
        ),
    )
    op.create_table(
        "audit_event_tombstones",
        sa.Column("event_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("replay_digest", sa.Text(), nullable=False),
        sa.Column("causation_id", sa.Uuid(as_uuid=True)),
        sa.Column("expires_at", sa.Text()),
        sa.CheckConstraint(
            "length(replay_digest) = 64",
            name="audit_event_tombstone_digest_length",
        ),
    )
    _redact_move_diagnostics()
    _backfill_move_transitions()


def downgrade() -> None:
    op.drop_table("audit_event_tombstones")
    op.drop_table("audit_move_heads")
    op.drop_index("audit_events_expiry_idx", table_name="audit_events")
    op.drop_index("audit_events_type_idx", table_name="audit_events")
    op.drop_index("audit_events_object_idx", table_name="audit_events")
    op.drop_index("audit_events_move_idx", table_name="audit_events")
    op.drop_index("audit_events_job_idx", table_name="audit_events")
    op.drop_index("audit_events_correlation_idx", table_name="audit_events")
    op.drop_table("audit_events")
