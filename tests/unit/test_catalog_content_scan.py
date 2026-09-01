from __future__ import annotations

import gc
import hashlib
import tracemalloc
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from cognistore.core.catalog import Catalog, CatalogStore
from cognistore.core.content_identity import (
    MAX_IN_MEMORY_CONTENT_CHUNKS,
    ContentChunkSequence,
    ContentIdentityBuilder,
    ContentReader,
    ContentSizeMismatchError,
    ObjectContent,
)
from cognistore.core.indexer import Indexer
from cognistore.core.mime_detection import MimeDetectionAdapter
from cognistore.core.scanner import scan_catalog
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.db.catalog import SQLCatalog
from cognistore.db.schema import (
    content_blobs,
    content_manifest_chunks,
    content_manifests,
    object_contents,
)
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.drivers.storage_driver import (
    ObjectGenerationMismatchError,
    StorageDriver,
)

_TEST_CHUNK_SIZE = 4


class _DistinctChunkReader:
    def __init__(self, chunk_count: int, chunk_size: int) -> None:
        self.remaining = chunk_count
        self.chunk_size = chunk_size
        self.index = 0

    def read(self, size: int = -1, /) -> bytes:
        if self.remaining == 0:
            return b""
        assert size == self.chunk_size
        payload = self.index.to_bytes(self.chunk_size, "big")
        self.remaining -= 1
        self.index += 1
        return payload


def _distinct_content(chunk_count: int) -> ObjectContent:
    chunk_size = 8
    return ContentIdentityBuilder(chunk_size=chunk_size).build(
        _DistinctChunkReader(chunk_count, chunk_size),
        expected_size=chunk_count * chunk_size,
    )


@pytest.fixture(params=("memory", "sqlite"))
def catalog(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> Iterator[CatalogStore]:
    if request.param == "memory":
        yield Catalog()
        return
    with SQLiteCatalog(tmp_path / "catalog.db") as sqlite_catalog:
        yield sqlite_catalog


def _indexer(*, chunk_size: int = _TEST_CHUNK_SIZE) -> Indexer:
    return Indexer(
        mime_detector=MimeDetectionAdapter(None),
        content_builder=ContentIdentityBuilder(chunk_size=chunk_size),
    )


def _publish_content(
    catalog: CatalogStore,
    key: str,
    content: ObjectContent,
) -> None:
    fence = catalog.capture_scan_fence("bucket", key)
    assert catalog.upsert_scan_observation(
        "bucket",
        key,
        size=content.size,
        tier="hot",
        generation=f"hot:{key}",
        metadata={},
        fence=fence,
        content=content,
    )


def _content_topology_counts(catalog: SQLCatalog) -> tuple[int, int, int, int]:
    with catalog.engine.connect() as connection:
        counts = tuple(
            connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
            for table in (
                content_blobs,
                content_manifests,
                content_manifest_chunks,
                object_contents,
            )
        )
    return counts[0], counts[1], counts[2], counts[3]


def _content_blob_rows(catalog: SQLCatalog) -> tuple[dict[str, object], ...]:
    with catalog.engine.connect() as connection:
        return tuple(
            dict(row)
            for row in connection.execute(
                sa.select(content_blobs).order_by(content_blobs.c.sha256)
            ).mappings()
        )


def test_sql_catalog_inserts_unique_content_blobs_in_global_digest_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"abcdabcdwxyz"
    content = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(payload),
        expected_size=len(payload),
    )
    inserted: list[str] = []
    original_insert = SQLCatalog._insert_content_blob

    def record_insert(connection: Any, **values: Any) -> None:
        inserted.append(values["sha256"])
        original_insert(connection, **values)

    monkeypatch.setattr(
        SQLCatalog,
        "_insert_content_blob",
        staticmethod(record_insert),
    )
    with SQLiteCatalog(tmp_path / "ordered-content-blobs.db") as catalog:
        fence = catalog.capture_scan_fence("bucket", "object")
        assert catalog.upsert_scan_observation(
            "bucket",
            "object",
            size=len(payload),
            tier="hot",
            generation="hot:v1",
            metadata={},
            fence=fence,
            content=content,
        )

    expected = sorted({content.sha256, *(chunk.sha256 for chunk in content.chunks)})
    assert inserted == expected


