"""Composition and deterministic loader for the content-search sample."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from cognistore.api.gateway import CogniStoreGateway
from cognistore.core.embedding_index import (
    EmbeddingIndexer,
    HnswIndexConfig,
)
from cognistore.core.embeddings import EmbeddingProvider
from cognistore.core.indexer import Indexer
from cognistore.core.scanner import scan_catalog
from cognistore.db import PgVectorEmbeddingStore, SQLCatalog
from cognistore.drivers.storage_driver import StorageDriver
from cognistore.jobs.protocols import JobQueue
from cognistore.search import (
    AnswerProvider,
    AskService,
    KeywordSearchService,
    TantivyKeywordIndex,
)

from .manifest import ContentSearchCorpus, SampleObject, read_sample_corpus
from .providers import (
    DeterministicFeatureHashEmbeddingProvider,
    ExtractiveSampleAnswerProvider,
)


class SampleLoadError(RuntimeError):
    """A checked sample object could not reach a searchable state."""


@dataclass(frozen=True)
class LoadedSampleObject:
    source: str
    key: str
    tier: str
    media_type: str
    size: int
    sha256: str
    document_id: str
    total_passages: int
    embedded_passages: int
    reused_passages: int


@dataclass(frozen=True)
class ContentSearchLoadReport:
    corpus_name: str
    bucket: str
    objects: tuple[LoadedSampleObject, ...]
    scan_observations: int
    keyword_objects_indexed: int
    keyword_passages_indexed: int
    embedding_space_id: str
    duplicate_content_groups: tuple[tuple[str, ...], ...]
    schema_version: int = 1

    @property
    def object_count(self) -> int:
        return len(self.objects)

    @property
    def embedded_passages(self) -> int:
        return sum(item.embedded_passages for item in self.objects)

    @property
    def reused_passages(self) -> int:
        return sum(item.reused_passages for item in self.objects)

    def to_dict(self) -> dict[str, object]:
        """Return a detached JSON-safe report for a CLI or setup endpoint."""

        return asdict(self)


class ContentSearchRuntime:
    """Own the sample's Tantivy writer and compose vector and Ask services.

    The caller owns the catalog and storage drivers.  Closing this runtime
    releases its host-local Tantivy writer lock but deliberately does not close
    those injected dependencies.
    """

    def __init__(
        self,
        catalog: SQLCatalog,
        drivers: Mapping[str, StorageDriver],
        *,
        keyword_index_path: str | Path,
        embedding_provider: EmbeddingProvider | None = None,
        answer_provider: AnswerProvider | None = None,
        hnsw: HnswIndexConfig | None = None,
    ) -> None:
        if not isinstance(catalog, SQLCatalog):
            raise TypeError("catalog must be a SQLCatalog")
        if not isinstance(drivers, Mapping) or any(
            not isinstance(name, str) or not isinstance(driver, StorageDriver)
            for name, driver in drivers.items()
        ):
            raise TypeError("drivers must map tier names to StorageDriver values")
        self.catalog = catalog
        self.drivers = dict(drivers)
        self.embedding_store = PgVectorEmbeddingStore(catalog)
        self.embedding_provider = (
            DeterministicFeatureHashEmbeddingProvider()
            if embedding_provider is None
            else embedding_provider
        )
        self.embedding_search = EmbeddingIndexer(
            self.embedding_store,
            self.embedding_provider,
            hnsw=hnsw,
        )
        self.keyword_index = TantivyKeywordIndex(keyword_index_path)
        self.keyword_search = KeywordSearchService(catalog, self.keyword_index)
        self.answer_provider = (
            ExtractiveSampleAnswerProvider()
            if answer_provider is None
            else answer_provider
        )
        self.ask_service = AskService(
            catalog,
            keyword=self.keyword_search,
            vector=self.embedding_search,
            answer_provider=self.answer_provider,
        )
        self._closed = False

    def __enter__(self) -> ContentSearchRuntime:
        if self._closed:
            raise RuntimeError("content-search runtime is closed")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.keyword_index.close()

    def create_gateway(
        self,
        *,
        queue: JobQueue | None = None,
        manage_queue: bool = False,
    ) -> CogniStoreGateway:
        if self._closed:
            raise RuntimeError("content-search runtime is closed")
        return CogniStoreGateway(
            self.catalog,
            self.drivers,
            ask_service=self.ask_service,
            queue=queue,
            manage_queue=manage_queue,
        )

    def load(
        self,
        corpus: ContentSearchCorpus | None = None,
        *,
        overwrite: bool = True,
        indexer: Indexer | None = None,
    ) -> ContentSearchLoadReport:
        if self._closed:
            raise RuntimeError("content-search runtime is closed")
        return load_sample_corpus(
            self,
            read_sample_corpus() if corpus is None else corpus,
            overwrite=overwrite,
            indexer=indexer,
        )


def _verified_record(
    runtime: ContentSearchRuntime,
    corpus: ContentSearchCorpus,
    item: SampleObject,
) -> None:
    record = runtime.catalog.get(corpus.manifest.bucket, item.key)
    if record is None:
        raise SampleLoadError(f"catalog scan did not publish sample object {item.key!r}")
    if record.tier != item.tier:
        raise SampleLoadError(f"sample object {item.key!r} has the wrong catalog tier")
    if record.size != len(corpus.payload_for(item)):
        raise SampleLoadError(f"sample object {item.key!r} has the wrong catalog size")
    if record.metadata.get("sha256") != item.sha256:
        raise SampleLoadError(f"sample object {item.key!r} has the wrong content digest")
    if record.metadata.get("mime") != item.media_type:
        raise SampleLoadError(f"sample object {item.key!r} has the wrong detected MIME type")
    extraction = record.metadata.get("document_extraction")
    if not isinstance(extraction, Mapping) or extraction.get("status") != "succeeded":
        raise SampleLoadError(f"sample object {item.key!r} was not extracted successfully")


def _duplicate_groups(objects: tuple[SampleObject, ...]) -> tuple[tuple[str, ...], ...]:
    by_digest: dict[str, list[str]] = defaultdict(list)
    for item in objects:
        by_digest[item.sha256].append(item.key)
    return tuple(
        tuple(sorted(keys))
        for _digest, keys in sorted(by_digest.items())
        if len(keys) > 1
    )


def load_sample_corpus(
    runtime: ContentSearchRuntime,
    corpus: ContentSearchCorpus,
    *,
    overwrite: bool = True,
    indexer: Indexer | None = None,
) -> ContentSearchLoadReport:
    """Write, extract, index, and embed every checked manifest object."""

    if not isinstance(runtime, ContentSearchRuntime):
        raise TypeError("runtime must be a ContentSearchRuntime")
    if runtime._closed:
        raise RuntimeError("content-search runtime is closed")
    if not isinstance(corpus, ContentSearchCorpus):
        raise TypeError("corpus must be a ContentSearchCorpus")
    if not isinstance(overwrite, bool):
        raise ValueError("overwrite must be a boolean")
    unknown_tiers = sorted(
        {item.tier for item in corpus.manifest.objects}.difference(runtime.drivers)
    )
    if unknown_tiers:
        raise SampleLoadError(
            "sample manifest references unknown tier(s): " + ", ".join(unknown_tiers)
        )

    bucket = corpus.manifest.bucket
    for item in corpus.manifest.objects:
        runtime.drivers[item.tier].put_object(
            bucket,
            item.key,
            corpus.payload_for(item),
            overwrite=overwrite,
        )

    scan_observations = 0
    scanned_coordinates: set[tuple[str, str]] = set()
    active_indexer = indexer or Indexer()
    for tier in sorted({item.tier for item in corpus.manifest.objects}):
        results = scan_catalog(
            tier=tier,
            bucket=bucket,
            driver=runtime.drivers[tier],
            catalog=runtime.catalog,
            indexer=active_indexer,
        )
        scan_observations += len(results)
        scanned_coordinates.update((result.tier, result.key) for result in results)

    missing = sorted(
        item.key
        for item in corpus.manifest.objects
        if (item.tier, item.key) not in scanned_coordinates
    )
    if missing:
        raise SampleLoadError(
            "catalog scan did not observe sample object(s): " + ", ".join(missing)
        )
    for item in corpus.manifest.objects:
        _verified_record(runtime, corpus, item)

    keyword_report = runtime.keyword_search.rebuild()
    loaded: list[LoadedSampleObject] = []
    for item in corpus.manifest.objects:
        embedding_report = runtime.embedding_search.index_object(bucket, item.key)
        loaded.append(
            LoadedSampleObject(
                source=item.source,
                key=item.key,
                tier=item.tier,
                media_type=item.media_type,
                size=len(corpus.payload_for(item)),
                sha256=item.sha256,
                document_id=str(embedding_report.document_id),
                total_passages=embedding_report.total_passages,
                embedded_passages=embedding_report.embedded_passages,
                reused_passages=embedding_report.reused_passages,
            )
        )

    return ContentSearchLoadReport(
        corpus_name=corpus.manifest.name,
        bucket=bucket,
        objects=tuple(loaded),
        scan_observations=scan_observations,
        keyword_objects_indexed=keyword_report.objects_indexed,
        keyword_passages_indexed=keyword_report.passages_indexed,
        embedding_space_id=str(runtime.embedding_provider.space.space_id),
        duplicate_content_groups=_duplicate_groups(corpus.manifest.objects),
    )


__all__ = [
    "ContentSearchLoadReport",
    "ContentSearchRuntime",
    "LoadedSampleObject",
    "SampleLoadError",
    "load_sample_corpus",
]
