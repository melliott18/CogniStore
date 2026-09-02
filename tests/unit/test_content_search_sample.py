from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pytest

from cognistore.core.catalog import Catalog
from cognistore.samples.content_search import (
    ContentSearchManifest,
    DeterministicFeatureHashEmbeddingProvider,
    ExtractiveSampleAnswerProvider,
    SampleManifestError,
    read_sample_corpus,
)
from cognistore.search import AnswerGenerationRequest, AskQuery, AskService


def _write_corpus(directory: Path, *, manifest: dict[str, Any] | None = None) -> Path:
    first = b"first project-authored document"
    second = b"second project-authored document"
    (directory / "first.pdf").write_bytes(first)
    (directory / "second.docx").write_bytes(second)
    document = manifest or {
        "schema_version": 1,
        "name": "unit-content-search-corpus",
        "license": "MIT",
        "bucket": "sample",
        "objects": [
            {
                "source": "first.pdf",
                "key": "one.pdf",
                "tier": "warm",
                "media_type": "application/pdf",
                "sha256": hashlib.sha256(first).hexdigest(),
            },
            {
                "source": "second.docx",
                "key": "two.docx",
                "tier": "hot",
                "media_type": (
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                ),
                "sha256": hashlib.sha256(second).hexdigest(),
            },
            {
                "source": "second.docx",
                "key": "copy.docx",
                "tier": "cold",
                "media_type": (
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                ),
                "sha256": hashlib.sha256(second).hexdigest(),
            },
        ],
        "queries": {
            name: {
                "text": f"{name} query",
                "filters": {"bucket": "sample"},
                "expected_keys": ["one.pdf"],
            }
            for name in ("keyword", "vector", "ask")
        },
    }
    manifest_payload = (
        json.dumps(document, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    ).encode("ascii")
    manifest_path = directory / "manifest.json"
    manifest_path.write_bytes(manifest_payload)
    checksums = {
        "first.pdf": hashlib.sha256(first).hexdigest(),
        "manifest.json": hashlib.sha256(manifest_payload).hexdigest(),
        "second.docx": hashlib.sha256(second).hexdigest(),
    }
    (directory / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items())),
        encoding="ascii",
    )
    return manifest_path


def _dot(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return sum(a * b for a, b in zip(left, right))


def test_offline_embedding_provider_is_normalized_versioned_and_deterministic() -> None:
    provider = DeterministicFeatureHashEmbeddingProvider(dimensions=96)
    query = provider.embed_query("ISOLATED restore backup")
    replay = provider.embed_query("isolated RESTORE BACKUP")
    related = provider.embed_query("An isolated restore verifies a backup")
    unrelated = provider.embed_query("Legal counsel releases a retention hold")

    assert query == replay
    assert len(query) == 96
    assert math.sqrt(_dot(query, query)) == pytest.approx(1.0)
    assert _dot(query, related) > _dot(query, unrelated)
    assert provider.space.model_revision == "2026-09-01-content-search-sample-v1"
    assert provider.space == DeterministicFeatureHashEmbeddingProvider().space

    collision_fallback = provider.embed_query("t60")
    assert collision_fallback == (1.0, *([0.0] * 95))


@pytest.mark.parametrize("dimensions", [True, 0, 2_001])
def test_offline_embedding_provider_rejects_invalid_dimensions(dimensions: object) -> None:
    with pytest.raises(ValueError, match="dimensions"):
        DeterministicFeatureHashEmbeddingProvider(dimensions=dimensions)  # type: ignore[arg-type]


def test_offline_embedding_provider_rejects_a_scalar_document_batch() -> None:
    provider = DeterministicFeatureHashEmbeddingProvider()

    with pytest.raises(ValueError, match="sequence of strings"):
        provider.embed_documents("one document")


def test_extractive_provider_cites_metadata_only_fallback() -> None:
    catalog = Catalog()
    catalog.upsert(
        "sample",
        "restore-guide.pdf",
        size=10,
        tier="warm",
        metadata={"title": "restore guide"},
    )
    response = AskService(catalog).ask(AskQuery("restore"))
    request = AnswerGenerationRequest("restore", response.mode, response.results)

    answer = ExtractiveSampleAnswerProvider().generate(request)

    assert answer.text == "The top matching sample object is sample/restore-guide.pdf."
    assert answer.citations == (response.results[0].citation.citation_id,)


def test_packaged_manifest_is_licensed_checked_and_models_one_duplicate() -> None:
    corpus = read_sample_corpus()

    assert isinstance(corpus.manifest, ContentSearchManifest)
    assert corpus.manifest.name == "cognistore-content-search-sample-v1"
    assert corpus.manifest.license == "MIT"
    assert {item.media_type for item in corpus.manifest.objects} == {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    duplicate_sources = [
        item for item in corpus.manifest.objects if item.source == "database-backups.docx"
    ]
    assert len(duplicate_sources) == 2
    assert len({item.sha256 for item in duplicate_sources}) == 1
    assert corpus.payload_for(duplicate_sources[0]) == corpus.payload_for(
        duplicate_sources[1]
    )


def test_explicit_manifest_loads_duplicate_sources_once_and_checks_integrity(
    tmp_path: Path,
) -> None:
    manifest_path = _write_corpus(tmp_path)
    corpus = read_sample_corpus(manifest_path)

    assert len(corpus.manifest.objects) == 3
    assert set(corpus.payloads) == {"first.pdf", "second.docx"}
    assert corpus.object_for_key("copy.docx").source == "second.docx"

    (tmp_path / "second.docx").write_bytes(b"tampered")
    with pytest.raises(SampleManifestError, match="SHA256SUMS"):
        read_sample_corpus(manifest_path)


def test_manifest_rejects_unknown_fields_even_with_a_matching_checksum(
    tmp_path: Path,
) -> None:
    manifest_path = _write_corpus(tmp_path)
    document = json.loads(manifest_path.read_text(encoding="ascii"))
    document["unexpected"] = True
    _write_corpus(tmp_path, manifest=document)

    with pytest.raises(SampleManifestError, match="unknown field"):
        read_sample_corpus(manifest_path)


def test_manifest_rejects_float_schema_version_with_a_matching_checksum(
    tmp_path: Path,
) -> None:
    manifest_path = _write_corpus(tmp_path)
    document = json.loads(manifest_path.read_text(encoding="ascii"))
    document["schema_version"] = 1.0
    _write_corpus(tmp_path, manifest=document)

    with pytest.raises(SampleManifestError, match="schema_version"):
        read_sample_corpus(manifest_path)
