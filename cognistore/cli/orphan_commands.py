"""Explicit, tenant-scoped orphan cleanup with a read-only default."""
from __future__ import annotations

import argparse
import getpass
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

from cognistore.cli.consistency_commands import _binding_id, _positive_float, _scope
from cognistore.core.audit import AuditContext
from cognistore.core.orphan_cleanup import OrphanCleanup
from cognistore.db import catalog_locator_is_persistent, open_catalog
from cognistore.drivers.driver_loader import load_drivers
from cognistore.drivers.tenancy import TENANT_STORAGE_DIRECTORY, scope_storage_drivers


def add_commands(command: Callable[..., argparse.ArgumentParser]) -> None:
    cleanup = command("orphan-cleanup", help="Report, quarantine, or explicitly delete one orphan")
    cleanup.add_argument("--tenant", required=True, help="Tenant from the trusted scope configuration")
    cleanup.add_argument("--scope-config", required=True, type=Path, help="Operator-managed tenant bindings")
    cleanup.add_argument("--key", required=True, help="One logical key inside the tenant binding")
    cleanup.add_argument("--tier", required=True, help="One tier allowed by the tenant binding")
    stages = cleanup.add_mutually_exclusive_group()
    stages.add_argument("--quarantine", action="store_true", help="Persist a cleanup candidate without deleting")
    stages.add_argument("--execute", action="store_true", help="Revalidate and delete a quarantined candidate")
    cleanup.add_argument("--candidate-id", help="Durable quarantine UUID; required by --execute")
    cleanup.add_argument("--grace-period-seconds", type=_positive_float, default=604800.0,
                         help="Minimum quarantine age (default: 604800)")
    cleanup.add_argument("--retention-seconds", type=_positive_float, default=604800.0,
                         help="Minimum backend object age (default: 604800)")


def effective_stage(args: argparse.Namespace) -> str:
    if args.dry_run:
        return "report"
    if args.execute:
        return "execute"
    return "quarantine" if args.quarantine else "report"


def _validate_key_path(value: str, *, prefix: bool = False) -> None:
    path = value[:-1] if prefix and value.endswith("/") else value
    if prefix and not value:
        return
    if (
        not path or "\\" in path or "\0" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or path.split("/", 1)[0] == TENANT_STORAGE_DIRECTORY
    ):
        raise ValueError("cleanup keys and prefixes must be canonical relative paths within the tenant namespace")


def _close_catalog(catalog: object) -> None:
    close = getattr(catalog, "close", None)
    if callable(close):
        close()


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.catalog_db or not catalog_locator_is_persistent(args.catalog_db):
        raise ValueError("orphan-cleanup requires an existing persistent --catalog-db")
    if not args.drivers:
        raise ValueError("orphan-cleanup requires --drivers")
    if args.execute and not args.candidate_id:
        raise ValueError("--execute requires --candidate-id")
    # Consistency scans allow repeated tiers; cleanup selects exactly one.
    scope_args = argparse.Namespace(**{**vars(args), "tier": [args.tier]})
    scope, binding = _scope(scope_args)
    _validate_key_path(scope.prefix, prefix=True)
    _validate_key_path(args.key)
    if not args.key.startswith(scope.prefix):
        raise ValueError("--key must stay within the configured tenant prefix")
    # Includes disjoint resolved POSIX roots and complete trusted configuration.
    binding_id = _binding_id(args, binding)
    stage = effective_stage(args)
    with ExitStack() as resources:
        # Validate that this tenant's persistent schema already exists before
        # entering a writable path. Neither previews nor cleanup run migrations.
        catalog = open_catalog(args.catalog_db, tenant_id=args.tenant, read_only=True, migrate=False)
        resources.callback(_close_catalog, catalog)
        drivers = load_drivers(args.drivers)
        for driver in drivers.values():
            close = getattr(driver, "close", None)
            if callable(close):
                resources.callback(close)
        if not set(scope.tiers).issubset(drivers):
            raise ValueError("tenant binding references an unconfigured driver tier")
        scoped_drivers = scope_storage_drivers(
            {tier: drivers[tier] for tier in scope.tiers}, args.tenant,
        )
        context = None
        if stage != "report":
            context = AuditContext(
                correlation_id=str(uuid4()), actor_type="user", actor_id=getpass.getuser(),
            )
            _close_catalog(catalog)
            catalog = open_catalog(
                args.catalog_db, tenant_id=args.tenant, read_only=False, migrate=False,
            )
            resources.callback(_close_catalog, catalog)
        summary = OrphanCleanup(catalog, scoped_drivers, scope, binding_id=binding_id).run(
            args.key, args.tier, stage=stage, candidate_id=args.candidate_id,
            grace_period_seconds=args.grace_period_seconds,
            retention_seconds=args.retention_seconds, context=context,
        )
        return {"summary": summary}
