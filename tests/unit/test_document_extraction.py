from __future__ import annotations

import logging
import math
import os
import sys
import threading
import time
from dataclasses import replace
from importlib.metadata import version
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter

import cognistore.core.document_extraction as document_extraction
from cognistore.core.document_extraction import (
    DOCX_MIME_TYPE,
    PDF_MIME_TYPE,
    DocumentExtractionPipeline,
    DocumentParseError,
    DocxParserAdapter,
    ExtractionLimits,
    ParsedDocument,
    ParserIdentity,
    PdfParserAdapter,
    normalize_document_text,
)
from cognistore.core.indexer import Indexer
from cognistore.core.mime_detection import MimeDetectionAdapter
from cognistore.core.scanner import scan_catalog
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "documents"
EXPECTED_TEXT = (
    "CogniStore Document Fixture\n"
    "This project-authored sample verifies deterministic text extraction.\n"
    "Alpha records stay searchable. Beta pages remain reproducible."
)
EXPECTED_COMMON_METADATA = {
    "title": "CogniStore Document Fixture",
    "author": "CogniStore Contributors",
    "subject": "Licensed deterministic extraction fixture",
    "keywords": "cognistore, extraction, fixture",
    "created_at": "2000-01-01T00:00:00Z",
    "modified_at": "2000-01-01T00:00:00Z",
}


class _StaticParser:
    identity = ParserIdentity("static", "7", "test-runtime")
    mime_types = frozenset({PDF_MIME_TYPE})

    def parse(self, data: bytes, *, mime: str) -> ParsedDocument:
        return ParsedDocument(text=data.decode("ascii"), metadata={})


class _SlowParser:
    identity = ParserIdentity("slow", "1", "test-runtime")
    mime_types = frozenset({PDF_MIME_TYPE})

    def parse(self, data: bytes, *, mime: str) -> ParsedDocument:
        time.sleep(5)
        return ParsedDocument(text="too late", metadata={})


def _restore_slow_bootstrap_parser() -> _StaticParser:
    time.sleep(5)
    return _StaticParser()


class _SlowBootstrapParser(_StaticParser):
    def __reduce__(self) -> tuple[object, tuple[()]]:
        return (_restore_slow_bootstrap_parser, ())


class _SlowBootstrapStateParser(_StaticParser):
    def __init__(self) -> None:
        self.payload = b"x" * (4 * 1024 * 1024)

    def __reduce__(
        self,
    ) -> tuple[object, tuple[()], dict[str, bytes]]:
        return (_restore_slow_bootstrap_parser, (), {"payload": self.payload})


class _NanMetadataParser:
    identity = ParserIdentity("non-finite", "1", "test-runtime")
    mime_types = frozenset({PDF_MIME_TYPE})

    def parse(self, data: bytes, *, mime: str) -> ParsedDocument:
        return ParsedDocument(text="valid", metadata={"invalid": math.nan})


class _CrashParser:
    identity = ParserIdentity("crash", "1", "test-runtime")
    mime_types = frozenset({PDF_MIME_TYPE})

    def parse(self, data: bytes, *, mime: str) -> ParsedDocument:
        os._exit(17)


class _NoisyParser(_StaticParser):
    def parse(self, data: bytes, *, mime: str) -> ParsedDocument:
        text = data.decode("ascii")
        print(text)
        print(text, file=sys.stderr)
        os.write(1, data)
        os.write(2, data)
        logging.getLogger("parser-test").error(text)
        raise RuntimeError(text)


def _fixture(name: str) -> bytes:
    return (FIXTURE_ROOT / name).read_bytes()


def _pipeline_for(
    parser: object,
    *,
    max_file_bytes: int = 1024,
    timeout_seconds: float = document_extraction.DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = 1024,
) -> DocumentExtractionPipeline:
    # Semantic assertions need the normal worker budget, including spawn imports.
    # Deadline tests pass their own short timeout explicitly.
    return DocumentExtractionPipeline(
        [parser],  # type: ignore[list-item]
        limits=ExtractionLimits(
            max_file_bytes=max_file_bytes,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        ),
    )


