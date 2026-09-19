from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import replace

import pytest

from cognistore.core.document_extraction import DocumentExtraction, ParserIdentity
from cognistore.core.pii import (
    ClassifiedPIIFinding,
    DetectorIdentity,
    PIIDetectionPipeline,
    PIIFinding,
    PIILimits,
    RegexPIIDetector,
    parse_pii_classification,
)

SECRET = "synthetic.person@example.test / 123-45-6789"
DIGEST = hashlib.sha256(b"synthetic source bytes").hexdigest()


def _extraction(text: str = SECRET) -> DocumentExtraction:
    return DocumentExtraction(
        status="succeeded", source_mime="application/pdf", source_size=12,
        parser=ParserIdentity("test", "1", "1"), text=text,
        text_bytes=len(text.encode("utf-8")), output_bytes=len(text.encode("utf-8")) + 2,
    )


class _RuleDetector:
    identity = DetectorIdentity("test-rule", "1.2")

    def detect(self, text: str) -> Iterable[PIIFinding]:
        yield PIIFinding("PERSON", 0.7, "rule")
        yield PIIFinding("PERSON", 0.8, "rule")
        yield PIIFinding("PHONE_NUMBER", 0.5, "ner")


class _SlowDetector:
    identity = DetectorIdentity("slow", "1")

    def detect(self, text: str) -> Iterable[PIIFinding]:
        time.sleep(10)
        return []


class _SlowBootstrapDetector(_SlowDetector):
    def __getstate__(self) -> dict[str, bytes]:
        return {"state": b"x" * (2 * 1024 * 1024)}

    def __setstate__(self, state: object) -> None:
        time.sleep(10)


class _FailingDetector:
    identity = DetectorIdentity("failure", "1")

    def detect(self, text: str) -> Iterable[PIIFinding]:
        print(text)
        print(text, file=sys.stderr)
        os.write(1, text.encode())
        os.write(2, text.encode())
        logging.getLogger("pii-test").error(text)
        raise RuntimeError(text)


class _CrashDetector:
    identity = DetectorIdentity("crash", "1")

    def detect(self, text: str) -> Iterable[PIIFinding]:
        os._exit(17)


class _MalformedDetector:
    identity = DetectorIdentity("malformed", "1")

    def detect(self, text: str) -> Iterable[PIIFinding]:
        return [{"type": "EMAIL_ADDRESS", "confidence": 0.9, "snippet": text}]  # type: ignore[list-item]


class _MutatedDetector:
    identity = DetectorIdentity("mutated", "1")

    def detect(self, text: str) -> Iterable[PIIFinding]:
        finding = PIIFinding("EMAIL_ADDRESS", 0.9, "regex")
        object.__setattr__(finding, "type", text)
        yield finding


class _InfiniteDetector:
    identity = DetectorIdentity("infinite", "1")

    def detect(self, text: str) -> Iterable[PIIFinding]:
        while True:
            yield PIIFinding("PERSON", 0.5, "rule")


