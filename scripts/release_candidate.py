#!/usr/bin/env python3
"""Assemble and verify an offline, source-bound M5 candidate (Python 3.12+)."""

from __future__ import annotations

import argparse
import email
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REGRESSIONS = (
    "tests/unit/test_tenant_namespace_aliases.py",
    "tests/unit/test_tenant_namespace_api.py",
    "tests/unit/test_object_mutation_fence.py",
    "tests/unit/test_api_object_mutations.py",
)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def wheel_lock(wheels: Path, output: Path) -> None:
    """Only the exact retained binary artifacts may satisfy installation."""
    packages = {}
    for path in sorted(wheels.iterdir()):
        if path.suffix != ".whl" or path.is_symlink():
            raise ValueError(f"Expected a regular wheel: {path.name}")
        with zipfile.ZipFile(path) as archive:
            metadata = [n for n in archive.namelist()
                        if n.endswith(".dist-info/METADATA") and len(n.split("/")) == 2]
            if len(metadata) != 1:
                raise ValueError(f"Invalid wheel metadata: {path.name}")
            info = email.message_from_bytes(archive.read(metadata[0]))
        name = re.sub(r"[-_.]+", "-", info["Name"]).lower()
        version = info["Version"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) or not re.fullmatch(
            r"[a-zA-Z0-9.!+_-]+", version
        ):
            raise ValueError("Unsafe wheel identity")
        if name in packages:
            raise ValueError(f"Duplicate distribution: {name}")
        packages[name] = f"{name}=={version} --hash=sha256:{digest(path)}\n"
    if not packages:
        raise ValueError("Cannot freeze an empty wheelhouse")
    output.write_text("".join(packages[name] for name in sorted(packages)))


def bundle_files(bundle: Path) -> dict[str, str]:
    files = {}
    for path in sorted(bundle.rglob("*")):
        if path.is_symlink():
            raise ValueError("Bundle symlinks are forbidden")
        if path.is_file() and path != bundle / "SHA256SUMS":
            name = path.relative_to(bundle).as_posix()
            if "\n" in name or "\r" in name:
                raise ValueError("Invalid bundle path")
            files[name] = digest(path)
    return files


def seal(bundle: Path) -> None:
    (bundle / "SHA256SUMS").write_text(
        "".join(f"{value}  {name}\n" for name, value in bundle_files(bundle).items())
    )


