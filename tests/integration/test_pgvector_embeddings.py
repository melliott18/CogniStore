from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from io import BytesIO

import pytest
import sqlalchemy as sa

from cognistore.core.content_identity import ContentIdentityBuilder
from cognistore.core.embedding_index import (
    MAX_SIMILARITY_RESULT_METADATA_BYTES,
    EmbeddingBackendUnsupportedError,
    EmbeddingDocument,
    EmbeddingForceConflictError,
    EmbeddingIndexer,
    EmbeddingSourceChangedError,
    EmbeddingSourceUnavailableError,
    HnswIndexConfig,
    SimilaritySearchFilters,
)
from cognistore.core.embeddings import EmbeddingSpace
from cognistore.core.passages import DeterministicPassageChunker, PassageChunkerConfig
from cognistore.db import PgVectorEmbeddingStore, SQLCatalog
from cognistore.db.schema import (
    embedding_document_spaces,
    embedding_documents,
    embedding_passages,
    embedding_spaces,
    embedding_vectors,
    object_embedding_documents,
)

pytestmark = pytest.mark.integration


class _SemanticProvider:
    def __init__(
        self,
        *,
        revision: str = "fixture-model-commit-a",
        after_documents: Callable[[], None] | None = None,
    ) -> None:
        self.space = EmbeddingSpace(
            provider_implementation="cognistore-test-semantic-provider",
            provider_implementation_version="1",
            model="cognistore/fixture-keywords",
            model_revision=revision,
            dimensions=3,
            preprocessing="fixture-keyword-projection",
        )
        self.after_documents = after_documents

    @staticmethod
    def _vector(text: str) -> tuple[float, float, float]:
        lowered = text.casefold()
        return (
            2.0 if "orchard" in lowered or "apple" in lowered else 0.1,
            2.0 if "database" in lowered or "backup" in lowered else 0.1,
            2.0 if "invoice" in lowered or "payment" in lowered else 0.1,
        )

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        result = tuple(self._vector(text) for text in texts)
        if self.after_documents is not None:
            callback, self.after_documents = self.after_documents, None
            callback()
        return result

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._vector(text)


def _publish_document(
    catalog: SQLCatalog,
    *,
    bucket: str,
    key: str,
    text: str,
    tier: str,
    mime: str,
    department: str,
    parser_runtime_version: str = "1.0",
    extraction_schema_version: int = 1,
    extra_metadata: Mapping[str, object] | None = None,
) -> None:
    payload = text.encode("utf-8")
    content = ContentIdentityBuilder(chunk_size=8).build(
        BytesIO(payload),
        expected_size=len(payload),
    )
    fence = catalog.capture_scan_fence(bucket, key)
    metadata: dict[str, object] = {
        "mime": mime,
        "department": department,
        "document_extraction": {
            "schema_version": extraction_schema_version,
            "status": "succeeded",
            "source_mime": mime,
            "source_size": len(payload),
            "parser": {
                "name": "fixture-parser",
                "implementation_version": "1",
                "runtime_version": parser_runtime_version,
            },
            "normalization_version": 1,
            "text": text,
            "text_bytes": len(payload),
            "output_bytes": len(payload) + 2,
            "document_metadata": {},
            "failure_code": None,
        },
    }
    metadata.update(dict(extra_metadata or {}))
    assert catalog.upsert_scan_observation(
        bucket,
        key,
        size=len(payload),
        tier=tier,
        generation=f"{tier}:{hashlib.sha256(payload).hexdigest()}",
        metadata=metadata,
        fence=fence,
        content=content,
    )


