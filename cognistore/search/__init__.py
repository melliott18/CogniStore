"""Derived search indexes backed by the authoritative CogniStore catalog."""

from .keyword import (
    DEFAULT_PASSAGE_CHARS,
    DEFAULT_PASSAGE_OVERLAP_CHARS,
    KEYWORD_INDEX_SCHEMA_VERSION,
    PASSAGE_CHUNKING_VERSION,
    KeywordIndexAdapter,
    KeywordObject,
    KeywordPassage,
    KeywordRebuildReport,
    KeywordSearchFilters,
    KeywordSearchHit,
    KeywordSearchQuery,
    KeywordSearchService,
    NormalizedPassageChunker,
    project_catalog_record,
)
from .tantivy import KeywordIndexCompatibilityError, TantivyKeywordIndex

__all__ = [
    "DEFAULT_PASSAGE_CHARS",
    "DEFAULT_PASSAGE_OVERLAP_CHARS",
    "KEYWORD_INDEX_SCHEMA_VERSION",
    "PASSAGE_CHUNKING_VERSION",
    "KeywordIndexAdapter",
    "KeywordIndexCompatibilityError",
    "KeywordObject",
    "KeywordPassage",
    "KeywordRebuildReport",
    "KeywordSearchFilters",
    "KeywordSearchHit",
    "KeywordSearchQuery",
    "KeywordSearchService",
    "NormalizedPassageChunker",
    "TantivyKeywordIndex",
    "project_catalog_record",
]
