from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Protocol

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
	"""Backend-neutral persistence contract for catalog state."""

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
	) -> MoveJob: ...


class Catalog(CatalogStore):
	"""A tiny in-memory catalog for objects and their current tier/metadata.

	This implementation is useful for isolated inline operations and tests.
	"""

	def __init__(self) -> None:
		self._objects: Dict[tuple[str, str], ObjectRecord] = {}
		self._move_jobs: Dict[str, MoveJob] = {}
		self._move_transitions: Dict[str, List[MoveJobTransition]] = {}
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
				metadata=metadata or {},
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
				metadata=metadata or {},
			)
			return True

	def get(self, bucket: str, key: str) -> Optional[ObjectRecord]:
		with self._lock:
			return self._objects.get((bucket, key))

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
			metadata = dict(rec.metadata) if rec is not None else {}
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
			out: List[ObjectRecord] = []
			for (b, k), rec in self._objects.items():
				if b != bucket:
					continue
				if k.startswith(prefix):
					out.append(rec)
			return out

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
				self._append_move_transition(
					job, None, MoveJobState.PREPARED, "move prepared", now
				)
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
	) -> MoveJob:
		validate_move_job_transition(expected_state, to_state)
		changes = dict(updates or {})
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
			self._append_move_transition(job, expected_state, to_state, reason, now)
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
			self._append_move_transition(
				job,
				MoveJobState.VERIFIED,
				MoveJobState.COMMITTED,
				"catalog placement committed",
				now,
			)
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
	) -> None:
		transitions = self._move_transitions.setdefault(job.idempotency_key, [])
		transitions.append(
			MoveJobTransition(
				sequence=len(transitions) + 1,
				idempotency_key=job.idempotency_key,
				from_state=from_state,
				to_state=to_state,
				reason=reason,
				created_at=now,
			)
		)
