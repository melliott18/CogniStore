"""Operator-scoped consistency scans, exports, and explicitly enabled repair."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

import yaml

from cognistore.core.consistency import (
    ConsistencyScanner,
    ScanScope,
    export_report,
    report_summary,
    validate_consistency_bucket,
)
from cognistore.db import catalog_locator_is_persistent, open_catalog, sqlite_catalog_path
from cognistore.db.engine import normalize_database_url
from cognistore.drivers.driver_loader import load_drivers

COMMANDS = {"consistency-scan", "consistency-export", "consistency-repair"}
_SQLITE_SUFFIXES = ("", "-wal", "-shm", "-journal")


def _positive_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return result


def _positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def add_commands(command: Callable[..., argparse.ArgumentParser]) -> None:
    scan = command("consistency-scan", help="Compare a tenant's catalog and storage read-only")
    scan.add_argument("--tenant", required=True, help="Tenant from the trusted scope configuration")
    scan.add_argument("--scope-config", required=True, type=Path, help="Operator-managed tenant bindings")
    scan.add_argument("--prefix", help="Narrow the configured tenant key prefix")
    scan.add_argument("--tier", action="append", help="Narrow the configured tiers (repeatable)")
    scan.add_argument("--report", required=True, type=Path, help="Separate SQLite report/checkpoint path")
    scan.add_argument("--resume", action="store_true", help="Continue an existing matching report")
    scan.add_argument("--max-items", type=_positive_int, help="Pause after this many work items")
    scan.add_argument("--page-size", type=_positive_int, default=100, help="Inventory page size (default: 100)")
    scan.add_argument("--requests-per-second", type=_positive_float, default=20.0)
    scan.add_argument("--bytes-per-second", type=_positive_float, default=8 * 1024 * 1024)
    export = command("consistency-export", help="Export one tenant's consistency report as JSONL")
    export.add_argument("--tenant", required=True)
    export.add_argument("--scope-config", required=True, type=Path)
    export.add_argument("--report", required=True, type=Path)
    export.add_argument("--output", required=True, type=Path, help="New JSONL path; existing files are refused")
    repair = command("consistency-repair", help="Plan or explicitly enable safe repair of a completed scan")
    repair.add_argument("--tenant", required=True)
    repair.add_argument("--scope-config", required=True, type=Path)
    repair.add_argument("--report", required=True, type=Path, help="Completed consistency scan report")
    repair.add_argument("--prefix", help="Original scan prefix within the configured tenant binding")
    repair.add_argument("--tier", action="append", help="Original scan tiers (repeatable)")
    repair.add_argument(
        "--enable-repair", action="store_true",
        help="Resume eligible existing moves; default only plans and audits decisions",
    )
    repair.add_argument("--requests-per-second", type=_positive_float, default=20.0)
    repair.add_argument("--bytes-per-second", type=_positive_float, default=8 * 1024 * 1024)


class _UniqueLoader(yaml.SafeLoader):
    """Reject ambiguous configuration before selecting an authorization binding."""


def _unique_mapping(loader: _UniqueLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ValueError("scope configuration keys must be strings") from exc
        if duplicate:
            raise ValueError("scope configuration contains a duplicate key")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _scope(args: argparse.Namespace) -> tuple[ScanScope, dict[str, Any]]:
    loader = _UniqueLoader(args.scope_config.read_text(encoding="utf-8"))
    try:
        data = loader.get_single_data()
    finally:
        loader.dispose()
    if not isinstance(data, dict) or set(data) != {"tenants"}:
        raise ValueError("scope configuration must contain only a tenants mapping")
    tenants = data["tenants"]
    if not isinstance(tenants, dict) or not tenants:
        raise ValueError("scope configuration tenants must be a non-empty mapping")
    for name, binding in tenants.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("tenant names must be non-empty strings")
        if not isinstance(binding, dict) or set(binding) != {"bucket", "prefix", "tiers"}:
            raise ValueError("each tenant binding requires exactly bucket, prefix, and tiers")
        validate_consistency_bucket(binding["bucket"])
        if not isinstance(binding["prefix"], str):
            raise ValueError("tenant prefix must be a string; use an explicit empty string for a whole bucket")
        tiers = binding["tiers"]
        if (
            not isinstance(tiers, list) or not tiers
            or any(not isinstance(tier, str) or not tier.strip() for tier in tiers)
            or len(set(tiers)) != len(tiers)
        ):
            raise ValueError("tenant tiers must be a non-empty list of unique tier names")
    # Authorize conservatively across case-insensitive/Unicode-normalizing
    # filesystems while preserving exact bucket/key strings for actual reads.
    namespaces = sorted(
        (unicodedata.normalize("NFC", item["bucket"]).casefold(),
         unicodedata.normalize("NFC", item["prefix"]).casefold())
        for item in tenants.values()
    )
    for (bucket, prefix), (next_bucket, next_prefix) in zip(namespaces, namespaces[1:]):
        if bucket == next_bucket and next_prefix.startswith(prefix):
            raise ValueError("tenant bucket/prefix namespaces must not overlap, regardless of tier")
    if args.tenant not in tenants:
        raise ValueError("tenant is not authorized by the scope configuration")
    binding = tenants[args.tenant]
    requested_prefix = getattr(args, "prefix", None)
    prefix = binding["prefix"] if requested_prefix is None else requested_prefix
    if not prefix.startswith(binding["prefix"]):
        raise ValueError("--prefix must stay within the configured tenant prefix")
    selected = getattr(args, "tier", None) or binding["tiers"]
    if not set(selected).issubset(binding["tiers"]):
        raise ValueError("--tier must stay within the configured tenant tiers")
    return ScanScope(
        tenant_id=args.tenant, bucket=binding["bucket"], prefix=prefix,
        tiers=tuple(sorted(set(selected))),
    ), binding


def _aliases(first: Path, second: Path) -> bool:
    return first.resolve() == second.resolve() or (
        first.exists() and second.exists() and first.samefile(second)
    )


def _driver_roots(config: Path | None) -> list[Path]:
    if config is None:
        return []
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("tiers"), dict):
        raise ValueError("driver configuration requires a tiers mapping")
    roots: list[Path] = []
    for info in data["tiers"].values():
        if isinstance(info, dict) and info.get("driver") == "posix":
            path = info.get("path")
            if not isinstance(path, str) or not path.strip():
                raise ValueError("POSIX tiers require a non-empty path")
            root = Path(path).expanduser().resolve()
            for other in roots:
                if (root == other or root in other.parents or other in root.parents
                        or (root.exists() and other.exists() and root.samefile(other))):
                    raise ValueError("consistency scans require disjoint POSIX tier roots")
            roots.append(root)
    return roots


def _protect_paths(
    args: argparse.Namespace, target: Path, *, sqlite_output: bool,
) -> None:
    protected = [
        Path(value).expanduser()
        for value in (
            getattr(args, "scope_config", None), args.drivers, args.config,
        ) if value is not None
    ]
    if args.catalog_db:
        catalog_path = sqlite_catalog_path(args.catalog_db)
        if catalog_path is not None:
            for base in (catalog_path, catalog_path.expanduser().resolve()):
                protected.extend(Path(str(base) + suffix) for suffix in _SQLITE_SUFFIXES)
    if args.cmd == "consistency-export" and not sqlite_output:
        for base in (args.report, args.report.expanduser().resolve()):
            protected.extend(Path(str(base) + suffix) for suffix in _SQLITE_SUFFIXES)
    candidates = [
        Path(str(base) + suffix)
        for base in (target, target.expanduser().resolve())
        for suffix in (_SQLITE_SUFFIXES if sqlite_output else ("",))
    ]
    roots = _driver_roots(None if args.drivers is None else Path(args.drivers))
    for candidate in candidates:
        if any(_aliases(candidate, path) for path in protected):
            raise ValueError("report/output must not alias a catalog, journal, configuration, or input report")
        if sqlite_output and candidate.exists() and candidate.stat().st_nlink > 1:
            raise ValueError("report and its journal files must not have hardlinks")
        resolved = candidate.expanduser().resolve()
        if any(resolved == root or root in resolved.parents for root in roots):
            raise ValueError("report/output must be outside POSIX storage roots")


def _binding_id(args: argparse.Namespace, binding: Mapping[str, Any]) -> str:
    catalog_path = sqlite_catalog_path(args.catalog_db)
    locator = (
        str(catalog_path.expanduser().resolve()) if catalog_path is not None
        else normalize_database_url(args.catalog_db)
    )
    serialized = json.dumps({
        "catalog": locator,
        "drivers_sha256": hashlib.sha256(Path(args.drivers).read_bytes()).hexdigest(),
        "posix_roots": sorted(str(root) for root in _driver_roots(Path(args.drivers))),
        "binding": {**binding, "tiers": sorted(binding["tiers"])},
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.catalog_db or not catalog_locator_is_persistent(args.catalog_db):
        raise ValueError(f"{args.cmd} requires an existing persistent --catalog-db")
    if not args.drivers:
        raise ValueError(f"{args.cmd} requires --drivers")
    scope, binding = _scope(args)
    _protect_paths(args, args.report, sqlite_output=True)
    binding_id = _binding_id(args, binding)
    if args.cmd == "consistency-export":
        _protect_paths(args, args.output, sqlite_output=False)
        summary = report_summary(args.report, tenant_id=args.tenant, binding_id=binding_id)
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError("--output must name a new file")
        if not args.dry_run:
            # Exclusive creation also rejects symlinks/hardlinks and prevents
            # a concurrent exporter from replacing an existing file.
            descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                summary = export_report(
                    args.report, output, tenant_id=args.tenant, binding_id=binding_id,
                )
        return {"summary": summary, "output": str(args.output)}

    if args.cmd == "consistency-repair":
        return _repair(args, scope, binding_id)

    if not args.resume and (args.report.exists() or args.report.is_symlink()):
        raise FileExistsError("--report already exists; use --resume with a matching report")
    # This branch runs before the CLI's ordinary mutable catalog/driver setup.
    # Read-only opening validates existing schema without installing migrations.
    catalog = open_catalog(args.catalog_db, read_only=True)
    try:
        drivers = load_drivers(args.drivers)
        if not set(scope.tiers).issubset(drivers):
            raise ValueError("tenant binding references an unconfigured driver tier")
        scanner = ConsistencyScanner(
            catalog, {tier: drivers[tier] for tier in scope.tiers}, scope,
            binding_id=binding_id, requests_per_second=args.requests_per_second,
            bytes_per_second=args.bytes_per_second, page_size=args.page_size,
        )
        if args.dry_run:
            with tempfile.TemporaryDirectory(prefix="cognistore-consistency-") as temporary:
                target = Path(temporary) / "preview.sqlite3"
                if args.resume:
                    report_summary(args.report, tenant_id=args.tenant, binding_id=binding_id)
                    uri = args.report.resolve().as_uri() + "?mode=ro"
                    with closing(sqlite3.connect(uri, uri=True)) as source:
                        with closing(sqlite3.connect(target)) as destination:
                            source.backup(destination)
                summary = scanner.run(target, resume=args.resume, max_items=args.max_items)
        else:
            summary = scanner.run(args.report, resume=args.resume, max_items=args.max_items)
        return {"summary": summary, "report": str(args.report)}
    finally:
        close = getattr(catalog, "close", None)
        if callable(close):
            close()


def _repair(args: argparse.Namespace, scope: ScanScope, binding_id: str) -> dict[str, Any]:
    from cognistore.core.consistency_repair import ConsistencyRepairer

    # Validate the original report before opening any writable source handle.
    saved = report_summary(args.report, tenant_id=args.tenant, binding_id=binding_id)
    if not saved["complete"]:
        raise ValueError("consistency-repair requires a completed scan report")
    if saved["scope"] != scope.to_dict():
        raise ValueError("repair scope must match the original scan prefix and tiers exactly")
    catalog_path = sqlite_catalog_path(args.catalog_db)
    if catalog_path is not None and not catalog_path.expanduser().is_file():
        raise ValueError("consistency-repair requires an existing persistent --catalog-db")
    enabled = args.enable_repair and not args.dry_run
    catalog = open_catalog(args.catalog_db, read_only=not enabled, migrate=False)
    drivers = {}
    try:
        drivers = load_drivers(args.drivers)
        if not set(scope.tiers).issubset(drivers):
            raise ValueError("tenant binding references an unconfigured driver tier")
        repairer = ConsistencyRepairer(
            catalog, {tier: drivers[tier] for tier in scope.tiers}, scope,
            binding_id=binding_id, requests_per_second=args.requests_per_second,
            bytes_per_second=args.bytes_per_second,
        )
        summary = repairer.run(args.report, enabled=enabled, dry_run=args.dry_run)
        return {"summary": summary, "report": str(args.report)}
    finally:
        for driver in drivers.values():
            close = getattr(driver, "close", None)
            if callable(close):
                close()
        close = getattr(catalog, "close", None)
        if callable(close):
            close()