def test_sql_catalog_stores_duplicate_content_once_and_deletes_independently(
    tmp_path: Path,
) -> None:
    content = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(b"abcdefghijkl"),
        expected_size=12,
    )
    unique_digests = {content.sha256, *(chunk.sha256 for chunk in content.chunks)}

    with SQLiteCatalog(tmp_path / "shared-content.db") as catalog:
        _publish_content(catalog, "first", content)
        _publish_content(catalog, "second", content)

        assert _content_topology_counts(catalog) == (
            len(unique_digests),
            1,
            len(content.chunks),
            2,
        )
        catalog.delete("bucket", "first")

        assert _content_topology_counts(catalog) == (
            len(unique_digests),
            1,
            len(content.chunks),
            1,
        )
        assert catalog.get_object_content("bucket", "first") is None
        assert catalog.get_object_content("bucket", "second") == content
        report = catalog.reconcile_content_references(
            grace_period_seconds=0,
            now="2100-01-01T00:00:00.000000Z",
        )

    assert report.consistent
    assert report.referenced_blobs == len(unique_digests)
    assert report.unreferenced_blobs == 0
    assert all(entry.stored_reference_count == 1 for entry in report.entries)
    assert all(entry.expected_reference_count == 1 for entry in report.entries)


def test_sql_reconciliation_reports_count_mismatch_without_modifying_state(
    tmp_path: Path,
) -> None:
    content = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(b"abcdefgh"),
        expected_size=8,
    )
    with SQLiteCatalog(tmp_path / "corrupt-content-references.db") as catalog:
        _publish_content(catalog, "object", content)
        with catalog.engine.begin() as connection:
            connection.execute(
                sa.update(content_blobs)
                .where(content_blobs.c.sha256 == content.sha256)
                .values(reference_count=content_blobs.c.reference_count + 7)
            )
        corrupted = _content_blob_rows(catalog)

        first = catalog.reconcile_content_references(
            grace_period_seconds=60,
            now="2100-01-01T00:00:00.000000Z",
        )
        second = catalog.reconcile_content_references(
            grace_period_seconds=60,
            now="2100-01-01T00:00:00.000000Z",
        )
        after = _content_blob_rows(catalog)

    assert first == second
    assert not first.consistent
    entry = next(entry for entry in first.entries if entry.sha256 == content.sha256)
    assert entry.expected_object_reference_count == 1
    assert entry.expected_chunk_reference_count == 0
    assert entry.expected_reference_count == 1
    assert entry.stored_reference_count == 8
    assert entry.issues == ("reference_count_mismatch",)
    assert not entry.reclamation_eligible
    assert after == corrupted


def test_sql_catalog_external_blob_order_stays_bounded_above_spill_limit() -> None:
    chunk_count = MAX_IN_MEMORY_CONTENT_CHUNKS * 64
    content = _distinct_content(chunk_count)
    assert isinstance(content.chunks, ContentChunkSequence)
    assert content.chunks.is_spilled

    gc.collect()
    tracemalloc.start()
    try:
        observed_count = 0
        previous = ""
        with SQLCatalog._ordered_content_blob_descriptors(content) as descriptors:
            for sha256, _algorithm, _size, _cas_key in descriptors:
                assert sha256 > previous
                previous = sha256
                observed_count += 1
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert observed_count == chunk_count + 1
    # The disposable SQLite primary-key spool uses a fixed page cache. The old
    # Python dict/tuple implementation grows to several MiB for this case.
    assert peak < 1024 * 1024


