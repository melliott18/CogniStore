"""Retained audit reports must be usable XML with matching evidence hashes."""

from __future__ import annotations

import gzip
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

EVIDENCE = Path(__file__).resolve().parents[2] / "docs/evidence/m5/ticket-162"
# Historical outcomes, including the deliberately failing baseline and the
# module-level collection skip. Repairing serialization must not change them.
JUNIT = {
    "full-tests.xml.gz": (6071, 0, 0, 315),
    "merge-tests.xml.gz": (218, 0, 0, 12),
    "mutation-before.xml.gz": (527, 0, 0, 0),
    "mutation-after.xml.gz": (645, 0, 0, 0),
    "scan-before.xml.gz": (6, 6, 0, 0),
    "scan-after.xml.gz": (14, 0, 0, 0),
    "tooling-final.xml.gz": (31, 0, 0, 0),
    "service-profile-initial/tests.xml.gz": (10, 0, 0, 10),
    "service-profile-final/tests.xml.gz": (10, 0, 0, 10),
}


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_json(name: str):
    return json.loads((EVIDENCE / name).read_text())


@pytest.mark.parametrize("name,expected", JUNIT.items())
def test_retained_junit_reports_parse_with_original_outcomes(name, expected):
    tree = ET.fromstring(gzip.decompress((EVIDENCE / name).read_bytes()))
    suite = tree.find("testsuite")
    assert suite is not None
    assert tuple(int(suite.get(key)) for key in ("tests", "failures", "errors", "skipped")) == expected
    cases = list(tree.iter("testcase"))
    assert (len(cases), *(sum(case.find(tag) is not None for case in cases)
                         for tag in ("failure", "error", "skipped"))) == expected
    assert suite.get("hostname") == "<LOCAL_HOST>"


def test_all_retained_artifacts_match_complete_checksum_inventory():
    expected = {}
    for line in (EVIDENCE / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert name not in expected
        expected[name] = digest
    actual = {p.relative_to(EVIDENCE).as_posix(): sha256(p.read_bytes())
              for p in EVIDENCE.rglob("*") if p.is_file() and p.name != "SHA256SUMS"}
    assert actual == expected


def test_xml_manifest_hashes_match_compressed_and_decompressed_artifacts():
    validation = read_json("validation.json")
    mutation = read_json("mutation-validation.json")
    for report in [validation["tests"]["full"], validation["tests"]["merge"], *mutation["runs"]]:
        for name, expected in report["artifacts"].items():
            assert sha256((EVIDENCE / name).read_bytes()) == expected
    assert sha256((EVIDENCE / "tooling-final.xml.gz").read_bytes()) == (
        validation["tests"]["tooling"]["artifact_sha256"]
    )
    for directory in ("service-profile-initial", "service-profile-final"):
        report = read_json(directory + "/report.json")
        for name, expected in report["artifacts"].items():
            raw = gzip.decompress((EVIDENCE / directory / (name + ".gz")).read_bytes())
            assert sha256(raw) == expected
        assert report["production_signoff"] is False
    # Preserve the original runner's misclassification; the serialization fix
    # does not rewrite an old failed report into a newer incomplete result.
    initial = read_json("service-profile-initial/report.json")["result"]
    assert (initial["status"], initial["skipped"], initial["unexpected_cases"]) == ("failed", 9, 1)
    final = read_json("service-profile-final/report.json")["result"]
    assert (final["status"], final["skipped"]) == ("incomplete", 10)


def test_repair_changes_only_placeholder_escaping_and_preserves_coverage():
    repair = read_json("xml-repair.json")
    assert set(repair["archives"]) == set(JUNIT)
    for name, record in repair["archives"].items():
        compressed = (EVIDENCE / name).read_bytes()
        raw = gzip.decompress(compressed)
        assert sha256(compressed) == record["after_gzip_sha256"]
        assert sha256(raw) == record["after_xml_sha256"]
        original = raw
        for token, count in record["placeholder_counts"].items():
            literal = ("<" + token + ">").encode()
            escaped = ("&lt;" + token + "&gt;").encode()
            assert literal not in raw
            assert raw.count(escaped) == count
            original = original.replace(escaped, literal)
        assert sha256(original) == record["before_xml_sha256"]
    coverage = (EVIDENCE / "full-coverage.xml.gz").read_bytes()
    assert sha256(coverage) == repair["unchanged_coverage_gzip_sha256"]
    assert ET.fromstring(gzip.decompress(coverage)).tag == "coverage"
