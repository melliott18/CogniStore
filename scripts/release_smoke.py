#!/usr/bin/env python3
"""Smoke an installed base wheel from outside the source checkout.

This disposable SQLite/POSIX development fixture is reduced packaging evidence,
not production qualification. No pytest, model provider, credentials, or broker
is required. Run with ``python -I release_smoke.py --output report.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import logging
import os
import platform
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PAYLOAD = b"CogniStore release smoke: synthetic quarterly report.\n"
BUCKET = "release-smoke"
KEY = "quarterly-report.txt"
LIMITATIONS = [
    "Disposable single-process SQLite and two POSIX roots; warm is not S3.",
    "ASGI in-process transport; no network, TLS, OIDC, tenant isolation or browser qualification.",
    "Policy execution uses the real synchronous runner; no broker or worker delivery is exercised.",
    "PostgreSQL/pgvector, native S3 and NATS JetStream remain separate external-service gates.",
    "No load, durability, recovery, encryption attestation or production qualification claim.",
]


class SmokeFailure(RuntimeError):
    """Only fixed, non-sensitive check names are safe to publish."""


class Checks:
    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []
        self.current = "initialization"
        self.requests = 0

    def require(self, condition: bool, name: str) -> None:
        self.current = name
        self.results.append({"name": name, "passed": bool(condition)})
        if not condition:
            raise SmokeFailure(name)

    async def request(self, client: Any, method: str, path: str, status: int, **kwargs: Any) -> Any:
        parts = path.strip("/").split("/")
        resource = parts[1] if parts[0] == "v1" else parts[0]
        self.current = f"http.{method.lower()}.{resource}"
        self.requests += 1
        response = await client.request(method, path, **kwargs)
        self.require(response.status_code == status, f"{self.current}.{status}")
        return response


def installed_identity(checks: Checks, source_root: Path | None) -> dict[str, Any]:
    import cognistore

    distribution = importlib.metadata.distribution("cognistore")
    imported = Path(cognistore.__file__).resolve()
    expected = Path(distribution.locate_file("cognistore/__init__.py")).resolve()
    recorded = {str(path) for path in distribution.files or ()}
    checks.require("cognistore/__init__.py" in recorded, "package.record_contains_module")
    checks.require(imported == expected, "package.import_matches_distribution")
    direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
    checks.require(not direct_url.get("dir_info", {}).get("editable", False), "package.noneditable")
    checks.require(not (imported.parent.parent / "pyproject.toml").exists(), "package.not_checkout")
    working = Path.cwd().resolve()
    checks.require(not any(
        (parent / "pyproject.toml").is_file() and (parent / "cognistore").is_dir()
        for parent in (working, *working.parents)
    ), "cwd.not_checkout")
    if source_root is not None:
        source_root = source_root.resolve()
        checks.require(not imported.is_relative_to(source_root), "package.outside_source_root")
        checks.require(not Path.cwd().resolve().is_relative_to(source_root), "cwd.outside_source_root")
    return {
        "name": distribution.metadata["Name"],
        "version": distribution.version,
        "import_location": "installed-distribution",
        "module_sha256": hashlib.sha256(imported.read_bytes()).hexdigest(),
        "source_root_checked": source_root is not None,
    }


async def exercise(root: Path, checks: Checks, report: dict[str, Any]) -> None:
    checks.current = "runtime.imports"
    import httpx

    checks.current = "native.libmagic_import"
    import magic

    checks.current = "runtime.application_imports"

    from cognistore.api.app import create_app
    from cognistore.api.gateway import CogniStoreGateway
    from cognistore.core.mover import Mover
    from cognistore.core.policy_factory import build_policy
    from cognistore.core.policy_runner import PolicyRunner
    from cognistore.db import SQLCatalog
    from cognistore.db.migrations import MigrationManager
    from cognistore.drivers.driver_loader import load_drivers
    from cognistore.policy_feature_runtime import load_policy_feature_loader

    checks.current = "native.libmagic"
    checks.require(magic.from_buffer(PAYLOAD, mime=True) == "text/plain", "native.libmagic_detection")
    report["native_libmagic"] = {"version": magic.version(), "detected_mime": "text/plain"}
    config_path = root / "drivers.json"
    config_path.write_text(json.dumps({"tiers": {
        tier: {"driver": "posix", "path": str(root / tier)} for tier in ("hot", "warm")
    }}), encoding="utf-8")
    drivers = load_drivers(str(config_path))
    database = root / "catalog.sqlite3"
    checks.current = "catalog.migration"
    with SQLCatalog(database) as catalog:
        with sqlite3.connect(database) as connection:
            current = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        heads = MigrationManager().heads()
        checks.require(len(heads) == 1 and current == heads[0], "catalog.at_packaged_migration_head")
        report["migration"] = {"current": current, "packaged_heads": list(heads)}
        features = load_policy_feature_loader(config_path, catalog)
        gateway = CogniStoreGateway(catalog, drivers, feature_loader=features)
        checks.require(features.embedding_provider is None, "providers.policy_embedding_absent")
        checks.require(
            gateway.ask_service.keyword is None and gateway.ask_service.vector is None
            and gateway.ask_service.answer_provider is None,
            "providers.optional_ask_providers_absent",
        )
        report["providers"] = {
            "metadata": type(gateway.ask_service.metadata).__name__,
            "keyword": None, "vector": None, "embedding": None, "answer": None,
            "policy": "simple", "sample_providers": False,
        }
        app = create_app(gateway)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://release-smoke.invalid",
            ) as client:
                object_path = f"/v1/objects/hot/{BUCKET}/{KEY}"
                uploaded = await checks.request(
                    client, "PUT", object_path, 201, content=PAYLOAD,
                    headers={"Content-Type": "text/plain"},
                )
                checks.require(uploaded.json()["size"] == len(PAYLOAD), "storage.upload_size")
                head = await checks.request(client, "HEAD", object_path, 200)
                checks.require(
                    head.content == b"" and head.headers["Content-Length"] == str(len(PAYLOAD))
                    and head.headers["ETag"] == uploaded.headers["ETag"], "storage.head",
                )
                downloaded = await checks.request(client, "GET", object_path, 200)
                checks.require(downloaded.content == PAYLOAD, "storage.download_exact_bytes")
                report["fixture_sha256"] = hashlib.sha256(downloaded.content).hexdigest()
                for label, requested, start, end in (
                    ("closed", "bytes=1-7", 1, 7),
                    ("open", "bytes=8-", 8, len(PAYLOAD) - 1),
                    ("suffix", "bytes=-5", len(PAYLOAD) - 5, len(PAYLOAD) - 1),
                ):
                    partial = await checks.request(
                        client, "GET", object_path, 206, headers={"Range": requested},
                    )
                    checks.require(
                        partial.content == PAYLOAD[start:end + 1]
                        and partial.headers["Content-Range"] == f"bytes {start}-{end}/{len(PAYLOAD)}",
                        f"storage.range_{label}",
                    )
                invalid = await checks.request(
                    client, "GET", object_path, 416, headers={"Range": "bytes=9999-"},
                )
                checks.require(invalid.json()["error"]["code"] == "range_not_satisfiable",
                               "storage.invalid_range")
                ask = await checks.request(client, "POST", "/v1/ask", 200, json={
                    "text": "quarterly report", "filters": {"bucket": BUCKET},
                })
                body = ask.json()
                checks.require(body["mode"] == "metadata" and body["active_signals"] == ["metadata"],
                               "ask.metadata_only")
                checks.require(
                    len(body["results"]) == 1 and body["results"][0]["citation"]["key"] == KEY
                    and body["results"][0]["passages"] == []
                    and body["answer"] is None and body["generation_status"] == "not_requested",
                    "ask.metadata_citation",
                )
                states = {item["component"]: item["state"] for item in body["providers"]}
                checks.require(states == {
                    "metadata": "succeeded", "keyword": "missing", "vector": "missing",
                    "generation": "not_requested",
                }, "ask.provider_diagnostics")
                no_match = await checks.request(client, "POST", "/v1/ask", 200, json={
                    "text": "nonexistent-zebra-983471", "filters": {"bucket": BUCKET},
                })
                checks.require(no_match.json()["results"] == [], "ask.no_match")
                generation = await checks.request(client, "POST", "/v1/ask", 200, json={
                    "text": "quarterly report", "synthesize": True,
                })
                checks.require(generation.json()["answer"] is None
                               and generation.json()["generation_status"] == "provider_missing",
                               "ask.explicit_missing_answer_provider")
                policy_config = {"policy": "simple", "threshold": 1,
                                 "allowed_tiers": ["hot", "warm"]}
                preview = await checks.request(client, "POST", "/v1/policies/evaluate", 200, json={
                    "bucket": BUCKET, "key": KEY, "config": policy_config,
                })
                checks.require(preview.json()["action"] == "move"
                               and preview.json()["destination_tier"] == "warm",
                               "policy.preview_move")
                checks.require(catalog.get(BUCKET, KEY).tier == "hot"
                               and drivers["hot"].get_object(BUCKET, KEY) == PAYLOAD,
                               "policy.preview_does_not_move")
                rejected = await checks.request(
                    client, "POST", "/v1/actions/policy-runs", 503,
                    json={"bucket": BUCKET, "config": policy_config},
                )
                checks.require(rejected.json()["error"]["code"] == "backend_unavailable",
                               "actions.missing_queue_fails_closed")
                checks.current = "policy.synchronous_execution"
                runner = PolicyRunner(
                    catalog, drivers, Mover(drivers, catalog),
                    build_policy("simple", threshold=1, allowed_tiers=["hot", "warm"]),
                    feature_loader=features,
                )
                actions = await asyncio.to_thread(runner.run_once, BUCKET)
                checks.require(len(actions) == 1 and actions[0].status == "completed"
                               and catalog.get(BUCKET, KEY).tier == "warm",
                               "policy.real_move_completed")
                moved_path = f"/v1/objects/warm/{BUCKET}/{KEY}"
                moved = await checks.request(client, "GET", moved_path, 200)
                checks.require(moved.content == PAYLOAD, "policy.destination_exact_bytes")
                await checks.request(client, "GET", object_path, 404)
                await checks.request(client, "DELETE", moved_path, 204)
                await checks.request(client, "GET", moved_path, 404)
                checks.require(catalog.get(BUCKET, KEY) is None, "storage.delete_catalog_removed")
                ui = await checks.request(client, "GET", "/ui/", 200)
                checks.require("<html" in ui.text.lower(), "package.ui_html_present")
        checks.require(not any(name.startswith("cognistore.samples") for name in sys.modules),
                       "providers.sample_modules_not_loaded")
        report["counts"] = {"http_requests": checks.requests, "objects_uploaded": 1,
                            "range_cases": 4, "ask_queries": 3, "policy_moves": len(actions)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="sanitized JSON report path")
    parser.add_argument("--source-root", type=Path, help="reject import or working directory here")
    args = parser.parse_args(argv)
    started = time.monotonic()
    checks = Checks()
    report: dict[str, Any] = {
        "schema_version": 1, "scope": "installed-base-runtime-local-smoke",
        "production_qualified": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "python": {"version": platform.python_version(), "implementation": platform.python_implementation(),
                   "system": platform.system(), "machine": platform.machine()},
        "configuration": {"security_profile": "development", "catalog": "sqlite",
                          "storage": {"hot": "posix", "warm": "posix"}, "queue": None},
        "limitations": LIMITATIONS,
    }
    # A fixture must never inherit the operator's production endpoints, providers or attestations.
    for key in tuple(os.environ):
        if key.startswith("COGNISTORE_"):
            del os.environ[key]
    os.environ["COGNISTORE_SECURITY_PROFILE"] = "development"
    logging.disable(logging.CRITICAL)
    try:
        report["package"] = installed_identity(checks, args.source_root)
        with tempfile.TemporaryDirectory(prefix="cognistore-release-smoke-") as temporary:
            asyncio.run(exercise(Path(temporary), checks, report))
        report["status"] = "passed"
    except Exception as exc:
        # Never include exception messages, traceback paths, response bodies or environment values.
        report["status"] = "failed"
        report["failure"] = {"check": checks.current, "exception_type": type(exc).__name__}
    report["checks"] = checks.results
    report["check_count"] = len(checks.results)
    report["passed_count"] = sum(item["passed"] for item in checks.results)
    report["http_request_count"] = checks.requests
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"installed runtime smoke: {report['status']} ({report['passed_count']} checks passed)")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
