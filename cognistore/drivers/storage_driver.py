from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Generator, Optional


class StorageDriver(ABC):
	"""Abstract base class for storage drivers.

	Implementations should provide an object-style API across different backends.
	"""

	@abstractmethod
	def put_object(
		self,
		bucket: str,
		key: str,
		data: bytes,
		range: Optional[str] = None,
		**opts: Any,
	) -> None:
		"""Write an object.

		Args:
			bucket: Logical bucket/namespace.
			key: Object key within the bucket.
			data: Bytes to write.
			range: Optional HTTP-style byte range header, e.g. "bytes=0-99" for partial writes.
			**opts: Backend-specific kwargs.
		"""

	@abstractmethod
	def get_object(
		self, bucket: str, key: str, range: Optional[str] = None
	) -> bytes:
		"""Read an object or a ranged portion of it and return bytes."""

	@abstractmethod
	def delete_object(self, bucket: str, key: str) -> None:
		"""Delete an object if it exists; should be idempotent."""

	@abstractmethod
	def list_objects(self, bucket: str, prefix: str = "") -> Generator[str, None, None]:
		"""Yield keys under a bucket, optionally filtered by prefix."""

	@abstractmethod
	def stat_object(self, bucket: str, key: str) -> Dict[str, Any]:
		"""Return object metadata like size and mtime.

		Required keys:
		  - size: int
		  - mtime: float (POSIX timestamp)
		Implementations may add more keys.
		"""

