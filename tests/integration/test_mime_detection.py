from __future__ import annotations

from pathlib import Path

import pytest

from cognistore.core.indexer import Indexer
from cognistore.core.mime_detection import MimeDetectionAdapter
from cognistore.core.scanner import scan_catalog
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver

pytestmark = pytest.mark.integration


class _PngDetector:
    name = "libmagic"

    def detect(self, _data: bytes) -> object:
        return "image/png"


def test_postgres_scan_persists_mime_detection_provenance(
    postgres_dsn: str,
    tmp_path: Path,
) -> None:
    driver = PosixDriver(str(tmp_path / "hot"))
    driver.put_object("bucket", "misleading.txt", b"representative binary")

    with SQLCatalog(postgres_dsn) as catalog:
        results = scan_catalog(
            tier="hot",
            bucket="bucket",
            driver=driver,
            catalog=catalog,
            indexer=Indexer(
                mime_detector=MimeDetectionAdapter(_PngDetector()),
            ),
        )

        assert [result.key for result in results] == ["misleading.txt"]
        record = catalog.get("bucket", "misleading.txt")
        assert record is not None
        assert record.metadata["mime"] == "image/png"
        assert record.metadata["mime_detection"] == {
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
