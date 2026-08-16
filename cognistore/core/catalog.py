from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

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


class Catalog:
	"""A tiny in-memory catalog for objects and their current tier/metadata.

	This is an MVP placeholder. A future version will persist to a DB.
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
		with self._lock:
			rec = ObjectRecord(
				bucket=bucket,
				key=key,
				size=size,
				tier=tier,
				metadata=metadata or {},
			)
			self._objects[(bucket, key)] = rec

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
		with self._lock:
			job = self._owned_move_job(idempotency_key, owner_id, expected_state)
			updated = self._replace_move_job(
				job,
				state=to_state,
				owner_id=None if to_state.terminal else owner_id,
				lease_expires_at=None if to_state.terminal else lease_expires_at,
				updated_at=now,
				**dict(updates or {}),
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
