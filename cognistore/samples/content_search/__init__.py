"""Runnable, offline content-search sample composition."""

from .manifest import (
    MAX_SAMPLE_CHECKSUM_MANIFEST_BYTES,
    MAX_SAMPLE_MANIFEST_BYTES,
    MAX_SAMPLE_OBJECT_BYTES,
    SAMPLE_MANIFEST_SCHEMA_VERSION,
    ContentSearchCorpus,
    ContentSearchManifest,
    SampleManifestError,
    SampleObject,
    SampleQuery,
    read_sample_corpus,
)
from .providers import (
    DEFAULT_SAMPLE_EMBEDDING_DIMENSIONS,
    DeterministicFeatureHashEmbeddingProvider,
    ExtractiveSampleAnswerProvider,
)
from .runtime import (
    ContentSearchLoadReport,
    ContentSearchRuntime,
    LoadedSampleObject,
    SampleLoadError,
    load_sample_corpus,
)

__all__ = [
    "DEFAULT_SAMPLE_EMBEDDING_DIMENSIONS",
    "MAX_SAMPLE_CHECKSUM_MANIFEST_BYTES",
    "MAX_SAMPLE_MANIFEST_BYTES",
    "MAX_SAMPLE_OBJECT_BYTES",
    "SAMPLE_MANIFEST_SCHEMA_VERSION",
    "ContentSearchCorpus",
    "ContentSearchLoadReport",
    "ContentSearchManifest",
    "ContentSearchRuntime",
    "DeterministicFeatureHashEmbeddingProvider",
    "ExtractiveSampleAnswerProvider",
    "LoadedSampleObject",
    "SampleLoadError",
    "SampleManifestError",
    "SampleObject",
    "SampleQuery",
    "load_sample_corpus",
    "read_sample_corpus",
]
