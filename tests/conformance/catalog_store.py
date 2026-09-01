"""Backend-neutral conformance tests for catalog object reads.

Concrete test classes inherit :class:`CatalogStoreConformance` and provide a
fresh ``catalog`` fixture.  The same cases therefore define the observable
snapshot contract for the in-memory, SQLite, and PostgreSQL backends.
"""

from __future__ import annotations

from io import BytesIO

import pytest

from cognistore.core.catalog import CatalogStore, ObjectRecord
from cognistore.core.content_identity import ContentIdentityBuilder, ObjectContent


def _content(data: bytes, *, chunk_size: int = 4) -> ObjectContent:
    return ContentIdentityBuilder(chunk_size=chunk_size).build(
        BytesIO(data),
        expected_size=len(data),
    )


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

    def test_iter_objects_returns_global_bucket_key_order(
        self, catalog: CatalogStore
    ) -> None:
        insertion_order = [
            ("z-bucket", "a-first"),
            ("a-bucket", "z-last"),
            ("middle-bucket", "m-middle"),
            ("a-bucket", "a-first"),
            ("z-bucket", "0-before-letters"),
        ]
        for bucket, key in insertion_order:
            catalog.upsert(
                bucket,
                key,
                size=len(bucket) + len(key),
                tier="hot",
            )

        records = list(catalog.iter_objects(batch_size=2))

        assert [(record.bucket, record.key) for record in records] == sorted(
            insertion_order
        )

    def test_iter_objects_returns_detached_nested_metadata_snapshots(
        self, catalog: CatalogStore
    ) -> None:
        metadata = {
            "nested": {
                "labels": ["original"],
                "attributes": {"retained": True},
            }
        }
        for key in ("first", "second"):
            catalog.upsert(
                "bucket",
                key,
                size=len(key),
                tier="hot",
                metadata=metadata,
            )

        expected_metadata = {
            "nested": {
                "labels": ["original"],
                "attributes": {"retained": True},
            }
        }
        first = list(catalog.iter_objects(batch_size=1))
        second = list(catalog.iter_objects(batch_size=2))

        first[0].metadata["nested"]["labels"].append("snapshot-mutated")  # type: ignore[index,union-attr]
        first[0].metadata["nested"]["attributes"]["retained"] = False  # type: ignore[index,union-attr]

        assert first[1].metadata == expected_metadata
        assert [record.metadata for record in second] == [
            expected_metadata,
            expected_metadata,
        ]
        assert all(left is not right for left, right in zip(first, second))
        assert [record.metadata for record in catalog.iter_objects(batch_size=1)] == [
            expected_metadata,
            expected_metadata,
        ]

    def test_iter_objects_rejects_invalid_batch_sizes(
        self, catalog: CatalogStore
    ) -> None:
        for invalid_batch_size in (0, -1, True, False, 1.5, "1", None):
            with pytest.raises(
                ValueError,
                match="^batch_size must be a positive integer$",
            ):
                list(catalog.iter_objects(batch_size=invalid_batch_size))  # type: ignore[arg-type]

    def test_iter_objects_can_be_closed_early(self, catalog: CatalogStore) -> None:
        for key in ("first", "second", "third"):
            catalog.upsert("bucket", key, size=len(key), tier="hot")

        iterator = catalog.iter_objects(batch_size=1)
        assert next(iterator).key == "first"

        close = getattr(iterator, "close", None)
        assert callable(close)
        close()
        close()

        catalog.upsert("bucket", "after-close", size=11, tier="warm")
        assert [record.key for record in catalog.iter_objects(batch_size=2)] == [
            "after-close",
            "first",
            "second",
            "third",
        ]

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

        assert catalog.get_object_content("bucket", "missing") is None

    def test_scan_content_is_atomic_and_preserves_independent_metadata(
        self, catalog: CatalogStore
    ) -> None:
        data = b"abcdefghij"
        content = _content(data)
        catalog.upsert(
            "bucket",
            "object",
            size=len(data),
            tier="hot",
            metadata={
                "classification": "retain",
                "nested": {"labels": ["independent"]},
                "sha256": "legacy-sample",
                "content_identity": {"forged": "old"},
            },
        )
        fence = catalog.capture_scan_fence("bucket", "object")

        assert catalog.upsert_scan_observation(
            "bucket",
            "object",
            size=len(data),
            tier="hot",
            generation="hot:v1",
            metadata={
                "path": "/observed",
                "sha256": "caller-mismatch",
                "content_identity": {"forged": "caller"},
            },
            fence=fence,
            content=content,
        )

        record = catalog.get("bucket", "object")
        first = catalog.get_object_content("bucket", "object")
        second = catalog.get_object_content("bucket", "object")
        assert record is not None
        assert record.metadata == {
            "classification": "retain",
            "nested": {"labels": ["independent"]},
            "sha256": content.sha256,
            "path": "/observed",
            "content_identity": content.to_metadata(),
        }
        assert first == content
        assert second == content

        # A scan without full content identity atomically clears both the
        # normalized mapping and its reserved metadata projection.
        fence = catalog.capture_scan_fence("bucket", "object")
        assert catalog.upsert_scan_observation(
            "bucket",
            "object",
            size=len(data),
            tier="hot",
            generation="hot:v2",
            metadata={"content_identity": {"forged": "replacement"}},
            fence=fence,
        )
        rescanned = catalog.get("bucket", "object")
        assert rescanned is not None
        assert "content_identity" not in rescanned.metadata
        assert catalog.get_object_content("bucket", "object") is None

    def test_manifest_replacement_never_exposes_mixed_layouts(
        self, catalog: CatalogStore
    ) -> None:
        data = b"abcdefghij"
        first = _content(data, chunk_size=4)
        second = _content(data, chunk_size=3)

        for generation, content in (("hot:v1", first), ("hot:v2", second)):
            fence = catalog.capture_scan_fence("bucket", "object")
            assert catalog.upsert_scan_observation(
                "bucket",
                "object",
                size=len(data),
                tier="hot",
                generation=generation,
                metadata={},
                fence=fence,
                content=content,
            )

        persisted = catalog.get_object_content("bucket", "object")
        assert persisted == second
        assert persisted is not None
        assert [chunk.index for chunk in persisted.chunks] == [0, 1, 2, 3]
        assert [chunk.offset for chunk in persisted.chunks] == [0, 3, 6, 9]

    def test_content_mapping_lifecycle_is_backend_neutral(
        self, catalog: CatalogStore
    ) -> None:
        data = b"shared bytes"
        content = _content(data)
        for key in ("first", "second"):
            fence = catalog.capture_scan_fence("bucket", key)
            assert catalog.upsert_scan_observation(
                "bucket",
                key,
                size=len(data),
                tier="hot",
                generation=f"hot:{key}",
                metadata={"content_identity": content.to_metadata()},
                fence=fence,
                content=content,
            )

        catalog.update_placement("bucket", "first", "warm")
        catalog.upsert_placement(
            "bucket",
            "first",
            size=len(data),
            tier="cold",
            checksum=content.sha256,
        )
        assert catalog.get_object_content("bucket", "first") == content

        replacement_checksum = "0" * 64
        catalog.upsert_placement(
            "bucket",
            "first",
            size=len(data),
            tier="archive",
            checksum=replacement_checksum,
        )
        replaced = catalog.get("bucket", "first")
        assert replaced is not None
        assert replaced.metadata["sha256"] == replacement_checksum
        assert "content_identity" not in replaced.metadata
        assert catalog.get_object_content("bucket", "first") is None

        catalog.delete("bucket", "first")
        assert catalog.get_object_content("bucket", "first") is None
        assert catalog.get_object_content("bucket", "second") == content

        catalog.upsert(
            "bucket",
            "second",
            size=len(data) + 1,
            tier="hot",
            metadata={
                "content_identity": {"forged": True},
                "independent": "retained",
            },
        )
        assert catalog.get_object_content("bucket", "second") is None
        rewritten = catalog.get("bucket", "second")
        assert rewritten is not None
        assert rewritten.metadata == {"independent": "retained"}
