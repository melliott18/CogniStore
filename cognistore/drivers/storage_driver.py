from __future__ import annotations

import json
from abc import ABC, abstractmethod
from base64 import b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Dict, Generator, Mapping, Optional, Protocol

DEFAULT_STREAM_CHUNK_SIZE = 8 * 1024 * 1024
MAX_LISTING_PAGE_SIZE = 1000


@dataclass(frozen=True)
class StorageListingPage:
	"""A bounded inventory page and an opaque continuation cursor.

	``next_cursor=None`` means the listing completed successfully. Cursors
	belong to one backend, bucket, and literal prefix, and survive driver
	reconstruction. Listings are observations, not snapshots of concurrent
	object mutations.
	"""

	keys: tuple[str, ...]
	next_cursor: str | None


def validate_listing_request(bucket: str, prefix: str, cursor: str | None, limit: int) -> None:
	if not isinstance(bucket, str) or not bucket:
		raise ValueError("Listing bucket must be a non-empty string")
	if not isinstance(prefix, str):
		raise ValueError("Listing prefix must be a string")
	if cursor is not None and (not isinstance(cursor, str) or not cursor):
		raise ValueError("Listing cursor must be a non-empty string or null")
	if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LISTING_PAGE_SIZE:
		raise ValueError(f"Listing limit must be an integer between 1 and {MAX_LISTING_PAGE_SIZE}")


def encode_listing_cursor(backend: str, bucket: str, prefix: str, position: str) -> str:
	payload = json.dumps([1, backend, bucket, prefix, position], ensure_ascii=True)
	return urlsafe_b64encode(payload.encode("ascii")).decode("ascii")


def decode_listing_cursor(cursor: str, backend: str, bucket: str, prefix: str) -> str:
	try:
		payload = json.loads(b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
	except (ValueError, UnicodeError, Base64Error):
		raise ValueError("Invalid storage listing cursor") from None
	if (
		not isinstance(payload, list)
		or len(payload) != 5
		or payload[:4] != [1, backend, bucket, prefix]
		or not isinstance(payload[4], str)
		or not payload[4]
	):
		raise ValueError("Storage listing cursor does not match this listing scope")
	return payload[4]


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


def external_encryption_status(*, key_configured: bool = False) -> Dict[str, Any]:
	"""Describe operator evidence without implying an unattested volume is encrypted."""
	from cognistore.encryption import encryption_status

	status = encryption_status()["at_rest"]["storage"]
	return {
		"source": status["source"],
		"mode": status["mode"] if status["current"] else "unattested",
		"key_configured": key_configured,
	}


class StorageDriver(ABC):
	"""Abstract base class for storage drivers.

	Implementations should provide an object-style API across different backends.
	"""

	capabilities = DriverCapabilities()

	def encryption_status(self) -> Dict[str, Any]:
		"""Report modes without credentials, paths, or key identifiers."""
		return {"source": "unknown", "mode": "unknown", "key_configured": False}

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

	def list_objects_page(
		self,
		bucket: str,
		prefix: str = "",
		*,
		cursor: str | None = None,
		limit: int = MAX_LISTING_PAGE_SIZE,
	) -> StorageListingPage:
		"""Read one resumable inventory page without modifying storage.

		Implementations must bound retained keys by ``limit``, propagate listing
		failures, and return a cursor whenever more keys remain. Backends without
		a resumable listing fail closed instead of exhausting ``list_objects``.
		"""

		validate_listing_request(bucket, prefix, cursor, limit)
		raise NotImplementedError("Storage driver does not support resumable inventory pages")

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
