#!/usr/bin/env python3
"""Run the versioned local audit matrix; never grant production signoff.

Reports contain no environment values or credentials. Raw pytest output stays
outside the checkout and must be reviewed before durable evidence retention.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = "release/audit-matrix.json"
BOUNDARIES = {
    "authentication", "tenant-isolation", "broker-worker", "legal-holds",
    "audit-integrity", "secrets-redaction", "untrusted-payloads", "put-delete",
    "move", "scan", "repair", "cleanup", "production-config", "artifacts",
}
PROFILES = ("local", "postgres", "services")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError(f"Nonfinite JSON value: {value}")

    value = json.loads(path.read_text(), object_pairs_hook=unique, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def snapshot(root: Path) -> dict:
    """Bind tracked and unignored inputs, excluding retained evidence itself."""
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root,
    ).decode().split("\0")
    hashes = {}
    for name in sorted(set(paths) - {""}):
        if name.startswith("docs/evidence/"):
            continue
        path = root / name
        if path.is_symlink():
            raise ValueError(f"Audit input is a symlink: {name}")
        hashes[name] = digest(path) if path.is_file() else None
    return {"head": git(root, "rev-parse", "HEAD"), "files": hashes}


def load_matrix(root: Path) -> dict:
    matrix = read_json(root / MATRIX)
    rows = matrix["boundaries"]
    if (matrix["schema_version"] != 1 or matrix["scope"] != "m5-pilot-v1"
            or {row["id"] for row in rows} != BOUNDARIES or len(rows) != len(BOUNDARIES)):
        raise ValueError("Incomplete or unsupported audit matrix")
    for row in rows:
        if not row["code"] or not row["staging_checks"] or not row["tests"]["local"]:
            raise ValueError(f"Missing review, tests, or staging checks: {row['id']}")
        if set(row["tests"]) != set(PROFILES):
            raise ValueError("Every boundary needs explicit profile coverage")
        for name in row["code"] + [t for tests in row["tests"].values() for t in tests]:
            path = root / name
            if (Path(name).is_absolute() or ".." in Path(name).parts
                    or path.is_symlink() or not path.is_file()
                    or not path.resolve().is_relative_to(root.resolve())):
                raise ValueError(f"Invalid audit reference: {name}")
        for tests in row["tests"].values():
            if len(tests) != len(set(tests)) or any(
                not t.startswith("tests/") or not t.endswith(".py") for t in tests
            ):
                raise ValueError("Audit tests must be unique repository test files")
    return matrix


def summarize(junit: Path, expected_files: list[str], returncode: int) -> dict:
    """Count leaf cases, not nested suite totals; missing/skipped is not pass."""
    counts = dict(tests=0, passed=0, failures=0, errors=0, skipped=0)
    modules = {name[:-3].replace("/", "."): name for name in expected_files}
    observed: set[str] = set()
    if not junit.is_file():
        return {**counts, "status": "failed", "missing_test_files": expected_files}
    if junit.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("JUnit report exceeds 64 MiB")
    raw = junit.read_text(encoding="utf-8")
    if "\x00" in raw or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", raw, re.IGNORECASE):
        raise ValueError("JUnit declarations/entities are forbidden")
    # Only bounded UTF-8 pytest XML without DTD/entity declarations is accepted.
    tree = ET.fromstring(raw)  # nosec B314
    unexpected = 0
    for case in tree.iter("testcase"):
        classname = case.get("classname", "")
        # pytest uses an empty class and the module name for collection skips
        # (for example an unconfigured MinIO module). Preserve that skip rather
        # than misclassifying it as an unrelated test result.
        if (not classname and case.find("skipped") is not None
                and case.get("name") in modules):
            classname = case.get("name", "")
        matches = [module for module in modules
                   if classname == module or classname.startswith(module + ".")]
        if not matches:
            unexpected += 1
            continue
        observed.update(modules[module] for module in matches)
        counts["tests"] += 1
        outcome = next((name for name in ("failure", "error", "skipped")
                        if case.find(name) is not None), None)
        key = {"failure": "failures", "error": "errors", "skipped": "skipped"}.get(outcome)
        counts[key or "passed"] += 1
    missing = sorted(set(expected_files) - observed)
    status = "passed"
    if returncode or counts["failures"] or counts["errors"] or unexpected:
        status = "failed"
    elif not counts["tests"] or counts["skipped"] or missing:
        status = "incomplete"
    return {**counts, "status": status, "missing_test_files": missing,
            "unexpected_cases": unexpected}


def run(root: Path, output: Path, profile: str) -> int:
    matrix = load_matrix(root)
    before = snapshot(root)
    output = output.resolve()
    if output.is_relative_to(root.resolve()):
        raise ValueError("Keep raw audit output outside the checkout; review before retention")
    output.mkdir(parents=True, exist_ok=False)
    tests = sorted({test for row in matrix["boundaries"] for test in row["tests"][profile]})
    command = [sys.executable, "-m", "pytest", "-q", "-o", "addopts=",
               "--import-mode=importlib",
               "--junitxml=" + str(output / "tests.xml"), *tests]
    started = datetime.now(timezone.utc).isoformat()
    environment = dict(os.environ)
    # Ambient selectors/plugins can silently omit cases inside an otherwise
    # observed file. Load only pytest's built-ins for this bounded campaign.
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTHONPATH"] = str(root)
    with (output / "pytest.log").open("w") as stream:
        completed = subprocess.run(command, cwd=root, env=environment,
                                   stdout=stream, stderr=subprocess.STDOUT)
    result = summarize(output / "tests.xml", tests, completed.returncode)
    unchanged = before == snapshot(root)
    if not unchanged:
        result["status"] = "invalidated"
    report = {
        "schema_version": 1, "scope": matrix["scope"], "matrix_revision": matrix["revision"],
        "profile": profile, "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "source": before, "inputs_unchanged_during_run": unchanged,
        "runtime": {"python": platform.python_version(), "system": platform.system(),
                    "machine": platform.machine()},
        "command": command, "returncode": completed.returncode, "result": result,
        "pytest_environment": "ambient options/plugins disabled; PYTHONPATH set to checkout",
        "boundaries": {row["id"]: row["tests"][profile] for row in matrix["boundaries"]},
        "artifacts": {p.name: digest(p) for p in sorted(output.iterdir()) if p.is_file()},
        "production_signoff": False,
        "qualification": "pending exact candidate, environment, staging checks and owner review",
        "hosted_ci": "skipped: user instruction; known GitHub billing/spending restriction",
    }
    (output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"result": result, "production_signoff": False, "report": str(output)}))
    return 0 if result["status"] == "passed" else 1


def verify(root: Path, output: Path) -> None:
    """Detect stale source and changed reports; checksums are not signatures."""
    report = read_json(output / "report.json")
    if report["schema_version"] != 1 or report["scope"] != "m5-pilot-v1":
        raise ValueError("Unsupported audit report identity")
    head = report["source"]["head"]
    if not isinstance(head, str) or not re.fullmatch(r"[a-f0-9]{40}", head):
        raise ValueError("Invalid source commit identity")
    subprocess.run(["git", "merge-base", "--is-ancestor", head, "HEAD"], cwd=root,
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    timestamps = [datetime.fromisoformat(report[field])
                  for field in ("started_at", "finished_at")]
    if any(t.utcoffset() is None for t in timestamps) or timestamps[1] < timestamps[0]:
        raise ValueError("Invalid audit interval")
    if not all(isinstance(report["runtime"][key], str) and report["runtime"][key]
               for key in ("python", "system", "machine")):
        raise ValueError("Missing runtime identity")
    if (report["source"]["files"] != snapshot(root)["files"]
            or not report["inputs_unchanged_during_run"]):
        raise ValueError("Audit inputs changed; scoped re-audit required")
    if any(p.is_symlink() or not p.is_file() for p in output.iterdir()):
        raise ValueError("Unexpected audit artifact type")
    actual = {p.name: digest(p) for p in output.iterdir()
              if p.is_file() and p.name != "report.json" and not p.is_symlink()}
    if actual != report["artifacts"] or set(actual) != {"pytest.log", "tests.xml"}:
        raise ValueError("Missing or changed audit artifacts")
    matrix = load_matrix(root)
    boundaries = {row["id"]: row["tests"][report["profile"]] for row in matrix["boundaries"]}
    if report["boundaries"] != boundaries or report["matrix_revision"] != matrix["revision"]:
        raise ValueError("Report does not cover the current audit matrix")
    expected = sorted({test for tests in boundaries.values() for test in tests})
    command = report["command"]
    if (not isinstance(command, list) or not all(isinstance(arg, str) for arg in command)
            or len(command) != 8 + len(expected) or not command[0]
            or command[1:7] != ["-m", "pytest", "-q", "-o", "addopts=", "--import-mode=importlib"]
            or not command[7].startswith("--junitxml=") or command[8:] != expected):
        raise ValueError("Invalid audit invocation")
    if summarize(output / "tests.xml", expected, report["returncode"]) != report["result"]:
        raise ValueError("Audit result disagrees with retained tests")
    if report["production_signoff"] is not False:
        raise ValueError("Local evidence cannot grant production signoff")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    runner = sub.add_parser("run")
    runner.add_argument("--profile", choices=PROFILES, default="local")
    runner.add_argument("--output", type=Path, required=True)
    verifier = sub.add_parser("verify")
    verifier.add_argument("--output", type=Path, required=True)
    sub.add_parser("check-matrix")
    args = parser.parse_args()
    try:
        if args.action == "run":
            return run(ROOT, args.output, args.profile)
        if args.action == "verify":
            verify(ROOT, args.output)
        else:
            load_matrix(ROOT)
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError,
            ET.ParseError) as exc:
        print(f"Audit rejected: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
