#!/usr/bin/env python3
"""Qualify the documented local operator path in a new, disposable workspace.

Run with a stdlib-only Python 3.10+ installation. Dependencies are installed into
a fresh virtual environment, from a captured copy of this checkout. This is a
local POSIX/SQLite drill, not a production backup tool or fault injector.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BUCKET = "operator-demo"
KEY = "demo/welcome.txt"
JOB = "operator-drill-expired-move"
PAYLOAD = b"CogniStore operator drill: this disposable object must survive recovery.\n"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def files(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): digest(path)
            for path in sorted(root.rglob("*")) if path.is_file()}


def database(path: Path) -> dict[str, Any]:
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise RuntimeError(f"SQLite integrity check failed: {integrity}")
        logical = "\n".join(connection.iterdump()).encode()
        return {
            "integrity_check": "ok",
            "logical_sha256": hashlib.sha256(logical).hexdigest(),
            "migration": connection.execute("SELECT version_num FROM alembic_version").fetchone()[0],
            "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        }


def configure(root: Path) -> None:
    root.mkdir()
    for tier in ("hot", "warm"):
        (root / tier).mkdir()
    write_json(root / "drivers.json", {
        "tiers": {tier: {"driver": "posix", "path": str(root / tier)}
                  for tier in ("hot", "warm")},
    })
    write_json(root / "tenants.json", {
        "tenants": {"demo": {"bucket": BUCKET, "prefix": "demo/", "tiers": ["hot", "warm"]}},
    })


class Drill:
    def __init__(self, workspace: Path, evidence: Path) -> None:
        self.workspace = workspace
        self.evidence = evidence
        self.commands: list[dict[str, Any]] = []
        self.checks: list[str] = []
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("COGNISTORE_") and key not in {"PYTHONPATH", "PYTHONHOME"}}
        self.env["COGNISTORE_SECURITY_PROFILE"] = "development"
        self.python = workspace / "venv" / "bin" / "python"
        self.cli = workspace / "venv" / "bin" / "cognistore"

    def require(self, condition: bool, label: str) -> None:
        if not condition:
            raise RuntimeError(f"Drill assertion failed: {label}")
        self.checks.append(label)

    def run(self, label: str, args: list[str], *, install: bool = False) -> str:
        print(f"operator-drill: {label}", flush=True)
        started = time.monotonic()
        result = subprocess.run(args, cwd=self.workspace, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        record: dict[str, Any] = {
            "label": label, "argv": args, "cwd": str(self.workspace),
            "exit_code": result.returncode, "seconds": round(time.monotonic() - started, 3),
        }
        if install:
            log = self.evidence.with_suffix(".install.log.gz")
            data = (result.stdout + result.stderr).encode()
            log.write_bytes(gzip.compress(data, mtime=0))
            record["log"] = {"file": log.name, "sha256": digest(log)}
        else:
            record.update(stdout=result.stdout, stderr=result.stderr)
        self.commands.append(record)
        if result.returncode:
            raise RuntimeError(f"{label} failed ({result.returncode}): {result.stderr[-2000:]}")
        return result.stdout

    def command(self, root: Path, label: str, *args: str) -> dict[str, Any]:
        result = json.loads(self.run(label, [
            str(self.cli), "--no-config", "--drivers", str(root / "drivers.json"),
            "--catalog-db", str(root / "catalog.sqlite3"), "--json", *args,
        ]))
        self.require(result.get("status") != "error", f"{label}: CLI returned no error")
        return result

    def consistency(self, root: Path, label: str, report: Path,
                    command: str = "consistency-scan", *extra: str) -> dict[str, Any]:
        return self.command(root, label, command, "--tenant", "demo", "--scope-config",
                            str(root / "tenants.json"), "--report", str(report), *extra)

    def run_all(self) -> dict[str, Any]:
        source = Path(__file__).resolve().parents[1]
        base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        status = subprocess.check_output(
            ["git", "status", "--short", "--untracked-files=normal"], cwd=source, text=True,
        )
        staged = self.workspace / "source"
        staged.mkdir()
        shutil.copytree(source / "cognistore", staged / "cognistore",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copy2(source / name, staged / name)
        manifest = files(staged)
        manifest["scripts/operator_drills.py"] = digest(Path(__file__).resolve())
        manifest_path = self.evidence.with_suffix(".source-sha256.json")
        write_json(manifest_path, manifest)
        self.run("create fresh virtual environment", [sys.executable, "-m", "venv",
                                                      str(self.workspace / "venv")])
        self.run("install captured source non-editably", [str(self.python), "-m", "pip",
                 "--isolated", "--disable-pip-version-check", "install", "--no-input",
                 "--progress-bar", "off", str(staged)], install=True)
        versions = self.run("capture installed distributions", [str(self.python), "-m", "pip",
                            "--disable-pip-version-check", "freeze", "--all"])
        requirements = self.evidence.with_suffix(".requirements.txt")
        requirements.write_text(versions, encoding="utf-8")
        runtime = json.loads(self.run("verify installed runtime", [str(self.python), "-I", "-c",
            "import cognistore,json,platform,sqlite3,sys; print(json.dumps({"
            "'python':sys.version,'platform':platform.platform(),'machine':platform.machine(),"
            "'sqlite':sqlite3.sqlite_version,'package_file':cognistore.__file__,"
            "'prefix':sys.prefix,'base_prefix':sys.base_prefix}))"]))
        self.require(runtime["prefix"] != runtime["base_prefix"], "runtime is in a fresh venv")
        self.require(Path(runtime["package_file"]).is_relative_to(self.workspace / "venv"),
                     "CLI uses the installed package, outside the checkout")
        self.require("include-system-site-packages = false" in
                     (self.workspace / "venv" / "pyvenv.cfg").read_text(),
                     "venv does not inherit system site packages")

        live = self.workspace / "live"
        configure(live)
        reports = self.workspace / "reports"
        reports.mkdir()
        input_file = self.workspace / "input.txt"
        input_file.write_bytes(PAYLOAD)
        download = self.workspace / "download.txt"
        self.command(live, "upload disposable object", "put", BUCKET, KEY, str(input_file))
        self.command(live, "retrieve disposable object", "get", BUCKET, KEY, str(download))
        self.require(download.read_bytes() == PAYLOAD, "first upload/download preserves all bytes")
        listing = self.command(live, "list hot tier", "ls-tier", "hot", BUCKET, "--prefix", "demo/")
        self.require(KEY in json.dumps(listing), "tier listing contains the uploaded object")
        self.command(live, "index the object inline", "catalog-scan", "hot", BUCKET,
                     "--prefix", "demo/", "--sync")
        clean = self.consistency(live, "scan first successful state", reports / "clean.sqlite3")
        self.require(clean["summary"]["complete"] and clean["summary"]["consistent"],
                     "first successful state is completely scanned and consistent")

        # All CLI subprocesses have exited; this isolated fixture has no services,
        # workers, scheduler, ingest clients, or direct writers. Keep it quiesced
        # through the complete catalog + tier-root capture.
        backup = self.workspace / "backup"
        backup.mkdir()
        before = database(live / "catalog.sqlite3")
        with sqlite3.connect((live / "catalog.sqlite3").as_uri() + "?mode=ro", uri=True) as src:
            with sqlite3.connect(backup / "catalog.sqlite3") as dst:
                src.backup(dst)
        for name in ("hot", "warm"):
            shutil.copytree(live / name, backup / name)
        for name in ("drivers.json", "tenants.json"):
            shutil.copy2(live / name, backup / name)
        backup_files = files(backup)
        self.require(database(backup / "catalog.sqlite3")["logical_sha256"] == before["logical_sha256"],
                     "SQLite backup preserves all logical catalog rows")
        for tier in ("hot", "warm"):
            self.require(files(live / tier) == files(backup / tier), f"backup preserves {tier} tier files")

        restored = self.workspace / "restored"
        configure(restored)
        shutil.copy2(backup / "catalog.sqlite3", restored / "catalog.sqlite3")
        for tier in ("hot", "warm"):
            shutil.copytree(backup / tier, restored / tier, dirs_exist_ok=True)
        self.require(database(restored / "catalog.sqlite3")["logical_sha256"] == before["logical_sha256"],
                     "restored catalog matches the backup before any writes")
        restore_scan = self.consistency(restored, "scan restored state", reports / "restored.sqlite3")
        self.require(restore_scan["summary"]["complete"] and restore_scan["summary"]["consistent"],
                     "restored catalog and tier roots are consistent")
        restored_download = self.workspace / "restored-download.txt"
        self.command(restored, "retrieve restored object", "get", BUCKET, KEY, str(restored_download))
        self.require(digest(restored_download) == digest(input_file), "restored object SHA-256 matches")

        # Deliberately synthesize one abandoned PREPARED journal entry. This does
        # not claim to reproduce a real killed worker, queue delivery, or outage.
        fixture = """import json,sys
