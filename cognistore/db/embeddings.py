"""PostgreSQL/pgvector persistence for versioned passage embeddings."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, RowMapping

from cognistore.core.embedding_index import (
    MAX_SIMILARITY_RESULT_METADATA_BYTES,
    EmbeddingBackendUnsupportedError,
    EmbeddingDocument,
    EmbeddingDocumentSource,
    EmbeddingForceConflictError,
    EmbeddingIdentityCollisionError,
    EmbeddingIndexIncompleteError,
    EmbeddingSourceChangedError,
    EmbeddingSourceUnavailableError,
    HnswIndexConfig,
    SimilaritySearchFilters,
    SimilaritySearchResult,
)
from cognistore.core.embeddings import EmbeddingSpace, validate_embedding_vectors
from cognistore.core.passages import NormalizedDocumentIdentity, Passage

from .catalog import SQLCatalog
from .schema import (
    content_manifests,
    embedding_document_spaces,
    embedding_documents,
    embedding_passages,
    embedding_spaces,
    embedding_vectors,
    object_contents,
    object_embedding_documents,
    object_mutation_fences,
    object_placements,
    objects,
)
from .types import PgVector, PortableVector

MAX_HNSW_VECTOR_DIMENSIONS = 2_000
MAX_PGVECTOR_DIMENSIONS = 16_000
MINIMUM_PGVECTOR_VERSION = (0, 8, 0)
_SEARCH_OVERFETCH_FACTOR = 4
_SEARCH_RESULT_INTERNAL_METADATA_KEYS = (
    "document_extraction",
    "content_identity",
)
_JSON_NUL_ESCAPE_PATTERN = r"(?<!\\)(?:\\\\)*\\u0000"
_JSON_VALID_SURROGATE_PAIR_PATTERN = (
    r"((?<!\\)(?:\\\\)*)"
    r"\\u[dD][89aAbB][0-9a-fA-F]{2}"
    r"\\u[dD][cCdDeEfF][0-9a-fA-F]{2}"
)
_JSON_SURROGATE_ESCAPE_PATTERN = r"(?<!\\)(?:\\\\)*\\u[dD][89a-fA-F][0-9a-fA-F]{2}"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _mapping(row: RowMapping, keys: Sequence[str]) -> dict[str, object]:
    return {key: row[key] for key in keys}


def _pgvector_version(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise EmbeddingBackendUnsupportedError(
            "the PostgreSQL vector extension has an invalid version"
        )
    pieces = value.split(".")
    if not 2 <= len(pieces) <= 3 or any(not piece.isdigit() for piece in pieces):
        raise EmbeddingBackendUnsupportedError(
            f"the PostgreSQL vector extension has an unsupported version: {value!r}"
        )
    return (
        int(pieces[0]),
        int(pieces[1]),
        int(pieces[2]) if len(pieces) == 3 else 0,
    )


def _require_pgvector_capabilities(connection: Connection) -> str:
    version = connection.exec_driver_sql(
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
    ).scalar_one_or_none()
    observed = _pgvector_version(version)
    if observed < MINIMUM_PGVECTOR_VERSION:
        minimum = ".".join(str(part) for part in MINIMUM_PGVECTOR_VERSION)
        raise EmbeddingBackendUnsupportedError(
            f"pgvector {minimum} or newer is required for filtered HNSW search; found {version}"
        )
    assert isinstance(version, str)
    return version


class PgVectorEmbeddingStore:
    """A focused embedding repository beside the backend-neutral catalog DAL.

    Provider calls never happen here.  Every write owns one short transaction,
    locks the same object mutation fence used by scans, and rechecks the active
    content/extraction before publishing vectors or activating a passage set.
    """

    def __init__(self, catalog: SQLCatalog) -> None:
        if not isinstance(catalog, SQLCatalog):
            raise TypeError("catalog must be a SQLCatalog")
        if catalog.backend != "postgresql":
            raise EmbeddingBackendUnsupportedError(
                "similarity search requires a PostgreSQL catalog with pgvector"
            )
        self.catalog = catalog

    @contextlib.contextmanager
    def _connection(self) -> Iterator[Connection]:
        with self.catalog.engine.connect() as connection:
            yield connection

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[Connection]:
        if self.catalog.read_only:
            raise PermissionError("cannot write embeddings through a read-only catalog")
        with self.catalog.engine.begin() as connection:
            yield connection

    @staticmethod
    def _source_statement(bucket: str, key: str) -> sa.Select[Any]:
        return (
            sa.select(
                objects.c.object_id,
                objects.c.bucket,
                objects.c.object_key,
                objects.c.size,
                objects.c.metadata,
                object_contents.c.manifest_id,
                content_manifests.c.content_sha256,
            )
            .select_from(
                objects.join(
                    object_contents,
                    object_contents.c.object_id == objects.c.object_id,
                ).join(
                    content_manifests,
                    content_manifests.c.manifest_id == object_contents.c.manifest_id,
                )
            )
            .where(
                objects.c.bucket == bucket,
                objects.c.object_key == key,
            )
        )

    @staticmethod
    def _source_from_row(row: RowMapping | None) -> EmbeddingDocumentSource:
        if row is None:
            raise EmbeddingSourceUnavailableError(
                "object has no current canonical content manifest"
            )
        metadata = row["metadata"]
        if not isinstance(metadata, Mapping):
            raise EmbeddingSourceUnavailableError("object metadata is malformed")
        extraction = metadata.get("document_extraction")
        if not isinstance(extraction, Mapping):
            raise EmbeddingSourceUnavailableError("object has no document extraction record")
        if extraction.get("status") != "succeeded":
            failure = extraction.get("failure_code")
            suffix = f": {failure}" if isinstance(failure, str) and failure else ""
            raise EmbeddingSourceUnavailableError(
                "object document extraction did not succeed" + suffix
            )
        text = extraction.get("text")
        parser = extraction.get("parser")
        source_mime = extraction.get("source_mime")
        schema_version = extraction.get("schema_version")
        normalization_version = extraction.get("normalization_version")
        source_size = extraction.get("source_size")
        text_bytes = extraction.get("text_bytes")
        if (
            not isinstance(text, str)
            or not isinstance(parser, Mapping)
            or not isinstance(source_mime, str)
            or not source_mime
            or isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version < 1
            or isinstance(normalization_version, bool)
            or not isinstance(normalization_version, int)
            or normalization_version < 1
            or source_size != row["size"]
            or text_bytes != len(text.encode("utf-8"))
        ):
            raise EmbeddingSourceUnavailableError("object document extraction record is malformed")
        parser_name = parser.get("name")
        parser_implementation_version = parser.get("implementation_version")
        parser_runtime_version = parser.get("runtime_version")
        if not all(
            isinstance(value, str) and bool(value)
            for value in (
                parser_name,
                parser_implementation_version,
                parser_runtime_version,
            )
        ):
            raise EmbeddingSourceUnavailableError("object parser identity is malformed")
        assert isinstance(parser_name, str)
        assert isinstance(parser_implementation_version, str)
        assert isinstance(parser_runtime_version, str)
        try:
            document = NormalizedDocumentIdentity(
                manifest_id=row["manifest_id"],
                source_sha256=row["content_sha256"],
                extraction_schema_version=schema_version,
                source_mime=source_mime,
                parser_name=parser_name,
                parser_implementation_version=parser_implementation_version,
                parser_runtime_version=parser_runtime_version,
                normalization_version=normalization_version,
            )
            return EmbeddingDocumentSource(
                bucket=row["bucket"],
                key=row["object_key"],
                extraction_schema_version=schema_version,
                source_mime=source_mime,
                document=document,
                text=text,
            )
        except (TypeError, UnicodeError, ValueError) as exc:
            raise EmbeddingSourceUnavailableError(
                "object document extraction identity is malformed"
            ) from exc

    def load_source(self, bucket: str, key: str) -> EmbeddingDocumentSource:
        with self._connection() as connection:
            row = connection.execute(self._source_statement(bucket, key)).mappings().first()
        return self._source_from_row(row)

    def _lock_current_source(
        self,
        connection: Connection,
        source: EmbeddingDocumentSource,
    ) -> UUID:
        connection.execute(
            postgresql.insert(object_mutation_fences)
            .values(bucket=source.bucket, object_key=source.key, generation=0)
            .on_conflict_do_nothing()
        )
        connection.execute(
            sa.select(object_mutation_fences.c.generation)
            .where(
                object_mutation_fences.c.bucket == source.bucket,
                object_mutation_fences.c.object_key == source.key,
            )
            .with_for_update()
        ).one()
        row = (
            connection.execute(self._source_statement(source.bucket, source.key)).mappings().first()
        )
        try:
            current = self._source_from_row(row)
        except EmbeddingSourceUnavailableError as exc:
            raise EmbeddingSourceChangedError(
                "embedding source changed while provider work was in progress"
            ) from exc
        if current != source:
            raise EmbeddingSourceChangedError(
                "embedding source changed while provider work was in progress"
            )
        assert row is not None
        return row["object_id"]

    @staticmethod
    def _lock_embedding_document(
        connection: Connection,
        document_id: UUID,
    ) -> None:
        """Serialize mutations of vectors and completion for a shared document."""

        locked_document = connection.execute(
            sa.select(embedding_documents.c.document_id)
            .where(embedding_documents.c.document_id == document_id)
            .with_for_update()
        ).scalar_one_or_none()
        if locked_document is None:
            raise EmbeddingIdentityCollisionError(
                "embedding document disappeared before its vectors were mutated"
            )

    @staticmethod
    def _space_values(
        space: EmbeddingSpace,
        config: HnswIndexConfig,
        *,
        now: str,
    ) -> dict[str, object]:
        index_name = f"embedding_vectors_hnsw_{space.space_id.hex}" if config.enabled else None
        return {
            "space_id": space.space_id,
            "provider_implementation": space.provider_implementation,
            "provider_implementation_version": space.provider_implementation_version,
            "model": space.model,
            "model_revision": space.model_revision,
            "dimensions": space.dimensions,
            "distance_metric": space.distance_metric,
            "normalization": space.normalization,
            "normalization_version": space.normalization_version,
            "preprocessing": space.preprocessing,
            "preprocessing_version": space.preprocessing_version,
            "fingerprint": space.fingerprint,
            "hnsw_enabled": config.enabled,
            "hnsw_m": config.m,
            "hnsw_ef_construction": config.ef_construction,
            "hnsw_ef_search": config.ef_search,
            "hnsw_iterative_scan": config.iterative_scan,
            "hnsw_index_name": index_name,
            "created_at": now,
            "updated_at": now,
        }

    def ensure_space(self, space: EmbeddingSpace, config: HnswIndexConfig) -> None:
        if not isinstance(space, EmbeddingSpace):
            raise ValueError("space must be an EmbeddingSpace")
        if not isinstance(config, HnswIndexConfig):
            raise ValueError("config must be an HnswIndexConfig")
        if space.dimensions > MAX_PGVECTOR_DIMENSIONS:
            raise ValueError(
                f"pgvector dense vectors support at most {MAX_PGVECTOR_DIMENSIONS} dimensions"
            )
        if config.enabled and space.dimensions > MAX_HNSW_VECTOR_DIMENSIONS:
            raise ValueError(
                "pgvector HNSW indexes support at most "
                f"{MAX_HNSW_VECTOR_DIMENSIONS} vector dimensions; disable HNSW "
                "for exact search or use a lower-dimensional model"
            )
        now = _timestamp()
        values = self._space_values(space, config, now=now)
        comparable = tuple(key for key in values if key not in {"created_at", "updated_at"})
        with self._transaction() as connection:
            _require_pgvector_capabilities(connection)
            inserted_space = connection.execute(
                postgresql.insert(embedding_spaces)
                .values(**values)
                .on_conflict_do_nothing()
                .returning(embedding_spaces.c.space_id)
            ).scalar_one_or_none()
            row = (
                connection.execute(
                    sa.select(*embedding_spaces.c).where(
                        embedding_spaces.c.space_id == space.space_id
                    )
                )
                .mappings()
                .first()
            )
            if row is None or _mapping(row, comparable) != {key: values[key] for key in comparable}:
                raise EmbeddingIdentityCollisionError(
                    "embedding space identity or HNSW configuration conflicts "
                    "with the persisted search space"
                )
            if config.enabled:
                index_name = values["hnsw_index_name"]
                assert isinstance(index_name, str)
                dimensions = space.dimensions
                index_exists = connection.execute(
                    sa.text("SELECT to_regclass(:index_name)"),
                    {"index_name": index_name},
                ).scalar_one()
                if inserted_space is not None or index_exists is None:
                    connection.exec_driver_sql(
                        f"CREATE INDEX IF NOT EXISTS {index_name} "
                        "ON embedding_vectors USING hnsw "
                        f"((embedding::vector({dimensions})) vector_cosine_ops) "
                        f"WITH (m = {config.m}, ef_construction = {config.ef_construction}) "
                        f"WHERE space_id = '{space.space_id}'::uuid "
                        f"AND dimensions = {dimensions}"
                    )

    @staticmethod
    def _document_values(
        document: EmbeddingDocument,
        *,
        now: str,
    ) -> dict[str, object]:
        source = document.source
        identity = source.document
        return {
            "document_id": document.document_id,
            "source_document_id": identity.document_id,
            "source_fingerprint": identity.fingerprint,
            "fingerprint": document.fingerprint,
            "manifest_id": identity.manifest_id,
            "source_sha256": identity.source_sha256,
            "extraction_schema_version": source.extraction_schema_version,
            "source_mime": source.source_mime,
            "parser_name": identity.parser_name,
            "parser_implementation_version": identity.parser_implementation_version,
            "parser_runtime_version": identity.parser_runtime_version,
            "normalization_name": identity.normalization_name,
            "normalization_version": identity.normalization_version,
            "normalized_text": source.text,
            "text_sha256": source.text_sha256,
            "chunker_algorithm": document.chunker.algorithm,
            "chunker_version": document.chunker.version,
            "max_codepoints": document.chunker.max_codepoints,
            "overlap_codepoints": document.chunker.overlap_codepoints,
            "passage_count": len(document.passages),
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _passage_values(
        document: EmbeddingDocument,
        passage: Passage,
    ) -> dict[str, object]:
        return {
            "passage_id": passage.passage_id,
            "document_id": document.document_id,
            "passage_index": passage.index,
            "start_codepoint": passage.start_codepoint,
            "end_codepoint": passage.end_codepoint,
            "text": passage.text,
            "text_sha256": passage.text_sha256,
            "fingerprint": passage.fingerprint,
        }

    def prepare_document(self, document: EmbeddingDocument) -> None:
        if not isinstance(document, EmbeddingDocument):
            raise ValueError("document must be an EmbeddingDocument")
        now = _timestamp()
        values = self._document_values(document, now=now)
        comparable = tuple(key for key in values if key not in {"created_at", "updated_at"})
        passage_values = [self._passage_values(document, passage) for passage in document.passages]
        with self._transaction() as connection:
            connection.execute(
                postgresql.insert(embedding_documents).values(**values).on_conflict_do_nothing()
            )
            row = (
                connection.execute(
                    sa.select(*embedding_documents.c).where(
                        embedding_documents.c.document_id == document.document_id
                    )
                )
                .mappings()
                .first()
            )
            if row is None or _mapping(row, comparable) != {key: values[key] for key in comparable}:
                raise EmbeddingIdentityCollisionError(
                    "embedding document identity conflicts with persisted provenance"
                )
            if passage_values:
                connection.execute(
                    postgresql.insert(embedding_passages)
                    .values(passage_values)
                    .on_conflict_do_nothing()
                )
            rows = (
                connection.execute(
                    sa.select(*embedding_passages.c)
                    .where(embedding_passages.c.document_id == document.document_id)
                    .order_by(embedding_passages.c.passage_index)
                )
                .mappings()
                .all()
            )
            expected = passage_values
            observed = [
                _mapping(row, tuple(expected_row)) for row, expected_row in zip(rows, expected)
            ]
            if len(rows) != len(expected) or observed != expected:
                raise EmbeddingIdentityCollisionError(
                    "embedding passage identities conflict with the persisted layout"
                )

    def existing_passage_ids(
        self,
        document_id: UUID,
        space_id: UUID,
    ) -> frozenset[UUID]:
        statement = (
            sa.select(embedding_vectors.c.passage_id)
            .select_from(
                embedding_vectors.join(
                    embedding_passages,
                    embedding_passages.c.passage_id == embedding_vectors.c.passage_id,
                )
            )
            .where(
                embedding_passages.c.document_id == document_id,
                embedding_vectors.c.space_id == space_id,
            )
        )
        with self._connection() as connection:
            return frozenset(connection.execute(statement).scalars())

    def reset_document_space(
        self,
        document: EmbeddingDocument,
        space: EmbeddingSpace,
    ) -> None:
        passage_ids = sa.select(embedding_passages.c.passage_id).where(
            embedding_passages.c.document_id == document.document_id
        )
        with self._transaction() as connection:
            object_id = self._lock_current_source(connection, document.source)
            self._lock_embedding_document(connection, document.document_id)
            shared_mapping_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(object_embedding_documents)
                .where(
                    object_embedding_documents.c.document_id == document.document_id,
                    object_embedding_documents.c.space_id == space.space_id,
                    object_embedding_documents.c.object_id != object_id,
                )
            ).scalar_one()
            if shared_mapping_count:
                raise EmbeddingForceConflictError(
                    "cannot force re-embed a document/model space shared by another object"
                )
            connection.execute(
                sa.delete(object_embedding_documents).where(
                    object_embedding_documents.c.object_id == object_id,
                    object_embedding_documents.c.space_id == space.space_id,
                )
            )
            connection.execute(
                sa.delete(embedding_document_spaces).where(
                    embedding_document_spaces.c.document_id == document.document_id,
                    embedding_document_spaces.c.space_id == space.space_id,
                )
            )
            connection.execute(
                sa.delete(embedding_vectors).where(
                    embedding_vectors.c.space_id == space.space_id,
                    embedding_vectors.c.passage_id.in_(passage_ids),
                )
            )

    def write_embeddings(
        self,
        document: EmbeddingDocument,
        space: EmbeddingSpace,
        values: Sequence[tuple[Passage, Sequence[float]]],
    ) -> None:
        if space.dimensions > MAX_PGVECTOR_DIMENSIONS:
            raise ValueError(
                f"pgvector dense vectors support at most {MAX_PGVECTOR_DIMENSIONS} dimensions"
            )
        pairs = tuple(values)
        if not pairs:
            return
        passages = {passage.passage_id: passage for passage in document.passages}
        ordered = sorted(pairs, key=lambda pair: pair[0].passage_id.hex)
        vectors = validate_embedding_vectors(
            [vector for _passage, vector in ordered],
            expected_count=len(ordered),
            dimensions=space.dimensions,
            distance_metric=space.distance_metric,
        )
        now = _timestamp()
        rows: list[dict[str, object]] = []
        for (passage, _raw), vector in zip(ordered, vectors):
            if passages.get(passage.passage_id) != passage:
                raise ValueError("embedding passage does not belong to the document")
            rows.append(
                {
                    "space_id": space.space_id,
                    "passage_id": passage.passage_id,
                    "dimensions": space.dimensions,
                    "input_text_sha256": passage.text_sha256,
                    "embedding": vector,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        statement = postgresql.insert(embedding_vectors).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=[
                embedding_vectors.c.space_id,
                embedding_vectors.c.passage_id,
            ],
            set_={
                "dimensions": statement.excluded.dimensions,
                "input_text_sha256": statement.excluded.input_text_sha256,
                "embedding": statement.excluded.embedding,
                "updated_at": statement.excluded.updated_at,
            },
        )
        with self._transaction() as connection:
            self._lock_current_source(connection, document.source)
            self._lock_embedding_document(connection, document.document_id)
            connection.execute(statement)

    def complete_document_space(
        self,
        document: EmbeddingDocument,
        space: EmbeddingSpace,
    ) -> None:
        now = _timestamp()
        with self._transaction() as connection:
            object_id = self._lock_current_source(connection, document.source)
            self._lock_embedding_document(connection, document.document_id)
            count = connection.execute(
                sa.select(sa.func.count())
                .select_from(
                    embedding_passages.join(
                        embedding_vectors,
                        sa.and_(
                            embedding_vectors.c.passage_id == embedding_passages.c.passage_id,
                            embedding_vectors.c.space_id == space.space_id,
                        ),
                    )
                )
                .where(embedding_passages.c.document_id == document.document_id)
            ).scalar_one()
            if count != len(document.passages):
                raise EmbeddingIndexIncompleteError(
                    "embedding document has incomplete vectors for this model space"
                )
            connection.execute(
                postgresql.insert(embedding_document_spaces)
                .values(
                    document_id=document.document_id,
                    space_id=space.space_id,
                    completed_at=now,
                )
                .on_conflict_do_nothing()
            )
            mapping_insert = postgresql.insert(object_embedding_documents).values(
                object_id=object_id,
                space_id=space.space_id,
                document_id=document.document_id,
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                mapping_insert.on_conflict_do_update(
                    index_elements=[
                        object_embedding_documents.c.object_id,
                        object_embedding_documents.c.space_id,
                    ],
                    set_={
                        "document_id": mapping_insert.excluded.document_id,
                        "updated_at": mapping_insert.excluded.updated_at,
                    },
                )
            )

    @staticmethod
    def _literal_prefix(connection: Connection, column: Any, value: str) -> Any:
        parameter: Any = sa.bindparam(
            "embedding_key_prefix",
            value,
            type_=column.type,
        )
        return sa.func.substr(column, 1, sa.func.octet_length(parameter)) == parameter

    @staticmethod
    def _space_config(connection: Connection, space: EmbeddingSpace) -> RowMapping | None:
        return (
            connection.execute(
                sa.select(
                    embedding_spaces.c.fingerprint,
                    embedding_spaces.c.dimensions,
                    embedding_spaces.c.hnsw_enabled,
                    embedding_spaces.c.hnsw_ef_search,
                    embedding_spaces.c.hnsw_iterative_scan,
                    embedding_spaces.c.hnsw_index_name,
                    embedding_spaces.c.hnsw_m,
                    embedding_spaces.c.hnsw_ef_construction,
                ).where(embedding_spaces.c.space_id == space.space_id)
            )
            .mappings()
            .first()
        )

    def search(
        self,
        space: EmbeddingSpace,
        query_vector: Sequence[float],
        *,
        filters: SimilaritySearchFilters,
        limit: int,
        exact: bool = False,
    ) -> list[SimilaritySearchResult]:
        if space.dimensions > MAX_PGVECTOR_DIMENSIONS:
            raise ValueError(
                f"pgvector dense vectors support at most {MAX_PGVECTOR_DIMENSIONS} dimensions"
            )
        vector = validate_embedding_vectors(
            [query_vector],
            expected_count=1,
            dimensions=space.dimensions,
            distance_metric=space.distance_metric,
        )[0]
        if not isinstance(filters, SimilaritySearchFilters):
            raise ValueError("filters must be SimilaritySearchFilters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        if not isinstance(exact, bool):
            raise ValueError("exact must be a boolean")

        with self.catalog.engine.begin() as connection:
            _require_pgvector_capabilities(connection)
            config = self._space_config(connection, space)
            if config is None:
                return []
            if (
                config["fingerprint"] != space.fingerprint
                or config["dimensions"] != space.dimensions
            ):
                raise EmbeddingIdentityCollisionError(
                    "persisted search space does not match the requested model identity"
                )
            use_exact_search = exact or not bool(config["hnsw_enabled"])
            if use_exact_search:
                # Keep qualification baselines transaction-local and prevent the
                # space-specific HNSW expression index from participating. A
                # space with HNSW disabled is necessarily exact even when the
                # caller leaves ``exact`` at its default.
                connection.exec_driver_sql("SET LOCAL enable_indexscan = off")
                connection.exec_driver_sql("SET LOCAL enable_bitmapscan = off")
            else:
                connection.exec_driver_sql(
                    f"SET LOCAL hnsw.ef_search = {int(config['hnsw_ef_search'])}"
                )
                iterative_scan = str(config["hnsw_iterative_scan"])
                connection.exec_driver_sql(f"SET LOCAL hnsw.iterative_scan = '{iterative_scan}'")

            dimensions = space.dimensions
            stored_vector = sa.cast(embedding_vectors.c.embedding, PgVector(dimensions))
            query_parameter = sa.bindparam(
                "embedding_query_vector",
                vector,
                type_=PortableVector(),
            )
            query_value = sa.cast(query_parameter, PgVector(dimensions))
            distance = stored_vector.op("<=>")(query_value).label("cosine_distance")
            space_literal = sa.literal_column(
                f"'{space.space_id}'::uuid",
                type_=embedding_vectors.c.space_id.type,
            )
            dimension_literal = sa.literal_column(str(dimensions), type_=sa.Integer())

            source = (
                embedding_vectors.join(
                    embedding_passages,
                    embedding_passages.c.passage_id == embedding_vectors.c.passage_id,
                )
                .join(
                    embedding_documents,
                    embedding_documents.c.document_id == embedding_passages.c.document_id,
                )
                .join(
                    embedding_document_spaces,
                    sa.and_(
                        embedding_document_spaces.c.document_id
                        == embedding_documents.c.document_id,
                        embedding_document_spaces.c.space_id == embedding_vectors.c.space_id,
                    ),
                )
                .join(
                    object_embedding_documents,
                    sa.and_(
                        object_embedding_documents.c.document_id
                        == embedding_documents.c.document_id,
                        object_embedding_documents.c.space_id == embedding_vectors.c.space_id,
                    ),
                )
                .join(
                    objects,
                    objects.c.object_id == object_embedding_documents.c.object_id,
                )
                .join(
                    object_contents,
                    sa.and_(
                        object_contents.c.object_id == objects.c.object_id,
                        object_contents.c.manifest_id == embedding_documents.c.manifest_id,
                    ),
                )
                .join(
                    object_placements,
                    object_placements.c.object_id == objects.c.object_id,
                )
            )
            raw_metadata_text = sa.cast(objects.c.metadata, sa.Text())
            raw_metadata_bytes = sa.func.octet_length(raw_metadata_text)
            bounded_public_metadata = sa.case(
                (
                    raw_metadata_bytes <= MAX_SIMILARITY_RESULT_METADATA_BYTES,
                    objects.c.metadata,
                ),
                else_=sa.cast(sa.literal("{}"), sa.JSON()),
            ).label("metadata")
            public_metadata_truncated = (
                raw_metadata_bytes > MAX_SIMILARITY_RESULT_METADATA_BYTES
            ).label("metadata_truncated")
            statement = (
                sa.select(
                    embedding_vectors.c.space_id,
                    embedding_passages.c.passage_id,
                    embedding_documents.c.document_id,
                    objects.c.bucket,
                    objects.c.object_key,
                    object_placements.c.tier_name,
                    embedding_documents.c.source_mime,
                    embedding_passages.c.passage_index,
                    embedding_passages.c.start_codepoint,
                    embedding_passages.c.end_codepoint,
                    embedding_passages.c.text,
                    embedding_passages.c.text_sha256,
                    bounded_public_metadata,
                    public_metadata_truncated,
                    distance,
                )
                .select_from(source)
                .where(
                    embedding_vectors.c.space_id == space_literal,
                    embedding_vectors.c.dimensions == dimension_literal,
                )
            )
            if filters.buckets:
                statement = statement.where(objects.c.bucket.in_(sorted(filters.buckets)))
            if filters.key_prefix:
                statement = statement.where(
                    self._literal_prefix(
                        connection,
                        objects.c.object_key,
                        filters.key_prefix,
                    )
                )
            if filters.tiers:
                statement = statement.where(
                    object_placements.c.tier_name.in_(sorted(filters.tiers))
                )
            if filters.mime_types:
                statement = statement.where(
                    embedding_documents.c.source_mime.in_(sorted(filters.mime_types))
                )
            for key, value in filters.metadata.items():
                # PostgreSQL ``json`` can retain escaped NUL and lone Unicode
                # surrogates that ``->>`` cannot materialize. Remove valid
                # surrogate pairs before scanning for malformed escapes. The
                # parity-aware patterns distinguish actual JSON escapes from a
                # user's literal backslash-u text.
                metadata_without_valid_surrogate_pairs = sa.func.regexp_replace(
                    raw_metadata_text,
                    sa.literal(_JSON_VALID_SURROGATE_PAIR_PATTERN),
                    sa.literal(r"\1x"),
                    sa.literal("g"),
                )
                metadata_is_text_materializable = sa.not_(
                    sa.or_(
                        raw_metadata_text.op("~")(sa.literal(_JSON_NUL_ESCAPE_PATTERN)),
                        metadata_without_valid_surrogate_pairs.op("~")(
                            sa.literal(_JSON_SURROGATE_ESCAPE_PATTERN)
                        ),
                    )
                )
                statement = statement.where(
                    sa.case(
                        (
                            metadata_is_text_materializable,
                            objects.c.metadata[key].as_string() == value,
                        ),
                        else_=sa.false(),
                    )
                )

            candidate_order: tuple[Any, ...] = (distance,)
            if use_exact_search:
                candidate_order += (
                    objects.c.bucket,
                    objects.c.object_key,
                    embedding_passages.c.passage_index,
                    embedding_passages.c.passage_id,
                )
            candidates = (
                statement.order_by(*candidate_order)
                .limit(limit * _SEARCH_OVERFETCH_FACTOR)
                .subquery("embedding_candidates")
            )
            rows = (
                connection.execute(
                    sa.select(candidates)
                    .order_by(
                        candidates.c.cosine_distance,
                        candidates.c.bucket,
                        candidates.c.object_key,
                        candidates.c.passage_index,
                        candidates.c.passage_id,
                    )
                    .limit(limit)
                )
                .mappings()
                .all()
            )

        return [self._search_result(row) for row in rows]

    @staticmethod
    def _search_result(row: RowMapping) -> SimilaritySearchResult:
        metadata = deepcopy(dict(row["metadata"] or {}))
        for metadata_key in _SEARCH_RESULT_INTERNAL_METADATA_KEYS:
            metadata.pop(metadata_key, None)
        return SimilaritySearchResult(
            space_id=row["space_id"],
            passage_id=row["passage_id"],
            document_id=row["document_id"],
            bucket=row["bucket"],
            key=row["object_key"],
            tier=row["tier_name"],
            mime=row["source_mime"],
            passage_index=row["passage_index"],
            start_codepoint=row["start_codepoint"],
            end_codepoint=row["end_codepoint"],
            text=row["text"],
            text_sha256=row["text_sha256"],
            cosine_distance=float(row["cosine_distance"]),
            object_metadata=MappingProxyType(metadata),
            object_metadata_truncated=bool(row["metadata_truncated"]),
        )

    def index_metadata(self, space: EmbeddingSpace) -> dict[str, object]:
        """Return sanitized pgvector/index settings for qualification reports."""

        with self._connection() as connection:
            postgres_version = connection.exec_driver_sql("SHOW server_version").scalar_one()
            pgvector_version = _require_pgvector_capabilities(connection)
            config = self._space_config(connection, space)
            if config is None:
                raise KeyError(f"embedding space is not registered: {space.space_id}")
            index_name = config["hnsw_index_name"]
            index_size = 0
            index_definition = None
            if isinstance(index_name, str):
                index_size = int(
                    connection.execute(
                        sa.text("SELECT pg_relation_size(to_regclass(:name))"),
                        {"name": index_name},
                    ).scalar_one()
                    or 0
                )
                index_definition = connection.execute(
                    sa.text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE schemaname = current_schema() AND indexname = :name"
                    ),
                    {"name": index_name},
                ).scalar_one_or_none()
            vector_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(embedding_vectors)
                .where(embedding_vectors.c.space_id == space.space_id)
            ).scalar_one()
        return {
            "postgres_version": str(postgres_version),
            "pgvector_version": str(pgvector_version),
            "space_id": str(space.space_id),
            "dimensions": space.dimensions,
            "vector_count": int(vector_count),
            "hnsw": {
                "enabled": bool(config["hnsw_enabled"]),
                "m": int(config["hnsw_m"]),
                "ef_construction": int(config["hnsw_ef_construction"]),
                "ef_search": int(config["hnsw_ef_search"]),
                "iterative_scan": str(config["hnsw_iterative_scan"]),
                "index_name": index_name,
                "index_size_bytes": index_size,
                "index_definition": index_definition,
            },
        }