def test_pgvector_indexes_queries_filters_and_reembeds_idempotently(
    postgres_dsn: str,
) -> None:
    with SQLCatalog(postgres_dsn) as catalog:
        _publish_document(
            catalog,
            bucket="knowledge",
            key="agriculture/orchard.pdf",
            text="Apple trees in the orchard need winter pruning.",
            tier="warm",
            mime="application/pdf",
            department="agriculture",
        )
        _publish_document(
            catalog,
            bucket="knowledge",
            key="platform/database.docx",
            text="The database backup is restored during a recovery drill.",
            tier="warm",
            mime=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            department="platform",
        )
        _publish_document(
            catalog,
            bucket="finance",
            key="payables/invoices.pdf",
            text="Each approved invoice follows its payment terms.",
            tier="hot",
            mime="application/pdf",
            department="finance",
        )

        store = PgVectorEmbeddingStore(catalog)
        provider = _SemanticProvider()
        with pytest.raises(ValueError, match="at most 16000 dimensions"):
            store.ensure_space(
                replace(provider.space, dimensions=16_001),
                HnswIndexConfig(enabled=False),
            )
        with pytest.raises(ValueError, match="at most 2000 vector dimensions"):
            store.ensure_space(
                replace(provider.space, dimensions=2_001),
                HnswIndexConfig(),
            )
        indexer = EmbeddingIndexer(store, provider, batch_size=2)
        hnsw_ddl: list[str] = []

        def observe_hnsw_ddl(
            _connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany: bool,
        ) -> None:
            if statement.lstrip().upper().startswith("CREATE INDEX"):
                hnsw_ddl.append(statement)

        sa.event.listen(catalog.engine, "before_cursor_execute", observe_hnsw_ddl)
        try:
            for bucket, key in (
                ("knowledge", "agriculture/orchard.pdf"),
                ("knowledge", "platform/database.docx"),
                ("finance", "payables/invoices.pdf"),
            ):
                first = indexer.index_object(bucket, key)
                replay = indexer.index_object(bucket, key)
                assert first.embedded_passages == 1
                assert replay.embedded_passages == 0
                assert replay.reused_passages == 1
        finally:
            sa.event.remove(catalog.engine, "before_cursor_execute", observe_hnsw_ddl)
        assert len(hnsw_ddl) == 1
        forced = indexer.index_object(
            "knowledge",
            "agriculture/orchard.pdf",
            force=True,
        )
        assert forced.embedded_passages == 1
        assert forced.reused_passages == 0

        hits = indexer.search("How should an apple orchard be maintained?", limit=3)
        assert (hits[0].bucket, hits[0].key) == (
            "knowledge",
            "agriculture/orchard.pdf",
        )
        assert {(hit.bucket, hit.key) for hit in hits[1:]} == {
            ("knowledge", "platform/database.docx"),
            ("finance", "payables/invoices.pdf"),
        }
        assert hits[0].score > hits[1].score
        assert hits[0].passage_index == 0
        assert hits[0].text.startswith("Apple trees")
        orchard = catalog.get("knowledge", "agriculture/orchard.pdf")
        assert orchard is not None
        content_identity = orchard.metadata["content_identity"]
        assert isinstance(content_identity, dict)
        assert hits[0].source_sha256 == content_identity["sha256"]
        extraction = orchard.metadata["document_extraction"]
        assert isinstance(extraction, dict)
        normalized_text = extraction["text"]
        assert isinstance(normalized_text, str)
        assert hits[0].document_text_sha256 == hashlib.sha256(
            normalized_text.encode("utf-8")
        ).hexdigest()
        assert hits[0].object_metadata["department"] == "agriculture"
        assert "document_extraction" not in hits[0].object_metadata
        assert "content_identity" not in hits[0].object_metadata
        assert hits[0].object_metadata_truncated is False
        exact_hits = store.search(
            provider.space,
            provider.embed_query("How should an apple orchard be maintained?"),
            filters=SimilaritySearchFilters(),
            limit=3,
            exact=True,
        )
        assert [hit.passage_id for hit in exact_hits] == [hit.passage_id for hit in hits]

        filtered = indexer.search(
            "backup database",
            filters=SimilaritySearchFilters(
                buckets=frozenset({"knowledge"}),
                key_prefix="platform/",
                tiers=frozenset({"warm"}),
                mime_types=frozenset(
                    {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
                ),
                metadata={"department": "platform"},
            ),
            limit=5,
        )
        assert [(hit.bucket, hit.key) for hit in filtered] == [
            ("knowledge", "platform/database.docx")
        ]

        with catalog.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(embedding_vectors)
                    .where(embedding_vectors.c.space_id == provider.space.space_id)
                ).scalar_one()
                == 3
            )
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(embedding_document_spaces)
                ).scalar_one()
                == 3
            )
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(object_embedding_documents)
                ).scalar_one()
                == 3
            )

        index = store.index_metadata(provider.space)
        assert index["pgvector_version"] == "0.8.6"
        assert index["dimensions"] == 3
        assert index["vector_count"] == 3
        hnsw = index["hnsw"]
        assert isinstance(hnsw, dict)
        assert hnsw["enabled"] is True
        assert hnsw["m"] == 16
        assert hnsw["ef_construction"] == 64
        assert hnsw["ef_search"] == 40
        assert hnsw["iterative_scan"] == "strict_order"
        assert "vector_cosine_ops" in str(hnsw["index_definition"])
        assert "vector(3)" in str(hnsw["index_definition"]).lower()


