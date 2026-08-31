"""Backend-neutral conformance tests for catalog object reads.

Concrete test classes inherit :class:`CatalogStoreConformance` and provide a
fresh ``catalog`` fixture.  The same cases therefore define the observable
snapshot contract for the in-memory, SQLite, and PostgreSQL backends.
"""

from __future__ import annotations

import pytest

from cognistore.core.catalog import CatalogStore, ObjectRecord


class CatalogStoreConformance:
    """Tests that every catalog backend's object reads must pass."""

    @pytest.fixture
    def catalog(self) -> CatalogStore:
        raise NotImplementedError("concrete conformance tests must provide a catalog")

    def test_get_returns_a_detached_snapshot(self, catalog: CatalogStore) -> None:
        metadata = {
            "classification": "internal",
            "nested": {"labels": ["original"]},
        }
        expected = ObjectRecord(
            bucket="bucket",
            key="object",
            size=11,
            tier="hot",
            metadata={
                "classification": "internal",
                "nested": {"labels": ["original"]},
            },
        )
        catalog.upsert(
            expected.bucket,
            expected.key,
            size=expected.size,
            tier=expected.tier,
            metadata=metadata,
        )

        # The caller retains ownership of values passed into a write method.
        metadata["classification"] = "input-mutated"
        metadata["nested"]["labels"].append("input-mutated")  # type: ignore[index,union-attr]

        first = catalog.get(expected.bucket, expected.key)
        second = catalog.get(expected.bucket, expected.key)

        assert first == expected
        assert second == expected
        assert first is not second
        assert first is not None
        first.tier = "cold"
        first.metadata["classification"] = "snapshot-mutated"
        first.metadata["nested"]["labels"].append("snapshot-mutated")  # type: ignore[index,union-attr]

        assert second == expected
        assert catalog.get(expected.bucket, expected.key) == expected

        # Persistence remains available through an explicit mutation method.
        catalog.update_placement(expected.bucket, expected.key, "warm")
        updated = catalog.get(expected.bucket, expected.key)
        assert updated == ObjectRecord(
            bucket=expected.bucket,
            key=expected.key,
            size=expected.size,
            tier="warm",
            metadata=expected.metadata,
        )

        scan_metadata = {"nested": {"embedding": [0.25, 0.75]}}
        fence = catalog.capture_scan_fence("bucket", "scanned")
        assert catalog.upsert_scan_observation(
            "bucket",
            "scanned",
            size=7,
            tier="hot",
            generation="hot:v1",
            metadata=scan_metadata,
            fence=fence,
        )
        scan_metadata["nested"]["embedding"][0] = 1.0  # type: ignore[index]

        assert catalog.get("bucket", "scanned") == ObjectRecord(
            bucket="bucket",
            key="scanned",
            size=7,
            tier="hot",
            metadata={"nested": {"embedding": [0.25, 0.75]}},
        )

    def test_list_returns_ordered_independent_snapshots(
        self, catalog: CatalogStore
    ) -> None:
        shared_metadata = {"nested": {"labels": ["original"]}}
        for key in ("z-last", "a-first", "m-middle"):
            catalog.upsert(
                "bucket",
                key,
                size=len(key),
                tier="hot",
                metadata=shared_metadata,
            )
        catalog.upsert(
            "other-bucket",
            "a-first",
            size=1,
            tier="cold",
            metadata={"excluded": True},
        )

        expected = [
            ObjectRecord(
                bucket="bucket",
                key=key,
                size=len(key),
                tier="hot",
                metadata={"nested": {"labels": ["original"]}},
            )
            for key in ("a-first", "m-middle", "z-last")
        ]
        first = catalog.list("bucket")
        second = catalog.list("bucket")

        assert first == expected
        assert second == expected
        assert all(left is not right for left, right in zip(first, second))

        first[0].key = "changed"
        first[0].metadata["nested"]["labels"].append("changed")  # type: ignore[index,union-attr]

        assert first[1] == expected[1]
        assert second == expected
        assert catalog.list("bucket") == expected
        assert catalog.list("bucket", prefix="m-") == [expected[1]]

    def test_missing_object_behavior_is_backend_neutral(
        self, catalog: CatalogStore
    ) -> None:
        assert catalog.get("bucket", "missing") is None
        assert catalog.list("bucket") == []

        catalog.delete("bucket", "missing")
        catalog.delete("bucket", "missing")

        with pytest.raises(KeyError) as error:
            catalog.update_placement("bucket", "missing", "warm")
        assert error.value.args == ("Object not found: bucket/missing",)

        for invalid_size in (-1, True, 2**63):
            with pytest.raises(ValueError, match="non-negative integer"):
                catalog.upsert(
                    "bucket",
                    "invalid",
                    size=invalid_size,
                    tier="hot",
                )
