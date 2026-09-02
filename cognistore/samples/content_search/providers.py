"""Offline providers used by the content-search sample.

The embedding provider is intentionally a deterministic feature-hashing model,
not a substitute for a production semantic embedding model.  Keeping it local
lets a clean checkout exercise the complete pgvector path without credentials,
network access, or a model download.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Sequence

from cognistore.core.embeddings import EmbeddingSpace, EmbeddingVector
from cognistore.search import AnswerGenerationRequest, GeneratedAnswer

DEFAULT_SAMPLE_EMBEDDING_DIMENSIONS = 96
_TOKEN = re.compile(r"[\w]+", re.UNICODE)


class DeterministicFeatureHashEmbeddingProvider:
    """Map token and token-bigram features into a stable normalized vector."""

    def __init__(self, *, dimensions: int = DEFAULT_SAMPLE_EMBEDDING_DIMENSIONS) -> None:
        if isinstance(dimensions, bool) or not isinstance(dimensions, int):
            raise ValueError("dimensions must be an integer")
        if not 1 <= dimensions <= 2_000:
            raise ValueError("dimensions must be between 1 and 2000")
        self.space = EmbeddingSpace(
            provider_implementation="cognistore-sample-feature-hashing",
            provider_implementation_version="1",
            model="cognistore/sample-token-bigram-hash",
            model_revision="2026-09-01-content-search-sample-v1",
            dimensions=dimensions,
            preprocessing="unicode-nfc-casefold-token-bigram-sha256",
            preprocessing_version=1,
        )

    @staticmethod
    def _features(text: str) -> tuple[str, ...]:
        if not isinstance(text, str):
            raise ValueError("embedding input must be a string")
        normalized = unicodedata.normalize("NFC", text).casefold()
        tokens = tuple(_TOKEN.findall(normalized))
        bigrams = tuple(
            f"{left}\x1f{right}" for left, right in zip(tokens, tokens[1:])
        )
        # The bias keeps punctuation-only and empty inputs non-zero.  Public
        # query validation still rejects an empty Ask query before it gets here.
        return ("__bias__", *tokens, *bigrams)

    def _vector(self, text: str) -> EmbeddingVector:
        values = [0.0] * self.space.dimensions
        for feature in self._features(text):
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:8], "big") % self.space.dimensions
            sign = 1.0 if digest[8] & 1 else -1.0
            values[bucket] += sign
        norm = math.sqrt(sum(component * component for component in values))
        if norm == 0:
            # Signed collisions can cancel every feature, including the bias.
            # A stable unit vector keeps arbitrary valid UI queries searchable.
            values[0] = 1.0
            norm = 1.0
        return tuple(component / norm for component in values)

    def embed_documents(self, texts: Sequence[str]) -> tuple[EmbeddingVector, ...]:
        if isinstance(texts, (str, bytes, bytearray)):
            raise ValueError("document inputs must be a sequence of strings")
        return tuple(self._vector(text) for text in texts)

    def embed_query(self, text: str) -> EmbeddingVector:
        return self._vector(text)


class ExtractiveSampleAnswerProvider:
    """Return bounded source passages and their exact citation identifiers."""

    def __init__(self, *, max_passages: int = 2, max_characters: int = 4_000) -> None:
        if (
            isinstance(max_passages, bool)
            or not isinstance(max_passages, int)
            or max_passages < 1
        ):
            raise ValueError("max_passages must be a positive integer")
        if (
            isinstance(max_characters, bool)
            or not isinstance(max_characters, int)
            or max_characters < 1
        ):
            raise ValueError("max_characters must be a positive integer")
        self.max_passages = max_passages
        self.max_characters = max_characters

    def generate(self, request: AnswerGenerationRequest) -> GeneratedAnswer:
        if not isinstance(request, AnswerGenerationRequest):
            raise ValueError("request must be an AnswerGenerationRequest")

        excerpts: list[str] = []
        citations: list[str] = []
        seen_text: set[str] = set()
        remaining = self.max_characters
        for result in request.results:
            for passage in result.passages:
                text = passage.text.strip()
                if not text or text in seen_text:
                    continue
                if len(text) > remaining:
                    text = text[:remaining].rstrip()
                if not text:
                    break
                excerpts.append(text)
                citations.append(passage.citation.citation_id)
                seen_text.add(passage.text.strip())
                remaining -= len(text)
                if len(excerpts) >= self.max_passages or remaining == 0:
                    break
            if len(excerpts) >= self.max_passages or remaining == 0:
                break

        if excerpts:
            return GeneratedAnswer("\n\n".join(excerpts), tuple(citations))

        # Metadata-only retrieval has no passage to quote.  Cite the selected
        # object explicitly rather than inventing supporting text.
        result = request.results[0]
        return GeneratedAnswer(
            f"The top matching sample object is {result.citation.bucket}/{result.citation.key}.",
            (result.citation.citation_id,),
        )


__all__ = [
    "DEFAULT_SAMPLE_EMBEDDING_DIMENSIONS",
    "DeterministicFeatureHashEmbeddingProvider",
    "ExtractiveSampleAnswerProvider",
]