def test_search_result_metadata_is_sql_sanitized_and_bounded(
    postgres_dsn: str,
) -> None:
    with SQLCatalog(postgres_dsn) as catalog:
        _publish_document(
            catalog,
            bucket="knowledge",
            key="metadata/normal.pdf",
            text="A database backup passage with internal extraction text.",
            tier="warm",
            mime="application/pdf",
            department="platform",
            extra_metadata={
                "nested": {"label": "public"},
                "nul_value": "before\0after",
            },
        )
        _publish_document(
            catalog,
            bucket="knowledge",
            key="metadata/oversized.pdf",
            text="Another database backup passage.",
            tier="warm",
            mime="application/pdf",
            department="platform",
            extra_metadata={
                "oversized": "x" * (MAX_SIMILARITY_RESULT_METADATA_BYTES + 1),
                "literal_escape": r"\u0000",
            },
        )
        _publish_document(
            catalog,
            bucket="knowledge",
            key="metadata/invalid-high-surrogate.pdf",
            text="A database backup passage with a lone high surrogate tag.",
            tier="warm",
            mime="application/pdf",
            department="platform",
            extra_metadata={"surrogate": chr(0xD800)},
        )
        _publish_document(
            catalog,
            bucket="knowledge",
            key="metadata/invalid-low-surrogate.pdf",
            text="A database backup passage with a lone low surrogate tag.",
            tier="warm",
            mime="application/pdf",
            department="platform",
            extra_metadata={"surrogate": chr(0xDC00)},
        )
        _publish_document(
            catalog,
            bucket="knowledge",
            key="metadata/valid-emoji.pdf",
            text="A database backup passage with a valid emoji tag.",
            tier="warm",
            mime="application/pdf",
            department="platform",
            extra_metadata={"emoji": "😀"},
        )
        _publish_document(
            catalog,
            bucket="knowledge",
            key="metadata/literal-surrogate.pdf",
            text="A database backup passage with a literal surrogate escape tag.",
            tier="warm",
            mime="application/pdf",
            department="platform",
            extra_metadata={"literal_escape": r"\uD800"},
        )
        indexer = EmbeddingIndexer(PgVectorEmbeddingStore(catalog), _SemanticProvider())
        for key in (
            "metadata/normal.pdf",
            "metadata/oversized.pdf",
            "metadata/invalid-high-surrogate.pdf",
            "metadata/invalid-low-surrogate.pdf",
            "metadata/valid-emoji.pdf",
            "metadata/literal-surrogate.pdf",
        ):
            indexer.index_object("knowledge", key)

        hits = indexer.search(
            "database backup",
            limit=10,
            exact=True,
        )
        by_key = {hit.key: hit for hit in hits}

        normal = by_key["metadata/normal.pdf"]
        assert normal.object_metadata["department"] == "platform"
        assert normal.object_metadata["nested"] == {"label": "public"}
        assert normal.object_metadata["nul_value"] == "before\0after"
        assert "document_extraction" not in normal.object_metadata
        assert "content_identity" not in normal.object_metadata
        assert normal.object_metadata_truncated is False

        oversized = by_key["metadata/oversized.pdf"]
        assert dict(oversized.object_metadata) == {}
        assert oversized.object_metadata_truncated is True
        assert by_key["metadata/valid-emoji.pdf"].object_metadata["emoji"] == "😀"
        assert (
            by_key["metadata/literal-surrogate.pdf"].object_metadata["literal_escape"] == r"\uD800"
        )
        assert (
            ord(by_key["metadata/invalid-high-surrogate.pdf"].object_metadata["surrogate"])
            == 0xD800
        )
        assert (
            ord(by_key["metadata/invalid-low-surrogate.pdf"].object_metadata["surrogate"]) == 0xDC00
        )

        # PostgreSQL JSON can retain escaped NUL and lone surrogates but cannot
        # materialize fields from those objects as TEXT. Filtering excludes the
        # malformed rows without aborting the query, while valid pairs and
        # literal backslash-u strings remain eligible.
        filtered = indexer.search(
            "database backup",
            filters=SimilaritySearchFilters(metadata={"department": "platform"}),
            limit=10,
            exact=True,
        )
        assert {hit.key for hit in filtered} == {
            "metadata/literal-surrogate.pdf",
            "metadata/oversized.pdf",
            "metadata/valid-emoji.pdf",
        }


