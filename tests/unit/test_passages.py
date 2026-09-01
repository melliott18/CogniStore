from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import UUID

import pytest

from cognistore.core.document_extraction import ParserIdentity
from cognistore.core.passages import (
    DeterministicPassageChunker,
    NormalizedDocumentIdentity,
    Passage,
    PassageChunkerConfig,
)

SOURCE_SHA256 = hashlib.sha256(b"source bytes").hexdigest()
MANIFEST_ID = UUID("d133184e-9e0f-51f6-919d-a401f1593fe7")


def _document(**changes: object) -> NormalizedDocumentIdentity:
    values: dict[str, object] = {
        "manifest_id": MANIFEST_ID,
        "source_sha256": SOURCE_SHA256,
        "extraction_schema_version": 1,
        "source_mime": "application/pdf",
        "parser_name": "test-parser",
        "parser_implementation_version": "3",
        "parser_runtime_version": "7.1.2",
    }
    values.update(changes)
    return NormalizedDocumentIdentity(**values)  # type: ignore[arg-type]


def test_passages_use_unicode_codepoint_boundaries_not_source_bytes() -> None:
    chunker = DeterministicPassageChunker(
        PassageChunkerConfig(max_codepoints=2, overlap_codepoints=0)
    )

    passages = chunker.chunk("Aé🙂B", document=_document())

    assert [passage.text for passage in passages] == ["Aé", "🙂B"]
    assert [(passage.start_codepoint, passage.end_codepoint) for passage in passages] == [
        (0, 2),
        (2, 4),
    ]
    assert len(passages[0].text.encode("utf-8")) == 3
    assert len(passages[1].text.encode("utf-8")) == 5


def test_overlap_and_final_short_passage_are_deterministic() -> None:
    chunker = DeterministicPassageChunker(
        PassageChunkerConfig(max_codepoints=4, overlap_codepoints=1)
    )

    first = chunker.chunk("abcdefgh", document=_document())
    second = chunker.chunk("abcdefgh", document=_document())

    assert [(item.index, item.start_codepoint, item.end_codepoint, item.text) for item in first] == [
        (0, 0, 4, "abcd"),
        (1, 3, 7, "defg"),
        (2, 6, 8, "gh"),
    ]
    assert first == second
    assert [item.passage_id for item in first] == [item.passage_id for item in second]


def test_empty_normalized_text_has_no_passages() -> None:
    assert DeterministicPassageChunker().chunk("", document=_document()) == ()


def test_codepoint_boundaries_may_preserve_outer_passage_whitespace() -> None:
    passages = DeterministicPassageChunker(
        PassageChunkerConfig(max_codepoints=4, overlap_codepoints=0)
    ).chunk("abc def", document=_document())

    assert [passage.text for passage in passages] == ["abc ", "def"]


@pytest.mark.parametrize(
    "text",
    [
        "Cafe\u0301",
        "outer  spaces",
        "line\r\nbreak",
        "contains\x00control",
    ],
)
def test_chunker_rejects_text_outside_declared_normalization(text: str) -> None:
    with pytest.raises(ValueError, match="normalization contract"):
        DeterministicPassageChunker().chunk(text, document=_document())


def test_document_identity_is_stable_and_includes_complete_provenance() -> None:
    base = _document()

    assert base == _document()
    assert base.document_id == _document().document_id
    assert len(base.fingerprint) == 64
    assert base.to_metadata()["manifest_id"] == str(MANIFEST_ID)
    assert base.to_metadata()["source_sha256"] == SOURCE_SHA256
    assert base.to_metadata()["extraction_schema_version"] == 1
    assert base.to_metadata()["source_mime"] == "application/pdf"

    alternatives = [
        _document(manifest_id=UUID("3d01c8ac-20f8-56cf-8f71-2012363eb8dc")),
        _document(source_sha256=hashlib.sha256(b"other source").hexdigest()),
        _document(extraction_schema_version=2),
        _document(source_mime="application/vnd.example.document"),
        _document(parser_name="other-parser"),
        _document(parser_implementation_version="4"),
        _document(parser_runtime_version="7.1.3"),
    ]
    assert all(item.document_id != base.document_id for item in alternatives)


