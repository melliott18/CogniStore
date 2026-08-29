from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa

from cognistore.core.move_jobs import MoveJobState
from cognistore.db import MigrationManager, SQLCatalog
from cognistore.db.schema import object_placements, objects, pools, tiers


def test_concurrent_sqlite_initialization_serializes_migrations(
    tmp_path: Path,
) -> None:
    database = tmp_path / "concurrent.sqlite3"
    worker_count = 8
    barrier = threading.Barrier(worker_count)

    def initialize() -> None:
        barrier.wait(timeout=10)
        with SQLCatalog(database) as catalog:
            assert MigrationManager().is_at_head(catalog.engine)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(initialize) for _ in range(worker_count)]
        for future in futures:
            future.result(timeout=30)

    with SQLCatalog(database, migrate=False) as catalog:
        assert MigrationManager().is_at_head(catalog.engine)
        with catalog.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_legacy_sqlite_upgrade_preserves_move_transition_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.sqlite3"
    manager = MigrationManager()
    catalog = SQLCatalog(database)
    manager.downgrade(catalog.engine, "0001_legacy_catalog")
    with catalog.engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO move_jobs(
                    idempotency_key, src_tier, dst_tier, bucket, object_key,
                    expected_size, source_metadata, state, owner_id,
                    lease_expires_at, verification_details, created_at, updated_at
                ) VALUES(
                    :idempotency_key, :src_tier, :dst_tier, :bucket, :object_key,
                    :expected_size, :source_metadata, :state, :owner_id,
                    :lease_expires_at, :verification_details, :created_at, :updated_at
                )
                """
            ),
            {
                "idempotency_key": "legacy:move",
                "src_tier": "hot",
                "dst_tier": "warm",
                "bucket": "bucket",
                "object_key": "object",
                "expected_size": 1,
                "source_metadata": '{"generation":"source:v1"}',
                "state": MoveJobState.PREPARED.value,
                "owner_id": "worker",
                "lease_expires_at": "2000-01-01T00:01:00.000000Z",
                "verification_details": "[]",
                "created_at": "2000-01-01T00:00:00.000000Z",
                "updated_at": "2000-01-01T00:00:00.000000Z",
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO move_job_transitions(
                    idempotency_key, sequence, from_state, to_state, reason,
                    created_at
                ) VALUES(
                    :idempotency_key, 1, NULL, :to_state, :reason, :created_at
                )
                """
            ),
            {
                "idempotency_key": "legacy:move",
                "to_state": MoveJobState.PREPARED.value,
                "reason": "move prepared",
                "created_at": "2000-01-01T00:00:00.000000Z",
            },
        )
    catalog.close()

    with SQLCatalog(database) as upgraded:
        assert manager.is_at_head(upgraded.engine)
        assert [
            transition.to_state for transition in upgraded.list_move_job_transitions("legacy:move")
        ] == [MoveJobState.PREPARED]
        with upgraded.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_downgrade_refuses_to_drop_an_object_without_a_placement(
    tmp_path: Path,
) -> None:
    manager = MigrationManager()
    with SQLCatalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.upsert("bucket", "object", size=1, tier="hot")
        with catalog.engine.begin() as connection:
            object_id = connection.execute(sa.select(objects.c.object_id)).scalar_one()
            connection.execute(
                sa.delete(object_placements).where(object_placements.c.object_id == object_id)
            )

        with pytest.raises(RuntimeError, match="objects without placements"):
            manager.downgrade(catalog.engine, "0001_legacy_catalog")

        assert manager.is_at_head(catalog.engine)
        assert "objects_legacy" not in sa.inspect(catalog.engine).get_table_names()
        with catalog.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(objects.c.object_id).where(objects.c.object_id == object_id)
                ).scalar_one()
                == object_id
            )


@pytest.mark.parametrize(
    "incompatible_state",
    ["pool", "tier-metadata", "unused-tier", "noncanonical-id"],
)
def test_downgrade_refuses_normalized_state_the_legacy_schema_cannot_represent(
    tmp_path: Path,
    incompatible_state: str,
) -> None:
    manager = MigrationManager()
    with SQLCatalog(tmp_path / f"{incompatible_state}.sqlite3") as catalog:
        catalog.upsert("bucket", "object", size=1, tier="hot")
        if incompatible_state == "pool":
            catalog.register_pool("pool-a", "hot", {"device": "nvme0"})
            with catalog.engine.begin() as connection:
                connection.execute(sa.update(object_placements).values(pool_id="pool-a"))
        elif incompatible_state == "tier-metadata":
            catalog.register_tier("hot", {"region": "west"})
        elif incompatible_state == "unused-tier":
            catalog.register_tier("archive")
        else:
            noncanonical_id = uuid4()
            with catalog.engine.begin() as connection:
                connection.execute(
                    sa.update(object_placements).values(placement_id=noncanonical_id)
                )

        with pytest.raises(RuntimeError, match="cannot downgrade.*legacy schema"):
            manager.downgrade(catalog.engine, "0001_legacy_catalog")

        assert manager.is_at_head(catalog.engine)
        assert catalog.get("bucket", "object") is not None
        with catalog.engine.connect() as connection:
            if incompatible_state == "pool":
                assert connection.execute(sa.select(pools.c.pool_id)).scalar_one() == "pool-a"
            elif incompatible_state == "tier-metadata":
                assert connection.execute(
                    sa.select(tiers.c.metadata).where(tiers.c.name == "hot")
                ).scalar_one() == {"region": "west"}
            elif incompatible_state == "unused-tier":
                assert (
                    connection.execute(
                        sa.select(tiers.c.name).where(tiers.c.name == "archive")
                    ).scalar_one()
                    == "archive"
                )
            else:
                assert (
                    connection.execute(sa.select(object_placements.c.placement_id)).scalar_one()
                    == noncanonical_id
                )
