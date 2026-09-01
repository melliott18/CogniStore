from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Dict, Generator, Mapping, Optional, Protocol

DEFAULT_STREAM_CHUNK_SIZE = 8 * 1024 * 1024


def validate_object_generation(generation: object) -> str:
	"""Return one valid opaque generation token."""

	if not isinstance(generation, str) or not generation:
		raise ValueError("generation must be a non-empty string")
	return generation


class ReadableStream(Protocol):
	"""Minimal binary stream interface consumed by storage drivers."""

	def read(self, size: int = -1) -> bytes:
		"""Read up to ``size`` bytes, or all remaining bytes when negative."""
		...


@dataclass(frozen=True)
class DriverCapabilities:
	"""Optional storage operations supported by a driver.

	Capabilities let callers and the shared conformance suite distinguish a
	backend limitation from an implementation error.  Operations in the base
	contract remain required unless explicitly represented here.
	"""

	range_reads: bool = False
	range_writes: bool = False
	atomic_no_overwrite: bool = False
	conditional_delete: bool = False


class ObjectGenerationMismatchError(RuntimeError):
	"""Raised when a conditional operation observes another object generation."""


class StorageDriver(ABC):
	"""Abstract base class for storage drivers.

	Implementations should provide an object-style API across different backends.
	"""

	capabilities = DriverCapabilities()

	@abstractmethod
	def put_object(
		self,
		bucket: str,
		key: str,
		data: bytes,
		range: Optional[str] = None,
		overwrite: bool = True,
		**opts: Any,
	) -> None:
		"""Write an object.

		Args:
			bucket: Logical bucket/namespace.
			key: Object key within the bucket.
			data: Bytes to write.
			range: Optional HTTP-style byte range header, e.g. "bytes=0-99" for partial writes.
			overwrite: Whether an existing object may be replaced. Implementations must
				raise ``FileExistsError`` when this is false and the object exists.
			**opts: Backend-specific kwargs.
		"""

	@abstractmethod
	def get_object(
		self, bucket: str, key: str, range: Optional[str] = None
	) -> bytes:
		"""Read an object or a ranged portion of it and return bytes."""

	@abstractmethod
	def open_object_reader(
		self,
		bucket: str,
		key: str,
		range: Optional[str] = None,
	) -> AbstractContextManager[ReadableStream]:
		"""Open a context-managed stream for an object or byte range.

		The returned context manager owns any backend resources associated with
		the stream and must release them on both normal and exceptional exits.
		"""

	def open_object_reader_if_generation(
		self,
		bucket: str,
		key: str,
		generation: str,
		range: Optional[str] = None,
	) -> AbstractContextManager[ReadableStream]:
		"""Open bytes belonging to exactly ``generation``.

		Concrete backends should bind validation to opening the stream, such as
		by checking the opened POSIX descriptor or issuing a conditional object-
		store GET. A different generation must raise
		:class:`ObjectGenerationMismatchError` without being accepted as the
		requested object.

		The base implementation fails closed. Existing unbound reader callers
		remain compatible, while generation-sensitive callers cannot silently
		fall back to a check/read/check sequence that admits ABA replacements.
		"""

		validate_object_generation(generation)
		raise NotImplementedError(
			"Storage driver does not support generation-bound object reads"
		)

	@abstractmethod
	def put_object_stream(
		self,
		bucket: str,
		key: str,
		source: ReadableStream,
		*,
		size: int,
		overwrite: bool = True,
		metadata: Optional[Mapping[str, Any]] = None,
	) -> int:
		"""Write exactly ``size`` bytes from ``source`` and return that count.

		Implementations must not publish a partial object when the source ends
		early, contains additional bytes, or raises while being consumed.
		"""

	@abstractmethod
	def delete_object(self, bucket: str, key: str) -> None:
		"""Delete an object if it exists; should be idempotent."""

	@abstractmethod
	def delete_object_if_generation(
		self, bucket: str, key: str, generation: str
	) -> bool:
		"""Delete exactly ``generation`` and return whether it still existed.

		The comparison and deletion must be atomic with respect to mutations made
		through the backend.  A different live generation must raise
		:class:`ObjectGenerationMismatchError` and remain untouched.
		"""

	@abstractmethod
	def list_objects(self, bucket: str, prefix: str = "") -> Generator[str, None, None]:
		"""Yield keys under a bucket, optionally filtered by prefix."""

	@abstractmethod
	def stat_object(self, bucket: str, key: str) -> Dict[str, Any]:
		"""Return object metadata like size and mtime.

		Required keys:
		  - size: int
		  - mtime: float (POSIX timestamp)
		  - generation: opaque, stable string identifying the observed object
		Implementations may add more keys.
		"""

	def object_generation(self, bucket: str, key: str) -> str:
		"""Return the opaque generation token from object metadata."""

		generation = self.stat_object(bucket, key).get("generation")
		if not isinstance(generation, str) or not generation:
			raise RuntimeError(
				f"Storage driver returned no generation for {bucket}/{key}"
			)
		return generation

	def ensure_object_durable(self, bucket: str, key: str) -> None:
		"""Confirm that an acknowledged object is durable before cleanup.

		Remote object stores may rely on the successful publication response and
		therefore need no additional operation. Filesystem drivers should override
		this hook to barrier both file contents and the containing namespace. The
		mover calls it when recovering a destination whose original publication
		may have been interrupted before its final durability barrier.
		"""

		return None

	def same_backend(self, other: "StorageDriver") -> bool:
		"""Return whether two drivers address the same physical backend.

		The identity default is conservative for generic drivers. Backends with a
		canonical endpoint or root should override this method.
		"""

		return self is other