def test_sql_catalog_streams_large_manifest_round_trip_with_bounded_memory(
    tmp_path: Path,
) -> None:
    chunk_count = MAX_IN_MEMORY_CONTENT_CHUNKS * 16
    content = _distinct_content(chunk_count)
    with SQLiteCatalog(tmp_path / "spilled-content-manifest.db") as catalog:
        fence = catalog.capture_scan_fence("bucket", "object")
        assert catalog.upsert_scan_observation(
            "bucket",
            "object",
            size=content.size,
            tier="hot",
            generation="hot:v1",
            metadata={},
            fence=fence,
            content=content,
        )

        gc.collect()
        tracemalloc.start()
        try:
            persisted = catalog.get_object_content("bucket", "object")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    assert persisted == content
    assert persisted is not None
    assert isinstance(persisted.chunks, ContentChunkSequence)
    assert persisted.chunks.is_spilled
    assert peak < 1024 * 1024


@pytest.mark.parametrize(
    ("key", "payload", "expected_chunks"),
    [
        ("empty.bin", b"", ()),
        ("one-byte.bin", b"x", ((0, 0, 1),)),
        (
            "multi-chunk.bin",
            b"abcdefghij",
            ((0, 0, 4), (1, 4, 4), (2, 8, 2)),
        ),
    ],
    ids=("empty", "one-byte", "multi-chunk"),
)
def test_scan_persists_canonical_object_and_chunk_identity(
    catalog: CatalogStore,
    tmp_path: Path,
    key: str,
    payload: bytes,
    expected_chunks: tuple[tuple[int, int, int], ...],
) -> None:
    driver = PosixDriver(tmp_path / "hot")
    driver.put_object("bucket", key, payload)

    results = scan_catalog(
        tier="hot",
        bucket="bucket",
        driver=driver,
        catalog=catalog,
        indexer=_indexer(),
    )

    assert [result.key for result in results] == [key]
    record = catalog.get("bucket", key)
    content = catalog.get_object_content("bucket", key)
    assert record is not None
    assert content is not None
    assert content.sha256 == hashlib.sha256(payload).hexdigest()
    assert content.size == len(payload)
    assert content.chunk_size == _TEST_CHUNK_SIZE
    assert tuple(
        (chunk.index, chunk.offset, chunk.size) for chunk in content.chunks
    ) == expected_chunks
    assert record.metadata["sha256"] == content.sha256
    assert record.metadata["content_identity"] == content.to_metadata()


def test_scan_hashes_bytes_after_the_legacy_one_mib_prefix(
    catalog: CatalogStore,
    tmp_path: Path,
) -> None:
    prefix = b"p" * (1024 * 1024)
    first_payload = prefix + b"first suffix"
    second_payload = prefix + b"second suffix"
    driver = PosixDriver(tmp_path / "hot")
    driver.put_object("bucket", "first.bin", first_payload)
    driver.put_object("bucket", "second.bin", second_payload)

    results = scan_catalog(
        tier="hot",
        bucket="bucket",
        driver=driver,
        catalog=catalog,
        indexer=_indexer(chunk_size=64 * 1024),
    )

    assert {result.key for result in results} == {"first.bin", "second.bin"}
    first = catalog.get_object_content("bucket", "first.bin")
    second = catalog.get_object_content("bucket", "second.bin")
    assert first is not None
    assert second is not None
    assert first.sha256 == hashlib.sha256(first_payload).hexdigest()
    assert second.sha256 == hashlib.sha256(second_payload).hexdigest()
    assert first.sha256 != second.sha256
    assert [chunk.sha256 for chunk in first.chunks[:-1]] == [
        chunk.sha256 for chunk in second.chunks[:-1]
    ]
    assert first.chunks[-1].sha256 != second.chunks[-1].sha256