def test_normalization_v1_is_deterministic() -> None:
    decomposed = "Cafe\u0301\tvalue\r\n\r\n\r\nnext\x00  line  \r"

    assert normalize_document_text(decomposed) == "Café value\n\nnext line"


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_file_bytes", 0),
        ("max_file_bytes", sys.maxsize + 1),
        ("max_output_bytes", True),
        ("timeout_seconds", 0),
        ("timeout_seconds", math.inf),
        ("timeout_seconds", math.nan),
        ("timeout_seconds", math.nextafter(threading.TIMEOUT_MAX, math.inf)),
    ],
)
def test_extraction_limits_reject_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        ExtractionLimits(**{field: value})  # type: ignore[arg-type]


def test_pdf_fixture_yields_expected_text_and_metadata() -> None:
    parsed = PdfParserAdapter().parse(_fixture("sample.pdf"), mime=PDF_MIME_TYPE)

    assert normalize_document_text(parsed.text) == EXPECTED_TEXT
    assert parsed.metadata == {
        "format": "pdf",
        **EXPECTED_COMMON_METADATA,
        "language": None,
        "page_count": 1,
    }


def test_pdf_parser_diagnostics_do_not_log_sensitive_dictionary_keys(
    capfd: pytest.CaptureFixture[str],
) -> None:
    sensitive = "synthetic.person@example.test"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_metadata({f"/{sensitive}": "x"})
    output = BytesIO()
    writer.write(output)
    original = f"/{sensitive} (x)".encode("ascii")
    repeated = f"/{sensitive} (x) /{sensitive} (y)".encode("ascii")
    data = output.getvalue()
    assert original in data
    # pypdf recovers this malformed dictionary while logging its duplicated
    # key verbatim unless the isolated parser worker suppresses diagnostics.
    malformed = data.replace(original, repeated)

    result = DocumentExtractionPipeline().extract(malformed, mime=PDF_MIME_TYPE)

    assert result.status == "succeeded"
    captured = capfd.readouterr()
    assert sensitive not in captured.out + captured.err


def test_parser_failure_suppresses_logging_python_and_native_output(
    capfd: pytest.CaptureFixture[str],
) -> None:
    sensitive = "synthetic.person@example.test"

    result = _pipeline_for(_NoisyParser()).extract(
        sensitive.encode("ascii"), mime=PDF_MIME_TYPE,
    )

    assert result.status == "failed"
    assert result.failure_code == "parser_error"
    captured = capfd.readouterr()
    assert sensitive not in captured.out + captured.err


def test_docx_fixture_yields_expected_text_and_metadata() -> None:
    parsed = DocxParserAdapter().parse(_fixture("sample.docx"), mime=DOCX_MIME_TYPE)

    assert normalize_document_text(parsed.text) == EXPECTED_TEXT
    assert parsed.metadata == {
        "format": "docx",
        **EXPECTED_COMMON_METADATA,
        "language": "en-US",
        "paragraph_count": 3,
        "table_count": 0,
    }


@pytest.mark.parametrize(
    "adapter,mime,name,code",
    [
        (PdfParserAdapter(), PDF_MIME_TYPE, "encrypted.pdf", "encrypted"),
        (PdfParserAdapter(), PDF_MIME_TYPE, "corrupt.pdf", "corrupt"),
        (DocxParserAdapter(), DOCX_MIME_TYPE, "corrupt.docx", "corrupt"),
    ],
)
def test_parser_adapters_classify_unsafe_documents(
    adapter: object,
    mime: str,
    name: str,
    code: str,
) -> None:
    with pytest.raises(DocumentParseError) as raised:
        adapter.parse(_fixture(name), mime=mime)  # type: ignore[attr-defined]

    assert raised.value.code == code