def verify(bundle: Path) -> None:
    expected = {}
    for line in (bundle / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  (.+)", line)
        if not match:
            raise ValueError("Invalid checksum record")
        value, name = match.groups()
        if name in expected or Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("Invalid or duplicate checksum path")
        expected[name] = value
    actual = bundle_files(bundle)
    if not expected or expected != actual:
        raise ValueError("Bundle is incomplete, changed, or contains unrecorded files")
    manifest = json.loads((bundle / "manifest.json").read_text())
    for name, value in manifest["artifacts"].items():
        if actual.get(name) != value:
            raise ValueError(f"Manifest artifact mismatch: {name}")
    for required in ("image.tar", "runtime.lock", "runtime.txt", "source.tar"):
        if required not in manifest["artifacts"]:
            raise ValueError(f"Missing required artifact: {required}")


def capture(command: list[str], *, cwd: Path | None = None) -> str:
    return subprocess.check_output(command, cwd=cwd, text=True).strip()


def run(command: list[str], report: Path, *, cwd: Path | None = None) -> int:
    print(f"Running {report.name}", flush=True)
    with report.open("w") as stream:
        stream.write(json.dumps(command) + "\n")
        stream.flush()
        return subprocess.run(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT).returncode


def required(command: list[str], report: Path, *, cwd: Path | None = None) -> None:
    if run(command, report, cwd=cwd):
        raise RuntimeError(f"Command failed; see {report}")


def hosted_passed(evidence: dict | None, source: str) -> bool:
    """Validate supplied run metadata, not a signature or a qualification approval."""
    if not evidence or evidence.get("source_sha") != source or evidence.get("status") != "passed":
        return False
    gates = evidence.get("gates", [])
    if len(gates) != 3 or {g.get("workflow") for g in gates} != {
        "ci.yml", "kubernetes.yml", "terraform.yml",
    }:
        return False
    return all(
        gate.get("status") == "passed" and gate.get("conclusion") == "success"
        and gate.get("head_sha") == source and gate.get("required") is True
        and gate.get("event") in {"push", "workflow_dispatch"}
        and isinstance(gate.get("run_id"), int) and gate["run_id"] > 0
        and isinstance(gate.get("run_attempt"), int) and gate["run_attempt"] > 0
        and gate.get("url", "").startswith("https://github.com/melliott18/CogniStore/actions/runs/")
        for gate in gates
    )


def build(output: Path, allow_unmerged: bool, hosted_evidence: Path | None) -> None:
    root = Path(capture(["git", "rev-parse", "--show-toplevel"]))
    if capture(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root):
        raise ValueError("Candidate source must be clean, including untracked files")
    source = capture(["git", "rev-parse", "HEAD"], cwd=root)
    main = capture(["git", "rev-parse", "origin/main"], cwd=root)
    if source != main and not allow_unmerged:
        raise ValueError("Candidate source must equal freshly fetched origin/main")
    # Assembly needs Python 3.12; helpers remain importable in the supported 3.10 test matrix.
    import tomllib

    version = tomllib.loads(capture(["git", "show", f"{source}:pyproject.toml"], cwd=root))["project"]["version"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+rc[0-9]+", version):
        raise ValueError("Candidate package must have an explicit release-candidate version")
    # Read only the committed sanitized selection; never collect host environment/secrets.
    profile = json.loads(capture(["git", "show", f"{source}:release/pilot.json"], cwd=root))
    if profile["platform"] != "linux/amd64" or profile["runtime_extras"]:
        raise ValueError("This assembler supports only the selected base-runtime amd64 pilot")
    if profile["ask"] != {
        "mode": "metadata", "keyword_provider": None, "vector_provider": None,
        "answer_provider": None, "models": [],
    } or profile["embedding_configuration"] is not None:
        raise ValueError("Provider changes require a new qualification implementation")
    output = output.resolve()
    if output.is_relative_to(root):
        raise ValueError("Keep candidate output outside the source checkout")
    output.mkdir(parents=True, exist_ok=False)
    reports = output / "reports"
    reports.mkdir()
    blockers = [
        "Named owner acceptance of specification #159 is pending.",
        "Rendered deployment configuration, environment and service workflows #161 are pending.",
        "Native/image vulnerability scan and reviewed findings disposition are pending.",
        "Registry transfer and verified immutable registry manifest digest are pending.",
        "Production qualification and pilot approval #162-#166 are separate outstanding gates.",
    ]
    if allow_unmerged or source != main:
        blockers.append("Unmerged/PR validation is not a clean-main release candidate.")
    hosted = None
    if hosted_evidence:
        hosted = json.loads(hosted_evidence.read_text())
        if hosted.get("source_sha") != source:
            raise ValueError("Hosted evidence source does not match the candidate")
        write_json(reports / "hosted-ci.json", hosted)
    if not hosted_passed(hosted, source):
        blockers.append("Applicable same-source hosted gates are absent or not passing (#158).")
    with (output / "source.tar").open("wb") as stream:
        subprocess.run(["git", "archive", "--format=tar", source], cwd=root, stdout=stream,
                       check=True)
    tag = f"cognistore:rc-{source[:12]}"
    statuses = {}
    with tempfile.TemporaryDirectory(prefix="cognistore-candidate-") as directory:
        context = Path(directory)
        with tarfile.open(output / "source.tar") as archive:
            archive.extractall(context, filter="data")
        shutil.copyfile(context / "release/pilot.json", output / "pilot.json")
        shutil.copyfile(context / "release/Dockerfile", output / "Dockerfile")
        specification_digest = digest(context / "docs/production_pilot.md")
        migrations = {
            path.relative_to(context).as_posix(): digest(path)
            for path in sorted((context / "cognistore/db/migrations").rglob("*"))
            if path.is_file()
        }
        # The main development Dockerfile's allowlist intentionally excludes release files.
        (context / ".dockerignore").unlink(missing_ok=True)
        base_command = [
            "docker", "build", "--platform", "linux/amd64", "--file", "release/Dockerfile",
            "--build-arg", f"SOURCE_SHA={source}",
            "--build-arg", f"PACKAGE_VERSION={version}",
        ]
        for stage in ("assembly", "runtime", "qualification"):
            stage_tag = tag if stage == "runtime" else f"{tag}-{stage}"
            required([*base_command, "--target", stage, "--tag", stage_tag, "."],
                     reports / f"build-{stage}.log", cwd=context)
        container = capture(["docker", "create", f"{tag}-assembly"])
        try:
            subprocess.run(["docker", "cp", f"{container}:/bundle/.", str(output)], check=True)
        finally:
            subprocess.run(["docker", "rm", container], check=True, stdout=subprocess.DEVNULL)

        image = json.loads(capture(["docker", "image", "inspect", tag]))[0]
        if image["Architecture"] != "amd64" or image["Os"] != "linux":
            raise ValueError("Wrong candidate platform")
        subprocess.run(["docker", "save", "--output", str(output / "image.tar"), image["Id"]],
                       check=True)
        runtime = ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none"]
        required([*runtime, "--entrypoint", "python", tag, "-m", "pip", "check"],
                 reports / "pip-check.log")
        # stdout inventories exclude Docker host config, credentials and environment values.
        write_json(output / "python-inventory.json", json.loads(capture([
            *runtime, "--entrypoint", "python", tag, "-m", "pip", "list", "--format=json",
        ])))
        (output / "native-inventory.tsv").write_text(capture([
            *runtime, "--entrypoint", "dpkg-query", tag, "-W",
            "-f=${Package}\t${Version}\t${Architecture}\n",
        ]) + "\n")
        # Mount reports into disposable containers; runtime itself remains non-root.
        reports.chmod(0o777)
        statuses["installed_smoke"] = run([
            *runtime, "--mount", f"type=bind,source={context / 'scripts/release_smoke.py'},target=/smoke.py,readonly",
            "--mount", f"type=bind,source={reports},target=/reports", "--entrypoint", "python",
            "-e", "COGNISTORE_SECURITY_PROFILE=development", tag,
            "-I", "/smoke.py", "--output", "/reports/smoke.json", "--source-root", "/source",
        ], reports / "smoke.log")
        qualifier = [
            "docker", "run", "--rm", "--platform", "linux/amd64",
            "--mount", f"type=bind,source={reports},target=/reports",
        ]
        statuses["regressions"] = run([
            *qualifier, "--network", "none", f"{tag}-qualification", "python", "-m", "pytest",
            *REGRESSIONS, "--junitxml=/reports/regressions.xml",
        ], reports / "regressions.log")
        statuses["bandit"] = run([
            *qualifier, "--network", "none", f"{tag}-qualification", "python", "-m", "bandit",
            "-c", "/qualification/pyproject.toml", "-r", "/scan/cognistore", "-ll", "-ii",
            "-f", "json", "-o", "/reports/bandit.json",
        ], reports / "bandit.log")
        # Audit the frozen runtime closure; never ask the scanner to resolve the project again.
        statuses["dependency_audit"] = run([
            *qualifier, f"{tag}-qualification", "python", "-m", "pip_audit",
            "--no-deps", "--disable-pip", "--strict", "-r", "/bundle/dependencies.txt",
            "--progress-spinner=off", "--format=json", "--output=/reports/pip-audit.json",
        ], reports / "pip-audit.log")

    for name, code in statuses.items():
        if code:
            blockers.append(f"{name} failed (exit {code}); inspect the retained report.")
    manifest = {
        "schema_version": 1,
        "status": "assembled-unqualified",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candidate_id": f"{version}-{source[:12]}-{image['Id'].split(':')[1][:12]}",
        "source": {"commit": source, "tree": capture(["git", "rev-parse", f"{source}^{{tree}}"], cwd=root),
                   "origin_main": main, "clean": True, "unmerged_validation": allow_unmerged},
        "image": {"local_tag": tag, "image_id": image["Id"], "digest_kind": "docker-config",
                  "registry_manifest_digest": None, "platform": "linux/amd64",
                  "archive": "image.tar", "base": (output / "Dockerfile").read_text().splitlines()[1]},
        "package": {"name": "cognistore", "version": version, "runtime_extras": []},
        "configuration": {"selection_sha256": digest(output / "pilot.json"),
                          "specification_sha256": specification_digest,
                          "deployment_revision": None, "kind": profile["configuration_kind"]},
        "providers": profile["ask"],
        "migrations": migrations,
        "checks": statuses,
        "release_blockers": blockers,
        "build_instructions": "docs/release_candidate.md in source.tar; rebuild creates a new candidate",
        "scope": "Installed POSIX/SQLite smoke and fixes' regressions; external services and production gates excluded",
        "artifacts": bundle_files(output),
    }
    write_json(output / "manifest.json", manifest)
    seal(output)
    verify(output)
    print(f"Frozen bundle: {output}; status: assembled-unqualified", flush=True)
    if any(statuses.values()):
        raise RuntimeError("Candidate checks failed; sealed evidence retained")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    assemble = commands.add_parser("build")
    assemble.add_argument("--output", type=Path, required=True)
    assemble.add_argument("--allow-unmerged", action="store_true")
    assemble.add_argument("--hosted-evidence", type=Path)
    check = commands.add_parser("verify")
    check.add_argument("--bundle", type=Path, required=True)
    lock = commands.add_parser("lock")
    lock.add_argument("--wheels", type=Path, required=True)
    lock.add_argument("--output", type=Path, required=True)
    constraints = commands.add_parser("constraints")
    constraints.add_argument("--lock", type=Path, required=True)
    constraints.add_argument("--output", type=Path, required=True)
    constraints.add_argument("--exclude-project", action="store_true")
    args = parser.parse_args()
    if args.command == "build":
        build(args.output, args.allow_unmerged, args.hosted_evidence)
    elif args.command == "verify":
        verify(args.bundle)
        print("Bundle checksums verified (integrity only, not release approval).")
    elif args.command == "lock":
        wheel_lock(args.wheels, args.output)
    else:
        args.output.write_text("".join(line.split(" --hash=")[0] + "\n"
                                      for line in args.lock.read_text().splitlines()
                                      if not (args.exclude_project and line.startswith("cognistore=="))))


if __name__ == "__main__":
    main()