def test_scan_merges_independent_metadata_and_replaces_legacy_sample_digest(
    catalog: CatalogStore,
    tmp_path: Path,
) -> None:
    payload = b"canonical bytes extend beyond an old sample"
    driver = PosixDriver(tmp_path / "hot")
    driver.put_object("bucket", "object.bin", payload)
    catalog.upsert(
        "bucket",
        "object.bin",
        size=len(payload),
        tier="hot",
        metadata={
            "classification": "retain",
            "nested": {"labels": ["independent"]},
            "sha256": "legacy-sample-digest",
        },
    )

    scan_catalog(
        tier="hot",
        bucket="bucket",
        driver=driver,
        catalog=catalog,
        indexer=_indexer(),
    )

    record = catalog.get("bucket", "object.bin")
    content = catalog.get_object_content("bucket", "object.bin")
    assert record is not None
    assert content is not None
    assert record.metadata["classification"] == "retain"
    assert record.metadata["nested"] == {"labels": ["independent"]}
    assert record.metadata["sha256"] == hashlib.sha256(payload).hexdigest()
    assert record.metadata["content_identity"] == content.to_metadata()


def test_concurrent_size_mismatch_is_discarded_by_generation_fence(
    catalog: CatalogStore,
    tmp_path: Path,
) -> None:
    original = b"original bytes"
    replacement = b"replacement is a different size"
    driver = PosixDriver(tmp_path / "hot")
    driver.put_object("bucket", "object.bin", original)
    catalog.upsert(
        "bucket",
        "object.bin",
        size=len(original),
        tier="hot",
        metadata={"classification": "retain"},
    )

    class ReplacingBuilder(ContentIdentityBuilder):
        def build(
            self,
            _reader: ContentReader,
            *,
            expected_size: int,
        ) -> ObjectContent:
            assert expected_size == len(original)
            driver.put_object("bucket", "object.bin", replacement)
            raise ContentSizeMismatchError("injected concurrent size mismatch")

    results = scan_catalog(
        tier="hot",
        bucket="bucket",
        driver=driver,
        catalog=catalog,
        indexer=Indexer(
            mime_detector=MimeDetectionAdapter(None),
            content_builder=ReplacingBuilder(),
        ),
    )

    assert results == []
    assert catalog.get("bucket", "object.bin") is not None
    assert catalog.get("bucket", "object.bin").metadata == {  # type: ignore[union-attr]
        "classification": "retain"
    }
    assert catalog.get_object_content("bucket", "object.bin") is None
    assert driver.get_object("bucket", "object.bin") == replacement


def test_scan_uses_one_generation_bound_stream_for_sample_and_identity() -> None:
    payload = b"one immutable stream"

    class SingleStreamDriver:
        bound_reads = 0

        def list_objects(self, bucket: str, prefix: str = "") -> Iterator[str]:
            assert (bucket, prefix) == ("bucket", "")
            yield "object.bin"

        def stat_object(self, bucket: str, key: str) -> dict[str, object]:
            assert (bucket, key) == ("bucket", "object.bin")
            return {"size": len(payload), "generation": "generation-A"}

        def get_object(
            self,
            bucket: str,
            key: str,
            range: str | None = None,
        ) -> bytes:
            pytest.fail("scan performed a separate MIME-sample read")

        @contextmanager
        def open_object_reader_if_generation(
            self,
            bucket: str,
            key: str,
            generation: str,
            range: str | None = None,
        ) -> Iterator[BytesIO]:
            assert (bucket, key, generation, range) == (
                "bucket",
                "object.bin",
                "generation-A",
                None,
            )
            self.bound_reads += 1
            yield BytesIO(payload)

        def object_generation(self, bucket: str, key: str) -> str:
            assert (bucket, key) == ("bucket", "object.bin")
            return "generation-A"

    driver = SingleStreamDriver()
    catalog = Catalog()
    results = scan_catalog(
        tier="hot",
        bucket="bucket",
        driver=driver,  # type: ignore[arg-type]
        catalog=catalog,
        indexer=_indexer(),
    )

    assert [result.key for result in results] == ["object.bin"]
    assert driver.bound_reads == 1
    content = catalog.get_object_content("bucket", "object.bin")
    assert content is not None
    assert content.sha256 == hashlib.sha256(payload).hexdigest()


