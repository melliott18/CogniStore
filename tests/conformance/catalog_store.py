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
from cognistore.core.content_references import ContentReferenceEntry
from cognistore.core.move_jobs import (
    EXPECTED_SOURCE_SHA256_METADATA_KEY,
    MoveJobConflictError,
    MoveJobState,
)


def _content(data: bytes, *, chunk_size: int = 4) -> ObjectContent:
    return ContentIdentityBuilder(chunk_size=chunk_size).build(
        BytesIO(data),
        expected_size=len(data),
    )


def _publish_content(
    catalog: CatalogStore,
    key: str,
    content: ObjectContent,
    *,
    bucket: str = "bucket",
) -> None:
    fence = catalog.capture_scan_fence(bucket, key)
    assert catalog.upsert_scan_observation(
        bucket,
        key,
        size=content.size,
        tier="hot",
        generation=f"hot:{key}",
        metadata={},
        fence=fence,
        content=content,
    )


def _publish_scan(
    catalog: CatalogStore,
    key: str,
    content: ObjectContent,
    *,
    generation: str,
    metadata: dict[str, object],
    tier: str = "hot",
    bucket: str = "bucket",
) -> None:
    fence = catalog.capture_scan_fence(bucket, key)
    assert catalog.upsert_scan_observation(
        bucket,
        key,
        size=content.size,
        tier=tier,
        generation=generation,
        metadata=metadata,
        fence=fence,
        content=content,
    )


def _source_derived_metadata(*, extraction_version: int = 1) -> dict[str, object]:
    return {
        "mime": "text/plain",
        "mime_detection": {
            "schema_version": 1,
            "mime": "text/plain",
            "provenance": "content",
        },
        "document_extraction": {
            "schema_version": extraction_version,
            "status": "succeeded",
            "text": f"extraction-v{extraction_version}",
        },
    }


def _reference_entries(catalog: CatalogStore) -> dict[str, ContentReferenceEntry]:
    report = catalog.reconcile_content_references(
        grace_period_seconds=0,
        now="2100-01-01T00:00:00.000000Z",
    )
    return {entry.sha256: entry for entry in report.entries}