def test_docx_adapter_rejects_encrypted_ooxml_container() -> None:
    encrypted_ole = bytes.fromhex("D0CF11E0A1B11AE1") + b"encrypted package"

    with pytest.raises(DocumentParseError) as raised:
        DocxParserAdapter().parse(encrypted_ole, mime=DOCX_MIME_TYPE)

    assert raised.value.code == "encrypted"


def test_pipeline_result_is_idempotent_and_versions_the_runtime() -> None:
    data = _fixture("sample.pdf")
    pipeline = DocumentExtractionPipeline()

    first = pipeline.extract(data, mime=PDF_MIME_TYPE)
    second = pipeline.extract(data, mime=PDF_MIME_TYPE)

    assert first == second
    assert first.status == "succeeded"
    assert first.text == EXPECTED_TEXT
    assert first.failure_code is None
    assert first.parser == ParserIdentity("pypdf", "1", version("pypdf"))
    assert first.text_bytes == len(EXPECTED_TEXT.encode("utf-8"))
    assert first.output_bytes > first.text_bytes
    assert first.to_metadata()["schema_version"] == 1
    assert first.to_metadata()["normalization_version"] == 1


def test_unsupported_mime_is_a_stable_non_raising_failure() -> None:
    result = DocumentExtractionPipeline().extract(
        _fixture("unsupported.txt"),
        mime="text/plain",
    )

    assert result.status == "failed"
    assert result.parser is None
    assert result.failure_code == "unsupported_mime"
    assert result.text is None
    assert result.document_metadata is None


def test_file_limit_accepts_exact_boundary_and_rejects_one_extra_byte() -> None:
    pipeline = _pipeline_for(_StaticParser(), max_file_bytes=4)

    exact = pipeline.extract(b"1234", mime=PDF_MIME_TYPE)
    oversized = pipeline.extract(b"12345", mime=PDF_MIME_TYPE)

    assert exact.status == "succeeded"
    assert exact.text == "1234"
    assert oversized.status == "failed"
    assert oversized.failure_code == "input_too_large"


def test_supported_partial_input_is_not_parsed() -> None:
    result = _pipeline_for(_StaticParser()).extract(
        b"part",
        mime=PDF_MIME_TYPE,
        source_size=5,
    )

    assert result.status == "failed"
    assert result.failure_code == "worker_error"


def test_output_limit_accepts_exact_boundary_and_rejects_one_byte_less() -> None:
    # The canonical output is three text bytes plus the two-byte JSON object {}.
    exact = _pipeline_for(_StaticParser(), max_output_bytes=5).extract(
        b"abc", mime=PDF_MIME_TYPE
    )
    too_small = _pipeline_for(_StaticParser(), max_output_bytes=4).extract(
        b"abc", mime=PDF_MIME_TYPE
    )

    assert exact.status == "succeeded"
    assert exact.output_bytes == 5
    assert too_small.status == "failed"
    assert too_small.failure_code == "output_too_large"


def test_timeout_stops_the_isolated_parser() -> None:
    pipeline = _pipeline_for(_SlowParser(), timeout_seconds=0.05)
    started = time.monotonic()

    result = pipeline.extract(b"input", mime=PDF_MIME_TYPE)

    assert result.status == "failed"
    assert result.failure_code == "timeout"
    assert time.monotonic() - started < 2


def test_timeout_includes_spawn_bootstrap_without_serializing_document_bytes() -> None:
    data = b"a" * (4 * 1024 * 1024)
    pipeline = _pipeline_for(
        _SlowBootstrapParser(),
        max_file_bytes=len(data),
        timeout_seconds=0.05,
    )
    started = time.monotonic()

    result = pipeline.extract(data, mime=PDF_MIME_TYPE)

    assert result.status == "failed"
    assert result.failure_code == "timeout"
    assert time.monotonic() - started < 2


