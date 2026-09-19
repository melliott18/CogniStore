"""Bounded, pluggable PII classification with a redacted catalog contract.

Detectors are trusted, pickle-compatible application configuration. Document
text and detector execution stay in a disposable subprocess; only validated
categories, confidence, and fixed provenance labels leave it. This isolation
enforces deadlines and output bounds, but is not a security sandbox for plugins.
"""

from __future__ import annotations

import json
import logging
import math
import multiprocessing
import os
import pickle
import re
import sys
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from multiprocessing.shared_memory import SharedMemory
from time import monotonic
from typing import Any, Literal, Protocol, cast

from cognistore.core.document_extraction import DocumentExtraction

PII_SCHEMA_VERSION = 1
SUPPORTED_PII_TYPES = frozenset({
    "EMAIL_ADDRESS", "US_SSN", "PHONE_NUMBER", "CREDIT_CARD_NUMBER",
    "IP_ADDRESS", "PERSON", "LOCATION", "DATE_OF_BIRTH", "ACCOUNT_NUMBER",
    "TAX_ID", "PASSPORT_NUMBER", "DRIVER_LICENSE_NUMBER",
})
PII_PROVENANCE = frozenset({"regex", "rule", "ner", "classifier"})
PIIStatus = Literal["succeeded", "unknown", "disabled"]
PIIFailureCode = Literal[
    "extraction_failed", "invalid_input", "input_too_large", "timeout",
    "output_too_large", "invalid_output", "detector_error", "worker_error",
]
_FAILURE_CODES = frozenset({
    "extraction_failed", "invalid_input", "input_too_large", "timeout",
    "output_too_large", "invalid_output", "detector_error", "worker_error",
})
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z", re.ASCII)
_VERSION = re.compile(r"[0-9]{1,8}(?:\.[0-9]{1,8}){0,3}(?:[a-z][a-z0-9]{0,7})?\Z", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_MAX_DETECTORS = 32
_MAX_OUTPUT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class PIILimits:
    """Bounds for a complete classification attempt, including all detectors."""

    max_text_bytes: int = 4 * 1024 * 1024
    timeout_seconds: float = 10.0
    max_findings: int = 1000

    def __post_init__(self) -> None:
        for name in ("max_text_bytes", "max_findings"):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= sys.maxsize:
                raise ValueError(f"{name} must be a positive bounded integer")
        value = self.timeout_seconds
        if (
            type(value) not in (int, float)
            or not 0 < value <= threading.TIMEOUT_MAX
            or not math.isfinite(value)
        ):
            raise ValueError("timeout_seconds must be positive, finite, and bounded")


@dataclass(frozen=True)
class DetectorIdentity:
    name: str
    version: str

    def __post_init__(self) -> None:
        if type(self.name) is not str or _NAME.fullmatch(self.name) is None:
            raise ValueError("detector name must be a bounded symbolic identifier")
        if type(self.version) is not str or _VERSION.fullmatch(self.version) is None:
            raise ValueError("detector version must be a bounded numeric version")

    def to_metadata(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version}


@dataclass(frozen=True)
class PIIFinding:
    """A normalized detector finding; never contains a match or its location."""

    type: str
    confidence: float
    provenance: str

    def __post_init__(self) -> None:
        if type(self.type) is not str or self.type not in SUPPORTED_PII_TYPES:
            raise ValueError("unsupported PII category")
        if (
            type(self.confidence) not in (int, float)
            or not 0 <= self.confidence <= 1
            or not math.isfinite(self.confidence)
        ):
            raise ValueError("confidence must be a finite number from zero to one")
        if type(self.provenance) is not str or self.provenance not in PII_PROVENANCE:
            raise ValueError("unsupported PII provenance")


@dataclass(frozen=True)
class ClassifiedPIIFinding(PIIFinding):
    detector: str
    detector_version: str

    def __post_init__(self) -> None:
        super().__post_init__()
        DetectorIdentity(self.detector, self.detector_version)

    def to_metadata(self) -> dict[str, object]:
        return {
            "type": self.type,
            "confidence": float(self.confidence),
            "provenance": self.provenance,
            "detector": self.detector,
            "detector_version": self.detector_version,
        }


class PIIDetector(Protocol):
    """One trusted pickle-compatible detector, with stable implementation identity."""

    identity: DetectorIdentity

    def detect(self, text: str) -> Iterable[PIIFinding]: ...


@dataclass(frozen=True)
class PIIClassification:
    status: PIIStatus
    content_sha256: str
    findings: tuple[ClassifiedPIIFinding, ...] = ()
    detectors: tuple[DetectorIdentity, ...] = ()
    failure_code: PIIFailureCode | None = None

    def to_metadata(self) -> dict[str, object]:
        return {
            "schema_version": PII_SCHEMA_VERSION,
            "status": self.status,
            "content_sha256": self.content_sha256,
            "findings": [finding.to_metadata() for finding in self.findings],
            "detectors": [detector.to_metadata() for detector in self.detectors],
            "failure_code": self.failure_code,
        }


class RegexPIIDetector:
    """Heuristics for ASCII email addresses and hyphenated US SSNs.

    Confidence values are fixed rule scores, not calibrated probabilities.
    SSNs exclude impossible area/group/serial values. These patterns neither
    prove identity nor establish that a document is free of other PII.
    """

    identity = DetectorIdentity("regex", "1")
    _email = re.compile(
        r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
        r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
        r"[A-Za-z]{2,63}(?![A-Za-z0-9-])"
    )
    _ssn = re.compile(r"(?<![0-9])([0-9]{3})-([0-9]{2})-([0-9]{4})(?![0-9])")

    def detect(self, text: str) -> Iterable[PIIFinding]:
        for _match in self._email.finditer(text):
            yield PIIFinding("EMAIL_ADDRESS", 0.9, "regex")
        for match in self._ssn.finditer(text):
            area, group, serial = map(int, match.groups())
            if 0 < area < 900 and area != 666 and group != 0 and serial != 0:
                yield PIIFinding("US_SSN", 0.85, "regex")


class PIIDetectionPipeline:
    """Classify complete extracted text, failing closed to an unknown outcome."""

    def __init__(
        self,
        detectors: Sequence[PIIDetector] | None = None,
        *,
        limits: PIILimits | None = None,
    ) -> None:
        configured = tuple(detectors) if detectors is not None else (RegexPIIDetector(),)
        if len(configured) > _MAX_DETECTORS:
            raise ValueError("too many PII detectors configured")
        identities: list[DetectorIdentity] = []
        for detector in configured:
            identity = detector.identity
            if type(identity) is not DetectorIdentity:
                raise ValueError("PII detector must declare a DetectorIdentity")
            # Revalidate even if application code bypassed frozen dataclass checks.
            identities.append(DetectorIdentity(identity.name, identity.version))
        if len({identity.name for identity in identities}) != len(identities):
            raise ValueError("PII detector names must be unique")
        try:
            self._serialized_detectors = pickle.dumps(configured, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:
            raise ValueError("PII detectors must be pickle-compatible") from None
        self.detectors = tuple(identities)
        self.limits = limits or PIILimits()

    def detect(
        self,
        extraction: DocumentExtraction,
        *,
        content_sha256: str,
    ) -> PIIClassification:
        # Invalid digests are replaced with a fixed empty binding, never echoed.
        if type(content_sha256) is not str or _DIGEST.fullmatch(content_sha256) is None:
            return self._unknown("", "invalid_input")
        if not self.detectors:
            return PIIClassification("disabled", content_sha256)
        if extraction.status != "succeeded":
            return self._unknown(content_sha256, "extraction_failed")
        if type(extraction.text) is not str or extraction.failure_code is not None:
            return self._unknown(content_sha256, "invalid_input")
        if len(extraction.text) > self.limits.max_text_bytes:
            return self._unknown(content_sha256, "input_too_large")
        try:
            data = extraction.text.encode("utf-8")
        except UnicodeError:
            return self._unknown(content_sha256, "invalid_input")
        if len(data) > self.limits.max_text_bytes:
            return self._unknown(content_sha256, "input_too_large")
        if type(extraction.text_bytes) is not int or extraction.text_bytes != len(data):
            return self._unknown(content_sha256, "invalid_input")
        return self._run(data, content_sha256)

    def _unknown(self, digest: str, code: PIIFailureCode) -> PIIClassification:
        return PIIClassification("unknown", digest, detectors=self.detectors, failure_code=code)

    def _run(self, data: bytes, digest: str) -> PIIClassification:
        deadline = monotonic() + self.limits.timeout_seconds
        segments: list[SharedMemory] = []
        process: multiprocessing.Process | None = None
        output_size = min(_MAX_OUTPUT_BYTES, 2048 + self.limits.max_findings * 384)
        try:
            shared_input = _share(data, segments)
            shared_detectors = _share(self._serialized_detectors, segments)
            shared_output = SharedMemory(create=True, size=output_size + 8)
            segments.append(shared_output)
            _buffer(shared_output)[:8] = b"\0" * 8
            context: Any = multiprocessing.get_context("spawn")
            process = context.Process(
                target=_detector_worker,
                args=(
                    shared_input.name, len(data), shared_detectors.name,
                    len(self._serialized_detectors), shared_output.name, output_size,
                    self.limits.max_findings,
                    tuple((identity.name, identity.version) for identity in self.detectors),
                ),
                daemon=True,
            )
            assert process is not None
            if monotonic() >= deadline:
                return self._unknown(digest, "timeout")
            process.start()
            remaining = deadline - monotonic()
            if remaining <= 0:
                return self._unknown(digest, "timeout")
            process.join(timeout=remaining)
            if process.is_alive() or monotonic() >= deadline:
                return self._unknown(digest, "timeout")
            if process.exitcode != 0:
                return self._unknown(digest, "worker_error")
            size = int.from_bytes(_buffer(shared_output)[:8], "big")
            if not 0 < size <= output_size:
                return self._unknown(digest, "worker_error")
            payload = json.loads(bytes(_buffer(shared_output)[8:8 + size]))
            if type(payload) is not dict or set(payload) != {"findings", "failure_code"}:
                return self._unknown(digest, "invalid_output")
            if payload["failure_code"] is not None:
                code = payload["failure_code"]
                if type(code) is not str or code not in _FAILURE_CODES:
                    return self._unknown(digest, "invalid_output")
                return self._unknown(digest, cast(PIIFailureCode, code))
            metadata = {
                **PIIClassification("succeeded", digest, detectors=self.detectors).to_metadata(),
                "findings": payload["findings"],
            }
            result = parse_pii_classification(metadata, content_sha256=digest)
            if result is None or len(result.findings) > self.limits.max_findings:
                return self._unknown(digest, "invalid_output")
            if monotonic() >= deadline:
                return self._unknown(digest, "timeout")
            return result
        except Exception:
            return self._unknown(digest, "worker_error")
        finally:
            if process is not None and process.pid is not None:
                _stop_process(process)
            for segment in segments:
                try:
                    segment.close()
                except (BufferError, OSError):
                    pass
                try:
                    segment.unlink()
                except OSError:
                    pass


def parse_pii_classification(
    value: object, *, content_sha256: str,
) -> PIIClassification | None:
    """Strictly project current, normalized catalog metadata for policy readers."""

    if type(value) is not dict:
        return None
    if set(value) != {
        "schema_version", "status", "content_sha256", "findings", "detectors", "failure_code",
    }:
        return None
    if type(value["schema_version"]) is not int or value["schema_version"] != PII_SCHEMA_VERSION:
        return None
    if (
        type(content_sha256) is not str
        or _DIGEST.fullmatch(content_sha256) is None
        or value["content_sha256"] != content_sha256
        or type(value["status"]) is not str
        or value["status"] not in {"succeeded", "unknown", "disabled"}
        or type(value["detectors"]) is not list
        or len(value["detectors"]) > _MAX_DETECTORS
        or type(value["findings"]) is not list
        or len(value["findings"]) > _MAX_DETECTORS * len(SUPPORTED_PII_TYPES) * len(PII_PROVENANCE)
    ):
        return None
    try:
        detectors = tuple(
            DetectorIdentity(**item) for item in value["detectors"] if type(item) is dict
        )
        if len(detectors) != len(value["detectors"]):
            return None
        if len({identity.name for identity in detectors}) != len(detectors):
            return None
        findings = tuple(
            ClassifiedPIIFinding(**item) for item in value["findings"] if type(item) is dict
        )
        if len(findings) != len(value["findings"]):
            return None
        declared = {(identity.name, identity.version) for identity in detectors}
        if any((finding.detector, finding.detector_version) not in declared for finding in findings):
            return None
        keys = {
            (finding.type, finding.provenance, finding.detector, finding.detector_version)
            for finding in findings
        }
        if len(keys) != len(findings):
            return None
        status = value["status"]
        code = value["failure_code"]
        if status == "unknown":
            if findings or type(code) is not str or code not in _FAILURE_CODES:
                return None
        elif code is not None:
            return None
        if status == "disabled" and (findings or detectors):
            return None
        if status == "succeeded" and not detectors:
            return None
        return PIIClassification(cast(PIIStatus, status), content_sha256, findings, detectors, code)
    except (TypeError, ValueError):
        return None


def _share(data: bytes, segments: list[SharedMemory]) -> SharedMemory:
    segment = SharedMemory(create=True, size=max(1, len(data)))
    segments.append(segment)
    _buffer(segment)[:len(data)] = data
    return segment


def _read_shared(name: str, size: int) -> bytes:
    segment = SharedMemory(name=name)
    try:
        return bytes(_buffer(segment)[:size])
    finally:
        segment.close()


def _detector_worker(
    input_name: str, input_size: int, detector_name: str, detector_size: int,
    output_name: str, output_size: int, max_findings: int,
    identities: tuple[tuple[str, str], ...],
) -> None:
    # Redirect native writes as well as Python output before detector unpickling.
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        sys.stdout = sink
        sys.stderr = sink
        logging.disable(sys.maxsize)
        try:
            text = _read_shared(input_name, input_size).decode("utf-8")
            # Trusted configuration produced locally at pipeline construction.
            detectors = pickle.loads(_read_shared(detector_name, detector_size))  # nosec B301
            payload = _collect_findings(detectors, text, identities, max_findings)
        except BaseException:
            payload = {"findings": [], "failure_code": "detector_error"}
        try:
            encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > output_size:
                encoded = b'{"findings":[],"failure_code":"output_too_large"}'
            segment = SharedMemory(name=output_name)
            try:
                _buffer(segment)[8:8 + len(encoded)] = encoded
                _buffer(segment)[:8] = len(encoded).to_bytes(8, "big")
            finally:
                segment.close()
        except BaseException:
            # Parent observes an empty buffer or abnormal exit, never exception text.
            return


def _collect_findings(
    detectors: tuple[PIIDetector, ...], text: str,
    identities: tuple[tuple[str, str], ...], max_findings: int,
) -> dict[str, object]:
    count = 0
    findings: dict[tuple[str, str, str, str], ClassifiedPIIFinding] = {}
    for detector, (name, version) in zip(detectors, identities):
        for finding in detector.detect(text):
            count += 1
            if count > max_findings:
                return {"findings": [], "failure_code": "output_too_large"}
            if type(finding) is not PIIFinding:
                return {"findings": [], "failure_code": "invalid_output"}
            try:
                normalized = ClassifiedPIIFinding(
                    finding.type, finding.confidence, finding.provenance, name, version,
                )
            except (TypeError, ValueError):
                return {"findings": [], "failure_code": "invalid_output"}
            key = (normalized.type, name, version, normalized.provenance)
            if key not in findings or normalized.confidence > findings[key].confidence:
                findings[key] = normalized
    return {
        "findings": [findings[key].to_metadata() for key in sorted(findings)],
        "failure_code": None,
    }


def _stop_process(process: multiprocessing.Process) -> None:
    try:
        if process.is_alive():
            process.terminate()
            process.join(timeout=0.2)
        if process.is_alive():
            process.kill()
            process.join(timeout=0.2)
        if not process.is_alive():
            process.join(timeout=0)
            process.close()
    except (OSError, ValueError, AssertionError):
        pass


def _buffer(segment: SharedMemory) -> memoryview:
    buffer = segment.buf
    if buffer is None:
        raise BufferError("shared memory is unavailable")
    return buffer
