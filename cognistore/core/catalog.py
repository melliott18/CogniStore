from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


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

	def upsert(
		self,
		bucket: str,
		key: str,
		size: int,
		tier: str,
		metadata: Optional[Dict[str, object]] = None,
	) -> None:
		rec = ObjectRecord(bucket=bucket, key=key, size=size, tier=tier, metadata=metadata or {})
		self._objects[(bucket, key)] = rec

	def get(self, bucket: str, key: str) -> Optional[ObjectRecord]:
		return self._objects.get((bucket, key))

	def update_placement(self, bucket: str, key: str, tier: str) -> None:
		rec = self._objects.get((bucket, key))
		if not rec:
			raise KeyError(f"Object not found: {bucket}/{key}")
		rec.tier = tier

	def delete(self, bucket: str, key: str) -> None:
		self._objects.pop((bucket, key), None)

	def list(self, bucket: str, prefix: str = "") -> List[ObjectRecord]:
		out: List[ObjectRecord] = []
		for (b, k), rec in self._objects.items():
			if b != bucket:
				continue
			if k.startswith(prefix):
				out.append(rec)
		return out

