from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from heapq import nsmallest
from typing import Any, Dict, List, Mapping, Optional, Protocol
from uuid import UUID

from cognistore.utils.redaction import redact, redact_text

from .access import (
	ACCESS_KINDS,
	AccessConfig,
	AccessEvent,
	AccessSnapshot,
	AccessWindow,
	access_cutoff,
	access_timestamp,
)
from .audit import (
	AuditContext,
	AuditEvent,
	AuditEventType,
	AuditOutcome,
	AuditQuery,
	AuditRetentionPolicy,
	audit_event_replay_digest,
	audit_text_identity,
	redact_audit_event,
	stable_audit_event_id,
)
from .budgets import (
	BudgetConstraintError,
	BudgetDefinition,
	BudgetOverride,
	active_budget_definitions,
	estimate_budget_charge,
	evaluate_budget,
)
from .content_identity import ObjectContent
from .content_references import (
	ContentReferenceReport,
	ContentReferenceSnapshot,
	build_content_reference_report,
)
from .move_jobs import (
	EXPECTED_SOURCE_SHA256_METADATA_KEY,
	MoveJob,
	MoveJobConflictError,
	MoveJobLeaseError,
	MoveJobState,
	MoveJobTransition,
	validate_move_job_transition,
)
from .placement_controls import (
	ImportanceTag,
	assert_move_allowed,
	validate_importance_provenance,
	validate_minimum_residency_seconds,
)
from .topology import (
	AttributeValue,
	PlacementCandidate,
	PlacementConstraints,
	Pool,
	Tier,
	eligible_candidates,
)


@dataclass
class ObjectRecord:
	"""A mutable, caller-owned snapshot of one catalog object.

	Catalog stores own their persisted state and recursively copy metadata at
	write and read boundaries.  Callers may freely mutate a returned record or
	its nested metadata, but those changes are local to that snapshot; persisting
	a change requires an explicit :class:`CatalogStore` mutation method.
	"""

	bucket: str
	key: str
	size: int
	tier: str
	metadata: Dict[str, object] = field(default_factory=dict)
	pool_id: str | None = None
	placement_started_at: str | None = None
	importance: ImportanceTag | None = None
	importance_revision: int = 0
	last_tier_move_at: str | None = None


MoveJobScanFingerprint = tuple[
	str,
	str,
	str,
	str,
	str | None,
	str | None,
	str,
	str,
]


@dataclass(frozen=True)
class ScanFence:
	"""Move state observed before a scanner reads one physical object."""

	bucket: str
	key: str
	move_jobs: tuple[MoveJobScanFingerprint, ...]


@dataclass
class _ContentBlobState:
	"""In-memory materialization of one global CAS blob's live references."""

	size: int
	cas_key: str
	reference_count: int
	unreferenced_at: str | None


def _content_reference_timestamp() -> str:
	return (
		datetime.now(timezone.utc)
		.isoformat(timespec="microseconds")
		.replace("+00:00", "Z")
	)


def _content_blob_descriptors(
	content: ObjectContent,
) -> Iterator[tuple[str, int, str, bool]]:
	"""Yield the full-object edge followed by every ordered chunk edge."""

	yield content.sha256, content.size, content.cas_key, True
	for chunk in content.chunks:
		yield chunk.sha256, chunk.size, chunk.cas_key, False


def validate_catalog_size(value: object, *, field: str = "size") -> int:
	"""Return a backend-neutral, non-negative catalog size."""

	if (
		isinstance(value, bool)
		or not isinstance(value, int)
		or value < 0
		or value > 2**63 - 1
	):
		raise ValueError(
			f"{field} must be a non-negative integer no greater than {2**63 - 1}"
		)
	return value


def _validate_importance_actor(
	tag: ImportanceTag | None, context: AuditContext, provenance: str | None = None,
) -> None:
	if not isinstance(context, AuditContext):
		raise ValueError("audit_context must be an AuditContext")
	if provenance is not None:
		validate_importance_provenance(provenance)
	if tag is None and provenance is None:
		raise ValueError("clearing importance requires provenance")
	if tag is not None:
		if not isinstance(tag, ImportanceTag):
			raise ValueError("tag must be an ImportanceTag or null")
		if (tag.actor_type, tag.actor_id) != (context.actor_type, context.actor_id):
			raise ValueError("importance tag actor must match audit context actor")


def _assert_catalog_move_allowed(
	record: ObjectRecord | None, src_tier: str, dst_tier: str,
	source_metadata: Mapping[str, Any], tier_metadata: Mapping[str, object] | None,
	now: str,
) -> None:
	if record is None:
		# Unscanned or deleted sources have unknown residency, never an expired clock.
		record = ObjectRecord(bucket="", key="", size=0, tier=src_tier)
	if record.tier != src_tier:
		raise MoveJobConflictError("Object placement changed since the move was planned")
	assert_move_allowed(
		record, dst_tier,
		controls=source_metadata.get("cognistore_movement_constraints"),
		tier_metadata=tier_metadata, as_of=now,
	)


def _importance_event(
	previous: ObjectRecord, updated: ObjectRecord, context: AuditContext,
	occurred_at: str | datetime | None, provenance: str | None = None,
) -> AuditEvent:
	return AuditEvent.create(
		AuditEventType.IMPORTANCE_CHANGED, AuditOutcome.SUCCEEDED, context,
		bucket=updated.bucket, object_key=updated.key, occurred_at=occurred_at,
		details={
			"previous_importance": previous.importance.to_dict() if previous.importance else None,
			"importance": updated.importance.to_dict() if updated.importance else None,
			"importance_revision": updated.importance_revision,
			"provenance": provenance or (updated.importance.provenance if updated.importance else None),
		},
	)


def _budget_configuration_event(
	definition: BudgetDefinition, context: AuditContext, now: str | datetime | None,
) -> AuditEvent:
	return AuditEvent.create(
		AuditEventType.BUDGET_CONFIGURED, AuditOutcome.SUCCEEDED, context,
		event_id=stable_audit_event_id("budget-configured", definition.budget_id),
		occurred_at=now, recorded_at=now,
		details={"budget": definition.to_dict()},
	)


def _prepare_budget_reservations(
	definitions: List[BudgetDefinition], reservations: List[dict[str, Any]],
	pools_by_id: Mapping[str, Pool], record: ObjectRecord | None,
	*, move_id: str, bucket: str, key: str, src_tier: str, dst_tier: str,
	size: int, source_metadata: Mapping[str, Any], now: str,
	audit_context: AuditContext | None, trusted_override: bool = False,
) -> tuple[List[dict[str, Any]], dict[str, Any]]:
	"""Compute all admissions while the caller holds its global budget lock."""
	raw_override = source_metadata.get("cognistore_budget_override")
	override = None if raw_override is None else BudgetOverride.from_mapping(raw_override)
	authorized_before = trusted_override and any(
		item["move_id"] == move_id
		and item.get("evidence", {}).get("override") == raw_override
		for item in reservations
	)
	if override is not None and not authorized_before and (
		audit_context is None or audit_context.actor_type != "user"
		or audit_context.actor_id != override.actor_id
	):
		raise BudgetConstraintError("budget override requires its authenticated user actor")
	expected_pool = source_metadata.get("cognistore_expected_destination_pool_id")
	if expected_pool is not None:
		pool = pools_by_id.get(expected_pool)
		if pool is None or not pool.active or pool.tier != dst_tier:
			raise BudgetConstraintError("selected destination pool is unavailable or belongs to another tier")
	matching = [item for item in definitions if item.matches(bucket, key)]
	if not matching:
		metadata = dict(source_metadata)
		if not trusted_override:
			metadata.pop("cognistore_source_pool_id", None)
			metadata.pop("cognistore_destination_pool_id", None)
		if expected_pool is not None:
			metadata["cognistore_destination_pool_id"] = expected_pool
		return [], metadata
	active = active_budget_definitions(definitions, bucket, key, as_of=now)
	object_record = ObjectRecord(
		bucket=bucket, key=key, size=size, tier=src_tier,
		pool_id=(source_metadata.get("cognistore_source_pool_id", (
			None if record is None or record.tier != src_tier else record.pool_id
		)) if trusted_override else None if record is None else record.pool_id),
	)
	metadata = dict(source_metadata)
	metadata["cognistore_source_pool_id"] = object_record.pool_id
	bindings = {item.tier_pools.get(dst_tier) for item in active}
	if len(bindings) != 1 or None in bindings:
		raise BudgetConstraintError("active budgets must agree on the destination pool binding")
	bound_pool = next(iter(bindings))
	if expected_pool is not None and expected_pool != bound_pool:
		raise BudgetConstraintError("selected destination pool differs from the budget binding")
	if trusted_override and metadata.get("cognistore_destination_pool_id", bound_pool) != bound_pool:
		raise BudgetConstraintError("destination pool differs from the durable budget contract")
	metadata["cognistore_destination_pool_id"] = bound_pool
	prepared: List[dict[str, Any]] = []
	for definition in sorted(active, key=lambda item: item.budget_id):
		previous = [item for item in reservations if item["budget_id"] == definition.budget_id]
		charge = estimate_budget_charge(
			definition, object_record, dst_tier, pools_by_id, as_of=now,
		)
		evidence = evaluate_budget(definition, charge, previous, as_of=now, override=override)
		if not evidence["allowed"]:
			raise BudgetConstraintError(
				f"budget {definition.budget_id!r} blocks move: "
				+ ", ".join(str(item) for item in evidence["binding_constraints"]),
				evidence=[evidence],
			)
		attempt = 1 + max(
			(item["attempt"] for item in previous if item["move_id"] == move_id), default=0,
		)
		prepared.append({
			"budget_id": definition.budget_id, "move_id": move_id, "attempt": attempt,
			"bucket": bucket, "key": key, "created_at": now,
			"cost_usd": charge["cost_usd"], "carbon_gco2e": charge["carbon_gco2e"],
			"evidence": evidence,
		})
	return prepared, metadata


