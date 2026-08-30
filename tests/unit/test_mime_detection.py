from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

import pytest

from cognistore.core import mime_detection
from cognistore.core.indexer import Indexer
from cognistore.core.mime_detection import (
    LibmagicMimeDetector,
    MimeDetectionAdapter,
    MimeDetectorUnavailableError,
)
from cognistore.core.scanner import scan_catalog
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)


class _StaticDetector:
    name = "libmagic"

    def __init__(self, mime: object) -> None:
        self.mime = mime

    def detect(self, _data: bytes) -> object:
        return self.mime


class _FailingDetector:
    name = "libmagic"

    def detect(self, _data: bytes) -> object:
        raise RuntimeError("injected detector failure")


class _SelectiveDetector:
    name = "libmagic"

    def detect(self, data: bytes) -> object:
        if data == b"malformed":
            raise RuntimeError("injected malformed-object failure")
        if data == b"":
            return "application/x-empty"
        return "text/plain"


def _libmagic_adapter() -> MimeDetectionAdapter:
    try:
        detector = LibmagicMimeDetector()
    except MimeDetectorUnavailableError as exc:
        if os.environ.get("COGNISTORE_REQUIRE_LIBMAGIC") == "1":
            pytest.fail(f"required native libmagic could not be loaded: {exc}")
        pytest.skip("native libmagic is not installed")
    return MimeDetectionAdapter(detector)