def test_timeout_includes_bootstrap_with_large_adapter_state() -> None:
    pipeline = _pipeline_for(
        _SlowBootstrapStateParser(),
        timeout_seconds=0.05,
    )
    started = time.monotonic()

    result = pipeline.extract(b"input", mime=PDF_MIME_TYPE)

    assert result.status == "failed"
    assert result.failure_code == "timeout"
    assert time.monotonic() - started < 2


def test_timeout_bounds_a_stalled_result_receive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_receive = threading.Event()

    def stalled_receive(*_args: object) -> None:
        release_receive.wait(timeout=5)

    monkeypatch.setattr(
        document_extraction,
        "_receive_worker_outcome",
        stalled_receive,
    )
    started = time.monotonic()
    try:
        result = _pipeline_for(
            _StaticParser(), timeout_seconds=0.05
        ).extract(b"input", mime=PDF_MIME_TYPE)
    finally:
        release_receive.set()

    assert result.status == "failed"
    assert result.failure_code == "timeout"
    assert time.monotonic() - started < 2


def test_timeout_includes_receiver_thread_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_start = threading.Thread.start

    def delayed_start(thread: threading.Thread) -> None:
        original_start(thread)
        if thread.name == "cognistore-document-parser-result":
            time.sleep(0.1)

    monkeypatch.setattr(threading.Thread, "start", delayed_start)

    result = _pipeline_for(
        _StaticParser(), timeout_seconds=0.05
    ).extract(b"input", mime=PDF_MIME_TYPE)

    assert result.status == "failed"
    assert result.failure_code == "timeout"


def test_receiver_thread_start_failure_is_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_start = threading.Thread.start

    def rejected_start(thread: threading.Thread) -> None:
        if thread.name == "cognistore-document-parser-result":
            raise RuntimeError("synthetic thread startup failure")
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", rejected_start)

    result = _pipeline_for(_StaticParser()).extract(
        b"input", mime=PDF_MIME_TYPE
    )

    assert result.status == "failed"
    assert result.failure_code == "worker_error"


def test_non_finite_metadata_is_rejected_as_parser_error() -> None:
    result = _pipeline_for(_NanMetadataParser()).extract(
        b"input", mime=PDF_MIME_TYPE
    )

    assert result.status == "failed"
    assert result.failure_code == "parser_error"


def test_abnormal_worker_exit_is_recorded() -> None:
    result = _pipeline_for(_CrashParser()).extract(b"input", mime=PDF_MIME_TYPE)

    assert result.status == "failed"
    assert result.failure_code == "worker_error"


def test_duplicate_parser_registration_is_rejected() -> None:
    with pytest.raises(ValueError, match="multiple document parsers"):
        DocumentExtractionPipeline([_StaticParser(), _StaticParser()])


def test_indexer_does_not_load_oversized_or_unsupported_objects() -> None:
    calls: list[None] = []

    def unexpected_load() -> bytes:
        calls.append(None)
        raise AssertionError("document loader should not run")

    oversized = Indexer(
        mime_detector=MimeDetectionAdapter(None),
        document_extractor=_pipeline_for(_StaticParser(), max_file_bytes=4),
    ).index_bytes(
        b"%PDF sample",
        filename="large.pdf",
        source_size=5,
        document_loader=unexpected_load,
    )
    unsupported = Indexer(
        mime_detector=MimeDetectionAdapter(None),
        document_extractor=_pipeline_for(_StaticParser()),
    ).index_bytes(
        b"plain text",
        filename="notes.txt",
        document_loader=unexpected_load,
    )

    assert calls == []
    assert oversized.document_extraction.failure_code == "input_too_large"
    assert unsupported.document_extraction.failure_code == "unsupported_mime"


