"""Catalog-only budget administration and mutation-free policy comparisons."""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from cognistore.core.audit import AuditContext
from cognistore.core.budgets import BudgetDefinition
from cognistore.core.mover import Mover
from cognistore.core.policy_factory import build_policy
from cognistore.core.policy_runner import PolicyRunner
from cognistore.core.policy_simulation import simulate_policy
from cognistore.db.catalog import SQLCatalog

COMMANDS = {"budget-configure", "budget-list", "policy-what-if"}


def add_commands(command: Callable[..., argparse.ArgumentParser]) -> None:
    configure = command("budget-configure", help="Register an immutable, audited budget period")
    configure.add_argument("--input", required=True, help="Budget definition JSON")
    configure.add_argument("--actor", required=True, help="Operator identity for the audit")
    command("budget-list", help="List budget definitions and admitted commitments")
    simulation = command("policy-what-if", help="Compare policies without storage or catalog writes")
    simulation.add_argument("bucket")
    simulation.add_argument("--prefix", default="")
    simulation.add_argument("--threshold", type=int, default=1024 * 1024)
    simulation.add_argument("--baseline-threshold", type=int, default=1024 * 1024)
    simulation.add_argument("--allowed-tiers", default="hot,warm")
    simulation.add_argument("--as-of", help="Frozen UTC evaluation instant")
    simulation.add_argument("--budget-file", help="Proposed budget definition or list, used only in simulation")


def _definitions(path: str) -> list[BudgetDefinition]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate budget configuration field: {key}")
            result[key] = value
        return result

    raw = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique)
    return [BudgetDefinition.from_mapping(item) for item in (raw if isinstance(raw, list) else [raw])]


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.catalog_db:
        raise ValueError(f"{args.cmd} requires --catalog-db")
    configure = args.cmd == "budget-configure"
    definitions = _definitions(args.input) if configure else None
    if configure and len(definitions or []) != 1:
        raise ValueError("budget-configure requires exactly one budget definition")
    dry_run = bool(args.dry_run)
    # Read-only opening verifies an existing schema and never creates/migrates a
    # database. No driver loader, storage preflight, feature provider or queue.
    with SQLCatalog(args.catalog_db, read_only=not configure or dry_run) as catalog:
        if configure:
            assert definitions is not None
            budget = definitions[0]
            if not dry_run:
                catalog.configure_budget(
                    budget,
                    audit_context=AuditContext(
                        correlation_id=str(uuid4()), actor_type="user", actor_id=args.actor,
                    ),
                )
            return {"mode": "simulation" if dry_run else "configured", "budget": budget.to_dict()}
        if args.cmd == "budget-list":
            return {
                "budgets": [budget.to_dict() for budget in catalog.list_budgets()],
                "reservations": catalog.list_budget_reservations(),
            }
        allowed = tuple(item.strip() for item in args.allowed_tiers.split(",") if item.strip())
        if not allowed:
            raise ValueError("--allowed-tiers must contain a tier")
        unknown = set(allowed).difference(tier.name for tier in catalog.list_tiers())
        if unknown:
            raise ValueError(f"unknown catalog tiers: {', '.join(sorted(unknown))}")
        baseline = build_policy("simple", threshold=args.baseline_threshold, allowed_tiers=allowed)
        proposed = build_policy("simple", threshold=args.threshold, allowed_tiers=allowed)
        runner = PolicyRunner(
            catalog, {}, Mover({}, catalog), baseline,
            allowed_tiers=allowed, simulation_only=True,
        )
        return simulate_policy(
            runner, args.bucket, prefix=args.prefix,
            as_of=args.as_of or datetime.now(timezone.utc),
            proposed_policy=proposed,
            proposed_budget_definitions=(
                None if args.budget_file is None else _definitions(args.budget_file)
            ),
        )
