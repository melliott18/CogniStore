from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from cognistore.core.mover import Mover
from cognistore.core.policy_factory import build_policy
from cognistore.core.policy_runner import PolicyRunner
from cognistore.core.scanner import scan_catalog
from cognistore.drivers.storage_driver import StorageDriver

from .models import InvalidJobError, JobContext, JobEnvelope
from .runtime import JobHandler


LOGGER = logging.getLogger(__name__)
CATALOG_SCAN_JOB = "catalog.scan"
POLICY_RUN_JOB = "policy.run"


async def _run_blocking_safely(function, /, *args, **kwargs):
    """Keep waiting after cancellation until side-effecting thread work stops.

    ``asyncio.to_thread`` cannot terminate its underlying thread. Returning a
    NAK while that thread is still mutating data would allow a second worker to
    execute the same job concurrently. Defer cancellation until the operation
    reaches a safe boundary. Once that operation completes, return its result
    so the worker ACKs the completed side effects.
    """

    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


def _string(payload: Mapping[str, Any], name: str, *, allow_empty: bool = False) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        qualification = "a string" if allow_empty else "a non-empty string"
        raise InvalidJobError(
            f"job payload field {name!r} must be {qualification}"
        )
    return value


def _integer(payload: Mapping[str, Any], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidJobError(f"job payload field {name!r} must be an integer")
    return value


def _optional_integer(payload: Mapping[str, Any], name: str) -> int | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidJobError(
            f"job payload field {name!r} must be an integer or null"
        )
    return value


def _strings(payload: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = payload.get(name, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise InvalidJobError(
            f"job payload field {name!r} must be a list of strings"
        )
    return tuple(value)


def build_handlers(
    drivers: Mapping[str, StorageDriver], catalog: Any
) -> dict[str, JobHandler]:
    """Build handlers whose dependencies are configured by the worker process."""

    async def catalog_scan(job: JobEnvelope, context: JobContext) -> None:
        tier = _string(job.payload, "tier")
        bucket = _string(job.payload, "bucket")
        prefix = _string(job.payload, "prefix", allow_empty=True)
        if tier not in drivers:
            raise InvalidJobError(f"unknown tier: {tier}")
        results = await _run_blocking_safely(
            scan_catalog,
            tier=tier,
            bucket=bucket,
            prefix=prefix,
            driver=drivers[tier],
            catalog=catalog,
        )
        LOGGER.info(
            "catalog scan completed",
            extra={
                "job_id": job.job_id,
                "correlation_id": job.correlation_id,
                "attempt": context.attempt,
                "objects": len(results),
            },
        )

    async def policy_run(job: JobEnvelope, context: JobContext) -> None:
        bucket = _string(job.payload, "bucket")
        prefix = _string(job.payload, "prefix", allow_empty=True)
        policy_name = _string(job.payload, "policy")
        if policy_name not in {"simple", "llm", "content"}:
            raise InvalidJobError(f"unknown policy: {policy_name}")
        allowed_tiers = _strings(job.payload, "allowed_tiers")
        if not allowed_tiers:
            raise InvalidJobError("allowed_tiers cannot be empty")
        unknown = sorted(set(allowed_tiers).difference(drivers))
        if unknown:
            raise InvalidJobError(
                f"unknown allowed tier(s): {', '.join(unknown)}"
            )

        policy = build_policy(
            policy_name,
            threshold=_integer(job.payload, "threshold"),
            llm_threshold=_optional_integer(job.payload, "llm_threshold"),
            allowed_tiers=allowed_tiers,
            hot_name_patterns=_strings(job.payload, "hot_name_patterns"),
            warm_name_patterns=_strings(job.payload, "warm_name_patterns"),
            cold_name_patterns=_strings(job.payload, "cold_name_patterns"),
            hot_mime_prefixes=_strings(job.payload, "hot_mime_prefixes"),
            warm_mime_prefixes=_strings(job.payload, "warm_mime_prefixes"),
            cold_mime_prefixes=_strings(job.payload, "cold_mime_prefixes"),
        )
        mover = Mover(dict(drivers), catalog)
        runner = PolicyRunner(
            catalog,
            dict(drivers),
            mover,
            policy,
            allowed_tiers=allowed_tiers,
        )
        actions = await _run_blocking_safely(
            runner.run_once, bucket, prefix=prefix, dry_run=False
        )
        LOGGER.info(
            "policy run completed",
            extra={
                "job_id": job.job_id,
                "correlation_id": job.correlation_id,
                "attempt": context.attempt,
                "actions": len(actions),
            },
        )

    return {CATALOG_SCAN_JOB: catalog_scan, POLICY_RUN_JOB: policy_run}


def policy_job_payload(
    *,
    bucket: str,
    prefix: str,
    policy: str,
    threshold: int,
    llm_threshold: int | None,
    allowed_tiers: Sequence[str],
    hot_name_patterns: Sequence[str],
    warm_name_patterns: Sequence[str],
    cold_name_patterns: Sequence[str],
    hot_mime_prefixes: Sequence[str],
    warm_mime_prefixes: Sequence[str],
    cold_mime_prefixes: Sequence[str],
) -> dict[str, Any]:
    return {
        "bucket": bucket,
        "prefix": prefix,
        "policy": policy,
        "threshold": threshold,
        "llm_threshold": llm_threshold,
        "allowed_tiers": list(allowed_tiers),
        "hot_name_patterns": list(hot_name_patterns),
        "warm_name_patterns": list(warm_name_patterns),
        "cold_name_patterns": list(cold_name_patterns),
        "hot_mime_prefixes": list(hot_mime_prefixes),
        "warm_mime_prefixes": list(warm_mime_prefixes),
        "cold_mime_prefixes": list(cold_mime_prefixes),
    }
