#!/usr/bin/env python3
"""Seed offline acceptance fixtures and check retained manual evidence (#163).

This tool never contacts a deployment or executes the acceptance matrix. A valid
partial record is useful evidence, not completion or release qualification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs/manual_acceptance_matrix.json"
HOSTED_CI = "skipped: user instruction; known GitHub billing/spending restriction"
STATUSES = {"not_run", "blocked", "failed", "passed"}
HEX = re.compile(r"[a-f0-9]{64}")
SOURCE = re.compile(r"[a-f0-9]{40}")
IMAGE = re.compile(r"sha256:[a-f0-9]{64}")


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def write_json(path: Path, value: object) -> None:
    # Do not overwrite a previous run, source fixture, or reviewed evidence.
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    result = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object")
    return result


def small_pdf(text: str | None) -> bytes:
    """Original deterministic text or image-only fixture; no third-party material."""
    stream = (b"q 24 0 0 24 40 40 cm /Im0 Do Q" if text is None else
              b"BT /F1 12 Tf 40 80 Td (" + text.encode("ascii") + b") Tj ET")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 120] "
        b"/Resources << /Font << /F1 5 0 R >> /XObject << /Im0 6 0 R >> >> "
        b"/Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Length 3 >>\nstream\n"
        b"\x00\x80\xff\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
                  f"startxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


def corpus(output: Path, limit_bytes: int = 65536) -> dict:
    assets = ROOT / "tests/fixtures/documents"
    sample = (assets / "sample.pdf").read_bytes()
    if type(limit_bytes) is not int or not len(sample) <= limit_bytes <= 64 * 1024 * 1024:
        raise ValueError("Fixture limit must fit the PDF and be at most 64 MiB")
    output.mkdir(parents=True, exist_ok=False)
    files = []

    def put(name, payload, purpose, source=None):
        path = output / name
        path.write_bytes(payload)
        files.append({"path": name, "bytes": len(payload), "sha256": digest(path),
                      "purpose": purpose, "source": source})

    for name in ("sample.pdf", "sample.docx", "encrypted.pdf", "corrupt.pdf", "corrupt.docx"):
        put(name, (assets / name).read_bytes(), "document extraction and failure isolation",
            f"tests/fixtures/documents/{name}")
    text = b"CogniStore manual acceptance synthetic corpus v1\nAlpha searchable record.\n"
    put("alpha.txt", text, "exact bytes, range reads and content duplicate")
    put("alpha-copy.txt", text, "same bytes under a different key")
    put("tenant-b.txt", b"CogniStore synthetic tenant B distinct same-key payload.\n",
        "upload under the same logical key in the second tenant")
    put("notes.md", b"# Synthetic acceptance notes\nNo production records.\n", "multiple formats")
    put("unsupported.bin", bytes(range(256)), "unsupported document MIME remains object-scoped")
    put("pii.pdf", small_pdf("Synthetic contact: fixture.pii@example.invalid; 202-555-0100"),
        "synthetic PII detection and search persistence boundary")
    put("image-only.pdf", small_pdf(None), "image-only PDF extracts empty text; no OCR claim")
    # Whitespace before startxref leaves all object/xref offsets intact and keeps
    # the final startxref/EOF lines readable even for the 25 MiB input boundary.
    head, tail = sample.rsplit(b"startxref", 1)
    boundary = head + b"\n" * (limit_bytes - len(sample)) + b"startxref" + tail
    put("at-limit.pdf", boundary, "configured input/body limit exactly reached")
    put("over-limit.pdf", head + b"\n" * (limit_bytes - len(sample) + 1) + b"startxref" + tail,
        "configured limit plus one byte")
    manifest = {
        "schema_version": 1, "corpus_id": "manual-acceptance-v1", "synthetic": True,
        "limit_bytes": limit_bytes, "objects": files,
        "limitations": [
            "Fixture generation does not execute uploads, extraction, PII detection or search.",
            "Use the matching configured limit; the default is a reduced local 64 KiB fixture.",
            "Output/time/worker extraction limits require separate controlled runtime settings.",
        ],
    }
    write_json(output / "manifest.json", manifest)
    return manifest


def local_file(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError("Invalid evidence path")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Evidence must remain below the record directory")
    path = root
    for part in relative.parts:
        path /= part
        if path.is_symlink():
            raise ValueError("Evidence symlinks are not permitted")
    if not path.is_file():
        raise ValueError("Missing evidence file")
    return path


def verify_corpus(directory: Path) -> dict:
    manifest = read_json(directory / "manifest.json")
    if manifest.get("schema_version") != 1 or manifest.get("synthetic") is not True:
        raise ValueError("Invalid corpus manifest")
    objects = manifest.get("objects")
    if not isinstance(objects, list) or not objects:
        raise ValueError("Missing corpus objects")
    names = []
    for item in objects:
        path = local_file(directory, item["path"])
        if item["sha256"] != digest(path) or item["bytes"] != path.stat().st_size:
            raise ValueError("Changed corpus object")
        names.append(item["path"])
    paths = list(directory.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise ValueError("Corpus symlinks are not permitted")
    actual = {p.relative_to(directory).as_posix() for p in paths if p.is_file()}
    if len(names) != len(set(names)) or actual != set(names) | {"manifest.json"}:
        raise ValueError("Duplicate or unrecorded corpus object")
    return manifest


def matrix_cases(path: Path = MATRIX) -> dict[str, dict]:
    matrix = read_json(path)
    scenarios = matrix.get("scenarios", [])
    required = {f"MA-{number:02}" for number in range(1, 17)}
    if (matrix.get("schema_version") != 1 or len(scenarios) != len(required)
            or {s["id"] for s in scenarios} != required):
        raise ValueError("Matrix must contain exactly MA-01 through MA-16")
    cases = {}
    for scenario in scenarios:
        if not scenario.get("cases"):
            raise ValueError("Every scenario needs explicit cases")
        for case in scenario["cases"]:
            if (case["id"] in cases or not case["id"].startswith(scenario["id"] + ".")
                    or case["surface"] not in {"api", "cli", "sdk", "web", "review"}
                    or type(case.get("minimum_repetitions")) is not int
                    or case["minimum_repetitions"] < 1
                    or not all(isinstance(case.get(k), str) and case[k].strip()
                               for k in ("role", "tenant", "expected"))):
                raise ValueError("Invalid or duplicate matrix case")
            cases[case["id"]] = {**case, "scenario_id": scenario["id"]}
    return cases


def template(args: argparse.Namespace) -> dict:
    manifest = verify_corpus(args.corpus)
    cases = matrix_cases()
    return {
        "schema_version": 1, "kind": "manual-acceptance", "tester": args.tester,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "production_qualified": False, "release_qualified": False, "hosted_ci": HOSTED_CI,
        "matrix_sha256": digest(MATRIX),
        "corpus": {"id": manifest["corpus_id"],
                   "manifest_sha256": digest(args.corpus / "manifest.json"), "evidence": []},
        "bindings": {
            "source_commit": args.source_sha, "source_state": "unverified",
            "candidate_id": args.candidate_id, "candidate_manifest_sha256": None,
            "configuration_id": args.configuration_id, "configuration_sha256": None,
            "environment_id": args.environment_id, "environment_kind": args.environment_kind,
            "registry_manifest_digest": args.image_digest,
        },
        "scope": {"specification_accepted": False, "specification_evidence": [],
                  "staging_accepted": False, "staging_evidence": [],
                  "production_providers_required": None, "provider_evidence": []},
        "cases": [{**case, "status": "not_run", "inputs": [], "actual": "",
                   "executed_at": None, "evidence": [], "browser": None,
                   "additional_attempts": []}
                  for case in cases.values()],
        "findings": [],
        "limitations": [
            "A valid partial record is not completed manual acceptance.",
            "Record synthetic inputs and sanitized evidence only; never retain credentials.",
            "This checker verifies record structure and retained bytes, not reviewer truth.",
        ],
    }


def nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def timestamp(value) -> datetime | None:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def retained_uri(value) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    # These are declared retention references; no network lookup is performed.
    return (parsed.scheme in {"https", "restricted"} and bool(parsed.netloc)
            and bool(parsed.path.strip("/")) and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment)


def validate(path: Path, matrix: Path = MATRIX) -> dict:
    issues, blockers = [], []
    summary = {
        "schema_version": 1, "record_valid": False, "manual_acceptance_complete": False,
        "production_qualified": False, "release_qualified": False, "hosted_ci": HOSTED_CI,
        "issues": issues, "blockers": blockers, "counts": dict.fromkeys(sorted(STATUSES), 0),
        "limitations": ["Evidence sanitization, execution truth and remote retention need review.",
                        "Manual acceptance alone cannot establish release qualification."],
    }

    def check(condition, label):
        if not condition:
            issues.append(label)

    def evidence(items, label, required=False):
        kinds = set()
        if not isinstance(items, list) or (required and not items):
            issues.append(label + ".missing")
            return kinds
        for item in items:
            try:
                source = local_file(path.parent, item["path"])
                if item.get("sanitized") is not True or item.get("kind") not in {
                    "screenshot", "log", "review",
                } or item.get("sha256") != digest(source) or not source.stat().st_size:
                    raise ValueError("Invalid evidence metadata")
                if item["kind"] == "screenshot":
                    with source.open("rb") as stream:
                        header = stream.read(8)
                    if not header.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
                        raise ValueError("Screenshot must be retained PNG or JPEG bytes")
                kinds.add(item["kind"])
                if not retained_uri(item.get("durable_uri")):
                    blockers.append(label + ".durable_retention_missing")
            except (KeyError, TypeError, ValueError, OSError):
                issues.append(label + ".invalid")
        return kinds

    def attempt(item, label, surface):
        check(nonempty(item.get("actual")), label + ".actual")
        check(isinstance(item.get("inputs"), list) and bool(item["inputs"])
              and all(nonempty(value) for value in item["inputs"]), label + ".inputs")
        check(timestamp(item.get("executed_at")) is not None, label + ".executed_at")
        kinds = evidence(item.get("evidence"), label + ".evidence", True)
        if surface == "web":
            browser = item.get("browser") or {}
            check(all(nonempty(browser.get(k)) for k in ("name", "version"))
                  and browser.get("interaction") == "actual-browser"
                  and {"screenshot", "log"} <= kinds, label + ".actual_browser")

    def rerun_after(case, fixed_at):
        times = {
            observed for item in [case, *case.get("additional_attempts", [])]
            if item.get("status") == "passed"
            and (observed := timestamp(item.get("executed_at"))) is not None
            and observed > fixed_at
        }
        return case.get("status") == "passed" and len(times) >= case["minimum_repetitions"]

    try:
        report, expected = read_json(path), matrix_cases(matrix)
        check(report.get("schema_version") == 1 and report.get("kind") == "manual-acceptance",
              "record.schema")
        check(report.get("production_qualified") is False
              and report.get("release_qualified") is False, "record.qualification_claim")
        check(nonempty(report.get("tester")), "record.tester")
        check(timestamp(report.get("created_at")) is not None, "record.created_at")
        check(report.get("matrix_sha256") == digest(matrix), "record.matrix_binding")
        check(report.get("hosted_ci") == HOSTED_CI, "record.hosted_ci")
        seed = report.get("corpus", {})
        check(seed.get("id") == "manual-acceptance-v1"
              and bool(HEX.fullmatch(seed.get("manifest_sha256", ""))), "record.corpus_binding")
        evidence(seed.get("evidence"), "corpus.evidence")
        if not seed.get("evidence"):
            blockers.append("corpus.retained_manifest_missing")
        elif not any(item.get("sha256") == seed.get("manifest_sha256")
                     for item in seed["evidence"]):
            issues.append("corpus.retained_manifest_binding")
        bindings = report["bindings"]
        check(bool(SOURCE.fullmatch(bindings.get("source_commit", ""))), "bindings.source_commit")
        for key in ("candidate_id", "configuration_id", "environment_id"):
            check(nonempty(bindings.get(key)), "bindings." + key)
        check(bindings.get("environment_kind") in {"local", "staging"}, "bindings.environment_kind")
        check(bindings.get("source_state") in {"unverified", "clean", "dirty"}, "bindings.source_state")
        if bindings.get("environment_kind") != "staging":
            blockers.append("staging_execution_missing")
        if bindings.get("source_state") != "clean":
            blockers.append("clean_candidate_source_missing")
        for key, pattern in (("candidate_manifest_sha256", HEX), ("configuration_sha256", HEX),
                             ("registry_manifest_digest", IMAGE)):
            value = bindings.get(key)
            check(value is None or (isinstance(value, str) and bool(pattern.fullmatch(value))),
                  "bindings." + key)
            if value is None:
                blockers.append("bindings." + key + ".missing")
        scope = report["scope"]
        for key in ("specification", "staging"):
            accepted = scope.get(key + "_accepted")
            check(type(accepted) is bool, "scope." + key)
            evidence(scope.get(key + "_evidence"), "scope." + key + "_evidence", accepted is True)
            if not accepted:
                blockers.append("scope." + key + "_acceptance_missing")
        providers = scope.get("production_providers_required")
        if providers is None:
            blockers.append("scope.production_providers_unresolved")
        elif type(providers) is not bool:
            issues.append("scope.production_providers_required")
        elif providers:
            # The current metadata-only matrix has no production quality cases.
            blockers.append("scope.production_provider_matrix_and_quality_evidence_required")
        evidence(scope.get("provider_evidence"), "scope.provider_evidence", providers is True)
        cases = report["cases"]
        actual = {case["id"]: case for case in cases}
        check(len(actual) == len(cases) and set(actual) == set(expected), "cases.coverage")
        for case_id, definition in expected.items():
            case = actual.get(case_id)
            if case is None:
                continue
            check(all(case.get(k) == v for k, v in definition.items()), case_id + ".definition")
            status = case.get("status")
            check(status in STATUSES, case_id + ".status")
            if status in STATUSES:
                summary["counts"][status] += 1
            if status != "passed":
                blockers.append(case_id + ".not_passed")
            if status in {"passed", "failed"}:
                attempt(case, case_id, case["surface"])
            elif status == "blocked":
                check(nonempty(case.get("actual")), case_id + ".blocked_reason")
                evidence(case.get("evidence"), case_id + ".evidence")
            else:
                evidence(case.get("evidence"), case_id + ".evidence")
            additional = case.get("additional_attempts")
            check(isinstance(additional, list), case_id + ".additional_attempts")
            for number, item in enumerate(additional, 2):
                check(item.get("status") in {"passed", "failed"}, case_id + ".attempt_status")
                attempt(item, f"{case_id}.attempt_{number}", case["surface"])
            if status == "passed":
                times = [timestamp(item.get("executed_at")) for item in [case, *additional]]
                check(len(times) >= definition["minimum_repetitions"]
                      and len(times) == len(set(times))
                      and all(item.get("status") == "passed" for item in additional),
                      case_id + ".minimum_repetitions")
        findings = report["findings"]
        check(isinstance(findings, list), "findings.schema")
        finding_ids = set()
        for finding in findings:
            identity = finding.get("id")
            check(nonempty(identity) and identity not in finding_ids, "findings.identity")
            finding_ids.add(identity)
            check(finding.get("severity") in {"release-blocking", "nonblocking"}, "findings.severity")
            check(finding.get("status") in {"open", "fixed"}, "findings.status")
            check(nonempty(finding.get("reproduction"))
                  and nonempty(finding.get("issue_reference")), "findings.reproduction_reference")
            if finding.get("status") == "fixed":
                check(nonempty(finding.get("fix_reference")), "findings.fix_reference")
            affected = finding.get("affected_cases")
            check(isinstance(affected, list) and bool(affected)
                  and all(item in expected for item in affected), "findings.affected_cases")
            evidence(finding.get("evidence"), "findings.evidence", True)
            if finding.get("severity") == "release-blocking":
                if finding.get("status") == "fixed":
                    check(bool(SOURCE.fullmatch(finding.get("fix_source_commit", ""))),
                          "findings.fix_source_commit")
                fixed_at = timestamp(finding.get("fixed_at"))
                if finding.get("status") != "fixed" or not fixed_at:
                    blockers.append("findings.unresolved_release_blocker")
                elif not affected or not all(
                    item in actual and rerun_after(actual[item], fixed_at) for item in affected
                ):
                    blockers.append("findings.affected_reruns_missing")
    except (KeyError, TypeError, ValueError, AttributeError, OSError):
        issues.append("record.malformed_or_unreadable")
    summary["record_valid"] = not issues
    summary["manual_acceptance_complete"] = not issues and not blockers
    return summary


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("corpus", help="create a new deterministic synthetic corpus")
    seed.add_argument("--output", type=Path, required=True)
    seed.add_argument("--limit-bytes", type=int, default=65536)
    new = commands.add_parser("template", help="create a new truthful not-run evidence record")
    new.add_argument("--output", type=Path, required=True)
    new.add_argument("--corpus", type=Path, required=True)
    for flag in ("tester", "source-sha", "candidate-id", "configuration-id", "environment-id"):
        new.add_argument("--" + flag, required=True)
    new.add_argument("--environment-kind", choices=("local", "staging"), default="local")
    new.add_argument("--image-digest")
    verify = commands.add_parser("validate", help="check record integrity without executing cases")
    verify.add_argument("record", type=Path)
    verify.add_argument("--require-complete", action="store_true")
    verify.add_argument("--output", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "corpus":
            corpus(args.output, args.limit_bytes)
            print("manual acceptance: synthetic corpus created; no scenarios executed")
        elif args.command == "template":
            write_json(args.output, template(args))
            print("manual acceptance: not-run record created; acceptance incomplete")
        else:
            summary = validate(args.record)
            if args.output:
                write_json(args.output, summary)
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 0 if (summary["manual_acceptance_complete"] if args.require_complete
                         else summary["record_valid"]) else 1
    except (KeyError, TypeError, ValueError, OSError):
        print("manual acceptance: invalid input or output already exists/unavailable")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
