from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any, AsyncContextManager, Callable, Protocol
from uuid import uuid4

from cognistore.auth.tenancy import DEFAULT_TENANT_ID, TenantIsolationError, current_tenant_id
from cognistore.core.audit import AuditContext
from cognistore.core.catalog import CatalogStore
from cognistore.core.move_jobs import MoveJobLeaseError, MoveJobState
from cognistore.core.mover import Mover
from cognistore.core.placement_controls import MovementConstraints
from cognistore.core.policy import (
    EmbeddingPolicyRule,
    PIIPolicyRule,
    validate_policy_config_size,
)
from cognistore.core.policy_factory import build_policy
from cognistore.core.policy_features import CatalogPolicyFeatureLoader
from cognistore.core.policy_runner import ActionResult, PolicyRunner
from cognistore.core.scanner import scan_catalog
from cognistore.core.throughput import ThroughputConfig
from cognistore.drivers.storage_driver import StorageDriver
from cognistore.drivers.tenancy import scope_storage_drivers
from cognistore.observability import current_correlation_id
from cognistore.pii_runtime import PIIConfig

from .models import (
    JOB_SCHEMA_VERSION_V1,
    JOB_SCHEMA_VERSION_V2,
    JOB_SCHEMA_VERSION_V3,
    JOB_SCHEMA_VERSION_V4,
    InvalidJobError,
    JobContext,
    JobEnvelope,
)
from .runtime import JobHandler

LOGGER = logging.getLogger(__name__)
CATALOG_SCAN_JOB = "catalog.scan"
POLICY_RUN_JOB = "policy.run"
POLICY_AUDIT_VERSION = "1"


def policy_job_schema_version(payload: Mapping[str, object]) -> int:
    """Select the oldest envelope schema that can represent a policy job."""

    if "pii_rules" in payload:
        return JOB_SCHEMA_VERSION_V4
    if "movement_constraints" in payload:
        return JOB_SCHEMA_VERSION_V3
    if "embedding_rules" in payload:
        return JOB_SCHEMA_VERSION_V2
    return JOB_SCHEMA_VERSION_V1


class MoveThroughputController(Protocol):
    @property
    def config(self) -> ThroughputConfig: ...

    def admit(self, src_tier: str, dst_tier: str) -> AsyncContextManager[None]: ...

    def consume_bytes(
        self,
        tier: str,
        amount: int,
        *,
        on_wait: Callable[[], None] | None = None,
    ) -> None: ...


async def _run_blocking_safely(
    function,
    /,
    *args,
    _propagate_cancellation: bool = False,
    **kwargs,
):
    """Keep waiting after cancellation until side-effecting thread work stops.

    ``asyncio.to_thread`` cannot terminate its underlying thread. Returning a
    NAK while that thread is still mutating data would allow a second worker to
    execute the same job concurrently. Defer cancellation until the operation
    reaches a safe boundary. Once that operation completes, return its result
    so the worker ACKs the completed side effects.
    """

    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancellation_requested = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellation_requested = True
            continue
    result = task.result()
    if cancellation_requested and _propagate_cancellation:
        raise asyncio.CancelledError
    return result


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


def _embedding_rules(
    payload: Mapping[str, Any],
    name: str = "embedding_rules",
) -> tuple[EmbeddingPolicyRule, ...]:
    value = payload.get(name, [])
    if not isinstance(value, list) or len(value) > 100:
        raise InvalidJobError(
            f"job payload field {name!r} must be a list of at most 100 rules"
        )
    rules: list[EmbeddingPolicyRule] = []
    for index, raw_rule in enumerate(value):
        if not isinstance(raw_rule, Mapping):
            raise InvalidJobError(
                f"job payload field {name!r} item {index} must be an object"
            )
        try:
            rules.append(EmbeddingPolicyRule.from_mapping(raw_rule))
        except ValueError as exc:
            raise InvalidJobError(
                f"invalid job payload field {name!r} item {index}: {exc}"
            ) from exc
    if len({rule.name for rule in rules}) != len(rules):
        raise InvalidJobError(
            f"job payload field {name!r} cannot contain duplicate rule names"
        )
    return tuple(rules)


def _pii_rules(payload: Mapping[str, Any]) -> tuple[PIIPolicyRule, ...]:
    value = payload.get("pii_rules", [])
    if not isinstance(value, list) or len(value) > 100:
        raise InvalidJobError("job payload field 'pii_rules' must be a list of at most 100 rules")
    rules = []
    for index, raw_rule in enumerate(value):
        if not isinstance(raw_rule, Mapping):
            raise InvalidJobError(f"job payload field 'pii_rules' item {index} must be an object")
        try:
            rules.append(PIIPolicyRule.from_mapping(raw_rule))
        except ValueError as exc:
            raise InvalidJobError(f"invalid job payload field 'pii_rules' item {index}: {exc}") from exc
    return tuple(rules)