def test_from_parser_preserves_extraction_parser_identity() -> None:
    document = NormalizedDocumentIdentity.from_parser(
        manifest_id=MANIFEST_ID,
        source_sha256=SOURCE_SHA256,
        extraction_schema_version=1,
        source_mime="application/pdf",
        parser=ParserIdentity("pypdf", "1", "6.0.0"),
    )

    assert document.parser_name == "pypdf"
    assert document.parser_implementation_version == "1"
    assert document.parser_runtime_version == "6.0.0"


def test_passage_identity_changes_with_text_layout_or_chunker_provenance() -> None:
    document = _document()
    base = DeterministicPassageChunker(
        PassageChunkerConfig(max_codepoints=4, overlap_codepoints=0)
    ).chunk("abcdefgh", document=document)[0]
    overlapping = DeterministicPassageChunker(
        PassageChunkerConfig(max_codepoints=4, overlap_codepoints=1)
    ).chunk("abcdefgh", document=document)[0]
    different_text = DeterministicPassageChunker(
        PassageChunkerConfig(max_codepoints=4, overlap_codepoints=0)
    ).chunk("wxyzefgh", document=document)[0]

    assert base.passage_id != overlapping.passage_id
    assert base.passage_id != different_text.passage_id
    assert base.fingerprint != overlapping.fingerprint
    assert base.to_metadata()["document"]["parser"]["name"] == "test-parser"  # type: ignore[index]
    assert base.to_metadata()["chunker"] == {
        "algorithm": "fixed-codepoints",
        "version": 1,
        "max_codepoints": 4,
        "overlap_codepoints": 0,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"max_codepoints": 0},
        {"max_codepoints": True},
        {"overlap_codepoints": -1},
        {"max_codepoints": 4, "overlap_codepoints": 4},
        {"algorithm": "source-bytes"},
        {"version": 2},
        {"version": True},
    ],
)
def test_chunker_config_rejects_invalid_or_source_byte_contracts(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        PassageChunkerConfig(**changes)  # type: ignore[arg-type]


def test_passage_value_rejects_tampered_text_digest_or_offsets() -> None:
    passage = DeterministicPassageChunker(
        PassageChunkerConfig(max_codepoints=4, overlap_codepoints=0)
    ).chunk("valid", document=_document())[0]

    with pytest.raises(ValueError, match="does not match"):
        replace(passage, text_sha256="0" * 64)
    with pytest.raises(ValueError, match="offsets"):
        replace(passage, end_codepoint=3)


def test_invalid_source_and_parser_provenance_is_rejected() -> None:
    with pytest.raises(ValueError, match="source_sha256"):
        _document(source_sha256="not-a-digest")
    with pytest.raises(ValueError, match="parser_name"):
        _document(parser_name="")
    with pytest.raises(ValueError, match="manifest_id"):
        _document(manifest_id="not-a-uuid")
    with pytest.raises(ValueError, match="extraction_schema_version"):
        _document(extraction_schema_version=0)
    with pytest.raises(ValueError, match="source_mime"):
        _document(source_mime="")
    with pytest.raises(ValueError, match="normalization_version"):
        _document(normalization_version=True)


def test_passage_ids_change_when_normalized_document_provenance_changes() -> None:
    chunker = DeterministicPassageChunker(
        PassageChunkerConfig(max_codepoints=4, overlap_codepoints=0)
    )
    first = chunker.chunk("text", document=_document())[0]
    second = chunker.chunk(
        "text",
        document=_document(parser_runtime_version="next-runtime"),
    )[0]

    assert first.text_sha256 == second.text_sha256
    assert first.passage_id != second.passage_id
    assert first.document.document_id != second.document.document_id
    assert isinstance(first.passage_id, UUID)
    assert isinstance(first, Passage)