def test_force_reset_cannot_race_completion_gate_publication(
    postgres_dsn: str,
) -> None:
    with SQLCatalog(postgres_dsn) as catalog:
        _publish_document(
            catalog,
            bucket="knowledge",
            key="concurrency/reset.pdf",
            text="A database backup passage.",
            tier="warm",
            mime="application/pdf",
            department="platform",
        )
        store = PgVectorEmbeddingStore(catalog)
        provider = _SemanticProvider()
        indexer = EmbeddingIndexer(store, provider)
        indexer.index_object("knowledge", "concurrency/reset.pdf")
        document = EmbeddingDocument.build(
            store.load_source("knowledge", "concurrency/reset.pdf"),
            indexer.chunker,
        )

        completion_at_gate = threading.Event()
        release_completion = threading.Event()
        reset_lock_attempted = threading.Event()
        reset_done = threading.Event()
        failures: list[BaseException] = []

        def observe_interleaving(
            _connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany: bool,
        ) -> None:
            normalized = " ".join(statement.upper().split())
            thread_name = threading.current_thread().name
            if thread_name == "embedding-complete" and normalized.startswith(
                "INSERT INTO EMBEDDING_DOCUMENT_SPACES"
            ):
                completion_at_gate.set()
                if not release_completion.wait(5):
                    raise TimeoutError("completion interleaving was not released")
            if (
                thread_name == "embedding-reset"
                and "FOR UPDATE" in normalized
                and (
                    "FROM OBJECT_MUTATION_FENCES" in normalized
                    or "FROM EMBEDDING_DOCUMENTS" in normalized
                )
            ):
                reset_lock_attempted.set()

        def complete() -> None:
            try:
                store.complete_document_space(document, provider.space)
            except BaseException as exc:  # pragma: no branch - reported below
                failures.append(exc)

        def reset() -> None:
            try:
                store.reset_document_space(document, provider.space)
            except BaseException as exc:  # pragma: no branch - reported below
                failures.append(exc)
            finally:
                reset_done.set()

        complete_thread = threading.Thread(
            target=complete,
            name="embedding-complete",
            daemon=True,
        )
        reset_thread = threading.Thread(
            target=reset,
            name="embedding-reset",
            daemon=True,
        )
        sa.event.listen(catalog.engine, "before_cursor_execute", observe_interleaving)
        try:
            complete_thread.start()
            assert completion_at_gate.wait(5)
            reset_thread.start()
            assert reset_lock_attempted.wait(5)
            assert not reset_done.wait(0.2)
            release_completion.set()
            complete_thread.join(timeout=5)
            reset_thread.join(timeout=5)
        finally:
            release_completion.set()
            complete_thread.join(timeout=5)
            if reset_thread.ident is not None:
                reset_thread.join(timeout=5)
            sa.event.remove(catalog.engine, "before_cursor_execute", observe_interleaving)

        assert complete_thread.is_alive() is False
        assert reset_thread.is_alive() is False
        assert failures == []
        with catalog.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(embedding_document_spaces)
                    .where(
                        embedding_document_spaces.c.document_id == document.document_id,
                        embedding_document_spaces.c.space_id == provider.space.space_id,
                    )
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(
                        embedding_vectors.join(
                            embedding_passages,
                            embedding_passages.c.passage_id == embedding_vectors.c.passage_id,
                        )
                    )
                    .where(
                        embedding_passages.c.document_id == document.document_id,
                        embedding_vectors.c.space_id == provider.space.space_id,
                    )
                ).scalar_one()
                == 0
            )
        assert indexer.search("database backup", exact=True) == []


