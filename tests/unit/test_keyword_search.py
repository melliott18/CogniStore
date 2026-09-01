from __future__ import annotations

import hashlib
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest

from cognistore.core.catalog import Catalog, ObjectRecord
from cognistore.core.content_identity import ContentIdentityBuilder
from cognistore.core.indexer import Indexer
from cognistore.core.mime_detection import MimeDetectionAdapter
from cognistore.core.scanner import scan_catalog
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.search.keyword import (
    KeywordObject,
    KeywordRebuildReport,
    KeywordSearchFilters,
    KeywordSearchHit,
    KeywordSearchQuery,
    KeywordSearchService,
    NormalizedPassageChunker,
    project_catalog_record,
)
from cognistore.search.tantivy import (
    MIN_WRITER_HEAP_BYTES,
    KeywordIndexCompatibilityError,
    TantivyKeywordIndex,
)


def _record(
    key: str,
    text: str,
    *,
    bucket: str = "documents",
    tier: str = "hot",
    author: str = "CogniStore Contributors",
    title: str | None = None,
    source_size: int | None = None,
) -> ObjectRecord:
    size = (
        len((key + text).encode("utf-8", errors="surrogatepass"))
        if source_size is None
        else source_size
    )
    digest = hashlib.sha256(
        f"{bucket}\0{key}\0{text}".encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    mime = "application/pdf"
    return ObjectRecord(
        bucket=bucket,
        key=key,
        size=size,
        tier=tier,
        metadata={
            "mime": mime,
            "sha256": digest,
            "content_identity": {
                "schema_version": 1,
                "representation": "source-bytes",
                "digest_algorithm": "sha256",
                "sha256": digest,
                "size": size,
                "cas_key": f"cas/v1/sha256/{digest[:2]}/{digest[2:]}",
                "chunking_algorithm": "fixed-size",
                "chunking_version": 1,
                "chunk_size": 1024 * 1024,
                "chunk_count": 1,
            },
            "document_extraction": {
                "schema_version": 1,
                "status": "succeeded",
                "source_mime": mime,
                "source_size": size,
                "parser": {
                    "name": "pypdf",
                    "implementation_version": "1",
                    "runtime_version": "6.16.2",
                },
                "normalization_version": 1,
                "text": text,
                "text_bytes": len(text.encode("utf-8")),
                "output_bytes": len(text.encode("utf-8")),
                "document_metadata": {
                    "format": "pdf",
                    "title": title or key,
                    "author": author,
                    "language": "en",
                },
                "failure_code": None,
            },
        },
    )


def _keys(hits: Iterable[KeywordSearchHit]) -> list[str]:
    return [hit.key for hit in hits]


def test_passage_chunking_is_deterministic_versioned_and_bounded() -> None:
    record = _record(
        "long.pdf",
        "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu",
    )
    chunker = NormalizedPassageChunker(max_chars=24, overlap_chars=5)

    first = project_catalog_record(record, chunker=chunker)
    second = project_catalog_record(deepcopy(record), chunker=chunker)

    assert first == second
    assert len(first.passages) > 1
    assert [passage.ordinal for passage in first.passages] == list(
        range(len(first.passages))
    )
    assert all(len(passage.text) <= 24 for passage in first.passages)
    assert all(passage.text == record.metadata["document_extraction"]["text"][
        passage.char_start : passage.char_end
    ] for passage in first.passages)  # type: ignore[index]
    assert first.passage_max_chars == 24
    assert first.passage_overlap_chars == 5


def test_metadata_only_passage_identity_includes_chunker_configuration() -> None:
    record = _record("metadata-only.pdf", "")

    first = project_catalog_record(
        record,
        chunker=NormalizedPassageChunker(max_chars=100, overlap_chars=10),
    )
    second = project_catalog_record(
        record,
        chunker=NormalizedPassageChunker(max_chars=200, overlap_chars=20),
    )

    assert first.passages[0].text == second.passages[0].text == ""
    assert first.passages[0].passage_id != second.passages[0].passage_id


@pytest.mark.parametrize(
    ("mutation", "retains_content_identity"),
    [
        (lambda metadata: metadata.pop("content_identity"), False),
        (lambda metadata: metadata["content_identity"].update(size=999), False),
        (
            lambda metadata: metadata["document_extraction"].update(source_size=999),
            True,
        ),
        (
            lambda metadata: metadata["document_extraction"].update(text_bytes=999),
            True,
        ),
        (
            lambda metadata: metadata["document_extraction"].update(
                normalization_version=2
            ),
            True,
        ),
    ],
)
def test_projection_never_indexes_stale_or_incompatible_extraction(
    mutation,
    retains_content_identity: bool,
) -> None:
    record = _record("stale.pdf", "secret stale passage", author="Former Author")
    mutation(record.metadata)

    projected = project_catalog_record(record)

    assert (projected.content_sha256 is not None) is retains_content_identity
    assert projected.extraction_identity is None
    assert projected.document_metadata == {}
    assert len(projected.passages) == 1
    assert projected.passages[0].text == ""


def test_tantivy_search_ranks_passages_and_combines_exact_filters(
    tmp_path: Path,
) -> None:
    frequent = project_catalog_record(
        _record("frequent.pdf", "nebula nebula nebula orbit", author="Alice")
    )
    sparse = project_catalog_record(
        _record(
            "sparse.pdf",
            "nebula orbit",
            bucket="archive",
            tier="warm",
            author="Bob",
        )
    )

    with TantivyKeywordIndex(tmp_path / "keyword") as index:
        assert index.rebuild([frequent, sparse]) == KeywordRebuildReport(2, 2)

        ranked = index.search(KeywordSearchQuery("nebula"))
        filtered = index.search(
            KeywordSearchQuery(
                "nebula",
                filters=KeywordSearchFilters(
                    bucket="archive",
                    tier="warm",
                    size=sparse.size,
                    mime="application/pdf",
                    document_metadata={"author": "Bob", "language": "en"},
                ),
            )
        )
        metadata_ranked = index.search(KeywordSearchQuery("Alice"))

    assert _keys(ranked) == ["frequent.pdf", "sparse.pdf"]
    assert ranked[0].score > ranked[1].score
    assert _keys(filtered) == ["sparse.pdf"]
    assert _keys(metadata_ranked) == ["frequent.pdf"]
    assert filtered[0].document_metadata["author"] == "Bob"


def test_size_filter_is_exact_and_validated(tmp_path: Path) -> None:
    projected = project_catalog_record(_record("sized.pdf", "measured object"))

    with TantivyKeywordIndex(tmp_path / "keyword") as index:
        index.replace_object(projected)
        assert _keys(
            index.search(
                KeywordSearchQuery(filters=KeywordSearchFilters(size=projected.size))
            )
        ) == ["sized.pdf"]
        assert index.search(
            KeywordSearchQuery(filters=KeywordSearchFilters(size=projected.size + 1))
        ) == []

    with pytest.raises(ValueError, match="size filter"):
        KeywordSearchFilters(size=-1)


def test_filters_do_not_change_text_relevance_scores(tmp_path: Path) -> None:
    documents = [
        project_catalog_record(
            _record("lean.pdf", "equalterm", author="Alice")
        ),
        project_catalog_record(
            _record("rich.pdf", "equalterm", author="Bob", title="Many Words Here")
        ),
    ]

    with TantivyKeywordIndex(tmp_path / "keyword") as index:
        index.rebuild(documents)
        unfiltered = {
            hit.key: hit.score for hit in index.search(KeywordSearchQuery("equalterm"))
        }
        filtered = {
            hit.key: hit.score
            for hit in index.search(
                KeywordSearchQuery(
                    "equalterm",
                    filters=KeywordSearchFilters(bucket="documents"),
                )
            )
        }

    assert filtered == pytest.approx(unfiltered)


def test_query_text_is_literal_and_pagination_is_stable_across_score_ties(
    tmp_path: Path,
) -> None:
    documents = [
        project_catalog_record(_record(f"{key}.pdf", "foo bar tiedterm"))
        for key in ("z", "y", "x", "a", "b")
    ]

    with TantivyKeywordIndex(tmp_path / "keyword") as index:
        index.rebuild(documents)
        assert len(index.search(KeywordSearchQuery("foo:bar"))) == 5
        pages = [
            index.search(KeywordSearchQuery("tiedterm", limit=1, offset=offset))[0].key
            for offset in range(5)
        ]

    assert pages == ["a.pdf", "b.pdf", "x.pdf", "y.pdf", "z.pdf"]


def test_opaque_catalog_coordinates_round_trip_and_remain_rebuildable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "keyword"
    coordinates = [
        ("nul\0bucket", "before\0after.pdf"),
        ("raw\udc80", "before\udc80after.pdf"),
        ("raw\udc81", "before\udc81after.pdf"),
    ]
    documents = [
        project_catalog_record(
            _record(
                key,
                "opaque coordinate",
                bucket=bucket,
            )
        )
        for bucket, key in coordinates
    ]

    with TantivyKeywordIndex(path) as index:
        assert index.rebuild(documents) == KeywordRebuildReport(3, 3)
        all_hits = index.search(KeywordSearchQuery(limit=10))
        assert [(hit.bucket, hit.key) for hit in all_hits] == [
            (document.bucket, document.key) for document in documents
        ]
        filtered = index.search(
            KeywordSearchQuery(
                "before\udc80after",
                filters=KeywordSearchFilters(bucket=documents[1].bucket),
            )
        )
        assert [(hit.bucket, hit.key) for hit in filtered] == [
            (documents[1].bucket, documents[1].key)
        ]

    with TantivyKeywordIndex(path) as reopened:
        assert [hit.key for hit in reopened.search(KeywordSearchQuery("after"))] == [
            document.key for document in documents
        ]
        reopened.delete_object(documents[1].bucket, documents[1].key)
        assert [hit.key for hit in reopened.search(KeywordSearchQuery("after"))] == [
            documents[0].key,
            documents[2].key,
        ]


def test_metadata_only_object_retains_current_content_digest_filter(
    tmp_path: Path,
) -> None:
    record = _record("failed.pdf", "untrusted parser output")
    record.metadata["document_extraction"] = {
        "schema_version": 1,
        "status": "failed",
        "source_mime": "application/pdf",
        "source_size": record.size,
        "parser": {
            "name": "pypdf",
            "implementation_version": "1",
            "runtime_version": "6.16.2",
        },
        "normalization_version": 1,
        "text": None,
        "text_bytes": 0,
        "output_bytes": 0,
        "document_metadata": {},
        "failure_code": "corrupt",
    }
    projected = project_catalog_record(record)
    digest = record.metadata["sha256"]
    assert isinstance(digest, str)

    with TantivyKeywordIndex(tmp_path / "keyword") as index:
        index.replace_object(projected)
        hits = index.search(
            KeywordSearchQuery(
                filters=KeywordSearchFilters(content_sha256=digest)
            )
        )

    assert _keys(hits) == ["failed.pdf"]
    assert hits[0].text == ""
    assert hits[0].content_sha256 == digest


def test_keyword_object_rejects_mismatched_coordinate_identity() -> None:
    projected = project_catalog_record(_record("identity.pdf", "identity"))

    with pytest.raises(ValueError, match="does not match bucket and key"):
        replace(projected, object_id="0" * 64)


def test_replace_and_delete_are_visible_before_return(tmp_path: Path) -> None:
    original = project_catalog_record(_record("mutable.pdf", "cobalt cobalt"))
    replacement = project_catalog_record(_record("mutable.pdf", "saffron saffron"))

    with TantivyKeywordIndex(tmp_path / "keyword") as index:
        assert index.replace_object(original) == 1
        assert _keys(index.search(KeywordSearchQuery("cobalt"))) == ["mutable.pdf"]

        assert index.replace_object(replacement) == 1
        assert index.search(KeywordSearchQuery("cobalt")) == []
        assert _keys(index.search(KeywordSearchQuery("saffron"))) == ["mutable.pdf"]

        index.delete_object("documents", "mutable.pdf")
        index.delete_object("documents", "mutable.pdf")
        assert index.search(KeywordSearchQuery("saffron")) == []


def test_rebuild_removes_stale_objects_and_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "keyword"
    first = project_catalog_record(_record("first.pdf", "first marker"))
    second = project_catalog_record(_record("second.pdf", "second marker"))

    with TantivyKeywordIndex(path) as index:
        index.rebuild([first, second])
        assert _keys(index.search(KeywordSearchQuery("marker"))) == [
            "first.pdf",
            "second.pdf",
        ]
        assert index.rebuild([second]) == KeywordRebuildReport(1, 1)
        assert _keys(index.search(KeywordSearchQuery("marker"))) == ["second.pdf"]

    with TantivyKeywordIndex(path) as reopened:
        assert _keys(reopened.search(KeywordSearchQuery("marker"))) == ["second.pdf"]


def test_index_root_allows_only_one_live_writer(tmp_path: Path) -> None:
    path = tmp_path / "keyword"
    with TantivyKeywordIndex(path):
        with pytest.raises(RuntimeError, match="already open by another writer"):
            TantivyKeywordIndex(path)

    with TantivyKeywordIndex(path):
        pass


def test_failed_rebuild_keeps_previous_generation_searchable(tmp_path: Path) -> None:
    original = project_catalog_record(_record("stable.pdf", "stable marker"))
    replacement = project_catalog_record(_record("replacement.pdf", "new marker"))

    def fail_after_one() -> Iterable[KeywordObject]:
        yield replacement
        raise RuntimeError("catalog stream failed")

    with TantivyKeywordIndex(tmp_path / "keyword") as index:
        index.rebuild([original])
        with pytest.raises(RuntimeError, match="catalog stream failed"):
            index.rebuild(fail_after_one())

        assert _keys(index.search(KeywordSearchQuery("stable"))) == ["stable.pdf"]
        assert index.search(KeywordSearchQuery("new")) == []


def test_failed_generation_open_keeps_current_and_removes_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "keyword"
    original = project_catalog_record(_record("stable.pdf", "stable marker"))
    replacement = project_catalog_record(_record("replacement.pdf", "new marker"))

    with TantivyKeywordIndex(path) as index:
        index.rebuild([original])
        current = (path / "CURRENT").read_text(encoding="ascii").strip()

        def fail_open(_generation: str) -> None:
            raise RuntimeError("generation open failed")

        monkeypatch.setattr(index, "_open_generation", fail_open)
        with pytest.raises(RuntimeError, match="generation open failed"):
            index.rebuild([replacement])

        assert (path / "CURRENT").read_text(encoding="ascii").strip() == current
        assert sorted(item.name for item in (path / "generations").iterdir()) == [
            current
        ]
        assert _keys(index.search(KeywordSearchQuery("stable"))) == ["stable.pdf"]


def test_service_reconstructs_complete_index_from_catalog(tmp_path: Path) -> None:
    catalog = Catalog()
    for key, text in (("one.pdf", "catalog alpha"), ("two.pdf", "catalog beta")):
        source = (key + text).encode("utf-8")
        record = _record(key, text, source_size=len(source))
        fence = catalog.capture_scan_fence(record.bucket, record.key)
        assert catalog.upsert_scan_observation(
            record.bucket,
            record.key,
            size=len(source),
            tier=record.tier,
            generation=f"generation:{key}",
            metadata=record.metadata,
            fence=fence,
            content=ContentIdentityBuilder(chunk_size=8).build(
                BytesIO(source), expected_size=len(source)
            ),
        )

    with TantivyKeywordIndex(tmp_path / "keyword") as index:
        service = KeywordSearchService(catalog, index, rebuild_batch_size=1)
        assert service.rebuild() == KeywordRebuildReport(2, 2)
        assert _keys(service.search(KeywordSearchQuery("catalog"))) == [
            "one.pdf",
            "two.pdf",
        ]
        assert _keys(service.search(KeywordSearchQuery("beta"))) == ["two.pdf"]


def test_real_pdf_extraction_flows_through_catalog_rebuild(tmp_path: Path) -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "documents" / "sample.pdf"
    driver = PosixDriver(str(tmp_path / "hot"))
    driver.put_object("documents", "fixtures/sample.pdf", fixture.read_bytes())
    catalog = Catalog()

    assert scan_catalog(
        tier="hot",
        bucket="documents",
        driver=driver,
        catalog=catalog,
        indexer=Indexer(mime_detector=MimeDetectionAdapter(None)),
    )
    record = catalog.get("documents", "fixtures/sample.pdf")
    assert record is not None
    extraction = record.metadata["document_extraction"]
    assert isinstance(extraction, dict)
    assert extraction["status"] == "succeeded"
    assert extraction["parser"]["name"] == "pypdf"  # type: ignore[index]

    with TantivyKeywordIndex(tmp_path / "keyword") as index:
        service = KeywordSearchService(catalog, index)
        assert service.rebuild() == KeywordRebuildReport(1, 1)
        hits = service.search(
            KeywordSearchQuery(
                "reproducible",
                filters=KeywordSearchFilters(
                    bucket="documents",
                    document_metadata={"author": "CogniStore Contributors"},
                ),
            )
        )

    assert _keys(hits) == ["fixtures/sample.pdf"]
    assert "Beta pages remain reproducible." in hits[0].text
    assert hits[0].document_metadata["format"] == "pdf"


def test_manifest_mismatch_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "keyword"
    with TantivyKeywordIndex(path):
        pass
    current = (path / "CURRENT").read_text(encoding="ascii").strip()
    manifest_path = path / "generations" / current / "manifest.json"
    manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 999
    manifest_path.write_text(__import__("json").dumps(manifest), encoding="utf-8")

    with pytest.raises(KeywordIndexCompatibilityError, match="schema_version"):
        TantivyKeywordIndex(path)


def test_writer_heap_validation_matches_tantivy_runtime_minimum(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match=str(MIN_WRITER_HEAP_BYTES)):
        TantivyKeywordIndex(
            tmp_path / "too-small",
            writer_heap_bytes=MIN_WRITER_HEAP_BYTES - 1,
        )
    with TantivyKeywordIndex(
        tmp_path / "minimum",
        writer_heap_bytes=MIN_WRITER_HEAP_BYTES,
    ):
        pass


class _FailingAdapter:
    def replace_object(self, _document: KeywordObject) -> int:
        raise RuntimeError("backend unavailable")

    def delete_object(self, _bucket: str, _key: str) -> None:
        raise RuntimeError("backend unavailable")

    def search(self, _query: KeywordSearchQuery) -> list[KeywordSearchHit]:
        raise RuntimeError("backend unavailable")

    def rebuild(self, _documents: Iterable[KeywordObject]) -> KeywordRebuildReport:
        raise RuntimeError("backend unavailable")

    def close(self) -> None:
        pass


def test_backend_failure_cannot_mutate_catalog_state() -> None:
    catalog = Catalog()
    record = _record("safe.pdf", "catalog remains authoritative")
    catalog.upsert(
        record.bucket,
        record.key,
        record.size,
        record.tier,
        record.metadata,
    )
    before = catalog.get(record.bucket, record.key)
    service = KeywordSearchService(catalog, _FailingAdapter())

    with pytest.raises(RuntimeError, match="backend unavailable"):
        service.sync_object(record.bucket, record.key)
    with pytest.raises(RuntimeError, match="backend unavailable"):
        service.rebuild()

    assert catalog.get(record.bucket, record.key) == before


def test_service_requires_catalog_delete_before_index_delete() -> None:
    catalog = Catalog()
    catalog.upsert("bucket", "key", 1, "hot")
    service = KeywordSearchService(catalog, _FailingAdapter())

    with pytest.raises(ValueError, match="delete it before"):
        service.delete_object("bucket", "key")