@pytest.mark.parametrize("field,value", [
    ("max_text_bytes", 0), ("max_findings", True), ("max_findings", sys.maxsize + 1),
    ("timeout_seconds", 0), ("timeout_seconds", math.nan),
    ("timeout_seconds", math.inf), ("timeout_seconds", threading.TIMEOUT_MAX * 2),
    ("timeout_seconds", 10**1000),
])
def test_limits_reject_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        PIILimits(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("args", [
    (SECRET, 0.5, "regex"), ("EMAIL_ADDRESS", True, "regex"),
    ("EMAIL_ADDRESS", math.nan, "regex"), ("EMAIL_ADDRESS", math.inf, "regex"),
    ("EMAIL_ADDRESS", -0.1, "regex"), ("EMAIL_ADDRESS", 1.1, "regex"),
    ("EMAIL_ADDRESS", 10**1000, "regex"),
    ("EMAIL_ADDRESS", 0.5, SECRET),
])
def test_findings_reject_raw_or_malformed_fields(args: tuple[object, ...]) -> None:
    with pytest.raises(ValueError):
        PIIFinding(*args)  # type: ignore[arg-type]


@pytest.mark.parametrize("name,version", [
    (SECRET, "1"), ("plugin", SECRET), ("a" * 65, "1"), ("plugin", "1-23-4567"),
])
def test_identity_rejects_content_and_unbounded_strings(name: str, version: str) -> None:
    with pytest.raises(ValueError):
        DetectorIdentity(name, version)


def test_default_regex_reports_redacted_versioned_deterministic_categories() -> None:
    pipeline = PIIDetectionPipeline()
    first = pipeline.detect(_extraction(), content_sha256=DIGEST)
    second = pipeline.detect(_extraction(), content_sha256=DIGEST)

    assert first == second
    assert first.status == "succeeded"
    assert first.failure_code is None
    assert first.detectors == (DetectorIdentity("regex", "1"),)
    assert first.findings == (
        ClassifiedPIIFinding("EMAIL_ADDRESS", 0.9, "regex", "regex", "1"),
        ClassifiedPIIFinding("US_SSN", 0.85, "regex", "regex", "1"),
    )
    assert first.to_metadata() == {
        "schema_version": 1, "status": "succeeded", "content_sha256": DIGEST,
        "findings": [
            {"type": "EMAIL_ADDRESS", "confidence": 0.9, "provenance": "regex",
             "detector": "regex", "detector_version": "1"},
            {"type": "US_SSN", "confidence": 0.85, "provenance": "regex",
             "detector": "regex", "detector_version": "1"},
        ],
        "detectors": [{"name": "regex", "version": "1"}], "failure_code": None,
    }
    persisted = json.dumps(first.to_metadata())
    assert "synthetic.person" not in persisted
    assert "123-45-6789" not in persisted
    assert parse_pii_classification(first.to_metadata(), content_sha256=DIGEST) == first


def test_clean_text_and_impossible_ssns_have_no_findings() -> None:
    result = PIIDetectionPipeline().detect(
        _extraction("Clean text: 000-12-3456 666-12-3456 900-12-3456 123-00-3456 123-12-0000"),
        content_sha256=DIGEST,
    )
    assert result.status == "succeeded"
    assert result.findings == ()


def test_plugins_combine_without_raw_output_and_deduplicate_at_highest_confidence() -> None:
    result = PIIDetectionPipeline([_RuleDetector(), RegexPIIDetector()]).detect(
        _extraction(), content_sha256=DIGEST,
    )
    assert result.status == "succeeded"
    assert len(result.findings) == 4
    assert next(finding for finding in result.findings if finding.type == "PERSON").confidence == 0.8
    assert result.detectors == (DetectorIdentity("test-rule", "1.2"), DetectorIdentity("regex", "1"))


def test_empty_detector_configuration_is_explicitly_disabled() -> None:
    result = PIIDetectionPipeline([]).detect(_extraction(), content_sha256=DIGEST)
    assert result.status == "disabled"
    assert result.findings == result.detectors == ()
    assert result.failure_code is None
    assert parse_pii_classification(result.to_metadata(), content_sha256=DIGEST) == result


def test_failed_extraction_is_unknown() -> None:
    extraction = replace(_extraction(), status="failed", text=None, failure_code="timeout")
    result = PIIDetectionPipeline().detect(extraction, content_sha256=DIGEST)
    assert result.status == "unknown"
    assert result.failure_code == "extraction_failed"
    assert result.findings == ()
    assert parse_pii_classification(result.to_metadata(), content_sha256=DIGEST) == result


def test_text_byte_bound_accepts_boundary_and_never_truncates() -> None:
    pipeline = PIIDetectionPipeline(limits=PIILimits(max_text_bytes=4))
    exact = pipeline.detect(_extraction("éé"), content_sha256=DIGEST)
    extra = pipeline.detect(_extraction("ééa"), content_sha256=DIGEST)
    assert exact.status == "succeeded"
    assert extra.status == "unknown"
    assert extra.failure_code == "input_too_large"
    assert extra.findings == ()


def test_partial_or_invalid_text_is_unknown() -> None:
    result = PIIDetectionPipeline().detect(
        replace(_extraction(), text_bytes=1000), content_sha256=DIGEST,
    )
    assert result.status == "unknown"
    assert result.failure_code == "invalid_input"
    malformed = PIIDetectionPipeline().detect(
        replace(_extraction(), text="\ud800"), content_sha256=DIGEST,
    )
    assert malformed.failure_code == "invalid_input"


def test_invalid_digest_is_not_echoed() -> None:
    result = PIIDetectionPipeline().detect(_extraction(), content_sha256=SECRET)
    assert result.status == "unknown"
    assert result.failure_code == "invalid_input"
    assert SECRET not in json.dumps(result.to_metadata())


@pytest.mark.parametrize("detector", [_SlowDetector(), _SlowBootstrapDetector()])
def test_timeout_includes_execution_and_large_plugin_bootstrap(detector: object) -> None:
    pipeline = PIIDetectionPipeline(
        [detector], limits=PIILimits(timeout_seconds=0.05),  # type: ignore[list-item]
    )
    started = time.monotonic()
    result = pipeline.detect(_extraction("a" * (2 * 1024 * 1024)), content_sha256=DIGEST)
    assert result.status == "unknown"
    assert result.failure_code == "timeout"
    assert result.findings == ()
    assert time.monotonic() - started < 2


def test_failure_redacts_exception_native_output_and_logs(capfd: pytest.CaptureFixture[str]) -> None:
    result = PIIDetectionPipeline([_FailingDetector()]).detect(_extraction(), content_sha256=DIGEST)
    assert result.status == "unknown"
    assert result.failure_code == "detector_error"
    captured = capfd.readouterr()
    assert SECRET not in captured.out + captured.err + json.dumps(result.to_metadata())


@pytest.mark.parametrize("detector,code", [
    (_CrashDetector(), "worker_error"), (_MalformedDetector(), "invalid_output"),
    (_MutatedDetector(), "invalid_output"), (_InfiniteDetector(), "output_too_large"),
])
def test_bad_or_unbounded_plugin_output_fails_closed(detector: object, code: str) -> None:
    result = PIIDetectionPipeline(
        [detector], limits=PIILimits(max_findings=3),  # type: ignore[list-item]
    ).detect(_extraction(), content_sha256=DIGEST)
    assert result.status == "unknown"
    assert result.failure_code == code
    assert result.findings == ()
    assert SECRET not in json.dumps(result.to_metadata())


def test_findings_limit_counts_emitted_duplicates_and_drops_partial_results() -> None:
    result = PIIDetectionPipeline(limits=PIILimits(max_findings=1)).detect(
        _extraction(), content_sha256=DIGEST,
    )
    assert result.status == "unknown"
    assert result.failure_code == "output_too_large"
    assert result.findings == ()


def test_failure_after_successful_detector_discards_partial_classification() -> None:
    result = PIIDetectionPipeline([RegexPIIDetector(), _FailingDetector()]).detect(
        _extraction(), content_sha256=DIGEST,
    )
    assert result.status == "unknown"
    assert result.findings == ()


def test_duplicate_detector_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        PIIDetectionPipeline([RegexPIIDetector(), RegexPIIDetector()])


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2), ("status", "clean"),
    ("content_sha256", "0" * 64), ("failure_code", SECRET),
    ("findings", [{"type": SECRET}]),
    ("detectors", [{"name": "regex", "version": SECRET}]),
    ("snippet", SECRET),
])
def test_catalog_parser_rejects_malformed_or_stale_metadata(field: str, value: object) -> None:
    metadata = {
        "schema_version": 1, "status": "succeeded", "content_sha256": DIGEST,
        "findings": [], "detectors": [{"name": "regex", "version": "1"}],
        "failure_code": None,
    }
    metadata[field] = value
    assert parse_pii_classification(metadata, content_sha256=DIGEST) is None


