from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document

from cognistore.auth.tenancy import TenantIsolationError, tenant_context
from cognistore.core.catalog import Catalog, CatalogStore
from cognistore.core.content_identity import ContentIdentityBuilder
from cognistore.core.document_extraction import DocumentExtraction
from cognistore.core.indexer import Indexer
from cognistore.core.mime_detection import MimeDetectionAdapter
from cognistore.core.pii import DetectorIdentity, PIIDetectionPipeline, RegexPIIDetector
from cognistore.core.policy import ContentAwarePolicy, PIIPolicyRule
from cognistore.core.scanner import scan_catalog
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.pii_runtime import PIIConfig, load_pii_config


class ReplacementDetector(RegexPIIDetector):
    identity = DetectorIdentity("replacement", "2")


def extracted(text: str) -> DocumentExtraction:
    return DocumentExtraction(
        status="succeeded", source_mime="application/pdf", source_size=len(text),
        parser=None, text=text, text_bytes=len(text.encode("utf-8")),
    )


def test_tenant_selection_and_replacement(tmp_path: Path) -> None:
    path = tmp_path / "drivers.yaml"
    path.write_text(
        "pii:\n  detectors: [regex]\n  tenants:\n    private:\n"
        "      detectors: [replacement]\n      limits:\n        max_text_bytes: 32\n"
        "    disabled:\n      detectors: []\n",
        encoding="utf-8",
    )
    config = load_pii_config(path, detector_factories={
        "regex": RegexPIIDetector, "replacement": ReplacementDetector,
    })
    doc = extracted("a@example.com")
    digest = hashlib.sha256(b"source").hexdigest()
    with tenant_context("private"):
        result = config.pipeline_for_tenant("private").detect(doc, content_sha256=digest)
        assert result.status == "succeeded"
        assert result.to_metadata()["findings"][0]["detector"] == "replacement"
        assert result.to_metadata()["findings"][0]["detector_version"] == "2"
        with pytest.raises(TenantIsolationError):
            config.pipeline_for_tenant("disabled")
    assert config.pipeline_for_tenant("disabled").detect(doc, content_sha256=digest).status == "disabled"
    assert config.pipeline_for_tenant("other").detect(doc, content_sha256=digest).status == "succeeded"
    assert load_pii_config(None).pipeline_for_tenant("default").detect(
        doc, content_sha256=digest
    ).status == "disabled"


@pytest.mark.parametrize("settings", [
    [], {"enabled": True}, {"detectors": "regex"}, {"detectors": ["missing"]},
    {"detectors": ["regex", "regex"]}, {"limits": {"timeout_seconds": 0}},
    {"limits": {"max_text_bytes": True}}, {"limits": {"store_matches": True}},
    {"tenants": []}, {"tenants": {"../bad": {}}},
    {"tenants": {"tenant": {"unknown": 1}}},
])
def test_invalid_configuration(settings: object) -> None:
    with pytest.raises(ValueError):
        PIIConfig(settings)


def test_factory_failure_is_content_free() -> None:
    def broken():
        raise RuntimeError("a@example.com")

    config = PIIConfig({"detectors": ["custom"]}, detector_factories={"custom": broken})
    with pytest.raises(ValueError) as error:
        config.pipeline_for_tenant("default")
    assert "a@example.com" not in str(error.value)