@pytest.mark.parametrize(
    ("filename", "data", "expected_mime"),
    [
        ("fixture.bin", b"CogniStore plain text fixture\n", "text/plain"),
        ("fixture.txt", _PNG_1X1, "image/png"),
        ("fixture.txt", b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n", "application/pdf"),
    ],
    ids=("text", "png", "pdf"),
)
def test_real_libmagic_detects_representative_content(
    filename: str,
    data: bytes,
    expected_mime: str,
) -> None:
    detection = _libmagic_adapter().detect(data, filename=filename)

    assert detection.mime == expected_mime
    assert detection.detector == "libmagic"
    assert detection.provenance == "content"
    assert detection.content_mime == expected_mime
    assert detection.confidence == "high"


def test_real_libmagic_handles_empty_and_arbitrary_binary_content() -> None:
    adapter = _libmagic_adapter()

    empty = adapter.detect(b"", filename="empty.txt")
    binary = adapter.detect(b"\x00\xff" * 128, filename="payload.txt")

    assert empty.mime in {"application/x-empty", "inode/x-empty"}
    assert empty.provenance == "content"
    assert empty.confidence == "low"
    assert binary.mime == "application/octet-stream"
    assert binary.provenance == "content"
    assert binary.confidence == "low"


def test_content_result_wins_and_records_filename_disagreement() -> None:
    adapter = MimeDetectionAdapter(_StaticDetector("image/png"))

    detection = adapter.detect(_PNG_1X1, filename="misleading.txt")

    assert detection.mime == "image/png"
    assert detection.detector == "libmagic"
    assert detection.filename_mime == "text/plain"
    assert detection.disagreement is True
    assert detection.fallback_reason is None
    assert detection.to_metadata() == {
        "schema_version": 1,
        "mime": "image/png",
        "detector": "libmagic",
        "provenance": "content",
        "confidence": "high",
        "content_mime": "image/png",
        "filename_mime": "text/plain",
        "filename_encoding": None,
        "disagreement": True,
        "status": "detected",
        "fallback_reason": None,
    }


@pytest.mark.parametrize("failure", [ImportError("missing"), OSError("cannot load")])
def test_default_adapter_contains_libmagic_load_failures(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    def fail_import(_name: str) -> object:
        raise failure

    monkeypatch.setattr(
        "cognistore.core.mime_detection.importlib.import_module",
        fail_import,
    )

    detection = MimeDetectionAdapter.default().detect(b"data", filename="report.pdf")

    assert detection.mime == "application/pdf"
    assert detection.detector == "filename"
    assert detection.provenance == "filename"
    assert detection.confidence == "low"
    assert detection.status == "fallback"
    assert detection.fallback_reason == "libmagic_unavailable"


def test_default_adapter_contains_libmagic_initialization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenMagicModule:
        @staticmethod
        def Magic(*, mime: bool) -> object:
            assert mime is True
            raise OSError("native library failed to initialize")

    monkeypatch.setattr(
        "cognistore.core.mime_detection.importlib.import_module",
        lambda _name: BrokenMagicModule(),
    )

    detection = MimeDetectionAdapter.default().detect(b"data", filename=None)

    assert detection.mime is None
    assert detection.detector == "none"
    assert detection.fallback_reason == "libmagic_unavailable"


@pytest.mark.parametrize(
    ("key", "expected_mime"),
    [
        ("data:text/html,not-a-file", None),
        ("report.pdf?version=1", None),
        ("report.pdf#part", None),
        ("data:report.pdf", "application/pdf"),
        (r"windows:key\report.pdf", "application/pdf"),
    ],
)
def test_filename_fallback_treats_storage_keys_as_literal_paths(
    key: str,
    expected_mime: str | None,
) -> None:
    detection = MimeDetectionAdapter(None).detect(b"data", filename=key)

    assert detection.mime == expected_mime
    assert detection.filename_mime == expected_mime
    assert detection.fallback_reason == "content_detector_unavailable"


def test_filename_provenance_retains_compression_encoding() -> None:
    detection = MimeDetectionAdapter(_StaticDetector("application/gzip")).detect(
        b"compressed", filename="archive.tar.gz"
    )

    assert detection.mime == "application/gzip"
    assert detection.filename_mime == "application/x-tar"
    assert detection.filename_encoding == "gzip"
    assert detection.disagreement is True


def test_filename_database_failure_does_not_block_content_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_filename_inference(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected filename database failure")

    monkeypatch.setattr(
        mime_detection.mimetypes,
        "guess_file_type",
        fail_filename_inference,
        raising=False,
    )

    detection = MimeDetectionAdapter(_StaticDetector("text/plain")).detect(
        b"content", filename="object.txt"
    )

    assert detection.mime == "text/plain"
    assert detection.content_mime == "text/plain"
    assert detection.filename_mime is None
    assert detection.disagreement is False


def test_detector_error_and_invalid_result_fall_back_per_object() -> None:
    failed = MimeDetectionAdapter(_FailingDetector()).detect(
        b"malformed", filename="report.pdf"
    )
    invalid = MimeDetectionAdapter(_StaticDetector("not a mime type")).detect(
        b"data", filename=None
    )

    assert failed.mime == "application/pdf"
    assert failed.fallback_reason == "libmagic_error"
    assert failed.disagreement is False
    assert invalid.mime is None
    assert invalid.detector == "none"
    assert invalid.confidence == "none"
    assert invalid.status == "unclassified"
    assert invalid.fallback_reason == "libmagic_no_match"


def test_indexer_uses_bounded_sample_for_detection_and_returns_provenance() -> None:
    data = b"index this"
    observed: list[bytes] = []

    def record_sample(sample: bytes) -> object:
        observed.append(sample)
        return "text/plain"

    indexer = Indexer(
        sample_size=5,
        mime_detector=MimeDetectionAdapter(LibmagicMimeDetector(record_sample)),
    )

    result = indexer.index_bytes(data, filename="object.txt")

    assert observed == [b"index"]
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.sample == b"index"
    assert result.mime == "text/plain"
    assert result.mime_detection.filename_mime == "text/plain"
    assert result.mime_detection.disagreement is False


def test_scan_continues_after_object_detection_failure_and_persists_provenance(
    tmp_path: Path,
) -> None:
    driver = PosixDriver(str(tmp_path / "hot"))
    driver.put_object("bucket", "empty.unknown", b"")
    driver.put_object("bucket", "good.pdf", b"plain text")
    driver.put_object("bucket", "malformed.pdf", b"malformed")
    database = tmp_path / "catalog.sqlite3"
    detector = MimeDetectionAdapter(_SelectiveDetector())

    with SQLiteCatalog(database) as catalog:
        results = scan_catalog(
            tier="hot",
            bucket="bucket",
            driver=driver,
            catalog=catalog,
            indexer=Indexer(mime_detector=detector),
        )
        assert {result.key for result in results} == {
            "empty.unknown",
            "good.pdf",
            "malformed.pdf",
        }

    with SQLiteCatalog(database) as reopened:
        empty = reopened.get("bucket", "empty.unknown")
        good = reopened.get("bucket", "good.pdf")
        malformed = reopened.get("bucket", "malformed.pdf")

        assert empty is not None
        assert empty.metadata["mime"] == "application/x-empty"
        assert empty.metadata["mime_detection"] == {
            "schema_version": 1,
            "mime": "application/x-empty",
            "detector": "libmagic",
            "provenance": "content",
            "confidence": "low",
            "content_mime": "application/x-empty",
            "filename_mime": None,
            "filename_encoding": None,
            "disagreement": False,
            "status": "detected",
            "fallback_reason": None,
        }

        assert good is not None
        assert good.metadata["mime"] == "text/plain"
        assert good.metadata["mime_detection"]["status"] == "detected"
        assert good.metadata["mime_detection"]["filename_mime"] == "application/pdf"
        assert good.metadata["mime_detection"]["disagreement"] is True

        assert malformed is not None
        assert malformed.metadata["mime"] == "application/pdf"
        assert malformed.metadata["mime_detection"]["detector"] == "filename"
        assert malformed.metadata["mime_detection"]["fallback_reason"] == "libmagic_error"