def test_catalog_parser_rejects_undeclared_detector_and_unknown_partial_results() -> None:
    metadata = PIIDetectionPipeline().detect(_extraction(), content_sha256=DIGEST).to_metadata()
    metadata["detectors"] = [{"name": "different", "version": "1"}]
    assert parse_pii_classification(metadata, content_sha256=DIGEST) is None
    metadata["detectors"] = [{"name": "regex", "version": "1"}]
    metadata["status"] = "unknown"
    metadata["failure_code"] = "timeout"
    assert parse_pii_classification(metadata, content_sha256=DIGEST) is None


def test_catalog_parser_rejects_duplicate_findings_and_huge_confidence() -> None:
    finding = ClassifiedPIIFinding("EMAIL_ADDRESS", 0.9, "regex", "regex", "1").to_metadata()
    metadata = {
        "schema_version": 1, "status": "succeeded", "content_sha256": DIGEST,
        "findings": [finding, finding], "detectors": [{"name": "regex", "version": "1"}],
        "failure_code": None,
    }
    assert parse_pii_classification(metadata, content_sha256=DIGEST) is None
    metadata["findings"] = [{**finding, "confidence": 10**1000}]
    assert parse_pii_classification(metadata, content_sha256=DIGEST) is None


def test_metadata_snapshot_is_detached() -> None:
    result = PIIDetectionPipeline().detect(_extraction(), content_sha256=DIGEST)
    snapshot = result.to_metadata()
    snapshot["findings"][0]["type"] = "PERSON"  # type: ignore[index]
    snapshot["detectors"][0]["name"] = "changed"  # type: ignore[index]
    assert result.findings[0].type == "EMAIL_ADDRESS"
    assert result.detectors[0].name == "regex"