class CatalogStoreConformance:
    """Tests that every catalog backend's object reads must pass."""

    @pytest.fixture
    def catalog(self) -> CatalogStore:
        raise NotImplementedError("concrete conformance tests must provide a catalog")

    @pytest.mark.parametrize("terminal", [False, True])
    def test_fenced_claim_conflicts_with_unfenced_contract_atomically(
        self,
        catalog: CatalogStore,
        terminal: bool,
    ) -> None:
        move_key = f"source-contract-atomic-{terminal}"
        source_metadata = {"generation": "source-v1", "size": 7}
        original = catalog.claim_move_job(
            move_key,
            src_tier="hot",
            dst_tier="warm",
            bucket="bucket",
            key="object",
            expected_size=7,
            source_metadata=source_metadata,
            owner_id="owner-a",
            now="2026-01-01T00:00:00.000000Z",
            lease_expires_at="2026-01-01T00:01:00.000000Z",
        )
        if terminal:
            original = catalog.transition_move_job(
                move_key,
                owner_id="owner-a",
                expected_state=MoveJobState.PREPARED,
                to_state=MoveJobState.FAILED,
                reason="test terminal contract",
                now="2026-01-01T00:00:01.000000Z",
                lease_expires_at="2026-01-01T00:01:01.000000Z",
                updates={"terminal_reason": "test terminal contract"},
            )

        with pytest.raises(
            MoveJobConflictError,
            match="different expected source digest",
        ):
            catalog.claim_move_job(
                move_key,
                src_tier="hot",
                dst_tier="warm",
                bucket="bucket",
                key="object",
                expected_size=7,
                source_metadata={
                    **source_metadata,
                    EXPECTED_SOURCE_SHA256_METADATA_KEY: "a" * 64,
                },
                owner_id="owner-b",
                now="2026-01-01T00:00:02.000000Z",
                lease_expires_at="2026-01-01T00:01:02.000000Z",
            )

        assert catalog.get_move_job(move_key) == original

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

    def test_content_replacement_invalidates_omitted_source_derived_metadata(
        self, catalog: CatalogStore
    ) -> None:
        original = _content(b"original bytes")
        replacement = _content(b"replacement bytes")
        initial_metadata = {
            **_source_derived_metadata(),
            "classification": "retain",
        }
        _publish_scan(
            catalog,
            "object",
            original,
            generation="hot:v1",
            metadata=initial_metadata,
        )

        _publish_scan(
            catalog,
            "object",
            replacement,
            generation="hot:v2",
            metadata={"path": "/replacement"},
        )

        record = catalog.get("bucket", "object")
        assert record is not None
        assert record.metadata["classification"] == "retain"
        assert record.metadata["path"] == "/replacement"
        assert record.metadata["sha256"] == replacement.sha256
        assert "mime" not in record.metadata
        assert "mime_detection" not in record.metadata
        assert "document_extraction" not in record.metadata

    def test_same_content_scan_preserves_omitted_source_derived_metadata(
        self, catalog: CatalogStore
    ) -> None:
        content = _content(b"stable bytes")
        source_metadata = _source_derived_metadata()
        _publish_scan(
            catalog,
            "object",
            content,
            generation="hot:v1",
            metadata=source_metadata,
        )

        _publish_scan(
            catalog,
            "object",
            content,
            tier="warm",
            generation="warm:v1",
            metadata={"path": "/same-content"},
        )

        record = catalog.get("bucket", "object")
        assert record is not None
        assert record.tier == "warm"
        assert record.metadata["mime"] == source_metadata["mime"]
        assert record.metadata["mime_detection"] == source_metadata["mime_detection"]
        assert record.metadata["document_extraction"] == source_metadata["document_extraction"]
        assert record.metadata["path"] == "/same-content"

    def test_extraction_change_invalidates_omitted_mime_evidence(
        self, catalog: CatalogStore
    ) -> None:
        content = _content(b"stable bytes")
        _publish_scan(
            catalog,
            "object",
            content,
            generation="hot:v1",
            metadata=_source_derived_metadata(),
        )
        changed_extraction = _source_derived_metadata(extraction_version=2)[
            "document_extraction"
        ]

        _publish_scan(
            catalog,
            "object",
            content,
            generation="hot:v2",
            metadata={"document_extraction": changed_extraction},
        )

        record = catalog.get("bucket", "object")
        assert record is not None
        assert record.metadata["document_extraction"] == changed_extraction
        assert "mime" not in record.metadata
        assert "mime_detection" not in record.metadata

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

    def test_deleting_one_live_duplicate_preserves_the_other_reference(
        self,
        catalog: CatalogStore,
    ) -> None:
        content = _content(b"abcdefgh")
        _publish_content(catalog, "first", content)
        _publish_content(catalog, "second", content)

        before = _reference_entries(catalog)
        assert before[content.sha256].stored_reference_count == 2
        assert before[content.sha256].expected_object_reference_count == 2
        assert before[content.sha256].expected_chunk_reference_count == 0
        for chunk in content.chunks:
            assert before[chunk.sha256].stored_reference_count == 2
            assert before[chunk.sha256].expected_object_reference_count == 0
            assert before[chunk.sha256].expected_chunk_reference_count == 2

        catalog.delete("bucket", "first")

        assert catalog.get("bucket", "first") is None
        assert catalog.get_object_content("bucket", "first") is None
        assert catalog.get_object_content("bucket", "second") == content
        after = _reference_entries(catalog)
        assert all(entry.stored_reference_count == 1 for entry in after.values())
        assert all(entry.expected_reference_count == 1 for entry in after.values())
        assert all(entry.unreferenced_at is None for entry in after.values())
        assert all(not entry.reclamation_eligible for entry in after.values())

    def test_logical_deletion_is_idempotent_for_content_references(
        self,
        catalog: CatalogStore,
    ) -> None:
        content = _content(b"abcdefgh")
        _publish_content(catalog, "retained", content)
        _publish_content(catalog, "deleted", content)
        catalog.delete("bucket", "deleted")
        expected = catalog.reconcile_content_references(
            grace_period_seconds=0,
            now="2100-01-01T00:00:00.000000Z",
        )

        catalog.delete("bucket", "deleted")
        catalog.delete("bucket", "missing")

        assert catalog.reconcile_content_references(
            grace_period_seconds=0,
            now="2100-01-01T00:00:00.000000Z",
        ) == expected
        assert catalog.get_object_content("bucket", "retained") == content

    def test_reference_edges_preserve_root_and_chunk_position_multiplicity(
        self,
        catalog: CatalogStore,
    ) -> None:
        one_chunk = _content(b"same")
        repeated_chunk = _content(b"abcdabcd")
        _publish_content(catalog, "one-chunk", one_chunk)
        _publish_content(catalog, "repeated-chunk", repeated_chunk)

        report = catalog.reconcile_content_references(
            grace_period_seconds=0,
            now="2100-01-01T00:00:00.000000Z",
        )
        entries = {entry.sha256: entry for entry in report.entries}

        # A one-chunk object has two independent edges to one digest: the
        # full-object root and chunk position zero.
        same = entries[one_chunk.sha256]
        assert one_chunk.sha256 == one_chunk.chunks[0].sha256
        assert same.expected_object_reference_count == 1
        assert same.expected_chunk_reference_count == 1
        assert same.expected_reference_count == 2
        assert same.stored_reference_count == 2

        # Repeated equal chunks retain both positional edges even though their
        # CAS identity is represented by one report entry.
        repeated = entries[repeated_chunk.chunks[0].sha256]
        assert repeated_chunk.chunks[0].sha256 == repeated_chunk.chunks[1].sha256
        assert repeated.expected_object_reference_count == 0
        assert repeated.expected_chunk_reference_count == 2
        assert repeated.expected_reference_count == 2
        assert repeated.stored_reference_count == 2
        assert report.consistent

    def test_shared_chunk_reclamation_requires_zero_references_and_grace(
        self,
        catalog: CatalogStore,
    ) -> None:
        first = _content(b"abcd1111")
        second = _content(b"abcd2222")
        assert first.chunks[0].sha256 == second.chunks[0].sha256
        shared_digest = first.chunks[0].sha256
        first_only_digests = {first.sha256, first.chunks[1].sha256}
        _publish_content(catalog, "first", first)
        _publish_content(catalog, "second", second)

        catalog.delete("bucket", "first")

        waiting = catalog.reconcile_content_references(
            grace_period_seconds=10**10,
            now="2100-01-01T00:00:00.000000Z",
        )
        waiting_entries = {entry.sha256: entry for entry in waiting.entries}
        assert all(
            waiting_entries[digest].expected_reference_count == 0
            for digest in first_only_digests
        )
        assert all(
            not waiting_entries[digest].reclamation_eligible
            for digest in first_only_digests
        )
        assert waiting_entries[shared_digest].expected_chunk_reference_count == 1
        assert waiting_entries[shared_digest].unreferenced_at is None

        eligible = catalog.reconcile_content_references(
            grace_period_seconds=0,
            now="2100-01-01T00:00:00.000000Z",
        )
        eligible_entries = {entry.sha256: entry for entry in eligible.entries}
        assert {
            entry.sha256 for entry in eligible.entries if entry.reclamation_eligible
        } == first_only_digests
        assert not eligible_entries[shared_digest].reclamation_eligible
        assert catalog.get_object_content("bucket", "second") == second

    def test_reconciliation_is_deterministic_and_non_destructive(
        self,
        catalog: CatalogStore,
    ) -> None:
        content = _content(b"abcdefgh")
        _publish_content(catalog, "object", content)
        record_before = catalog.get("bucket", "object")
        content_before = catalog.get_object_content("bucket", "object")

        first = catalog.reconcile_content_references(
            grace_period_seconds=60,
            now="2100-01-01T00:00:00.000000Z",
        )
        second = catalog.reconcile_content_references(
            grace_period_seconds=60,
            now="2100-01-01T00:00:00.000000Z",
        )

        assert first == second
        assert first.generated_at == "2100-01-01T00:00:00.000000Z"
        assert first.grace_period_seconds == 60.0
        assert first.consistent
        assert first.total_blobs == len(
            {content.sha256, *(chunk.sha256 for chunk in content.chunks)}
        )
        assert first.referenced_blobs == first.total_blobs
        assert first.unreferenced_blobs == 0
        assert first.eligible_blobs == 0
        assert [entry.sha256 for entry in first.entries] == sorted(
            entry.sha256 for entry in first.entries
        )
        assert all(entry.issues == () for entry in first.entries)
        assert catalog.get("bucket", "object") == record_before
        assert catalog.get_object_content("bucket", "object") == content_before