@pytest.fixture(params=("memory", "sqlite"))
def catalog(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[CatalogStore]:
    if request.param == "memory":
        yield Catalog()
    else:
        with SQLiteCatalog(tmp_path / "catalog.db") as store:
            yield store


def document_bytes() -> bytes:
    document = Document()
    document.add_paragraph("Contact alice@example.com about 123-45-6789")
    document.core_properties.author = "alice@example.com"
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def test_scan_persists_redacted_classification(catalog: CatalogStore, tmp_path: Path) -> None:
    driver = PosixDriver(str(tmp_path / "objects"))
    driver.put_object("bucket", "private.docx", document_bytes())
    results = scan_catalog(
        tier="hot", bucket="bucket", driver=driver, catalog=catalog,
        indexer=Indexer(mime_detector=MimeDetectionAdapter(None)),
        pii_pipeline=PIIDetectionPipeline(),
    )
    assert len(results) == 1
    record = catalog.get("bucket", "private.docx")
    assert record is not None
    classification = record.metadata["pii_detection"]
    assert classification["status"] == "succeeded"
    assert {finding["type"] for finding in classification["findings"]} == {"EMAIL_ADDRESS", "US_SSN"}
    assert classification["content_sha256"] == record.metadata["content_identity"]["sha256"]
    assert record.metadata["document_extraction"]["text"] is None
    assert record.metadata["document_extraction"]["document_metadata"] == {}
    serialized = json.dumps(record.metadata)
    assert "alice@example.com" not in serialized
    assert "123-45-6789" not in serialized
    decision = ContentAwarePolicy(
        pii_rules=[PIIPolicyRule("EMAIL_ADDRESS", "warm", 0.8)],
    ).evaluate_record(record)
    assert (decision.action, decision.dst_tier, decision.reason_code) == ("move", "warm", "pii_rule")
    if isinstance(catalog, SQLiteCatalog):
        with SQLiteCatalog(tmp_path / "catalog.db") as reopened:
            assert reopened.get("bucket", "private.docx").metadata == record.metadata


def test_limits_and_disable_replace_prior_result(catalog: CatalogStore, tmp_path: Path) -> None:
    driver = PosixDriver(str(tmp_path / "objects"))
    driver.put_object("bucket", "private.docx", document_bytes())
    config = PIIConfig({"detectors": ["regex"], "limits": {"max_text_bytes": 1}})
    kwargs = dict(
        tier="hot", bucket="bucket", driver=driver, catalog=catalog,
        indexer=Indexer(mime_detector=MimeDetectionAdapter(None)),
    )
    scan_catalog(**kwargs, pii_pipeline=config.pipeline_for_tenant("default"))
    record = catalog.get("bucket", "private.docx")
    assert record.metadata["pii_detection"]["status"] == "unknown"
    assert "alice@example.com" not in json.dumps(record.metadata)
    scan_catalog(**kwargs)
    assert catalog.get("bucket", "private.docx").metadata["pii_detection"]["status"] == "disabled"


@pytest.mark.parametrize("change", ["content", "extraction"])
def test_scan_invalidates_omitted_classification(catalog: CatalogStore, change: str) -> None:
    content = ContentIdentityBuilder().build(BytesIO(b"before"), expected_size=6)
    for index in range(2):
        metadata = {"document_extraction": {"parser": "v1"}}
        if index == 0:
            metadata["pii_detection"] = {"status": "succeeded", "findings": []}
        elif change == "content":
            content = ContentIdentityBuilder().build(BytesIO(b"after!"), expected_size=6)
        else:
            metadata["document_extraction"] = {"parser": "v2"}
        assert catalog.upsert_scan_observation(
            "bucket", "key", size=6, tier="hot", generation=str(index),
            metadata=metadata, content=content, fence=catalog.capture_scan_fence("bucket", "key"),
        )
    assert "pii_detection" not in catalog.get("bucket", "key").metadata


def test_classifications_are_tenant_local(catalog: CatalogStore) -> None:
    first, second = catalog.for_tenant("first"), catalog.for_tenant("second")
    first.upsert("bucket", "key", 1, "hot", metadata={"pii_detection": {"status": "unknown"}})
    second.upsert("bucket", "key", 1, "hot", metadata={"pii_detection": {"status": "disabled"}})
    assert first.get("bucket", "key").metadata["pii_detection"]["status"] == "unknown"
    assert second.get("bucket", "key").metadata["pii_detection"]["status"] == "disabled"


def test_detection_keeps_generation_fence_and_dry_run_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = PosixDriver(str(tmp_path / "objects"))
    driver.put_object("bucket", "key.txt", b"original")
    catalog = Catalog()
    pipeline = PIIDetectionPipeline(detectors=())
    original_detect = pipeline.detect
    calls = []

    def detect(extraction, *, content_sha256):
        calls.append(content_sha256)
        if len(calls) == 1:
            driver.put_object("bucket", "key.txt", b"replaced")
        return original_detect(extraction, content_sha256=content_sha256)

    monkeypatch.setattr(pipeline, "detect", detect)
    kwargs = dict(
        tier="hot", bucket="bucket", driver=driver, catalog=catalog,
        indexer=Indexer(mime_detector=MimeDetectionAdapter(None)), pii_pipeline=pipeline,
    )
    assert scan_catalog(**kwargs) == []
    assert catalog.get("bucket", "key.txt") is None
    assert len(scan_catalog(**kwargs, dry_run=True)) == 1
    assert len(calls) == 2
    assert catalog.get("bucket", "key.txt") is None