def test_scan_streams_a_complete_document_larger_than_the_mime_sample(
    tmp_path: Path,
) -> None:
    data = b"a" * (1024 * 1024 + 17)
    driver = PosixDriver(str(tmp_path / "hot"))
    driver.put_object("documents", "large.pdf", data)
    pipeline = _pipeline_for(
        _StaticParser(),
        max_file_bytes=len(data),
        max_output_bytes=len(data) + 2,
    )
    indexer = Indexer(
        mime_detector=MimeDetectionAdapter(None),
        document_extractor=pipeline,
    )

    with SQLiteCatalog(tmp_path / "catalog.sqlite3") as catalog:
        results = scan_catalog(
            tier="hot",
            bucket="documents",
            driver=driver,
            catalog=catalog,
            indexer=indexer,
        )
        record = catalog.get("documents", "large.pdf")

    assert len(results) == 1
    assert record is not None
    extraction = record.metadata["document_extraction"]
    assert extraction["status"] == "succeeded"
    assert extraction["text_bytes"] == len(data)
    assert extraction["output_bytes"] == len(data) + 2


def test_scan_persists_mixed_outcomes_and_continues_after_failures(
    tmp_path: Path,
) -> None:
    driver = PosixDriver(str(tmp_path / "hot"))
    names = (
        "sample.pdf",
        "sample.docx",
        "encrypted.pdf",
        "corrupt.pdf",
        "corrupt.docx",
        "unsupported.txt",
    )
    for name in names:
        driver.put_object("documents", name, _fixture(name))
    indexer = Indexer(mime_detector=MimeDetectionAdapter(None))
    database = tmp_path / "catalog.sqlite3"

    with SQLiteCatalog(database) as catalog:
        results = scan_catalog(
            tier="hot",
            bucket="documents",
            driver=driver,
            catalog=catalog,
            indexer=indexer,
        )
        first_metadata = {
            name: catalog.get("documents", name).metadata  # type: ignore[union-attr]
            for name in names
        }

        assert {result.key for result in results} == set(names)
        assert first_metadata["sample.pdf"]["document_extraction"]["text"] == EXPECTED_TEXT
        assert first_metadata["sample.docx"]["document_extraction"]["status"] == "succeeded"
        assert first_metadata["encrypted.pdf"]["document_extraction"]["failure_code"] == (
            "encrypted"
        )
        assert first_metadata["corrupt.pdf"]["document_extraction"]["failure_code"] == (
            "corrupt"
        )
        assert first_metadata["corrupt.docx"]["document_extraction"]["failure_code"] == (
            "corrupt"
        )
        assert first_metadata["unsupported.txt"]["document_extraction"]["failure_code"] == (
            "unsupported_mime"
        )

        scan_catalog(
            tier="hot",
            bucket="documents",
            driver=driver,
            catalog=catalog,
            indexer=indexer,
        )
        second_metadata = {
            name: catalog.get("documents", name).metadata  # type: ignore[union-attr]
            for name in names
        }
        assert second_metadata == first_metadata

    with SQLiteCatalog(database) as reopened:
        persisted_metadata = {
            name: reopened.get("documents", name).metadata  # type: ignore[union-attr]
            for name in names
        }
    assert persisted_metadata == first_metadata


def test_metadata_snapshot_is_detached_from_result() -> None:
    result = DocumentExtractionPipeline().extract(
        _fixture("sample.docx"), mime=DOCX_MIME_TYPE
    )
    metadata = result.to_metadata()
    parser = metadata["parser"]
    assert isinstance(parser, dict)
    parser["name"] = "mutated"

    assert result.parser is not None
    assert result.parser.name == "python-docx"
    assert replace(result).to_metadata()["parser"]["name"] == "python-docx"  # type: ignore[index]

    nested_result = replace(
        result,
        document_metadata={"nested": {"value": "original"}},
    )
    nested_snapshot = nested_result.to_metadata()["document_metadata"]
    assert isinstance(nested_snapshot, dict)
    nested_value = nested_snapshot["nested"]
    assert isinstance(nested_value, dict)
    nested_value["value"] = "mutated"
    assert nested_result.to_metadata()["document_metadata"] == {
        "nested": {"value": "original"}
    }