def _budget_reservation_event(
	reservation: Mapping[str, Any], context: AuditContext,
) -> AuditEvent:
	return AuditEvent.create(
		AuditEventType.BUDGET_RESERVED, AuditOutcome.SUCCEEDED, context,
		event_id=stable_audit_event_id(
			"budget-reserved", reservation["budget_id"], reservation["move_id"],
			str(reservation["attempt"]),
		),
		occurred_at=reservation["created_at"], recorded_at=reservation["created_at"],
		bucket=reservation["bucket"], object_key=reservation["key"],
		details={"reservation": deepcopy(dict(reservation))},
	)


class CatalogStore(Protocol):
	"""Backend-neutral persistence contract for catalog state.

	``get()`` and ``list()`` return detached :class:`ObjectRecord` snapshots.
	``list()`` returns matching records in ascending key order.
	Tier/pool reads are also detached snapshots. Active placement references
	prevent deletion or deactivation; historical move-journal names do not.
	``assign_pool()`` atomically changes both pool and tier; clearing the pool
	retains the tier. Tier-only writes preserve a pool only within the same tier.
	"""

	def configure_budget(
		self, definition: BudgetDefinition, *, audit_context: AuditContext,
		occurred_at: str | datetime | None = None,
	) -> BudgetDefinition: ...

	def list_budgets(self) -> List[BudgetDefinition]: ...

	def list_budget_reservations(self, budget_id: str | None = None) -> List[dict[str, Any]]: ...

	def read_budget_snapshot(self) -> tuple[List[BudgetDefinition], List[dict[str, Any]]]: ...

	def register_tier(
		self, name: str, metadata: Mapping[str, object] | None = None, *, active: bool = True
	) -> None: ...

	def get_tier(self, name: str) -> Tier | None: ...

	def list_tiers(self) -> list[Tier]: ...

	def delete_tier(self, name: str) -> None: ...

	def register_pool(
		self,
		pool_id: str,
		tier: str,
		metadata: Mapping[str, object] | None = None,
		*,
		region: str | None = None,
		members: tuple[str, ...] = (),
		localities: tuple[str, ...] = (),
		attributes: Mapping[str, AttributeValue] | None = None,
		active: bool = True,
	) -> None: ...

	def get_pool(self, pool_id: str) -> Pool | None: ...

	def list_pools(self, *, tier: str | None = None) -> list[Pool]: ...

	def delete_pool(self, pool_id: str) -> None: ...

	def assign_pool(self, bucket: str, key: str, pool_id: str | None) -> None: ...

	def eligible_placements(
		self,
		constraints: PlacementConstraints | None = None,
		*,
		now: str | datetime | None = None,
	) -> list[PlacementCandidate]: ...

	def upsert(
		self,
		bucket: str,
		key: str,
		size: int,
		tier: str,
		metadata: Optional[Dict[str, object]] = None,
	) -> None: ...

	def capture_scan_fence(self, bucket: str, key: str) -> ScanFence: ...

	def upsert_scan_observation(
		self,
		bucket: str,
		key: str,
		*,
		size: int,
		tier: str,
		generation: str,
		metadata: Optional[Dict[str, object]],
		fence: ScanFence,
		content: ObjectContent | None = None,
	) -> bool: ...

	def get(self, bucket: str, key: str) -> Optional[ObjectRecord]: ...

	def set_importance(
		self, bucket: str, key: str, tag: ImportanceTag | None, *,
		audit_context: AuditContext, occurred_at: str | datetime | None = None,
		provenance: str | None = None,
	) -> ObjectRecord: ...

	def get_object_content(self, bucket: str, key: str) -> ObjectContent | None: ...

	def reconcile_content_references(
		self,
		*,
		grace_period_seconds: float,
		now: str | datetime | None = None,
	) -> ContentReferenceReport: ...

	def update_placement(self, bucket: str, key: str, tier: str) -> None: ...

	def upsert_placement(
		self,
		bucket: str,
		key: str,
		*,
		size: int,
		tier: str,
		checksum: Optional[str] = None,
	) -> None: ...

	def delete(self, bucket: str, key: str) -> None: ...

	def list(self, bucket: str, prefix: str = "") -> List[ObjectRecord]: ...

	def list_page(
		self,
		bucket: str,
		prefix: str = "",
		*,
		after_key: str | None = None,
		limit: int = 100,
		tier: str | None = None,
	) -> List[ObjectRecord]: ...

	def iter_objects(self, *, batch_size: int = 1000) -> Iterator[ObjectRecord]: ...

	def append_access_event(self, event: AccessEvent) -> AccessEvent: ...

	def aggregate_access_events(
		self,
		bucket: str,
		key: str | None,
		*,
		config: AccessConfig,
		as_of: str | datetime,
	) -> AccessSnapshot: ...

	def prune_access_events(
		self,
		occurred_before: str | datetime,
		*,
		limit: int = 1000,
		retention_seconds: int = 2592000,
	) -> int: ...

	def append_audit_event(self, event: AuditEvent) -> AuditEvent: ...

	def get_audit_event(self, event_id: str) -> AuditEvent | None: ...

	def list_audit_events(
		self, query: AuditQuery | None = None
	) -> List[AuditEvent]: ...

	def prune_audit_events(
		self,
		occurred_before: str | datetime,
		*,
		limit: int = 1000,
	) -> int: ...

	def prune_expired_audit_events(
		self,
		now: str | datetime,
		*,
		limit: int = 1000,
	) -> int: ...

	def claim_move_job(
		self,
		idempotency_key: str,
		*,
		src_tier: str,
		dst_tier: str,
		bucket: str,
		key: str,
		expected_size: int,
		source_metadata: Mapping[str, Any],
		owner_id: str,
		now: str,
		lease_expires_at: str,
		audit_context: AuditContext | None = None,
	) -> MoveJob: ...

	def get_move_job(self, idempotency_key: str) -> MoveJob | None: ...

	def list_move_jobs(
		self,
		*,
		states: set[MoveJobState] | None = None,
		idempotency_prefix: str | None = None,
	) -> List[MoveJob]: ...

	def list_move_jobs_page(
		self,
		bucket: str,
		prefix: str = "",
		*,
		after_id: str | None = None,
		limit: int = 100,
	) -> List[MoveJob]: ...

	def list_move_job_transitions(
		self, idempotency_key: str
	) -> List[MoveJobTransition]: ...

	def renew_move_job_lease(
		self,
		idempotency_key: str,
		*,
		owner_id: str,
		expected_state: MoveJobState | None,
		now: str,
		lease_expires_at: str,
	) -> MoveJob: ...

	def transition_move_job(
		self,
		idempotency_key: str,
		*,
		owner_id: str,
		expected_state: MoveJobState,
		to_state: MoveJobState,
		reason: str,
		now: str,
		lease_expires_at: str,
		updates: Mapping[str, Any] | None = None,
		audit_context: AuditContext | None = None,
	) -> MoveJob: ...

	def commit_move_job_placement(
		self,
		idempotency_key: str,
		*,
		owner_id: str,
		size: int,
		tier: str,
		checksum: str,
		now: str,
		lease_expires_at: str,
		audit_context: AuditContext | None = None,
	) -> MoveJob: ...