def build_handlers(
    drivers: Mapping[str, StorageDriver],
    catalog: CatalogStore,
    *,
    throughput: MoveThroughputController | None = None,
    policy_feature_loader: CatalogPolicyFeatureLoader | None = None,
    pii_config: PIIConfig | None = None,
) -> dict[str, JobHandler]:
    """Build handlers whose dependencies are configured by the worker process."""

    # Bound all handler-produced admission attempts together. Coordinators may
    # wait on this semaphore, but their count is bounded by worker in-flight
    # capacity; only these slots can occupy the controller's active+queue set.
    admission_slots = (
        asyncio.Semaphore(throughput.config.max_queue_depth)
        if throughput is not None
        else None
    )

    async def execute_move(
        src_tier: str,
        dst_tier: str,
        function,
        /,
        *args,
        **kwargs,
    ) -> Any:
        if throughput is None:
            return await _run_blocking_safely(
                function,
                *args,
                _propagate_cancellation=True,
                **kwargs,
            )
        assert admission_slots is not None
        await admission_slots.acquire()
        admitted = False
        try:
            async with throughput.admit(src_tier, dst_tier):
                # This slot bounds only not-yet-admitted work. Once the
                # controller owns both tier permits, release it so configured
                # concurrency can exceed the admission queue depth.
                admission_slots.release()
                admitted = True
                return await _run_blocking_safely(
                    function,
                    *args,
                    _propagate_cancellation=True,
                    **kwargs,
                )
        finally:
            if not admitted:
                admission_slots.release()

    async def recover_moves(
        mover: Mover,
        idempotency_prefix: str,
        *,
        audit_context: AuditContext | None = None,
    ) -> None:
        nonterminal_states = {
            state for state in MoveJobState if not state.terminal
        }
        jobs = await asyncio.to_thread(
            mover.list_jobs,
            states=nonterminal_states,
            idempotency_prefix=idempotency_prefix,
        )
        first_terminal_failure: Exception | None = None
        for move in jobs:
            # A live lease means another delivery is still executing this
            # logical job. Propagate the retryable conflict before planning:
            # the owner may already have published the destination while the
            # durable move record is still PREPARED, which would otherwise be
            # mistaken for a terminal destination collision.
            try:
                await execute_move(
                    move.src_tier,
                    move.dst_tier,
                    mover.move,
                    move.src_tier,
                    move.dst_tier,
                    move.bucket,
                    move.key,
                    idempotency_key=move.idempotency_key,
                    audit_context=audit_context,
                )
            except MoveJobLeaseError:
                raise
            except Exception as error:
                failed_move = await asyncio.to_thread(
                    mover.get_job, move.idempotency_key
                )
                if failed_move is None or failed_move.state != MoveJobState.FAILED:
                    raise
                if first_terminal_failure is None:
                    first_terminal_failure = error
        if first_terminal_failure is not None:
            raise first_terminal_failure

    async def execute_actions(
        runner: PolicyRunner, actions: Sequence[ActionResult]
    ) -> None:
        if throughput is None:
            for action in actions:
                await execute_move(
                    action.from_tier,
                    action.to_tier,
                    runner.execute,
                    action,
                )
            return

        # Interleave tier-pair lanes before filling a bounded task window. This
        # exposes unrelated work to the central fair scheduler even when the
        # catalog order contains a long prefix for one busy pair.
        lanes: dict[tuple[str, str], deque[ActionResult]] = {}
        for action in actions:
            lanes.setdefault(
                (action.from_tier, action.to_tier), deque()
            ).append(action)
        lane_order = deque(lanes)

        config = throughput.config
        active_capacity = min(
            sum(limits.source_concurrency for limits in config.tiers.values()),
            sum(
                limits.destination_concurrency
                for limits in config.tiers.values()
            ),
        )
        window = config.max_queue_depth + active_capacity
        pending: set[asyncio.Task[Any]] = set()

        def next_action() -> ActionResult | None:
            if not lane_order:
                return None
            lane = lane_order.popleft()
            action = lanes[lane].popleft()
            if lanes[lane]:
                lane_order.append(lane)
            return action

        try:
            while lane_order or pending:
                while lane_order and len(pending) < window:
                    next_item = next_action()
                    assert next_item is not None
                    pending.add(
                        asyncio.create_task(
                            execute_move(
                                next_item.from_tier,
                                next_item.to_tier,
                                runner.execute,
                                next_item,
                            ),
                            name=(
                                "cognistore-policy-move-"
                                f"{next_item.from_tier}-{next_item.to_tier}"
                            ),
                        )
                    )
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                results = await asyncio.gather(*done, return_exceptions=True)
                failure = next(
                    (result for result in results if isinstance(result, BaseException)),
                    None,
                )
                if failure is not None:
                    raise failure
        except BaseException:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            raise

    def dependencies(job: JobEnvelope):
        owner = job.tenant_id or DEFAULT_TENANT_ID
        bound_owner = current_tenant_id()
        if bound_owner is not None and owner != bound_owner:
            raise TenantIsolationError()
        return owner, catalog.for_tenant(owner), scope_storage_drivers(drivers, owner)

    async def catalog_scan(job: JobEnvelope, context: JobContext) -> None:
        owner, job_catalog, job_drivers = dependencies(job)
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
            driver=job_drivers[tier],
            catalog=job_catalog,
            audit_context=AuditContext(
                correlation_id=job.correlation_id,
                actor_type=job.principal.actor_type if job.principal else "worker",
                actor_id=job.principal.actor_id if job.principal else job.job_id,
                job_id=job.job_id,
            ),
            pii_pipeline=pii_config.pipeline_for_tenant(owner) if pii_config is not None else None,
        )
        LOGGER.info(
            "catalog scan completed",
            extra={
                "job_id": job.job_id,
                "correlation_id": current_correlation_id(),
                "attempt": context.attempt,
                "objects": len(results),
            },
        )

    async def policy_run(job: JobEnvelope, context: JobContext) -> None:
        owner, job_catalog, job_drivers = dependencies(job)
        if (
            job.schema_version < JOB_SCHEMA_VERSION_V3
            and "movement_constraints" in job.payload
        ):
            raise InvalidJobError(
                "job payload field 'movement_constraints' requires schema_version 3"
            )
        if job.schema_version < JOB_SCHEMA_VERSION_V4 and "pii_rules" in job.payload:
            raise InvalidJobError("job payload field 'pii_rules' requires schema_version 4")
        movement_constraints = None
        if "movement_constraints" in job.payload:
            raw_constraints = job.payload["movement_constraints"]
            if not isinstance(raw_constraints, Mapping):
                raise InvalidJobError(
                    "job payload field 'movement_constraints' must be an object"
                )
            try:
                movement_constraints = MovementConstraints.from_mapping(raw_constraints)
            except ValueError as exc:
                raise InvalidJobError(
                    f"invalid job payload field 'movement_constraints': {exc}"
                ) from exc
        if (
            job.schema_version == JOB_SCHEMA_VERSION_V1
            and "embedding_rules" in job.payload
        ):
            raise InvalidJobError(
                "job payload field 'embedding_rules' requires schema_version 2"
            )
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

        if movement_constraints is not None:
            unknown_residency = sorted(
                set(movement_constraints.minimum_residency_seconds).difference(drivers)
            )
            if unknown_residency:
                raise InvalidJobError(
                    "unknown residency tier(s): " + ", ".join(unknown_residency)
                )

        pii_rules = _pii_rules(job.payload)
        if pii_rules and policy_name != "content":
            raise InvalidJobError("PII rules require the content policy")
        disallowed_pii_tiers = sorted({
            rule.destination_tier for rule in pii_rules if rule.destination_tier not in allowed_tiers
        })
        if disallowed_pii_tiers:
            raise InvalidJobError(
                "PII rule destination tier(s) are not allowed: " + ", ".join(disallowed_pii_tiers)
            )
        embedding_rules = _embedding_rules(job.payload)
        if embedding_rules and policy_name != "content":
            raise InvalidJobError("embedding rules require the content policy")
        disallowed_rule_tiers = sorted(
            {
                rule.destination_tier
                for rule in embedding_rules
                if rule.destination_tier not in allowed_tiers
            }
        )
        if disallowed_rule_tiers:
            raise InvalidJobError(
                "embedding rule destination tier(s) are not allowed: "
                + ", ".join(disallowed_rule_tiers)
            )

        hot_name_patterns = _strings(job.payload, "hot_name_patterns")
        warm_name_patterns = _strings(job.payload, "warm_name_patterns")
        cold_name_patterns = _strings(job.payload, "cold_name_patterns")
        hot_mime_prefixes = _strings(job.payload, "hot_mime_prefixes")
        warm_mime_prefixes = _strings(job.payload, "warm_mime_prefixes")
        cold_mime_prefixes = _strings(job.payload, "cold_mime_prefixes")
        try:
            validate_policy_config_size(
                allowed_tiers=allowed_tiers,
                hot_name_patterns=hot_name_patterns,
                warm_name_patterns=warm_name_patterns,
                cold_name_patterns=cold_name_patterns,
                hot_mime_prefixes=hot_mime_prefixes,
                warm_mime_prefixes=warm_mime_prefixes,
                cold_mime_prefixes=cold_mime_prefixes,
                embedding_rules=embedding_rules,
                pii_rules=pii_rules,
            )
        except ValueError as exc:
            raise InvalidJobError(f"invalid policy job payload: {exc}") from exc

        policy = build_policy(
            policy_name,
            threshold=_integer(job.payload, "threshold"),
            llm_threshold=_optional_integer(job.payload, "llm_threshold"),
            allowed_tiers=allowed_tiers,
            hot_name_patterns=hot_name_patterns,
            warm_name_patterns=warm_name_patterns,
            cold_name_patterns=cold_name_patterns,
            hot_mime_prefixes=hot_mime_prefixes,
            warm_mime_prefixes=warm_mime_prefixes,
            cold_mime_prefixes=cold_mime_prefixes,
            embedding_rules=embedding_rules,
            pii_rules=pii_rules,
        )
        principal = job.principal
        audit_context = AuditContext(
            correlation_id=job.correlation_id,
            actor_type=principal.actor_type if principal is not None else "worker",
            actor_id=principal.actor_id if principal is not None else job.job_id,
            job_id=job.job_id,
        )
        # Each delivery owns a distinct catalog lease. Reusing one process-wide
        # owner would let concurrent duplicate deliveries bypass exclusivity.
        mover = Mover(
            job_drivers,
            job_catalog,
            owner_id=f"{job.job_id}:{uuid4()}",
            throughput=throughput,
            audit_context=audit_context,
        )
        await recover_moves(
            mover,
            f"{job.job_id}:",
            audit_context=audit_context,
        )
        runner = PolicyRunner(
            job_catalog,
            job_drivers,
            mover,
            policy,
            allowed_tiers=allowed_tiers,
            idempotency_namespace=job.job_id,
            policy_name=policy_name,
            policy_version=POLICY_AUDIT_VERSION,
            audit_context=audit_context,
            audit_occurred_at=job.created_at,
            feature_loader=(
                policy_feature_loader.for_tenant(owner)
                if policy_feature_loader is not None else None
            ),
            movement_constraints=movement_constraints,
        )
        actions = await _run_blocking_safely(
            runner.plan_once,
            bucket,
            prefix=prefix,
            dry_run=False,
            _propagate_cancellation=True,
        )
        await execute_actions(runner, actions)
        LOGGER.info(
            "policy run completed",
            extra={
                "job_id": job.job_id,
                "correlation_id": current_correlation_id(),
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
    embedding_rules: Sequence[EmbeddingPolicyRule | Mapping[str, object]] = (),
    pii_rules: Sequence[PIIPolicyRule | Mapping[str, object]] = (),
    movement_constraints: MovementConstraints | None = None,
) -> dict[str, Any]:
    if len(pii_rules) > 100:
        raise ValueError("pii_rules must contain at most 100 rules")
    pii_rule_objects = [
        rule if isinstance(rule, PIIPolicyRule) else PIIPolicyRule.from_mapping(rule)
        for rule in pii_rules
    ]
    if pii_rule_objects and policy != "content":
        raise ValueError("PII rules require the content policy")
    if any(rule.destination_tier not in allowed_tiers for rule in pii_rule_objects):
        raise ValueError("PII rule destination tier(s) must be allowed")
    rule_objects = [
        (
            rule
            if isinstance(rule, EmbeddingPolicyRule)
            else EmbeddingPolicyRule.from_mapping(rule)
        )
        for rule in embedding_rules
    ]
    validate_policy_config_size(
        allowed_tiers=allowed_tiers,
        hot_name_patterns=hot_name_patterns,
        warm_name_patterns=warm_name_patterns,
        cold_name_patterns=cold_name_patterns,
        hot_mime_prefixes=hot_mime_prefixes,
        warm_mime_prefixes=warm_mime_prefixes,
        cold_mime_prefixes=cold_mime_prefixes,
        embedding_rules=rule_objects,
        pii_rules=pii_rule_objects,
    )
    normalized_rules = [rule.to_mapping() for rule in rule_objects]
    payload = {
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
    if normalized_rules:
        payload["embedding_rules"] = normalized_rules
    if pii_rule_objects:
        payload["pii_rules"] = [rule.to_mapping() for rule in pii_rule_objects]
    if movement_constraints is not None:
        payload["movement_constraints"] = movement_constraints.to_dict()
    return payload