def test_scan_discards_an_aba_replacement_at_generation_bound_open() -> None:
    class ABADriver:
        generation = "generation-A"
        legacy_sample_reads = 0
        legacy_stream_reads = 0

        def list_objects(self, bucket: str, prefix: str = "") -> Iterator[str]:
            yield "object.bin"

        def stat_object(self, bucket: str, key: str) -> dict[str, object]:
            return {"size": 3, "generation": self.generation}

        def get_object(
            self,
            bucket: str,
            key: str,
            range: str | None = None,
        ) -> bytes:
            # The old two-read scanner sampled A here.
            self.legacy_sample_reads += 1
            return b"old"

        @contextmanager
        def open_object_reader(
            self,
            bucket: str,
            key: str,
            range: str | None = None,
        ) -> Iterator[BytesIO]:
            # It then hashed B before the live key returned to A.
            self.legacy_stream_reads += 1
            yield BytesIO(b"new")

        @contextmanager
        def open_object_reader_if_generation(
            self,
            bucket: str,
            key: str,
            generation: str,
            range: str | None = None,
        ) -> Iterator[BytesIO]:
            assert generation == "generation-A"
            self.generation = "generation-B"
            try:
                raise ObjectGenerationMismatchError(
                    "generation B won the atomic reader open"
                )
            finally:
                # Restore A before the scanner's final generation check: the
                # vulnerable before/after comparison accepted this ABA shape.
                self.generation = "generation-A"
            yield BytesIO(b"new")  # pragma: no cover - generator marker

        def object_generation(self, bucket: str, key: str) -> str:
            return self.generation

    driver = ABADriver()
    catalog = Catalog()

    assert scan_catalog(
        tier="hot",
        bucket="bucket",
        driver=driver,  # type: ignore[arg-type]
        catalog=catalog,
        indexer=_indexer(),
    ) == []
    assert driver.generation == "generation-A"
    assert driver.legacy_sample_reads == 0
    assert driver.legacy_stream_reads == 0
    assert catalog.get("bucket", "object.bin") is None


def test_scan_fails_closed_for_an_unbound_legacy_driver() -> None:
    class LegacyDriver(StorageDriver):
        def put_object(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("unexpected write")

        def get_object(self, *args: Any, **kwargs: Any) -> bytes:
            raise AssertionError("unexpected unbound read")

        def list_objects(self, bucket: str, prefix: str = "") -> Iterator[str]:
            yield "object.bin"

        def stat_object(self, bucket: str, key: str) -> dict[str, object]:
            return {"size": 3, "generation": "generation-A"}

        @contextmanager
        def open_object_reader(
            self,
            bucket: str,
            key: str,
            range: str | None = None,
        ) -> Iterator[BytesIO]:
            yield BytesIO(b"old")

        def put_object_stream(self, *args: Any, **kwargs: Any) -> int:
            raise AssertionError("unexpected write")

        def delete_object(self, bucket: str, key: str) -> None:
            raise AssertionError("unexpected delete")

        def delete_object_if_generation(
            self,
            bucket: str,
            key: str,
            generation: str,
        ) -> bool:
            raise AssertionError("unexpected delete")

    driver = LegacyDriver()
    with pytest.raises(ValueError, match="generation.*non-empty string"):
        driver.open_object_reader_if_generation(
            "bucket",
            "object.bin",
            "",
        )

    catalog = Catalog()
    with pytest.raises(NotImplementedError, match="generation-bound"):
        scan_catalog(
            tier="hot",
            bucket="bucket",
            driver=driver,
            catalog=catalog,
            indexer=_indexer(),
        )

    assert catalog.get("bucket", "object.bin") is None
