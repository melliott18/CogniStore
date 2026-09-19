"""Server-owned data locality constraints for physical movement destinations.

The policy file is reread at every decision, including durable move checkpoints.
Object metadata is deliberately excluded from this policy's trust boundary.
Evaluation returns detached JSON evidence and never writes to the catalog.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cognistore.auth.authorization import AuthorizationError, Permission, current_authorizer
from cognistore.auth.principal import Principal, current_principal
from cognistore.auth.tenancy import validate_tenant_id

from .placement_controls import MovementConstraintError
from .topology import Pool, Tier

if TYPE_CHECKING:
    from .catalog import ObjectRecord

_MAX_CONFIG_BYTES = 1024 * 1024
_RESTRICTIONS = {"allowed_regions", "required_localities"}


class LocalityConstraintError(MovementConstraintError):
    """The current locality policy forbids the requested physical destination."""


def _text(value: object, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not empty and not value.strip())
        or len(value) > 2048
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("Invalid locality configuration")
    value.encode("utf-8")
    return value


def _names(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("Invalid locality configuration")
    names = [_text(item) for item in value]
    if len(set(names)) != len(names):
        raise ValueError("Invalid locality configuration")
    return names


def _fields(value: object, required: set[str], optional: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or not required <= value.keys():
        raise ValueError("Invalid locality configuration")
    if value.keys() - required - optional:
        raise ValueError("Invalid locality configuration")
    return value


def _timestamp(value: str | datetime) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _restrictions(value: dict[str, Any]) -> dict[str, Any]:
    regions = value.get("allowed_regions")
    return {
        "allowed_regions": None if regions is None else _names(regions),
        "required_localities": _names(value.get("required_localities", [])),
    }


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Invalid locality configuration")
        result[key] = value
    return result


def _read_config(path: str) -> dict[str, Any]:
    with Path(path).open("rb") as handle:
        encoded = handle.read(_MAX_CONFIG_BYTES + 1)
    if len(encoded) > _MAX_CONFIG_BYTES:
        raise ValueError("Invalid locality configuration")
    value = _fields(
        json.loads(encoded, object_pairs_hook=_unique_object), {"version", "tenants"}, set()
    )
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Invalid locality configuration")
    if not isinstance(value["tenants"], dict):
        raise ValueError("Invalid locality configuration")
    tenants: dict[str, Any] = {}
    for tenant_id, raw in value["tenants"].items():
        validate_tenant_id(tenant_id)
        tenant = _fields(raw, {"tier_pools"}, _RESTRICTIONS | {"objects", "exceptions"})
        if not isinstance(tenant["tier_pools"], dict):
            raise ValueError("Invalid locality configuration")
        bindings = {_text(tier): _text(pool) for tier, pool in tenant["tier_pools"].items()}
        raw_objects = tenant.get("objects", [])
        raw_exceptions = tenant.get("exceptions", [])
        if not isinstance(raw_objects, list) or not isinstance(raw_exceptions, list):
            raise ValueError("Invalid locality configuration")
        objects: list[dict[str, Any]] = []
        for raw_object in raw_objects:
            rule = _fields(raw_object, {"bucket", "key_prefix"}, _RESTRICTIONS)
            objects.append({
                "bucket": _text(rule["bucket"]),
                "key_prefix": _text(rule["key_prefix"], empty=True),
                **_restrictions(rule),
            })
        exceptions: dict[str, dict[str, Any]] = {}
        for raw_exception in raw_exceptions:
            approval = _fields(raw_exception, {
                "id", "bucket", "key", "destination_pool_id", "issuer", "subject", "reason",
                "expires_at",
            }, set())
            normalized = {key: _text(item) for key, item in approval.items()}
            Principal(issuer=normalized["issuer"], subject=normalized["subject"])
            normalized["expires_at"] = _timestamp(normalized["expires_at"]).isoformat()
            if normalized["id"] in exceptions:
                raise ValueError("Invalid locality configuration")
            exceptions[normalized["id"]] = normalized
        tenants[tenant_id] = {
            **_restrictions(tenant), "tier_pools": bindings, "objects": objects,
            "exceptions": exceptions,
        }
    return tenants


def _locality_evidence(pool: Pool, now: datetime) -> tuple[str | None, dict[str, Any] | None]:
    raw = pool.metadata.get("locality_evidence")
    if raw is None:
        return "destination locality evidence is missing", None
    try:
        value = _fields(raw, {"observed_at", "max_age_seconds", "source"}, set())
        observed = _timestamp(value["observed_at"])
        maximum = value["max_age_seconds"]
        if (
            isinstance(maximum, bool) or not isinstance(maximum, (int, float))
            or not math.isfinite(maximum) or maximum <= 0
        ):
            raise ValueError("Invalid maximum age")
        source = _text(value["source"])
        age = (now - observed).total_seconds()
        evidence = {
            "source": source, "observed_at": observed.isoformat(),
            "max_age_seconds": maximum,
        }
        if age < 0:
            return "destination locality evidence is future-dated", evidence
        if age > maximum:
            return "destination locality evidence is stale", evidence
        return None, evidence
    except (TypeError, ValueError, OverflowError):
        return "destination locality evidence is malformed", None


def _exception(
    tenant: dict[str, Any], record: ObjectRecord, exception_id: str, now: datetime,
) -> dict[str, Any]:
    principal = current_principal()
    authorizer = current_authorizer()
    approval = tenant["exceptions"].get(exception_id)
    if (
        approval is None or principal is None or authorizer is None
        or approval["issuer"] != principal.issuer or approval["subject"] != principal.subject
        or approval["bucket"] != record.bucket or approval["key"] != record.key
        or now >= _timestamp(approval["expires_at"])
    ):
        raise ValueError("locality exception is absent, expired, or not authorized for this object")
    # Unlike ordinary trusted in-process calls, an exception always requires an
    # authenticated principal and current explicit administrative/movement grants.
    # No catalog is supplied: previews must not persist authorization events.
    authorizer.require(
        principal, [Permission.ADMIN, Permission.MOVEMENT], operation="locality.exception",
        boundary="locality",
    )
    public_approval = {
        key: value for key, value in approval.items() if key not in {"issuer", "subject"}
    }
    return {
        **public_approval, "actor_id": principal.actor_id, "used": False, "bypassed_rules": [],
    }


def evaluate_locality(
    record: ObjectRecord,
    *,
    tenant_id: str,
    tiers: Iterable[Tier],
    pools: Iterable[Pool],
    as_of: str | datetime | None = None,
    exception_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate all driver-bound destinations against current server policy.

    All matching tenant and object rules apply conjunctively. Unknown tenants,
    malformed configuration, ambiguous topology, and unavailable evidence deny
    placement when policy is enabled. Unset configuration preserves legacy use.
    """
    now = datetime.now(timezone.utc) if as_of is None else _timestamp(as_of)
    tier_list, pool_list = list(tiers), list(pools)
    tier_names = sorted({tier.name for tier in tier_list})
    path = os.environ.get("COGNISTORE_LOCALITY_CONFIG")
    result: dict[str, Any] = {
        "configured": path is not None, "as_of": now.isoformat(), "tenant_id": tenant_id,
        "allowed_destination_tiers": [], "allowed_pool_ids": [], "tier_pools": {},
        "rejected": {}, "rules": [], "exception": None, "destination_evidence": {},
    }

    def deny_all(reason: str) -> dict[str, Any]:
        result["reason"] = reason
        result["rejected"] = {tier: reason for tier in tier_names}
        return result

    if path is None:
        if exception_id is not None:
            # Explicit exception requests are always constrained; callers must
            # not interpret a missing policy as a legacy unrestricted preview.
            result["configured"] = True
            return deny_all("locality exception requires configured locality policy")
        result["allowed_destination_tiers"] = tier_names
        result["allowed_pool_ids"] = sorted(pool.pool_id for pool in pool_list)
        return result
    try:
        config = _read_config(path)
        validate_tenant_id(tenant_id)
    except (OSError, TypeError, ValueError, RecursionError, OverflowError):
        result["configuration_error"] = True
        return deny_all("locality configuration is unreadable or invalid")
    tenant = config.get(tenant_id)
    if tenant is None:
        return deny_all("tenant has no configured locality policy")
    result["tier_pools"] = dict(tenant["tier_pools"])
    tier_names = sorted(set(tier_names) | set(tenant["tier_pools"]))
    rules = [{"scope": "tenant", **_restrictions(tenant)}]
    rules.extend(
        {"scope": "object", **rule} for rule in tenant["objects"]
        if rule["bucket"] == record.bucket and record.key.startswith(rule["key_prefix"])
    )
    result["rules"] = rules
    if exception_id is not None:
        try:
            _text(exception_id)
            result["exception"] = _exception(tenant, record, exception_id, now)
        except (AuthorizationError, TypeError, ValueError):
            return deny_all("locality exception is absent, expired, or not authorized")
    tier_by_name = {tier.name: tier for tier in tier_list}
    pool_by_id = {pool.pool_id: pool for pool in pool_list}
    if len(tier_by_name) != len(tier_list) or len(pool_by_id) != len(pool_list):
        return deny_all("destination topology is ambiguous")
    for tier_name in tier_names:
        reason: str | None = None
        tier = tier_by_name.get(tier_name)
        pool_id = tenant["tier_pools"].get(tier_name)
        pool = pool_by_id.get(pool_id)
        if tier is None or not tier.active:
            reason = "destination tier is missing or inactive"
        elif pool_id is None:
            reason = "destination tier has no configured physical pool binding"
        elif pool is None or not pool.active or pool.tier != tier_name:
            reason = "destination pool is missing, inactive, or bound to another tier"
        elif not pool.region or not pool.members:
            reason = "destination pool topology is incomplete"
        else:
            reason, provenance = _locality_evidence(pool, now)
            result["destination_evidence"][tier_name] = {
                "pool_id": pool.pool_id, "region": pool.region,
                "localities": list(pool.localities), "locality_evidence": provenance,
            }
            if reason is None:
                violations = []
                for index, rule in enumerate(rules):
                    if (
                        rule["allowed_regions"] is not None
                        and pool.region not in rule["allowed_regions"]
                    ):
                        violations.append(f"{rule['scope']} rule {index}: region is forbidden")
                    if not set(rule["required_localities"]) <= set(pool.localities):
                        violations.append(f"{rule['scope']} rule {index}: required locality is missing")
                approval = result["exception"]
                if violations and approval is not None and approval["destination_pool_id"] == pool_id:
                    approval["used"] = True
                    approval["bypassed_rules"] = violations
                elif violations:
                    reason = "; ".join(violations)
        if reason is not None:
            result["rejected"][tier_name] = reason
        else:
            result["allowed_destination_tiers"].append(tier_name)
            result["allowed_pool_ids"].append(pool_id)
    return result


