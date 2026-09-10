"""Regenerate the synthetic, privacy-filtered v1 smoke dataset (no storage I/O)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from cognistore.core.audit import AuditContext, AuditEvent, AuditRetentionPolicy
from cognistore.core.catalog import Catalog, ObjectRecord
from cognistore.core.policy import ContentAwarePolicy, SimplePolicy
from cognistore.core.policy_dataset import export_policy_dataset
from cognistore.core.policy_features import (
    FeatureState,
    MimePolicyFeature,
    PolicyFeatureProvenance,
    PolicyFeatures,
)
from cognistore.core.policy_snapshot import capture_policy_snapshot


def generate() -> dict:
    retention = AuditRetentionPolicy(None)
    catalog = Catalog(audit_retention=retention)
    features = PolicyFeatures(
        MimePolicyFeature(
            FeatureState.MISSING,
            PolicyFeatureProvenance("synthetic-fixture", 1, None),
        )
    )
    for index in range(60):
        at = datetime(2026, 9, 1 + 2 * (index // 20), tzinfo=timezone.utc) + timedelta(
            hours=index % 20
        )
        success = index % 20 < 10
        size = 100 + index % 10 if success else 10000 + index % 10
        policy = SimplePolicy(1024) if index % 2 else ContentAwarePolicy(size_threshold=1024)
        name = "simple" if index % 2 else "content"
        record = ObjectRecord(
            "synthetic", f"object-{index}.bin", size, "warm" if success else "hot"
        )
        decision = policy.evaluate(record.tier, size)
        assert decision.action == "move"
        snapshot = capture_policy_snapshot(
            record=record,
            features=features,
            policy=policy,
            allowed_tiers=["hot", "warm"],
            decision=decision,
            outcome="selected",
            policy_name=name,
            policy_version="1",
            decision_at=at,
        )
        decision_id, evidence_id = str(UUID(int=2 * index + 1)), str(UUID(int=2 * index + 2))
        context = AuditContext(
            correlation_id=f"fixture-{index}", actor_type="system", actor_id="fixture"
        )
        catalog.append_audit_event(
            AuditEvent.create(
                "policy.decision",
                "selected",
                context,
                event_id=decision_id,
                occurred_at=at,
                recorded_at=at,
                retention=retention,
                bucket=record.bucket,
                object_key=record.key,
                policy_name=name,
                policy_version="1",
                details={"dataset": snapshot},
            )
        )
        ended = at + timedelta(minutes=1)
        catalog.append_audit_event(
            AuditEvent.create(
                "move.completed" if success else "move.failed",
                "succeeded" if success else "failed",
                AuditContext(
                    correlation_id=context.correlation_id,
                    causation_id=decision_id,
                    actor_type="system",
                    actor_id="fixture",
                ),
                event_id=evidence_id,
                occurred_at=ended,
                recorded_at=ended,
                retention=retention,
                bucket=record.bucket,
                object_key=record.key,
                move_id=f"fixture-move-{index}",
            )
        )
    return export_policy_dataset(
        catalog, as_of="2026-09-08T00:00:00Z", observation_seconds=3600, seed="baseline-v1"
    )


if __name__ == "__main__":
    import json

    Path(__file__).with_name("dataset-v1.json").write_text(
        json.dumps(generate(), indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
