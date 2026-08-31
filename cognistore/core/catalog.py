from __future__ import annotations

import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Protocol
from uuid import UUID

from cognistore.utils.redaction import redact, redact_text

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
from .move_jobs import (
	MoveJob,
	MoveJobConflictError,
	MoveJobLeaseError,
	MoveJobState,
	MoveJobTransition,
	validate_move_job_transition,
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


class CatalogStore(Protocol):
	"""Backend-neutral persistence contract for catalog state.

	``get()`` and ``list()`` return detached :class:`ObjectRecord` snapshots.
	``list()`` returns matching records in ascending key order.
	"""

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
	) -> bool: ...

	def get(self, bucket: str, key: str) -> Optional[ObjectRecord]: ...

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
		self._move_jobs: Dict[str, MoveJob] = {}
		self._move_transitions: Dict[str, List[MoveJobTransition]] = {}
		self._audit_events: Dict[str, AuditEvent] = {}
		self._audit_move_heads: Dict[str, tuple[int, str]] = {}
		self._audit_event_tombstones: Dict[
			str,
			tuple[str, str | None, str | None],
		] = {}
		self.audit_retention = audit_retention or AuditRetentionPolicy()
		self._lock = threading.RLock()

	def upsert(
		self,
		bucket: str,
		key: str,
		size: int,
		tier: str,
		metadata: Optional[Dict[str, object]] = None,
	) -> None:
		validate_catalog_size(size)
		with self._lock:
			rec = ObjectRecord(
				bucket=bucket,
				key=key,
				size=size,
				tier=tier,
				metadata=deepcopy(metadata) if metadata is not None else {},
			)
			self._objects[(bucket, key)] = rec

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

		with self._lock:
			jobs = self._scan_move_jobs(bucket, key)
			if self._scan_move_job_fingerprints(jobs) != fence.move_jobs:
				return False
			if not self._scan_observation_is_authoritative(
				jobs, tier=tier, generation=generation
			):
				return False
			self._objects[(bucket, key)] = ObjectRecord(
				bucket=bucket,
				key=key,
				size=size,
				tier=tier,
				metadata=deepcopy(metadata) if metadata is not None else {},
			)
			return True

	def get(self, bucket: str, key: str) -> Optional[ObjectRecord]:
		with self._lock:
			record = self._objects.get((bucket, key))
			return None if record is None else self._copy_object_record(record)

	def update_placement(self, bucket: str, key: str, tier: str) -> None:
		with self._lock:
			rec = self._objects.get((bucket, key))
			if not rec:
				raise KeyError(f"Object not found: {bucket}/{key}")
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
			rec = self._objects.get((bucket, key))
			metadata = deepcopy(rec.metadata) if rec is not None else {}
			if checksum is not None:
				metadata["sha256"] = checksum
			self._objects[(bucket, key)] = ObjectRecord(
				bucket=bucket,
				key=key,
				size=size,
				tier=tier,
				metadata=metadata,
			)

	def delete(self, bucket: str, key: str) -> None:
		with self._lock:
			self._objects.pop((bucket, key), None)

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

	@staticmethod
	def _copy_object_record(record: ObjectRecord) -> ObjectRecord:
		"""Copy one record without preserving aliases to mutable fields."""

		return deepcopy(record)

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
		with self._lock:
			existing = self._move_jobs.get(idempotency_key)
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

			self._assert_same_move(
				existing,
				src_tier=src_tier,
				dst_tier=dst_tier,
				bucket=bucket,
				key=key,
			)
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
			self.upsert_placement(
				job.bucket, job.key, size=size, tier=tier, checksum=checksum
			)
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
	) -> None:
		actual = (job.src_tier, job.dst_tier, job.bucket, job.key)
		requested = (src_tier, dst_tier, bucket, key)
		if actual != requested:
			raise MoveJobConflictError(
				f"Idempotency key {job.idempotency_key!r} already identifies "
				f"{job.src_tier}:{job.bucket}/{job.key} -> {job.dst_tier}"
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