def assert_locality_allowed(
    record: ObjectRecord,
    destination: str,
    *,
    tenant_id: str,
    tiers: Iterable[Tier],
    pools: Iterable[Pool],
    as_of: str | datetime | None = None,
    exception_id: str | None = None,
    destination_pool_id: str | None = None,
) -> dict[str, Any]:
    """Reject an ineligible destination or a changed physical pool selection."""
    evidence = evaluate_locality(
        record, tenant_id=tenant_id, tiers=tiers, pools=pools, as_of=as_of,
        exception_id=exception_id,
    )
    if not evidence["configured"] and exception_id is None:
        return evidence
    if destination not in evidence["allowed_destination_tiers"]:
        reason = evidence["rejected"].get(
            destination, evidence.get("reason", "destination has no configured locality binding")
        )
        raise LocalityConstraintError(f"locality constraint forbids movement: {reason}", evidence)
    binding = evidence["tier_pools"].get(destination)
    if destination_pool_id is not None and destination_pool_id != binding:
        raise LocalityConstraintError(
            "locality constraint forbids movement: selected destination pool binding changed",
            evidence,
        )
    approval = evidence["exception"]
    if approval is not None and approval["destination_pool_id"] != binding:
        approval["used"] = False
        approval["bypassed_rules"] = []
    return evidence