class Catalog(CatalogStore):
	"""A tiny in-memory catalog for objects and their current tier/metadata.

	This implementation is useful for isolated inline operations and tests.
	"""

	def __init__(
		self,
		*,
		audit_retention: AuditRetentionPolicy | None = None,
	) -> None:
		self._objects: Dict[tuple[str, str], ObjectRecord] = {}
		self._tiers: dict[str, Tier] = {}
		self._pools: dict[str, Pool] = {}
		self._budgets: dict[str, BudgetDefinition] = {}
		self._budget_reservations: dict[tuple[str, str, int], dict[str, Any]] = {}
		self._object_contents: Dict[tuple[str, str], ObjectContent] = {}
		self._content_manifests: Dict[tuple[object, ...], ObjectContent] = {}
		self._content_blobs: Dict[str, _ContentBlobState] = {}
		self._move_jobs: Dict[str, MoveJob] = {}
		self._move_transitions: Dict[str, List[MoveJobTransition]] = {}
		self._access_events: Dict[str, AccessEvent] = {}
		self._pruned_access_events: set[str] = set()
		self._audit_events: Dict[str, AuditEvent] = {}
		self._audit_move_heads: Dict[str, tuple[int, str]] = {}
		self._audit_event_tombstones: Dict[
			str,
			tuple[str, str | None, str | None],
		] = {}
		self.audit_retention = audit_retention or AuditRetentionPolicy()
		self._lock = threading.RLock()

	def configure_budget(
		self, definition: BudgetDefinition, *, audit_context: AuditContext,
		occurred_at: str | datetime | None = None,
	) -> BudgetDefinition:
		if not isinstance(definition, BudgetDefinition):
			raise ValueError("definition must be a BudgetDefinition")
		if not isinstance(audit_context, AuditContext) or audit_context.actor_type != "user":
			raise ValueError("budget configuration requires a user audit context")
		with self._lock:
			existing = self._budgets.get(definition.budget_id)
			if existing is not None:
				if existing.to_dict() != definition.to_dict():
					raise BudgetConstraintError("budget definitions are immutable; use a new budget id")
				return BudgetDefinition.from_mapping(existing.to_dict())
			if any(not job.state.terminal and definition.matches(job.bucket, job.key)
				for job in self._move_jobs.values()):
				raise BudgetConstraintError("cannot install a budget while matching moves are in flight")
			self.append_audit_event(_budget_configuration_event(definition, audit_context, occurred_at))
			self._budgets[definition.budget_id] = BudgetDefinition.from_mapping(definition.to_dict())
			return BudgetDefinition.from_mapping(definition.to_dict())

	def list_budgets(self) -> List[BudgetDefinition]:
		with self._lock:
			return [BudgetDefinition.from_mapping(item.to_dict()) for item in sorted(
				self._budgets.values(), key=lambda item: item.budget_id,
			)]

	def list_budget_reservations(self, budget_id: str | None = None) -> List[dict[str, Any]]:
		with self._lock:
			return deepcopy([item for identity, item in sorted(self._budget_reservations.items())
				if budget_id is None or identity[0] == budget_id])

	def read_budget_snapshot(self) -> tuple[List[BudgetDefinition], List[dict[str, Any]]]:
		"""Detach definitions and held reservations under one admission lock."""
		with self._lock:
			return self.list_budgets(), self.list_budget_reservations()

	@contextmanager
	def _budget_claim_transaction(self) -> Iterator[None]:
		with self._lock:
			if not self._budgets:
				yield
				return
			# Restore the complete atomic admission if any audit append fails.
			state = (
				dict(self._budget_reservations), dict(self._move_jobs),
				{name: list(items) for name, items in self._move_transitions.items()},
				dict(self._audit_events), dict(self._audit_move_heads),
			)
			try:
				yield
			except BaseException:
				(self._budget_reservations, self._move_jobs, self._move_transitions,
				 self._audit_events, self._audit_move_heads) = state
				raise

	def _ensure_active_tier(self, name: str) -> None:
		"""Keep tier-only callers compatible while honoring explicit retirement."""
		tier = self._tiers.get(name)
		if tier is None:
			self._tiers[name] = Tier(name=name)
		elif not tier.active:
			raise ValueError(f"Tier is inactive: {name}")

	def register_tier(
		self, name: str, metadata: Mapping[str, object] | None = None, *, active: bool = True
	) -> None:
		if metadata is not None and "minimum_residency_seconds" in metadata:
			validate_minimum_residency_seconds(metadata["minimum_residency_seconds"])
		with self._lock:
			existing = self._tiers.get(name)
			value = Tier(
				name=name,
				metadata=(existing.metadata if existing and metadata is None else metadata or {}),
				active=active,
			)
			if not active and (
				any(record.tier == name for record in self._objects.values())
				or any(pool.tier == name and pool.active for pool in self._pools.values())
			):
				raise ValueError(f"Tier has active references: {name}")
			self._tiers[name] = deepcopy(value)

	def get_tier(self, name: str) -> Tier | None:
		with self._lock:
			return deepcopy(self._tiers.get(name))

	def list_tiers(self) -> list[Tier]:
		with self._lock:
			return [deepcopy(self._tiers[name]) for name in sorted(self._tiers)]

	def delete_tier(self, name: str) -> None:
		with self._lock:
			if any(record.tier == name for record in self._objects.values()) or any(
				pool.tier == name for pool in self._pools.values()
			):
				raise ValueError(f"Tier is referenced: {name}")
			self._tiers.pop(name, None)

	def register_pool(
		self,
		pool_id: str,
		tier: str,
		metadata: Mapping[str, object] | None = None,
		*,
		region: str | None = None,
		members: tuple[str, ...] = (),
		localities: tuple[str, ...] = (),
		attributes: Mapping[str, AttributeValue] | None = None,
		active: bool = True,
	) -> None:
		value = Pool(
			pool_id=pool_id, tier=tier, region=region, members=members,
			localities=localities, attributes=attributes or {}, metadata=metadata or {}, active=active,
		)
		with self._lock:
			parent = self._tiers.get(tier)
			if parent is None:
				raise KeyError(f"Tier not found: {tier}")
			if not parent.active:
				raise ValueError(f"Tier is inactive: {tier}")
			existing = self._pools.get(pool_id)
			if existing and (existing.tier != tier or not active) and any(
				record.pool_id == pool_id for record in self._objects.values()
			):
				raise ValueError(f"Pool is referenced: {pool_id}")
			self._pools[pool_id] = deepcopy(value)

	def get_pool(self, pool_id: str) -> Pool | None:
		with self._lock:
			return deepcopy(self._pools.get(pool_id))

	def list_pools(self, *, tier: str | None = None) -> list[Pool]:
		with self._lock:
			return [
				deepcopy(pool) for _, pool in sorted(self._pools.items())
				if tier is None or pool.tier == tier
			]

	def delete_pool(self, pool_id: str) -> None:
		with self._lock:
			if any(record.pool_id == pool_id for record in self._objects.values()):
				raise ValueError(f"Pool is referenced: {pool_id}")
			self._pools.pop(pool_id, None)

	def assign_pool(self, bucket: str, key: str, pool_id: str | None) -> None:
		with self._lock:
			record = self._objects.get((bucket, key))
			if record is None:
				raise KeyError(f"Object not found: {bucket}/{key}")
			if pool_id is not None:
				pool = self._pools.get(pool_id)
				if pool is None:
					raise KeyError(f"Pool not found: {pool_id}")
				if not pool.active or not self._tiers[pool.tier].active:
					raise ValueError(f"Pool or tier is inactive: {pool_id}")
				if record.tier != pool.tier:
					record.placement_started_at = _content_reference_timestamp()
					record.last_tier_move_at = record.placement_started_at
				record.tier = pool.tier
			record.pool_id = pool_id

	def eligible_placements(
		self,
		constraints: PlacementConstraints | None = None,
		*,
		now: str | datetime | None = None,
	) -> list[PlacementCandidate]:
		with self._lock:
			return eligible_candidates(
				self._tiers.values(), self._pools.values(), constraints, now=now
			)

	@staticmethod
	def _content_manifest_key(content: ObjectContent) -> tuple[object, ...]:
		return (
			content.sha256,
			content.schema_version,
			content.representation,
			content.chunking_algorithm,
			content.chunking_version,
			content.chunk_size,
		)

	def _canonical_content(self, content: ObjectContent, *, now: str) -> ObjectContent:
		"""Intern one immutable manifest and validate its global blob identities."""

		manifest_key = self._content_manifest_key(content)
		canonical = self._content_manifests.get(manifest_key)
		if canonical is not None and canonical != content:
			raise ValueError("content manifest chunks conflict with the persisted layout")

		descriptors: Dict[str, tuple[int, str]] = {}
		for sha256, size, cas_key, _is_object in _content_blob_descriptors(content):
			descriptor = (size, cas_key)
			previous = descriptors.setdefault(sha256, descriptor)
			if previous != descriptor:
				raise ValueError(f"content blob identity collision for sha256 {sha256}")
			persisted = self._content_blobs.get(sha256)
			if persisted is not None and (persisted.size, persisted.cas_key) != descriptor:
				raise ValueError(f"content blob identity collision for sha256 {sha256}")

		if canonical is None:
			canonical = content
			self._content_manifests[manifest_key] = canonical
		for sha256, (size, cas_key) in descriptors.items():
			self._content_blobs.setdefault(
				sha256,
				_ContentBlobState(
					size=size,
					cas_key=cas_key,
					reference_count=0,
					unreferenced_at=now,
				),
			)
		return canonical

	def _replace_object_content(
		self,
		object_key: tuple[str, str],
		content: ObjectContent | None,
	) -> None:
		"""Atomically replace one logical mapping and its global edge counts.

		The in-memory backend owns a single re-entrant lock, so callers invoke this
		helper only while holding ``self._lock``. Reference multiplicity matches the
		durable catalog: one full-object edge plus every chunk-position edge.
		"""

		previous = self._object_contents.get(object_key)
		now = _content_reference_timestamp()
		canonical = None if content is None else self._canonical_content(content, now=now)
		if previous is canonical:
			return

		deltas: Counter[str] = Counter()
		if previous is not None:
			for sha256, _size, _cas_key, _is_object in _content_blob_descriptors(previous):
				deltas[sha256] -= 1
		if canonical is not None:
			for sha256, _size, _cas_key, _is_object in _content_blob_descriptors(canonical):
				deltas[sha256] += 1

		for sha256, delta in deltas.items():
			state = self._content_blobs.get(sha256)
			if state is None or state.reference_count + delta < 0:
				raise RuntimeError(
					f"content reference invariant violated for sha256 {sha256}"
				)

		for sha256 in sorted(deltas):
			delta = deltas[sha256]
			if delta == 0:
				continue
			state = self._content_blobs[sha256]
			previous_count = state.reference_count
			state.reference_count += delta
			if state.reference_count == 0:
				if previous_count > 0:
					state.unreferenced_at = now
			else:
				state.unreferenced_at = None

		if canonical is None:
			self._object_contents.pop(object_key, None)
		else:
			self._object_contents[object_key] = canonical

	def upsert(
		self,
		bucket: str,
		key: str,
		size: int,
		tier: str,
		metadata: Optional[Dict[str, object]] = None,
	) -> None:
		validate_catalog_size(size)
		persisted_metadata = deepcopy(metadata) if metadata is not None else {}
		# ``content_identity`` is a catalog-owned projection of the normalized
		# object-to-manifest mapping. A generic object write invalidates that
		# mapping, so it must not be able to leave (or forge) the reserved header.
		persisted_metadata.pop("content_identity", None)
		with self._lock:
			object_key = (bucket, key)
			self._ensure_active_tier(tier)
			existing = self._objects.get(object_key)
			self._replace_object_content(object_key, None)
			now = _content_reference_timestamp()
			rec = ObjectRecord(
				bucket=bucket,
				key=key,
				size=size,
				tier=tier,
				metadata=persisted_metadata,
				pool_id=existing.pool_id if existing and existing.tier == tier else None,
				placement_started_at=(
					existing.placement_started_at if existing and existing.tier == tier
					else now
				),
				last_tier_move_at=(
					None if existing is None else (
						existing.last_tier_move_at if existing.tier == tier else now
					)
				),
				importance=deepcopy(existing.importance) if existing else None,
				importance_revision=existing.importance_revision if existing else 0,
			)
			self._objects[object_key] = rec

	def capture_scan_fence(self, bucket: str, key: str) -> ScanFence:
		"""Capture the move generations that can affect one scan observation."""

		with self._lock:
			jobs = self._scan_move_jobs(bucket, key)
			return ScanFence(
				bucket=bucket,
				key=key,
				move_jobs=self._scan_move_job_fingerprints(jobs),
			)

	def upsert_scan_observation(
		self,
		bucket: str,
		key: str,
		*,
		size: int,
		tier: str,
		generation: str,
		metadata: Optional[Dict[str, object]],
		fence: ScanFence,
		content: ObjectContent | None = None,
	) -> bool:
		"""Publish a stable scan observation unless a move makes it stale.

		The fence comparison and object write share the catalog lock. A move that
		starts or changes state after the scan begins therefore either precedes
		this write (and rejects it) or follows it (and later reasserts placement).
		"""

		if (fence.bucket, fence.key) != (bucket, key):
			raise ValueError("scan fence does not identify the observed object")
		if not isinstance(generation, str) or not generation:
			raise ValueError("scan observation requires a non-empty generation")
		validate_catalog_size(size)
		if content is not None and content.size != size:
			raise ValueError("content size must match the scan observation size")

		with self._lock:
			jobs = self._scan_move_jobs(bucket, key)
			if self._scan_move_job_fingerprints(jobs) != fence.move_jobs:
				return False
			if not self._scan_observation_is_authoritative(
				jobs, tier=tier, generation=generation
			):
				return False
			object_key = (bucket, key)
			self._ensure_active_tier(tier)
			existing = self._objects.get(object_key)
			existing_content = self._object_contents.get(object_key)
			content_changed = existing_content != content
			incoming_metadata = metadata or {}
			merged_metadata = (
				deepcopy(existing.metadata) if existing is not None else {}
			)
			if metadata is not None:
				merged_metadata.update(deepcopy(metadata))
			if content_changed and "document_extraction" not in incoming_metadata:
				# Extraction text is bound to exact source bytes. A partial scan
				# observation must not carry it across a content replacement.
				merged_metadata.pop("document_extraction", None)
			existing_extraction = (
				existing.metadata.get("document_extraction")
				if existing is not None
				else None
			)
			extraction_changed = (
				existing_extraction != merged_metadata.get("document_extraction")
			)
			if content_changed or extraction_changed:
				# MIME selection and provenance are observations of the same source
				# bytes/extraction. Omitted evidence may be merged only while that
				# source identity remains unchanged.
				if "mime" not in incoming_metadata:
					merged_metadata.pop("mime", None)
				if "mime_detection" not in incoming_metadata:
					merged_metadata.pop("mime_detection", None)
			if content is None:
				merged_metadata.pop("content_identity", None)
			else:
				merged_metadata["sha256"] = content.sha256
				merged_metadata["content_identity"] = content.to_metadata()
			self._replace_object_content(object_key, content)
			now = _content_reference_timestamp()
			self._objects[object_key] = ObjectRecord(
				bucket=bucket,
				key=key,
				size=size,
				tier=tier,
				metadata=merged_metadata,
				pool_id=existing.pool_id if existing and existing.tier == tier else None,
				placement_started_at=(
					existing.placement_started_at if existing and existing.tier == tier
					else now
				),
				last_tier_move_at=(
					None if existing is None else (
						existing.last_tier_move_at if existing.tier == tier else now
					)
				),
				importance=deepcopy(existing.importance) if existing else None,
				importance_revision=existing.importance_revision if existing else 0,
			)
			return True

	def get(self, bucket: str, key: str) -> Optional[ObjectRecord]:
		with self._lock:
			record = self._objects.get((bucket, key))
			return None if record is None else self._copy_object_record(record)

	def set_importance(
		self, bucket: str, key: str, tag: ImportanceTag | None, *,
		audit_context: AuditContext, occurred_at: str | datetime | None = None,
		provenance: str | None = None,
	) -> ObjectRecord:
		"""Atomically replace trusted importance and append its actor's audit event."""

		_validate_importance_actor(tag, audit_context, provenance)
		with self._lock:
			record = self._objects.get((bucket, key))
			if record is None:
				raise KeyError(f"Object not found: {bucket}/{key}")
			updated = replace(
				record, importance=deepcopy(tag),
				importance_revision=record.importance_revision + 1,
			)
			event = _importance_event(record, updated, audit_context, occurred_at, provenance)
			# Validate and append first: a rejected audit must leave the tag unchanged.
			self.append_audit_event(event)
			self._objects[(bucket, key)] = updated
			return self._copy_object_record(updated)

	def get_object_content(self, bucket: str, key: str) -> ObjectContent | None:
		with self._lock:
			content = self._object_contents.get((bucket, key))
			return None if content is None else deepcopy(content)

	def reconcile_content_references(
		self,
		*,
		grace_period_seconds: float,
		now: str | datetime | None = None,
	) -> ContentReferenceReport:
		"""Return a stable, non-mutating snapshot of CAS reference invariants."""

		with self._lock:
			object_references: Counter[str] = Counter()
			chunk_references: Counter[str] = Counter()
			for content in self._object_contents.values():
				object_references[content.sha256] += 1
				for chunk in content.chunks:
					chunk_references[chunk.sha256] += 1
			snapshots = tuple(
				ContentReferenceSnapshot(
					sha256=sha256,
					cas_key=state.cas_key,
					size=state.size,
					stored_reference_count=state.reference_count,
					expected_object_reference_count=object_references[sha256],
					expected_chunk_reference_count=chunk_references[sha256],
					unreferenced_at=state.unreferenced_at,
				)
				for sha256, state in sorted(self._content_blobs.items())
			)
		return build_content_reference_report(
			snapshots,
			grace_period_seconds=grace_period_seconds,
			now=now,
		)

	def update_placement(self, bucket: str, key: str, tier: str) -> None:
		with self._lock:
			rec = self._objects.get((bucket, key))
			if not rec:
				raise KeyError(f"Object not found: {bucket}/{key}")
			self._ensure_active_tier(tier)
			if rec.tier != tier:
				rec.pool_id = None
				rec.placement_started_at = _content_reference_timestamp()
				rec.last_tier_move_at = rec.placement_started_at
			rec.tier = tier

	def upsert_placement(
		self,
		bucket: str,
		key: str,
		*,
		size: int,
		tier: str,
		checksum: Optional[str] = None,
	) -> None:
		"""Commit verified placement data without replacing other metadata."""

		validate_catalog_size(size)
		with self._lock:
			self._ensure_active_tier(tier)
			rec = self._objects.get((bucket, key))
			metadata = deepcopy(rec.metadata) if rec is not None else {}
			if checksum is not None:
				metadata["sha256"] = checksum
			content = self._object_contents.get((bucket, key))
			content_mismatch = content is not None and (
				content.size != size
				or (checksum is not None and content.sha256 != checksum)
			)
			if content_mismatch:
				metadata.pop("content_identity", None)
				if checksum is None:
					metadata.pop("sha256", None)
				self._replace_object_content((bucket, key), None)
			now = _content_reference_timestamp()
			self._objects[(bucket, key)] = ObjectRecord(
				bucket=bucket,
				key=key,
				size=size,
				tier=tier,
				metadata=metadata,
				pool_id=rec.pool_id if rec and rec.tier == tier else None,
				placement_started_at=(
					rec.placement_started_at if rec and rec.tier == tier
					else now
				),
				last_tier_move_at=(
					None if rec is None else (
						rec.last_tier_move_at if rec.tier == tier else now
					)
				),
				importance=deepcopy(rec.importance) if rec else None,
				importance_revision=rec.importance_revision if rec else 0,
			)

	def delete(self, bucket: str, key: str) -> None:
		with self._lock:
			object_key = (bucket, key)
			self._replace_object_content(object_key, None)
			self._objects.pop(object_key, None)

	def list(self, bucket: str, prefix: str = "") -> List[ObjectRecord]:
		with self._lock:
			records = sorted(
				(
					record
					for (record_bucket, key), record in self._objects.items()
					if record_bucket == bucket and key.startswith(prefix)
				),
				key=lambda record: record.key,
			)
			return [self._copy_object_record(record) for record in records]

	def list_page(
		self,
		bucket: str,
		prefix: str = "",
		*,
		after_key: str | None = None,
		limit: int = 100,
		tier: str | None = None,
	) -> List[ObjectRecord]:
		"""Return one bounded keyset page in backend-neutral key order."""

		if after_key is not None and not isinstance(after_key, str):
			raise ValueError("after_key must be a string or null")
		if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
			raise ValueError("limit must be a positive integer")
		if tier is not None and not isinstance(tier, str):
			raise ValueError("tier must be a string or null")
		with self._lock:
			records = sorted(
				(
					record
					for (record_bucket, key), record in self._objects.items()
					if record_bucket == bucket
					and key.startswith(prefix)
					and (after_key is None or key > after_key)
					and (tier is None or record.tier == tier)
				),
				key=lambda record: record.key,
			)
			return [
				self._copy_object_record(record) for record in records[:limit]
			]

	def iter_objects(self, *, batch_size: int = 1000) -> Iterator[ObjectRecord]:
		"""Yield detached snapshots for every object in bucket/key order."""

		if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
			raise ValueError("batch_size must be a positive integer")
		with self._lock:
			records = tuple(
				self._copy_object_record(record)
				for record in sorted(
					self._objects.values(),
					key=lambda record: (record.bucket, record.key),
				)
			)
		yield from records

	@staticmethod
	def _copy_object_record(record: ObjectRecord) -> ObjectRecord:
		"""Copy one record without preserving aliases to mutable fields."""

		return deepcopy(record)

	def append_access_event(self, event: AccessEvent) -> AccessEvent:
		"""Append once per logical operation, retaining the first observation."""
		if not isinstance(event, AccessEvent):
			raise TypeError("event must be an AccessEvent")
		with self._lock:
			existing = self._access_events.get(event.event_id)
			if existing is not None:
				if existing.replay_identity() != event.replay_identity():
					raise ValueError("access event ID conflicts with persisted identity")
				return existing
			self._access_events[event.event_id] = event
			return event

	def aggregate_access_events(
		self,
		bucket: str,
		key: str | None,
		*,
		config: AccessConfig,
		as_of: str | datetime,
	) -> AccessSnapshot:
		instant = access_timestamp(as_of)
		lower = access_cutoff(instant, config.retention_seconds)
		cutoffs = tuple(access_cutoff(instant, seconds) for seconds in config.windows_seconds)
		counts = [[0 for _kind in ACCESS_KINDS] for _window in config.windows_seconds]
		estimates = [[0.0 for _kind in ACCESS_KINDS] for _window in config.windows_seconds]
		total = 0
		first: str | None = None
		last: str | None = None
		minimum_rate: float | None = None
		with self._lock:
			for event in self._access_events.values():
				if (
					event.event_id in self._pruned_access_events
					or (event.bucket, event.key) != (bucket, key)
					or not lower < event.occurred_at <= instant
				):
					continue
				total += 1
				first = min(first, event.occurred_at) if first is not None else event.occurred_at
				last = max(last, event.occurred_at) if last is not None else event.occurred_at
				minimum_rate = (
					min(minimum_rate, event.sample_rate)
					if minimum_rate is not None
					else event.sample_rate
				)
				kind_index = ACCESS_KINDS.index(event.kind)
				for index, cutoff in enumerate(cutoffs):
					if event.occurred_at > cutoff:
						counts[index][kind_index] += 1
						estimates[index][kind_index] += 1 / event.sample_rate
		return AccessSnapshot(
			windows=tuple(
				AccessWindow(seconds, tuple(counts[index]), tuple(estimates[index]))
				for index, seconds in enumerate(config.windows_seconds)
			),
			observed_events=total,
			observed_since=first,
			last_access_at=last,
			minimum_sample_rate=minimum_rate,
		)

	def prune_access_events(
		self,
		occurred_before: str | datetime,
		*,
		limit: int = 1000,
		retention_seconds: int = 2592000,
	) -> int:
		"""Expire a bounded batch, retaining retry IDs for one extra horizon.

		The returned count measures expired observations plus purged identities. Expired identities
		older than cutoff minus retention_seconds are physically removed in a
		separate bounded batch. Retry IDs are not promised beyond that horizon.
		"""
		if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
			raise ValueError("limit must be an integer from 1 to 10000")
		config = AccessConfig(windows_seconds=(1,), retention_seconds=retention_seconds)
		cutoff = access_timestamp(occurred_before)
		dedup_cutoff = access_cutoff(cutoff, config.retention_seconds)
		with self._lock:
			purge = sorted(
				(
					event
					for event in self._access_events.values()
					if event.event_id in self._pruned_access_events
					and event.occurred_at < dedup_cutoff
				),
				key=lambda event: (event.occurred_at, event.event_id),
			)[:limit]
			for event in purge:
				del self._access_events[event.event_id]
				self._pruned_access_events.remove(event.event_id)
			expire = sorted(
				(
					event
					for event in self._access_events.values()
					if event.event_id not in self._pruned_access_events
					and event.occurred_at < cutoff
				),
				key=lambda event: (event.occurred_at, event.event_id),
			)[:limit]
			self._pruned_access_events.update(event.event_id for event in expire)
			return len(expire) + len(purge)

	def append_audit_event(self, event: AuditEvent) -> AuditEvent:
		"""Append one redacted event, idempotently by event UUID."""

		if not isinstance(event, AuditEvent):
			raise ValueError("event must be an AuditEvent")
		with self._lock:
			direct_replay = self._audit_events.get(event.event_id)
			if direct_replay == event:
				return self._copy_audit_event(direct_replay)
			tombstone = self._audit_event_tombstones.get(event.event_id)
			safe = redact_audit_event(
				replace(
					event,
					expires_at=self.audit_retention.expires_at(event.occurred_at),
				),
				_allow_pseudonyms=tombstone is not None,
			)
			if tombstone is not None:
				replay_digest, causation_id, expires_at = tombstone
				if audit_event_replay_digest(safe) != replay_digest:
					raise ValueError(
						f"audit event {safe.event_id} was pruned with different data"
					)
				return self._copy_audit_event(
					replace(
						safe,
						causation_id=causation_id,
						expires_at=expires_at,
					)
				)
			existing = self._audit_events.get(safe.event_id)
			if existing is not None:
				expected = (
					replace(safe, causation_id=existing.causation_id)
					if safe.move_id is not None
					else safe
				)
				if replace(existing, expires_at=safe.expires_at) != expected:
					raise ValueError(
						f"audit event {safe.event_id} already exists with different data"
					)
				return self._copy_audit_event(existing)
			move_id = safe.move_id
			if move_id is not None:
				sequence, previous_id = self._audit_move_heads.get(
					move_id,
					(0, ""),
				)
				if previous_id:
					safe = replace(safe, causation_id=previous_id)
				sequence += 1
				self._audit_move_heads[move_id] = (sequence, safe.event_id)
			self._audit_events[safe.event_id] = safe
			return self._copy_audit_event(safe)

	def get_audit_event(self, event_id: str) -> AuditEvent | None:
		try:
			identifier = str(UUID(str(event_id)))
		except (AttributeError, TypeError, ValueError) as exc:
			raise ValueError("event_id must be a UUID") from exc
		with self._lock:
			event = self._audit_events.get(identifier)
			return None if event is None else self._copy_audit_event(event)

	def list_audit_events(self, query: AuditQuery | None = None) -> List[AuditEvent]:
		criteria = query or AuditQuery()
		if not isinstance(criteria, AuditQuery):
			raise ValueError("query must be an AuditQuery")
		with self._lock:
			events = [
				event
				for event in self._audit_events.values()
				if self._audit_event_matches(event, criteria)
			]
		events.sort(
			key=lambda event: (event.occurred_at, event.event_id),
			reverse=not criteria.ascending,
		)
		return [
			self._copy_audit_event(event)
			for event in events[: criteria.limit]
		]

	def prune_audit_events(
		self,
		occurred_before: str | datetime,
		*,
		limit: int = 1000,
	) -> int:
		query_value = (
			occurred_before.isoformat()
			if isinstance(occurred_before, datetime)
			else occurred_before
		)
		cutoff = AuditQuery(occurred_before=query_value, limit=limit).occurred_before
		assert cutoff is not None
		return self._prune_audit_events(
			lambda event: event.occurred_at < cutoff,
			limit=limit,
		)

	def prune_expired_audit_events(
		self,
		now: str | datetime,
		*,
		limit: int = 1000,
	) -> int:
		query_value = now.isoformat() if isinstance(now, datetime) else now
		cutoff = AuditQuery(occurred_before=query_value, limit=limit).occurred_before
		assert cutoff is not None
		return self._prune_audit_events(
			lambda event: event.expires_at is not None and event.expires_at <= cutoff,
			limit=limit,
		)

	def _prune_audit_events(
		self,
		eligible: Callable[[AuditEvent], bool],
		*,
		limit: int,
	) -> int:
		with self._lock:
			identifiers = [
				event.event_id
				for event in sorted(
					self._audit_events.values(),
					key=lambda item: (item.occurred_at, item.event_id),
				)
				if eligible(event)
			][:limit]
			for identifier in identifiers:
				event = self._audit_events.pop(identifier)
				self._audit_event_tombstones[identifier] = (
					audit_event_replay_digest(event),
					event.causation_id,
					event.expires_at,
				)
			return len(identifiers)

	@staticmethod
	def _copy_audit_event(event: AuditEvent) -> AuditEvent:
		return replace(event, details=deepcopy(event.details))

	@staticmethod
	def _audit_event_matches(event: AuditEvent, query: AuditQuery) -> bool:
		return all(
			(
				query.correlation_id is None
				or event.correlation_id == query.correlation_id,
				query.job_id is None or event.job_id == query.job_id,
				query.move_id is None or event.move_id == query.move_id,
				query.bucket is None or event.bucket == query.bucket,
				query.object_key is None or event.object_key == query.object_key,
				query.policy_name is None or event.policy_name == query.policy_name,
				query.policy_version is None
				or event.policy_version == query.policy_version,
				query.actor_type is None or event.actor_type == query.actor_type,
				query.actor_id is None or event.actor_id == query.actor_id,
				query.event_types is None or event.event_type in query.event_types,
				query.outcomes is None or event.outcome in query.outcomes,
				query.occurred_after is None
				or event.occurred_at >= query.occurred_after,
				query.occurred_before is None
				or event.occurred_at < query.occurred_before,
				query.before_event is None
				or (event.occurred_at, event.event_id) < query.before_event,
			)
		)

	def claim_move_job(
		self,
		idempotency_key: str,
		*,
		src_tier: str,
		dst_tier: str,
		bucket: str,
		key: str,
		expected_size: int,
		source_metadata: Mapping[str, Any],
		owner_id: str,
		now: str,
		lease_expires_at: str,
		audit_context: AuditContext | None = None,
	) -> MoveJob:
		"""Create or exclusively claim a move job by idempotency key."""

		if not idempotency_key.strip():
			raise ValueError("idempotency_key must be a non-empty string")
		validate_catalog_size(expected_size, field="expected_size")
		with self._budget_claim_transaction():
			existing = self._move_jobs.get(idempotency_key)
			if existing is not None:
				self._assert_same_move(
					existing, src_tier=src_tier, dst_tier=dst_tier,
					bucket=bucket, key=key, source_metadata=source_metadata,
				)
			if existing is not None:
				if existing.state.terminal:
					return existing
			if existing is None or existing.state in {
				MoveJobState.PREPARED, MoveJobState.TRANSFERRED, MoveJobState.VERIFIED,
			}:
				tier_definition = self._tiers.get(src_tier)
				_assert_catalog_move_allowed(
					self._objects.get((bucket, key)), src_tier, dst_tier,
					existing.source_metadata if existing is not None else source_metadata,
					tier_definition.metadata if tier_definition else None, now,
				)
			if existing is not None and (existing.owner_id not in (None, owner_id)
				and existing.lease_expires_at is not None and existing.lease_expires_at > now):
				raise MoveJobLeaseError(f"Move job {idempotency_key!r} is leased by {existing.owner_id!r}")
			reservations, source_metadata = _prepare_budget_reservations(
				list(self._budgets.values()), list(self._budget_reservations.values()),
				self._pools, self._objects.get((bucket, key)),
				move_id=idempotency_key, bucket=bucket, key=key, src_tier=src_tier,
				dst_tier=dst_tier, size=existing.expected_size if existing else expected_size,
				source_metadata=existing.source_metadata if existing else source_metadata,
				now=now, audit_context=audit_context, trusted_override=existing is not None,
			)
			for reservation in reservations:
				context = self._move_audit_context(
					idempotency_key, owner_id=owner_id, audit_context=audit_context,
					causation_id=None if audit_context is None else audit_context.causation_id,
				)
				self.append_audit_event(_budget_reservation_event(reservation, context))
				self._budget_reservations[(reservation["budget_id"], idempotency_key, reservation["attempt"])] = reservation
			if existing is None:
				job = MoveJob(
					idempotency_key=idempotency_key,
					src_tier=src_tier,
					dst_tier=dst_tier,
					bucket=bucket,
					key=key,
					expected_size=expected_size,
					source_metadata=dict(source_metadata),
					state=MoveJobState.PREPARED,
					owner_id=owner_id,
					lease_expires_at=lease_expires_at,
					created_at=now,
					updated_at=now,
				)
				self._move_jobs[idempotency_key] = job
				transition = self._append_move_transition(
					job, None, MoveJobState.PREPARED, "move prepared", now
				)
				try:
					self._append_move_audit_event(
						job,
						transition,
						owner_id=owner_id,
						audit_context=audit_context,
					)
				except BaseException:
					self._remove_last_move_transition(transition)
					self._move_jobs.pop(idempotency_key, None)
					raise
				return job

			if existing.state.terminal:
				return existing
			if (
				existing.owner_id not in (None, owner_id)
				and existing.lease_expires_at is not None
				and existing.lease_expires_at > now
			):
				raise MoveJobLeaseError(
					f"Move job {idempotency_key!r} is leased by "
					f"{existing.owner_id!r} until {existing.lease_expires_at}"
				)
			claimed = self._replace_move_job(
				existing,
				owner_id=owner_id,
				lease_expires_at=lease_expires_at,
				updated_at=now,
				source_metadata=source_metadata,
			)
			self._move_jobs[idempotency_key] = claimed
			try:
				self._append_move_retry_audit_event(
					existing,
					claimed,
					owner_id=owner_id,
					audit_context=audit_context,
				)
			except BaseException:
				self._move_jobs[idempotency_key] = existing
				raise
			return claimed

	def get_move_job(self, idempotency_key: str) -> MoveJob | None:
		with self._lock:
			return self._move_jobs.get(idempotency_key)

	def list_move_jobs(
		self,
		*,
		states: set[MoveJobState] | None = None,
		idempotency_prefix: str | None = None,
	) -> List[MoveJob]:
		with self._lock:
			return [
				job
				for job in self._move_jobs.values()
				if (states is None or job.state in states)
				and (
					idempotency_prefix is None
					or job.idempotency_key.startswith(idempotency_prefix)
				)
			]

	def list_move_job_transitions(
		self, idempotency_key: str
	) -> List[MoveJobTransition]:
		with self._lock:
			return list(self._move_transitions.get(idempotency_key, ()))

	def list_move_jobs_page(
		self,
		bucket: str,
		prefix: str = "",
		*,
		after_id: str | None = None,
		limit: int = 100,
	) -> List[MoveJob]:
		"""Read a bounded, detached job page for one bucket and literal key prefix.

		The exclusive cursor is an idempotency key in backend-neutral string
		order. Jobs inserted behind the cursor are observed by the next scan.
		"""

		if not isinstance(bucket, str) or not bucket:
			raise ValueError("bucket must be a non-empty string")
		if not isinstance(prefix, str):
			raise ValueError("prefix must be a string")
		if after_id is not None and not isinstance(after_id, str):
			raise ValueError("after_id must be a string or null")
		if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
			raise ValueError("limit must be a positive integer")
		with self._lock:
			jobs = nsmallest(
				limit,
				(
					job for job in self._move_jobs.values()
					if job.bucket == bucket and job.key.startswith(prefix)
					and (after_id is None or job.idempotency_key > after_id)
				),
				key=lambda job: job.idempotency_key,
			)
			return deepcopy(jobs)

	def renew_move_job_lease(
		self,
		idempotency_key: str,
		*,
		owner_id: str,
		expected_state: MoveJobState | None,
		now: str,
		lease_expires_at: str,
	) -> MoveJob:
		"""Extend an owned move lease without creating a state transition."""

		with self._lock:
			if expected_state is None:
				job = self._move_jobs.get(idempotency_key)
				if job is None:
					raise KeyError(f"Move job not found: {idempotency_key}")
				# A background heartbeat may race the final transition. Terminal
				# jobs no longer have a lease, so treating that race as a no-op is
				# safe and prevents a completed move from reporting a false failure.
				if job.state.terminal:
					return job
				if job.owner_id != owner_id:
					raise MoveJobLeaseError(
						f"Move job {idempotency_key!r} is not owned by {owner_id!r}"
					)
			else:
				job = self._owned_move_job(idempotency_key, owner_id, expected_state)
			updated = self._replace_move_job(
				job,
				lease_expires_at=lease_expires_at,
				updated_at=now,
			)
			self._move_jobs[idempotency_key] = updated
			return updated

	def transition_move_job(
		self,
		idempotency_key: str,
		*,
		owner_id: str,
		expected_state: MoveJobState,
		to_state: MoveJobState,
		reason: str,
		now: str,
		lease_expires_at: str,
		updates: Mapping[str, Any] | None = None,
		audit_context: AuditContext | None = None,
	) -> MoveJob:
		validate_move_job_transition(expected_state, to_state)
		changes = dict(redact(dict(updates or {})))
		reason = redact_text(reason)
		if "verification_details" in changes:
			changes["verification_details"] = tuple(changes["verification_details"])
		with self._lock:
			job = self._owned_move_job(idempotency_key, owner_id, expected_state)
			updated = self._replace_move_job(
				job,
				state=to_state,
				owner_id=None if to_state.terminal else owner_id,
				lease_expires_at=None if to_state.terminal else lease_expires_at,
				updated_at=now,
				**changes,
			)
			self._move_jobs[idempotency_key] = updated
			transition = self._append_move_transition(
				job, expected_state, to_state, reason, now
			)
			try:
				self._append_move_audit_event(
					updated,
					transition,
					owner_id=owner_id,
					audit_context=audit_context,
					updates=changes,
				)
			except BaseException:
				self._remove_last_move_transition(transition)
				self._move_jobs[idempotency_key] = job
				raise
			return updated

	def commit_move_job_placement(
		self,
		idempotency_key: str,
		*,
		owner_id: str,
		size: int,
		tier: str,
		checksum: str,
		now: str,
		lease_expires_at: str,
		audit_context: AuditContext | None = None,
	) -> MoveJob:
		"""Atomically commit placement and the verified job checkpoint."""

		validate_move_job_transition(
			MoveJobState.VERIFIED, MoveJobState.COMMITTED
		)
		validate_catalog_size(size)
		with self._lock:
			job = self._owned_move_job(
				idempotency_key, owner_id, MoveJobState.VERIFIED
			)
			object_key = (job.bucket, job.key)
			previous_object = self._objects.get(object_key)
			if tier != job.dst_tier:
				raise MoveJobConflictError("Committed tier must match move destination")
			tier_definition = self._tiers.get(job.src_tier)
			_assert_catalog_move_allowed(
				previous_object, job.src_tier, tier, job.source_metadata,
				tier_definition.metadata if tier_definition else None, now,
			)
			pool_id = job.source_metadata.get("cognistore_destination_pool_id")
			if pool_id is not None:
				pool = self._pools.get(pool_id)
				if pool is None or not pool.active or pool.tier != tier:
					raise BudgetConstraintError("budget destination pool is no longer available")
			previous_content = self._object_contents.get(object_key)
			self.upsert_placement(
				job.bucket, job.key, size=size, tier=tier, checksum=checksum
			)
			if pool_id is not None:
				self._objects[object_key].pool_id = pool_id
			if previous_object is None or previous_object.tier != tier:
				self._objects[object_key].placement_started_at = now
			if job.src_tier != tier:
				# Even an unscanned source has undergone a verified tier move.
				self._objects[object_key].last_tier_move_at = now
			updated = self._replace_move_job(
				job,
				state=MoveJobState.COMMITTED,
				updated_at=now,
				lease_expires_at=lease_expires_at,
			)
			self._move_jobs[idempotency_key] = updated
			transition = self._append_move_transition(
				job,
				MoveJobState.VERIFIED,
				MoveJobState.COMMITTED,
				"catalog placement committed",
				now,
			)
			try:
				self._append_move_audit_event(
					updated,
					transition,
					owner_id=owner_id,
					audit_context=audit_context,
				)
			except BaseException:
				self._remove_last_move_transition(transition)
				self._move_jobs[idempotency_key] = job
				if previous_object is None:
					self._objects.pop(object_key, None)
				else:
					self._objects[object_key] = previous_object
				self._replace_object_content(object_key, previous_content)
				raise
			return updated

	def _scan_move_jobs(self, bucket: str, key: str) -> List[MoveJob]:
		return sorted(
			(
				job
				for job in self._move_jobs.values()
				if job.bucket == bucket and job.key == key
			),
			key=self._scan_move_job_order,
		)

	@staticmethod
	def _scan_move_job_order(job: MoveJob) -> tuple[str, str, str]:
		return (job.created_at, job.updated_at, job.idempotency_key)

	@classmethod
	def _scan_move_job_fingerprints(
		cls, jobs: List[MoveJob]
	) -> tuple[MoveJobScanFingerprint, ...]:
		return tuple(cls._scan_move_job_fingerprint(job) for job in jobs)

	@staticmethod
	def _scan_move_job_fingerprint(job: MoveJob) -> MoveJobScanFingerprint:
		source_generation = job.source_metadata.get("generation")
		return (
			job.idempotency_key,
			job.state.value,
			job.src_tier,
			job.dst_tier,
			source_generation if isinstance(source_generation, str) else None,
			job.destination_generation,
			job.created_at,
			job.updated_at,
		)

	@staticmethod
	def _scan_observation_is_authoritative(
		jobs: List[MoveJob], *, tier: str, generation: str
	) -> bool:
		for job in jobs:
			# While a move is live, either physical copy may be transient and the
			# move's atomic commit/recovery path owns catalog placement.
			if not job.state.terminal:
				return False

		if not jobs:
			return True

		# Older terminal jobs are historical evidence. The newest terminal move
		# is authoritative for placement and supersedes an earlier failure for
		# the same key.
		latest = jobs[-1]
		source_generation = latest.source_metadata.get("generation")
		if (
			latest.state == MoveJobState.COMPLETED
			and tier == latest.src_tier
			and generation == source_generation
		):
			# A scan may have read the source before cleanup and reached the
			# catalog only after the source generation was retired.
			return False
		if latest.state == MoveJobState.FAILED and tier == latest.dst_tier:
			# A failed destination is evidence, not authoritative placement. This
			# also covers failures before a destination generation was recorded.
			return False
		return True

	@staticmethod
	def _replace_move_job(job: MoveJob, **changes: Any) -> MoveJob:
		values = dict(job.__dict__)
		values.update(changes)
		return MoveJob(**values)

	@staticmethod
	def _assert_same_move(
		job: MoveJob,
		*,
		src_tier: str,
		dst_tier: str,
		bucket: str,
		key: str,
		source_metadata: Mapping[str, Any],
	) -> None:
		actual = (job.src_tier, job.dst_tier, job.bucket, job.key)
		requested = (src_tier, dst_tier, bucket, key)
		if actual != requested:
			raise MoveJobConflictError(
				f"Idempotency key {job.idempotency_key!r} already identifies "
				f"{job.src_tier}:{job.bucket}/{job.key} -> {job.dst_tier}"
			)
		for name in ("cognistore_budget_override", "cognistore_expected_destination_pool_id"):
			if name in source_metadata and source_metadata[name] != job.source_metadata.get(name):
				raise MoveJobConflictError(f"{name} differs from the durable move contract")
		requested_digest = source_metadata.get(
			EXPECTED_SOURCE_SHA256_METADATA_KEY
		)
		if (
			requested_digest is not None
			and job.source_metadata.get(EXPECTED_SOURCE_SHA256_METADATA_KEY)
			!= requested_digest
		):
			raise MoveJobConflictError(
				f"Idempotency key {job.idempotency_key!r} already identifies "
				"a move with a different expected source digest"
			)

	def _owned_move_job(
		self,
		idempotency_key: str,
		owner_id: str,
		expected_state: MoveJobState,
	) -> MoveJob:
		job = self._move_jobs.get(idempotency_key)
		if job is None:
			raise KeyError(f"Move job not found: {idempotency_key}")
		if job.owner_id != owner_id:
			raise MoveJobLeaseError(
				f"Move job {idempotency_key!r} is not owned by {owner_id!r}"
			)
		if job.state != expected_state:
			raise RuntimeError(
				f"Move job {idempotency_key!r} is {job.state.value}, "
				f"expected {expected_state.value}"
			)
		return job

	def _append_move_transition(
		self,
		job: MoveJob,
		from_state: MoveJobState | None,
		to_state: MoveJobState,
		reason: str,
		now: str,
	) -> MoveJobTransition:
		transitions = self._move_transitions.setdefault(job.idempotency_key, [])
		transition = MoveJobTransition(
			sequence=len(transitions) + 1,
			idempotency_key=job.idempotency_key,
			from_state=from_state,
			to_state=to_state,
			reason=reason,
			created_at=now,
		)
		transitions.append(transition)
		return transition

	def _remove_last_move_transition(self, transition: MoveJobTransition) -> None:
		transitions = self._move_transitions.get(transition.idempotency_key)
		if not transitions or transitions[-1] != transition:
			raise RuntimeError("cannot roll back a non-latest move transition")
		transitions.pop()
		if not transitions:
			self._move_transitions.pop(transition.idempotency_key, None)

	def _append_move_audit_event(
		self,
		job: MoveJob,
		transition: MoveJobTransition,
		*,
		owner_id: str,
		audit_context: AuditContext | None,
		updates: Mapping[str, Any] | None = None,
	) -> AuditEvent:
		previous_event_id = self._latest_move_audit_event_id(job.idempotency_key)
		context = self._move_audit_context(
			job.idempotency_key,
			owner_id=owner_id,
			audit_context=audit_context,
			causation_id=(
				previous_event_id
				if previous_event_id is not None
				else None if audit_context is None else audit_context.causation_id
			),
		)
		event_type = AuditEventType.MOVE_TRANSITIONED
		outcome = AuditOutcome.SUCCEEDED
		if transition.to_state == MoveJobState.PREPARED:
			event_type = AuditEventType.MOVE_PREPARED
			outcome = AuditOutcome.STARTED
		elif transition.to_state == MoveJobState.COMPLETED:
			event_type = AuditEventType.MOVE_COMPLETED
		elif transition.to_state == MoveJobState.FAILED:
			event_type = AuditEventType.MOVE_FAILED
			outcome = AuditOutcome.FAILED

		details: dict[str, Any] = {
			"transition_sequence": transition.sequence,
			"from_state": (
				None
				if transition.from_state is None
				else transition.from_state.value
			),
			"to_state": transition.to_state.value,
			"reason": transition.reason,
			"src_tier": job.src_tier,
			"dst_tier": job.dst_tier,
			"expected_size": job.expected_size,
		}
		controls = job.source_metadata.get("cognistore_movement_constraints")
		if isinstance(controls, Mapping):
			details["movement_constraints"] = deepcopy(dict(controls))
			override = controls.get("stability_override")
			if override is not None:
				details["stability_override"] = deepcopy(override)
		for name, value in (updates or {}).items():
			if name in {
				"transferred_size",
				"source_size",
				"source_checksum",
				"destination_size",
				"destination_checksum",
				"destination_generation",
				"verification_details",
				"terminal_reason",
			}:
				details[name] = list(value) if isinstance(value, tuple) else value
		event = AuditEvent.create(
			event_type,
			outcome,
			context,
			event_id=stable_audit_event_id(
				"move-transition",
				job.idempotency_key,
				str(transition.sequence),
			),
			occurred_at=transition.created_at,
			recorded_at=transition.created_at,
			retention=self.audit_retention,
			bucket=job.bucket,
			object_key=job.key,
			move_id=job.idempotency_key,
			details=details,
		)
		return self.append_audit_event(event)

	def _append_move_retry_audit_event(
		self,
		previous_job: MoveJob,
		claimed_job: MoveJob,
		*,
		owner_id: str,
		audit_context: AuditContext | None,
	) -> AuditEvent:
		previous_event_id = self._latest_move_audit_event_id(
			claimed_job.idempotency_key
		)
		context = self._move_audit_context(
			claimed_job.idempotency_key,
			owner_id=owner_id,
			audit_context=audit_context,
			causation_id=(
				previous_event_id
				if previous_event_id is not None
				else None if audit_context is None else audit_context.causation_id
			),
		)
		event = AuditEvent.create(
			AuditEventType.MOVE_RETRY,
			AuditOutcome.RETRYING,
			context,
			event_id=stable_audit_event_id(
				"move-retry",
				claimed_job.idempotency_key,
				previous_event_id or "",
				claimed_job.updated_at,
				owner_id,
			),
			occurred_at=claimed_job.updated_at,
			recorded_at=claimed_job.updated_at,
			retention=self.audit_retention,
			bucket=claimed_job.bucket,
			object_key=claimed_job.key,
			move_id=claimed_job.idempotency_key,
			details={
				"state": claimed_job.state.value,
				"previous_owner": previous_job.owner_id,
				"lease_expires_at": previous_job.lease_expires_at,
			},
		)
		return self.append_audit_event(event)

	def _latest_move_audit_event_id(self, move_id: str) -> str | None:
		head = self._audit_move_heads.get(audit_text_identity(move_id))
		return None if head is None else head[1]

	@staticmethod
	def _move_audit_context(
		move_id: str,
		*,
		owner_id: str,
		audit_context: AuditContext | None,
		causation_id: str | None,
	) -> AuditContext:
		if audit_context is None:
			return AuditContext(
				correlation_id=move_id,
				actor_type="worker",
				actor_id=owner_id,
				causation_id=causation_id,
			)
		return AuditContext(
			correlation_id=audit_context.correlation_id,
			actor_type=audit_context.actor_type,
			actor_id=audit_context.actor_id,
			job_id=audit_context.job_id,
			causation_id=causation_id,
		)