def test_force_rejects_a_document_space_shared_by_another_object(
    postgres_dsn: str,
) -> None:
    with SQLCatalog(postgres_dsn) as catalog:
        for key in ("aliases/primary.pdf", "aliases/secondary.pdf"):
            _publish_document(
                catalog,
                bucket="knowledge",
                key=key,
                text="A shared database backup passage.",
                tier="warm",
                mime="application/pdf",
                department="platform",
            )

        store = PgVectorEmbeddingStore(catalog)
        provider = _SemanticProvider(revision="shared-alias-space")
        indexer = EmbeddingIndexer(store, provider)
        primary = indexer.index_object("knowledge", "aliases/primary.pdf")
        secondary = indexer.index_object("knowledge", "aliases/secondary.pdf")

        assert primary.document_id == secondary.document_id
        assert primary.space_id == secondary.space_id
        assert secondary.embedded_passages == 0
        provider_calls: list[str] = []
        provider.after_documents = lambda: provider_calls.append("documents")

        with pytest.raises(
            EmbeddingForceConflictError,
            match="cannot force re-embed a document/model space shared by another object",
        ):
            indexer.index_object("knowledge", "aliases/primary.pdf", force=True)

        assert provider_calls == []
        assert {hit.key for hit in indexer.search("database backup", limit=10, exact=True)} == {
            "aliases/primary.pdf",
            "aliases/secondary.pdf",
        }
        passage_ids = sa.select(embedding_passages.c.passage_id).where(
            embedding_passages.c.document_id == primary.document_id
        )
        with catalog.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(object_embedding_documents)
                    .where(
                        object_embedding_documents.c.document_id == primary.document_id,
                        object_embedding_documents.c.space_id == primary.space_id,
                    )
                ).scalar_one()
                == 2
            )
            assert (
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(embedding_document_spaces)
                    .where(
                        embedding_document_spaces.c.document_id == primary.document_id,
                        embedding_document_spaces.c.space_id == primary.space_id,
                    )
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(embedding_vectors)
                    .where(
                        embedding_vectors.c.space_id == primary.space_id,
                        embedding_vectors.c.passage_id.in_(passage_ids),
                    )
                ).scalar_one()
                == primary.total_passages
            )