from pathlib import Path
from cognistore.core.sqlite_catalog import SQLiteCatalog
from cognistore.drivers.posix_driver import PosixDriver
root=Path(sys.argv[1]); bucket=sys.argv[2]; key=sys.argv[3]; job=sys.argv[4]
driver=PosixDriver(root/'hot'); catalog=SQLiteCatalog(root/'catalog.sqlite3')
try:
    record=catalog.get(bucket,key)
    catalog.claim_move_job(job,src_tier='hot',dst_tier='warm',bucket=bucket,key=key,
        expected_size=record.size,source_metadata=driver.stat_object(bucket,key),
        owner_id='synthetic-abandoned-worker',now='2020-01-01T00:00:00.000000Z',
        lease_expires_at='2020-01-01T00:01:00.000000Z')
    print(json.dumps({'synthetic':True,'job':job,'state':'prepared','lease':'expired'}))
finally:
    catalog.close()
"""
        self.run("inject synthetic expired pending move", [str(self.python), "-I", "-c", fixture,
                 str(live), BUCKET, KEY, JOB])
        incident_report = reports / "incident.sqlite3"
        incident = self.consistency(live, "detect abandoned move", incident_report)
        self.require(incident["summary"]["complete"] and
                     incident["summary"]["reason_counts"].get("partial_job", 0) == 1,
                     "scanner detects the synthetic partial move")
        before_preview = {"live": files(live), "report": digest(incident_report)}
        preview = self.consistency(live, "preview repair without writes", incident_report,
                                   "consistency-repair", "--dry-run")
        self.require(preview["summary"]["plan_only"] and
                     preview["summary"]["counts"] == {"planned": 1},
                     "dry-run plans exactly one eligible move")
        self.require(before_preview == {"live": files(live), "report": digest(incident_report)},
                     "dry-run changes no catalog, tier files, or incident report bytes")
        plan = self.consistency(live, "record reviewed repair plan", incident_report,
                               "consistency-repair")
        self.require(plan["summary"]["counts"] == {"planned": 1}, "audited plan remains eligible")
        self.require(files(live) == before_preview["live"], "audited plan preserves source state")
        repaired = self.consistency(live, "explicitly execute eligible repair", incident_report,
                                   "consistency-repair", "--enable-repair")
        self.require(repaired["summary"]["counts"] == {"completed": 1}, "enabled repair completes move")
        repeated = self.consistency(live, "repeat repair idempotently", incident_report,
                                   "consistency-repair", "--enable-repair")
        self.require(repeated["summary"]["counts"] == {"resolved": 1}, "repeat repair is already resolved")
        verify = """import json,sys
