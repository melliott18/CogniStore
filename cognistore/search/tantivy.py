from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, BinaryIO, TypeVar
from uuid import uuid4

from cognistore.observability import instrument

from .keyword import (
    DEFAULT_PASSAGE_CHARS,
    DEFAULT_PASSAGE_OVERLAP_CHARS,
    KEYWORD_INDEX_SCHEMA_VERSION,
    PASSAGE_CHUNKING_ALGORITHM,
    PASSAGE_CHUNKING_VERSION,
    KeywordObject,
    KeywordRebuildReport,
    KeywordSearchFilters,
    KeywordSearchHit,
    KeywordSearchQuery,
)

TANTIVY_ADAPTER_VERSION = 1
TANTIVY_ANALYZER = "cognistore-default-v1"
DEFAULT_WRITER_HEAP_BYTES = 50_000_000
MIN_WRITER_HEAP_BYTES = 15_000_000

_GENERATION_NAME = re.compile(r"^[0-9a-f]{32}$")
_T = TypeVar("_T")


class KeywordIndexCompatibilityError(RuntimeError):
    """An on-disk keyword index does not match the configured contract."""


class TantivyKeywordIndex:
    """Persistent, single-writer Tantivy implementation of the keyword adapter.

    Incremental mutations use one Tantivy commit followed by a reader reload.
    Rebuilds are written and verified in a fresh generation before the CURRENT
    pointer is atomically replaced, so an interrupted rebuild leaves the prior
    searchable generation intact.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        passage_max_chars: int = DEFAULT_PASSAGE_CHARS,
        passage_overlap_chars: int = DEFAULT_PASSAGE_OVERLAP_CHARS,
        writer_heap_bytes: int = DEFAULT_WRITER_HEAP_BYTES,
    ) -> None:
        try:
            import tantivy
        except ImportError as exc:  # pragma: no cover - packaging installs the dependency
            raise RuntimeError(
                "Tantivy keyword search requires the 'tantivy' package"
            ) from exc

        self._tantivy = tantivy
        self.path = Path(path)
        if self.path.exists() and not self.path.is_dir():
            raise ValueError("keyword index path must be a directory")
        self.path.mkdir(parents=True, exist_ok=True)
        self._generations_path = self.path / "generations"
        self._generations_path.mkdir(exist_ok=True)
        self._current_path = self.path / "CURRENT"
        self.passage_max_chars = self._positive_int(
            passage_max_chars, field="passage_max_chars"
        )
        self.passage_overlap_chars = self._nonnegative_int(
            passage_overlap_chars, field="passage_overlap_chars"
        )
        if self.passage_overlap_chars >= self.passage_max_chars:
            raise ValueError("passage_overlap_chars must be smaller than passage_max_chars")
        self.writer_heap_bytes = self._positive_int(
            writer_heap_bytes, field="writer_heap_bytes"
        )
        if self.writer_heap_bytes < MIN_WRITER_HEAP_BYTES:
            raise ValueError(
                f"writer_heap_bytes must be at least {MIN_WRITER_HEAP_BYTES}"
            )
        self._lock = threading.RLock()
        self._closed = False
        self._query_analyzer = self._text_analyzer()
        self._process_lock: BinaryIO | None = None

        self._acquire_process_lock()
        try:
            generation = self._read_current_generation()
            if generation is None:
                generation, report = self._build_generation(())
                assert report == KeywordRebuildReport(0, 0)
                self._publish_current(generation)
            self._generation = generation
            self._index = self._open_generation(generation)
            self._writer = self._new_writer(self._index)
        except BaseException:
            self._release_process_lock()
            raise

    def __enter__(self) -> TantivyKeywordIndex:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @staticmethod
    def _positive_int(value: object, *, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field} must be a positive integer")
        return value

    @staticmethod
    def _nonnegative_int(value: object, *, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field} must be a non-negative integer")
        return value

    def _acquire_process_lock(self) -> None:
        """Own the complete index root for this adapter's lifetime."""

        lock_path = self.path / ".writer.lock"
        handle = lock_path.open("a+b")
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows CI
                msvcrt: Any = importlib.import_module("msvcrt")

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise RuntimeError(
                "keyword index is already open by another writer"
            ) from exc
        self._process_lock = handle

    def _release_process_lock(self) -> None:
        handle = self._process_lock
        self._process_lock = None
        if handle is None:
            return
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows CI
                msvcrt: Any = importlib.import_module("msvcrt")

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    @staticmethod
    def _object_id(bucket: str, key: str) -> str:
        encoded = json.dumps(
            ["catalog-object", bucket, key],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _filter_token(kind: str, value: object) -> str:
        encoded = json.dumps(
            [kind, value],
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _searchable_text(value: str) -> str:
        """Replace non-scalar surrogate code points before crossing into Rust.

        Catalog coordinates preserve arbitrary POSIX filename bytes with
        ``surrogatepass``. PyO3 strings must contain Unicode scalar values, so
        opaque surrogate code points become token separators in analyzed text
        while their exact representation is retained in stored bytes fields.
        """

        return "".join(
            " " if 0xD800 <= ord(character) <= 0xDFFF else character
            for character in value
        )

    def _schema(self) -> Any:
        builder = self._tantivy.SchemaBuilder()
        builder.add_text_field(
            "object_id",
            stored=True,
            tokenizer_name="raw",
            index_option="basic",
        )
        builder.add_bytes_field("passage_id", stored=True)
        builder.add_bytes_field("bucket", stored=True)
        builder.add_text_field(
            "key",
            tokenizer_name=TANTIVY_ANALYZER,
        )
        builder.add_bytes_field("key_raw", stored=True)
        builder.add_bytes_field("tier", stored=True)
        builder.add_integer_field("size", stored=True)
        builder.add_bytes_field("mime", stored=True)
        builder.add_bytes_field("content_sha256", stored=True)
        builder.add_integer_field("passage_ordinal", stored=True)
        builder.add_integer_field("char_start", stored=True)
        builder.add_integer_field("char_end", stored=True)
        builder.add_text_field(
            "passage",
            stored=True,
            tokenizer_name=TANTIVY_ANALYZER,
        )
        builder.add_text_field("metadata_text", tokenizer_name=TANTIVY_ANALYZER)
        builder.add_bytes_field("document_metadata", stored=True)
        builder.add_text_field(
            "filters",
            tokenizer_name="raw",
            index_option="basic",
        )
        builder.add_text_field(
            "sort_key",
            fast=True,
            tokenizer_name="raw",
            index_option="basic",
        )
        return builder.build()

    def _text_analyzer(self) -> Any:
        return (
            self._tantivy.TextAnalyzerBuilder(self._tantivy.Tokenizer.simple())
            .filter(self._tantivy.Filter.remove_long(40))
            .filter(self._tantivy.Filter.lowercase())
            .build()
        )

    def _configure_index(self, index: Any) -> None:
        index.register_tokenizer(TANTIVY_ANALYZER, self._text_analyzer())
        index.config_reader("Manual")

    def _manifest(self) -> dict[str, object]:
        return {
            "schema_version": KEYWORD_INDEX_SCHEMA_VERSION,
            "adapter": "tantivy",
            "adapter_version": TANTIVY_ADAPTER_VERSION,
            "tantivy_version": str(getattr(self._tantivy, "__version__", "unknown")),
            "analyzer": TANTIVY_ANALYZER,
            "passage_chunking": {
                "algorithm": PASSAGE_CHUNKING_ALGORITHM,
                "version": PASSAGE_CHUNKING_VERSION,
                "max_chars": self.passage_max_chars,
                "overlap_chars": self.passage_overlap_chars,
            },
        }

    def _validate_manifest(self, manifest: object) -> None:
        if not isinstance(manifest, Mapping):
            raise KeywordIndexCompatibilityError("keyword index manifest is not an object")
        expected = self._manifest()
        for field in ("schema_version", "adapter", "adapter_version", "analyzer"):
            if manifest.get(field) != expected[field]:
                raise KeywordIndexCompatibilityError(
                    f"keyword index manifest has incompatible {field}"
                )
        if manifest.get("passage_chunking") != expected["passage_chunking"]:
            raise KeywordIndexCompatibilityError(
                "keyword index manifest has incompatible passage chunking"
            )

    def _read_current_generation(self) -> str | None:
        if not self._current_path.exists():
            return None
        try:
            generation = self._current_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise KeywordIndexCompatibilityError(
                "keyword index CURRENT pointer is unreadable"
            ) from exc
        if not _GENERATION_NAME.fullmatch(generation):
            raise KeywordIndexCompatibilityError(
                "keyword index CURRENT pointer is invalid"
            )
        if not (self._generations_path / generation).is_dir():
            raise KeywordIndexCompatibilityError(
                "keyword index CURRENT generation is missing"
            )
        return generation

    def _publish_current(self, generation: str) -> None:
        if not _GENERATION_NAME.fullmatch(generation):
            raise ValueError("invalid keyword index generation")
        descriptor, temporary = tempfile.mkstemp(
            prefix=".CURRENT.", dir=self.path
        )
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as output:
                output.write(generation + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._current_path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _write_manifest(self, generation_path: Path) -> None:
        manifest_path = generation_path / "manifest.json"
        with manifest_path.open("w", encoding="utf-8") as output:
            json.dump(
                self._manifest(),
                output,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())

    def _open_generation(self, generation: str) -> Any:
        generation_path = self._generations_path / generation
        try:
            manifest = json.loads(
                (generation_path / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise KeywordIndexCompatibilityError(
                "keyword index manifest is missing or unreadable"
            ) from exc
        self._validate_manifest(manifest)
        index_path = generation_path / "index"
        if not self._tantivy.Index.exists(str(index_path)):
            raise KeywordIndexCompatibilityError("Tantivy generation is missing its index")
        index = self._tantivy.Index.open(str(index_path))
        self._configure_index(index)
        index.reload()
        return index

    def _new_writer(self, index: Any) -> Any:
        return index.writer(
            heap_size=self.writer_heap_bytes,
            num_threads=1,
        )

    def _validate_document_contract(self, document: KeywordObject) -> None:
        if not isinstance(document, KeywordObject):
            raise ValueError("document must be a KeywordObject")
        if document.object_id != self._object_id(document.bucket, document.key):
            raise ValueError("document object_id does not match bucket and key")
        if (
            document.passage_chunking_algorithm != PASSAGE_CHUNKING_ALGORITHM
            or document.passage_chunking_version != PASSAGE_CHUNKING_VERSION
            or document.passage_max_chars != self.passage_max_chars
            or document.passage_overlap_chars != self.passage_overlap_chars
        ):
            raise ValueError("document passage contract does not match the index manifest")

    @staticmethod
    def _metadata_text(metadata: Mapping[str, object]) -> str:
        values: list[str] = []
        for name in sorted(metadata):
            value = metadata[name]
            if value is not None:
                values.append(str(value))
        return TantivyKeywordIndex._searchable_text(" ".join(values))

    def _document_filters(self, document: KeywordObject) -> list[str]:
        filters = [
            self._filter_token("bucket", document.bucket),
            self._filter_token("tier", document.tier),
            self._filter_token("size", document.size),
        ]
        if document.mime is not None:
            filters.append(self._filter_token("mime", document.mime))
        if document.content_sha256 is not None:
            filters.append(
                self._filter_token("content_sha256", document.content_sha256)
            )
        filters.extend(
            self._filter_token(f"document_metadata.{name}", value)
            for name, value in sorted(document.document_metadata.items())
        )
        return filters

    def _tantivy_document(self, document: KeywordObject, passage: Any) -> Any:
        indexed = self._tantivy.Document()
        indexed.add_text("object_id", document.object_id)
        indexed.add_bytes("passage_id", passage.passage_id.encode("ascii"))
        indexed.add_bytes(
            "bucket", document.bucket.encode("utf-8", errors="surrogatepass")
        )
        indexed.add_text("key", self._searchable_text(document.key))
        indexed.add_bytes(
            "key_raw", document.key.encode("utf-8", errors="surrogatepass")
        )
        indexed.add_bytes(
            "tier", document.tier.encode("utf-8", errors="surrogatepass")
        )
        indexed.add_integer("size", document.size)
        if document.mime is not None:
            indexed.add_bytes(
                "mime", document.mime.encode("utf-8", errors="surrogatepass")
            )
        if document.content_sha256 is not None:
            indexed.add_bytes(
                "content_sha256", document.content_sha256.encode("ascii")
            )
        indexed.add_integer("passage_ordinal", passage.ordinal)
        indexed.add_integer("char_start", passage.char_start)
        indexed.add_integer("char_end", passage.char_end)
        indexed.add_text("passage", passage.text)
        metadata_text = self._metadata_text(document.document_metadata)
        if metadata_text:
            indexed.add_text("metadata_text", metadata_text)
        metadata_json = json.dumps(
            document.document_metadata,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        indexed.add_bytes("document_metadata", metadata_json)
        for token in self._document_filters(document):
            indexed.add_text("filters", token)
        indexed.add_text(
            "sort_key",
            self._sort_key(document.bucket, document.key, passage.ordinal),
        )
        return indexed

    @staticmethod
    def _sort_key(bucket: str, key: str, passage_ordinal: int) -> str:
        return (
            bucket.encode("utf-8", errors="surrogatepass").hex()
            + "/"
            + key.encode("utf-8", errors="surrogatepass").hex()
            + f"/{passage_ordinal:020d}"
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("keyword index is closed")

    def _mutate(self, operation: Callable[[Any], _T]) -> _T:
        with self._lock:
            self._ensure_open()
            try:
                result = operation(self._writer)
                self._writer.commit()
            except BaseException:
                try:
                    self._writer.rollback()
                except BaseException:
                    pass
                raise
            self._index.reload()
            return result

    @instrument("index", "replace", backend="keyword")
    def replace_object(self, document: KeywordObject) -> int:
        self._validate_document_contract(document)
        indexed = tuple(
            self._tantivy_document(document, passage) for passage in document.passages
        )

        def replace(writer: Any) -> int:
            writer.delete_documents_by_term("object_id", document.object_id)
            for item in indexed:
                writer.add_document(item)
            return len(indexed)

        return self._mutate(replace)

    @instrument("index", "delete", backend="keyword")
    def delete_object(self, bucket: str, key: str) -> None:
        if not isinstance(bucket, str) or not bucket:
            raise ValueError("bucket must be a non-empty string")
        if not isinstance(key, str) or not key:
            raise ValueError("key must be a non-empty string")
        object_id = self._object_id(bucket, key)
        self._mutate(
            lambda writer: writer.delete_documents_by_term("object_id", object_id)
        )

    def _query_filter_tokens(self, filters: KeywordSearchFilters) -> list[str]:
        tokens: list[str] = []
        for name in ("bucket", "tier", "size", "mime", "content_sha256"):
            value = getattr(filters, name)
            if value is not None:
                tokens.append(self._filter_token(name, value))
        tokens.extend(
            self._filter_token(f"document_metadata.{name}", value)
            for name, value in sorted(filters.document_metadata.items())
        )
        return tokens

    def _tantivy_query(self, query: KeywordSearchQuery) -> Any:
        clauses: list[tuple[Any, Any]] = []
        if query.text.strip():
            tokens = self._query_analyzer.analyze(self._searchable_text(query.text))
            term_clauses: list[tuple[Any, Any]] = []
            for token in tokens:
                alternatives = []
                for field_name, boost in (
                    ("passage", 2.0),
                    ("key", 1.5),
                    ("metadata_text", 1.0),
                ):
                    field_query = self._tantivy.Query.term_query(
                        self._index.schema,
                        field_name,
                        token,
                    )
                    alternatives.append(
                        self._tantivy.Query.boost_query(field_query, boost)
                    )
                term_clauses.append(
                    (
                        self._tantivy.Occur.Should,
                        self._tantivy.Query.disjunction_max_query(alternatives),
                    )
                )
            text_query = (
                self._tantivy.Query.boolean_query(term_clauses)
                if term_clauses
                else self._tantivy.Query.empty_query()
            )
        else:
            text_query = self._tantivy.Query.all_query()
        clauses.append((self._tantivy.Occur.Must, text_query))
        for token in self._query_filter_tokens(query.filters):
            filter_query = self._tantivy.Query.term_query(
                self._index.schema,
                "filters",
                token,
                "basic",
            )
            clauses.append(
                (
                    self._tantivy.Occur.Must,
                    self._tantivy.Query.const_score_query(filter_query, 0.0),
                )
            )
        return self._tantivy.Query.boolean_query(clauses)

    @staticmethod
    def _stored_first(document: Any, field: str) -> Any:
        value = document.get_first(field)
        if value is None:
            raise RuntimeError(f"indexed document is missing stored field {field}")
        return value

    @classmethod
    def _stored_text(cls, document: Any, field: str) -> str:
        value = cls._stored_first(document, field)
        if not isinstance(value, str):
            raise RuntimeError(f"indexed field {field} is not text")
        return value

    @classmethod
    def _stored_bytes_text(
        cls, document: Any, field: str, *, optional: bool = False
    ) -> str | None:
        value = document.get_first(field)
        if value is None and optional:
            return None
        if not isinstance(value, bytes):
            raise RuntimeError(f"indexed field {field} is not bytes")
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"indexed field {field} is not valid UTF-8") from exc

    @classmethod
    def _stored_catalog_text(
        cls, document: Any, field: str, *, optional: bool = False
    ) -> str | None:
        value = document.get_first(field)
        if value is None and optional:
            return None
        if not isinstance(value, bytes):
            raise RuntimeError(f"indexed field {field} is not bytes")
        return value.decode("utf-8", errors="surrogatepass")

    @classmethod
    def _stored_int(cls, document: Any, field: str) -> int:
        value = cls._stored_first(document, field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(f"indexed field {field} is not an integer")
        return value

    def _hit(self, score: object, stored: Any) -> KeywordSearchHit:
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise RuntimeError("Tantivy returned a non-numeric search score")
        metadata_value = self._stored_bytes_text(stored, "document_metadata")
        assert metadata_value is not None
        try:
            metadata = json.loads(metadata_value)
        except json.JSONDecodeError as exc:
            raise RuntimeError("indexed document metadata is invalid") from exc
        if not isinstance(metadata, dict):
            raise RuntimeError("indexed document metadata is not an object")
        passage_id = self._stored_bytes_text(stored, "passage_id")
        bucket = self._stored_catalog_text(stored, "bucket")
        tier = self._stored_catalog_text(stored, "tier")
        assert passage_id is not None and bucket is not None and tier is not None
        object_id = self._stored_text(stored, "object_id")
        key = self._stored_catalog_text(stored, "key_raw")
        assert key is not None
        if object_id != self._object_id(bucket, key):
            raise RuntimeError("indexed object_id does not match bucket and key")
        return KeywordSearchHit(
            score=float(score),
            object_id=object_id,
            passage_id=passage_id,
            bucket=bucket,
            key=key,
            tier=tier,
            size=self._stored_int(stored, "size"),
            mime=self._stored_catalog_text(stored, "mime", optional=True),
            content_sha256=self._stored_bytes_text(
                stored, "content_sha256", optional=True
            ),
            passage_ordinal=self._stored_int(stored, "passage_ordinal"),
            char_start=self._stored_int(stored, "char_start"),
            char_end=self._stored_int(stored, "char_end"),
            text=self._stored_text(stored, "passage"),
            document_metadata=metadata,
        )

    @instrument("index", "search", backend="keyword")
    def search(self, query: KeywordSearchQuery) -> list[KeywordSearchHit]:
        if not isinstance(query, KeywordSearchQuery):
            raise ValueError("query must be a KeywordSearchQuery")
        with self._lock:
            self._ensure_open()
            parsed = self._tantivy_query(query)
            searcher = self._index.searcher()
            desired = query.offset + query.limit
            if not query.text.strip():
                result = searcher.search(
                    parsed,
                    limit=query.limit,
                    offset=query.offset,
                    count=False,
                    order_by_field="sort_key",
                    order=self._tantivy.Order.Asc,
                )
                return [
                    self._hit(0.0, searcher.doc(address))
                    for _sort_key, address in result.hits
                ]
            fetch_limit = min(searcher.num_docs, max(64, desired))
            hits: list[KeywordSearchHit] = []
            while fetch_limit:
                result = searcher.search(parsed, limit=fetch_limit, count=False)
                hits = sorted(
                    (
                        self._hit(score, searcher.doc(address))
                        for score, address in result.hits
                    ),
                    key=lambda hit: (
                        -hit.score,
                        hit.bucket,
                        hit.key,
                        hit.passage_ordinal,
                    ),
                )
                if len(hits) < fetch_limit or fetch_limit >= searcher.num_docs:
                    break
                if len(hits) >= desired and hits[-1].score < hits[desired - 1].score:
                    break
                fetch_limit = min(searcher.num_docs, fetch_limit * 2)
        return hits[query.offset:desired]

    def _build_generation(
        self, documents: Iterable[KeywordObject]
    ) -> tuple[str, KeywordRebuildReport]:
        generation = uuid4().hex
        generation_path = self._generations_path / generation
        generation_path.mkdir()
        index_path = generation_path / "index"
        index_path.mkdir()
        writer: Any | None = None
        try:
            index = self._tantivy.Index(
                self._schema(),
                path=str(index_path),
                reuse=False,
            )
            self._configure_index(index)
            writer = self._new_writer(index)
            object_count = 0
            passage_count = 0
            try:
                for document in documents:
                    self._validate_document_contract(document)
                    for passage in document.passages:
                        writer.add_document(self._tantivy_document(document, passage))
                        passage_count += 1
                    object_count += 1
                writer.commit()
            except BaseException:
                try:
                    writer.rollback()
                except BaseException:
                    pass
                raise
            finally:
                writer.wait_merging_threads()
                writer = None
            index.reload()
            if index.searcher().num_docs != passage_count:
                raise RuntimeError("rebuilt Tantivy generation failed document verification")
            self._write_manifest(generation_path)
            report = KeywordRebuildReport(object_count, passage_count)
            return generation, report
        except BaseException:
            if writer is not None:
                try:
                    writer.wait_merging_threads()
                except BaseException:
                    pass
            shutil.rmtree(generation_path, ignore_errors=True)
            raise

    @instrument("index", "rebuild", backend="keyword")
    def rebuild(self, documents: Iterable[KeywordObject]) -> KeywordRebuildReport:
        with self._lock:
            self._ensure_open()
            generation, report = self._build_generation(documents)
            try:
                index = self._open_generation(generation)
                new_writer = self._new_writer(index)
            except BaseException:
                shutil.rmtree(
                    self._generations_path / generation,
                    ignore_errors=True,
                )
                raise
            try:
                self._publish_current(generation)
            except BaseException:
                new_writer.wait_merging_threads()
                shutil.rmtree(self._generations_path / generation, ignore_errors=True)
                raise
            old_generation = self._generation
            old_writer = self._writer
            self._generation = generation
            self._index = index
            self._writer = new_writer
            old_writer.wait_merging_threads()
            # Once the new pointer and live writer are established, the old
            # generation is purely reconstructable derived data.
            shutil.rmtree(self._generations_path / old_generation, ignore_errors=True)
            return report

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._writer.wait_merging_threads()
            finally:
                self._release_process_lock()


__all__ = [
    "DEFAULT_WRITER_HEAP_BYTES",
    "KeywordIndexCompatibilityError",
    "MIN_WRITER_HEAP_BYTES",
    "TANTIVY_ADAPTER_VERSION",
    "TantivyKeywordIndex",
]