def test_failed_force_with_new_passage_layout_hides_the_old_mapping(
    postgres_dsn: str,
) -> None:
    with SQLCatalog(postgres_dsn) as catalog:
        _publish_document(
            catalog,
            bucket="knowledge",
            key="layouts/forced-database.pdf",
            text=(
                "Database backup procedures require restore drills, checksum "
                "validation, and documented recovery ownership."
            ),
            tier="warm",
            mime="application/pdf",
            department="platform",
        )
        store = PgVectorEmbeddingStore(catalog)
        provider = _SemanticProvider(revision="force-layout-space")
        original_indexer = EmbeddingIndexer(
            store,
            provider,
            chunker=DeterministicPassageChunker(
                PassageChunkerConfig(max_codepoints=1_000, overlap_codepoints=0)
            ),
        )
        original = original_indexer.index_object(
            "knowledge",
            "layouts/forced-database.pdf",
        )
        assert len(original_indexer.search("database backup", exact=True)) == 1

        short_chunker = DeterministicPassageChunker(
            PassageChunkerConfig(max_codepoints=24, overlap_codepoints=4)
        )
        replacement_document = EmbeddingDocument.build(
            store.load_source("knowledge", "layouts/forced-database.pdf"),
            short_chunker,
        )
        assert replacement_document.document_id != original.document_id
        assert len(replacement_document.passages) > original.total_passages

        failure_calls: list[str] = []

        def fail_embedding() -> None:
            failure_calls.append("documents")
            raise RuntimeError("fixture embedding failure after force reset")

        failing_provider = _SemanticProvider(
            revision="force-layout-space",
            after_documents=fail_embedding,
        )
        replacement_indexer = EmbeddingIndexer(
            store,
            failing_provider,
            chunker=short_chunker,
            batch_size=1,
        )
        with pytest.raises(RuntimeError, match="fixture embedding failure after force reset"):
            replacement_indexer.index_object(
                "knowledge",
                "layouts/forced-database.pdf",
                force=True,
            )

        assert failure_calls == ["documents"]
        assert original_indexer.search("database backup", exact=True) == []
        original_passage_ids = sa.select(embedding_passages.c.passage_id).where(
            embedding_passages.c.document_id == original.document_id
        )
        replacement_passage_ids = sa.select(embedding_passages.c.passage_id).where(
            embedding_passages.c.document_id == replacement_document.document_id
        )
        with catalog.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(object_embedding_documents)
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(embedding_document_spaces)
                    .where(
                        embedding_document_spaces.c.document_id == original.document_id,
                        embedding_document_spaces.c.space_id == original.space_id,
                    )
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(embedding_vectors)
                    .where(
                        embedding_vectors.c.space_id == original.space_id,
                        embedding_vectors.c.passage_id.in_(original_passage_ids),
                    )
                ).scalar_one()
                == original.total_passages
            )
            assert (
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(embedding_document_spaces)
                    .where(
                        embedding_document_spaces.c.document_id == replacement_document.document_id,
                        embedding_document_spaces.c.space_id == original.space_id,
                    )
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(embedding_vectors)
                    .where(
                        embedding_vectors.c.space_id == original.space_id,
                        embedding_vectors.c.passage_id.in_(replacement_passage_ids),
                    )
                ).scalar_one()
                == 0
            )


def test_model_revisions_are_isolated_in_distinct_pgvector_spaces(
    postgres_dsn: str,
) -> None:
    with SQLCatalog(postgres_dsn) as catalog:
        _publish_document(
            catalog,
            bucket="knowledge",
            key="orchard.pdf",
            text="The apple orchard is ready for harvest.",
            tier="warm",
            mime="application/pdf",
            department="agriculture",
        )
        store = PgVectorEmbeddingStore(catalog)
        first = EmbeddingIndexer(store, _SemanticProvider(revision="model-commit-a"))
        second = EmbeddingIndexer(store, _SemanticProvider(revision="model-commit-b"))

        first_report = first.index_object("knowledge", "orchard.pdf")
        second_report = second.index_object("knowledge", "orchard.pdf")

        assert first_report.document_id == second_report.document_id
        assert first_report.space_id != second_report.space_id
        assert len(first.search("apple", limit=10)) == 1
        assert len(second.search("apple", limit=10)) == 1
        with catalog.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(embedding_spaces)
                ).scalar_one()
                == 2
            )
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(embedding_vectors)
                ).scalar_one()
                == 2
            )
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(embedding_passages)
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(embedding_documents)
                ).scalar_one()
                == 1
            )