from pathlib import Path
from cognistore.core.sqlite_catalog import SQLiteCatalog
catalog=SQLiteCatalog(Path(sys.argv[1])/'catalog.sqlite3',read_only=True)
try:
    jobs=catalog.list_move_jobs(); record=catalog.get(sys.argv[2],sys.argv[3])
    print(json.dumps({'jobs':[{'id':j.idempotency_key,'state':j.state.value} for j in jobs],
                      'tier':record.tier}))
finally:
    catalog.close()
"""
        state = json.loads(self.run("verify original move journal", [str(self.python), "-I", "-c",
                                     verify, str(live), BUCKET, KEY]))
        self.require(state == {"jobs": [{"id": JOB, "state": "completed"}], "tier": "warm"},
                     "repair reuses the original job and commits the warm placement")
        self.require(not (live / "hot" / BUCKET / KEY).exists(), "verified source cleanup completed")
        self.require(digest(live / "warm" / BUCKET / KEY) == digest(input_file),
                     "repaired destination SHA-256 matches original bytes")
        final_scan = self.consistency(live, "scan repaired state", reports / "repaired.sqlite3")
        self.require(final_scan["summary"]["complete"] and final_scan["summary"]["consistent"],
                     "fresh scan confirms repaired state is consistent")
        self.require(files(backup) == backup_files, "recovery drill leaves the backup immutable")
        return {
            "schema_version": 1, "status": "passed", "completed_at": datetime.now(timezone.utc).isoformat(),
            "source": {"base_revision": base, "git_status_at_capture": status,
                       "manifest": manifest_path.name, "manifest_sha256": digest(manifest_path),
                       "note": "Captured package/build inputs installed non-editably; base commit alone "
                               "does not reproduce any dirty files listed above."},
            "runtime": runtime, "harness_python": sys.version, "host": platform.platform(),
            "environment": {"COGNISTORE_SECURITY_PROFILE": "development", "config": "--no-config",
                            "inherited_cognistore_variables": "removed", "PYTHONPATH": "removed"},
            "requirements": {"file": requirements.name, "sha256": digest(requirements)},
            "fixture": {"bucket": BUCKET, "key": KEY, "size": len(PAYLOAD), "sha256": digest(input_file)},
            "backup": {"method": "quiesced SQLite backup API plus both complete POSIX tier trees",
                       "quiescence": "Only synchronous CLI subprocesses; all exited before capture; "
                                     "no API, worker, scheduler, or other fixture writer was started.",
                       "catalog": before, "file_sha256": backup_files},
            "incident": {"type": "synthetic expired prepared move", "idempotency_key": JOB,
                         "post_repair_state": state},
            "limits": ["Local POSIX/SQLite development profile; not production qualification.",
                       "No PostgreSQL, NATS, Kubernetes, cloud backend, TLS, OIDC, or encryption drill.",
                       "New venv and isolated config on the existing OS account, not a new OS user.",
                       "Synthetic abandoned journal; no live worker was killed.",
                       "Unpinned dependency resolution may change; installed versions are archived."],
            "checks": self.checks, "commands": self.commands,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path, help="New disposable directory")
    parser.add_argument("--evidence", required=True, type=Path, help="New JSON evidence file")
    args = parser.parse_args()
    workspace, evidence = args.workspace.resolve(), args.evidence.resolve()
    artifacts = [evidence, *(evidence.with_suffix(suffix) for suffix in
                             (".install.log.gz", ".requirements.txt", ".source-sha256.json"))]
    if workspace.exists() or any(path.exists() for path in artifacts):
        parser.error("workspace and evidence artifacts must be new; choose unused paths")
    workspace.mkdir(parents=True, mode=0o700)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    drill = Drill(workspace, evidence)
    try:
        result = drill.run_all()
    except Exception as error:
        write_json(evidence, {"status": "failed", "error": str(error),
                              "checks": drill.checks, "commands": drill.commands})
        raise
    write_json(evidence, result)
    print(f"operator-drill: passed {len(drill.checks)} checks; evidence: {evidence}")


if __name__ == "__main__":
    main()