def test_model_spaces_keep_independent_active_passage_layouts(
    postgres_dsn: str,
) -> None:
    with SQLCatalog(postgres_dsn) as catalog:
        _publish_document(
            catalog,
            bucket="knowledge",
            key="layouts/database.pdf",
            text=(
                "Database backup procedures require restore drills, checksum "
                "validation, and documented recovery ownership."
            ),
            tier="warm",
            mime="application/pdf",
            department="platform",
        )
        store = PgVectorEmbeddingStore(catalog)
        short_layout = EmbeddingIndexer(
            store,
            _SemanticProvider(revision="layout-model-a"),
            chunker=DeterministicPassageChunker(
                PassageChunkerConfig(max_codepoints=24, overlap_codepoints=4)
            ),
        )
        long_layout = EmbeddingIndexer(
            store,
            _SemanticProvider(revision="layout-model-b"),
            chunker=DeterministicPassageChunker(
                PassageChunkerConfig(max_codepoints=1_000, overlap_codepoints=0)
            ),
        )

        short_report = short_layout.index_object("knowledge", "layouts/database.pdf")
        long_report = long_layout.index_object("knowledge", "layouts/database.pdf")

        assert short_report.document_id != long_report.document_id
        assert short_report.total_passages > 1
        assert long_report.total_passages == 1
        short_hits = short_layout.search("database backup", limit=10, exact=True)
        long_hits = long_layout.search("database backup", limit=10, exact=True)
        assert short_hits
        assert len(long_hits) == 1
        assert {hit.space_id for hit in short_hits} == {short_report.space_id}
        assert {hit.space_id for hit in long_hits} == {long_report.space_id}
        with catalog.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(object_embedding_documents)
                ).scalar_one()
                == 2
            )


def test_hnsw_disabled_default_search_uses_global_exact_tie_order(
    postgres_dsn: str,
) -> None:
    with SQLCatalog(postgres_dsn) as catalog:
        store = PgVectorEmbeddingStore(catalog)
        provider = _SemanticProvider(revision="exact-only-space")
        indexer = EmbeddingIndexer(
            store,
            provider,
            hnsw=HnswIndexConfig(enabled=False),
        )
        for index in reversed(range(6)):
            key = f"ties/{index}.pdf"
            _publish_document(
                catalog,
                bucket="knowledge",
                key=key,
                text="A neutral passage with the same embedding projection.",
                tier="warm",
                mime="application/pdf",
                department="platform",
            )
            indexer.index_object("knowledge", key)

        observed_sql: list[str] = []

        def capture_sql(
            _connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany: bool,
        ) -> None:
            observed_sql.append(" ".join(statement.upper().split()))

        sa.event.listen(catalog.engine, "before_cursor_execute", capture_sql)
        try:
            default_hits = indexer.search("neutral", limit=1)
        finally:
            sa.event.remove(catalog.engine, "before_cursor_execute", capture_sql)

        assert [hit.key for hit in default_hits] == ["ties/0.pdf"]
        assert [hit.key for hit in indexer.search("neutral", limit=1, exact=True)] == ["ties/0.pdf"]
        assert "SET LOCAL ENABLE_INDEXSCAN = OFF" in observed_sql
        search_sql = next(
            statement
            for statement in observed_sql
            if statement.startswith("SELECT EMBEDDING_CANDIDATES.SPACE_ID")
        )
        assert (
            "ORDER BY COSINE_DISTANCE, OBJECTS.BUCKET, OBJECTS.OBJECT_KEY, "
            "EMBEDDING_PASSAGES.PASSAGE_INDEX, EMBEDDING_PASSAGES.PASSAGE_ID" in search_sql
        )


def test_source_replacement_during_provider_call_cannot_publish_stale_vectors(
    postgres_dsn: str,
) -> None:
    with SQLCatalog(postgres_dsn) as catalog:
        _publish_document(
            catalog,
            bucket="knowledge",
            key="changing.pdf",
            text="The original orchard passage.",
            tier="warm",
            mime="application/pdf",
            department="agriculture",
        )

        def replace_source() -> None:
            catalog.upsert(
                "knowledge",
                "changing.pdf",
                size=7,
                tier="warm",
                metadata={"generation": "replacement"},
            )

        provider = _SemanticProvider(after_documents=replace_source)
        indexer = EmbeddingIndexer(PgVectorEmbeddingStore(catalog), provider)

        with pytest.raises(EmbeddingSourceChangedError):
            indexer.index_object("knowledge", "changing.pdf")

        with catalog.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(embedding_vectors)
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(object_embedding_documents)
                ).scalar_one()
                == 0
            )


def test_rescan_retains_only_embeddings_for_the_current_extraction(
    postgres_dsn: str,
) -> None:
    with SQLCatalog(postgres_dsn) as catalog:
        source = {
            "bucket": "knowledge",
            "key": "rescan.pdf",
            "text": "The database backup is tested in a recovery drill.",
            "mime": "application/pdf",
            "department": "platform",
        }
        _publish_document(catalog, tier="warm", **source)
        store = PgVectorEmbeddingStore(catalog)
        provider = _SemanticProvider()
        indexer = EmbeddingIndexer(store, provider)
        first = indexer.index_object("knowledge", "rescan.pdf")

        # A placement-only rescan keeps the exact normalized-text identity live.
        _publish_document(catalog, tier="hot", **source)
        with catalog.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(object_embedding_documents)
                ).scalar_one()
                == 1
            )
        assert [hit.tier for hit in indexer.search("database backup")] == ["hot"]

        # Parser provenance is part of the document identity. The previous
        # vectors remain immutable history but cannot be returned as current.
        _publish_document(
            catalog,
            tier="hot",
            parser_runtime_version="2.0",
            **source,
        )
        with catalog.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(object_embedding_documents)
                ).scalar_one()
                == 0
            )
        assert indexer.search("database backup") == []

        second = indexer.index_object("knowledge", "rescan.pdf")
        assert second.document_id != first.document_id
        assert len(indexer.search("database backup")) == 1

        # Extraction envelope and MIME provenance also version the passage
        # identity, so either can evolve without colliding with prior history.
        source["mime"] = "application/x-fixture-document"
        _publish_document(
            catalog,
            tier="hot",
            parser_runtime_version="2.0",
            extraction_schema_version=2,
            **source,
        )
        third = indexer.index_object("knowledge", "rescan.pdf")
        assert third.document_id not in {first.document_id, second.document_id}
        assert len(indexer.search("database backup")) == 1


def test_source_replacement_cannot_inherit_omitted_stale_extraction(
    postgres_dsn: str,
) -> None:
    with SQLCatalog(postgres_dsn) as catalog:
        _publish_document(
            catalog,
            bucket="knowledge",
            key="partial-metadata.pdf",
            text="old text",
            tier="warm",
            mime="application/pdf",
            department="platform",
        )
        store = PgVectorEmbeddingStore(catalog)
        indexer = EmbeddingIndexer(store, _SemanticProvider())
        indexer.index_object("knowledge", "partial-metadata.pdf")

        replacement = b"new text"
        content = ContentIdentityBuilder(chunk_size=8).build(
            BytesIO(replacement),
            expected_size=len(replacement),
        )
        fence = catalog.capture_scan_fence("knowledge", "partial-metadata.pdf")
        assert catalog.upsert_scan_observation(
            "knowledge",
            "partial-metadata.pdf",
            size=len(replacement),
            tier="warm",
            generation="replacement-with-partial-metadata",
            metadata={"department": "platform"},
            fence=fence,
            content=content,
        )

        record = catalog.get("knowledge", "partial-metadata.pdf")
        assert record is not None
        assert "document_extraction" not in record.metadata
        with pytest.raises(EmbeddingSourceUnavailableError):
            store.load_source("knowledge", "partial-metadata.pdf")
        assert indexer.search("old text") == []


def test_embedding_store_rejects_sqlite_and_read_only_writes(
    postgres_dsn: str,
    tmp_path,
) -> None:
    with SQLCatalog(tmp_path / "catalog.db") as sqlite_catalog:
        with pytest.raises(EmbeddingBackendUnsupportedError):
            PgVectorEmbeddingStore(sqlite_catalog)

    with SQLCatalog(postgres_dsn) as writer:
        _publish_document(
            writer,
            bucket="knowledge",
            key="read-only.pdf",
            text="A database backup passage.",
            tier="warm",
            mime="application/pdf",
            department="platform",
        )
        EmbeddingIndexer(PgVectorEmbeddingStore(writer), _SemanticProvider()).index_object(
            "knowledge", "read-only.pdf"
        )

    with SQLCatalog(postgres_dsn, read_only=True) as reader:
        store = PgVectorEmbeddingStore(reader)
        indexer = EmbeddingIndexer(store, _SemanticProvider())
        assert len(indexer.search("database backup")) == 1
        with pytest.raises(PermissionError, match="read-only"):
            indexer.index_object("knowledge", "read-only.pdf")
