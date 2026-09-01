from __future__ import annotations

import hashlib
import os
import struct
import tempfile
import weakref
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from itertools import islice
from typing import Any, BinaryIO, Protocol, overload

CONTENT_IDENTITY_SCHEMA_VERSION = 1
CONTENT_REPRESENTATION = "source-bytes"
DIGEST_ALGORITHM = "sha256"
CHUNKING_ALGORITHM = "fixed-size"
CHUNKING_VERSION = 1
CAS_KEY_VERSION = 1
DEFAULT_CONTENT_CHUNK_SIZE = 1024 * 1024
DEFAULT_CHUNK_SIZE = DEFAULT_CONTENT_CHUNK_SIZE
MAX_IN_MEMORY_CONTENT_CHUNKS = 256

_MAX_CONTENT_SIZE = 2**63 - 1
_LOWERCASE_HEX = frozenset("0123456789abcdef")
_CHUNK_RECORD = struct.Struct(">QQQ32s")
_SPILL_BUFFER_SIZE = 64 * 1024


def _remove_spill_file(path: str, *, _unlink: Any = os.unlink) -> None:
    try:
        _unlink(path)
    except FileNotFoundError:
        pass


class ContentReader(Protocol):
    """The bounded binary-reader surface consumed by the identity builder."""

    def read(self, size: int = -1, /) -> bytes: ...


class ContentSizeMismatchError(ValueError):
    """The stream length did not match the authoritative expected object size."""


def _validate_integer(
    value: object,
    *,
    field: str,
    minimum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > _MAX_CONTENT_SIZE
    ):
        qualifier = "positive" if minimum == 1 else "non-negative"
        raise ValueError(
            f"{field} must be a {qualifier} integer no greater than "
            f"{_MAX_CONTENT_SIZE}"
        )
    return value


def _validate_sha256(value: object, *, field: str = "sha256") -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _LOWERCASE_HEX for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase 64-character SHA-256 digest")
    return value


def cas_key_for_sha256(digest: str) -> str:
    """Return the canonical, layout-independent key for SHA-256-addressed bytes."""

    validated = _validate_sha256(digest)
    return f"cas/v{CAS_KEY_VERSION}/sha256/{validated[:2]}/{validated[2:]}"


@dataclass(frozen=True)
class ContentChunk:
    """The stable identity and source extent of one canonical content chunk."""

    index: int
    offset: int
    size: int
    sha256: str
    cas_key: str

    def __post_init__(self) -> None:
        _validate_integer(self.index, field="chunk index", minimum=0)
        _validate_integer(self.offset, field="chunk offset", minimum=0)
        _validate_integer(self.size, field="chunk size", minimum=1)
        digest = _validate_sha256(self.sha256, field="chunk sha256")
        if self.cas_key != cas_key_for_sha256(digest):
            raise ValueError("chunk cas_key does not match its SHA-256 digest")

    def to_metadata(self) -> dict[str, object]:
        """Return a fresh JSON-safe representation of this chunk mapping."""

        return {
            "index": self.index,
            "offset": self.offset,
            "size": self.size,
            "sha256": self.sha256,
            "cas_key": self.cas_key,
        }


class ContentChunkSequence(Sequence[ContentChunk]):
    """An immutable chunk manifest with a fixed in-memory descriptor bound.

    Small manifests stay in a tuple. Once the fixed descriptor limit is
    exceeded, all fixed-width records move to an owned temporary file. Copies
    can safely share this immutable instance; the spill file is removed when
    the last reference to the sequence is released.
    """

    __slots__ = (
        "_chunks",
        "_length",
        "_spill_path",
        "__weakref__",
    )
    _chunks: tuple[ContentChunk, ...]
    _length: int
    _spill_path: str | None

    def __init__(self, chunks: Iterable[ContentChunk] = ()) -> None:
        accumulator = _ContentChunkAccumulator()
        try:
            for chunk in chunks:
                accumulator.append(chunk)
            memory, spill_path, length = accumulator.release()
        finally:
            accumulator.close()

        self._initialize(memory, spill_path, length)

    @classmethod
    def _from_storage(
        cls,
        memory: tuple[ContentChunk, ...],
        spill_path: str | None,
        length: int,
    ) -> ContentChunkSequence:
        instance = object.__new__(cls)
        instance._initialize(memory, spill_path, length)
        return instance

    def _initialize(
        self,
        memory: tuple[ContentChunk, ...],
        spill_path: str | None,
        length: int,
    ) -> None:
        object.__setattr__(self, "_chunks", memory)
        object.__setattr__(self, "_spill_path", spill_path)
        object.__setattr__(self, "_length", length)
        try:
            if spill_path is not None:
                weakref.finalize(self, _remove_spill_file, spill_path)
        except BaseException:
            if spill_path is not None:
                _remove_spill_file(spill_path)
            raise

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("ContentChunkSequence is immutable")

    @classmethod
    def from_iterable(
        cls,
        chunks: Iterable[ContentChunk],
    ) -> ContentChunkSequence:
        """Detach an iterable into immutable bounded manifest storage."""

        if isinstance(chunks, cls):
            return chunks
        return cls(chunks)

    @property
    def is_spilled(self) -> bool:
        """Return whether descriptors live in an owned temporary file."""

        return self._spill_path is not None

    def __len__(self) -> int:
        return self._length

    def __iter__(self) -> Iterator[ContentChunk]:
        if self._spill_path is None:
            return iter(self._chunks)
        return self._iter_spilled()

    def _iter_spilled(self) -> Iterator[ContentChunk]:
        assert self._spill_path is not None
        with open(self._spill_path, "rb", buffering=_SPILL_BUFFER_SIZE) as manifest:
            for _ in range(self._length):
                yield self._decode_record(manifest.read(_CHUNK_RECORD.size))
            if manifest.read(1):
                raise RuntimeError("content chunk manifest contains trailing data")

    @overload
    def __getitem__(self, index: int) -> ContentChunk: ...

    @overload
    def __getitem__(self, index: slice) -> ContentChunkSequence: ...

    def __getitem__(
        self,
        index: int | slice,
    ) -> ContentChunk | ContentChunkSequence:
        if isinstance(index, slice):
            start, stop, step = index.indices(self._length)
            if start == 0 and stop == self._length and step == 1:
                return self
            if step == 1:
                return ContentChunkSequence.from_iterable(islice(self, start, stop))
            return ContentChunkSequence.from_iterable(
                self[position] for position in range(start, stop, step)
            )

        if not isinstance(index, int):
            raise TypeError("content chunk indexes must be integers or slices")
        position = index
        if position < 0:
            position += self._length
        if position < 0 or position >= self._length:
            raise IndexError("content chunk index out of range")
        if self._spill_path is None:
            return self._chunks[position]
        with open(self._spill_path, "rb", buffering=0) as manifest:
            manifest.seek(position * _CHUNK_RECORD.size)
            return self._decode_record(manifest.read(_CHUNK_RECORD.size))

    def __eq__(self, other: object) -> bool:
        if self is other:
            return True
        if not isinstance(other, Sequence):
            return NotImplemented
        if len(self) != len(other):
            return False
        return all(left == right for left, right in zip(self, other))

    __hash__ = None  # type: ignore[assignment]

    def __copy__(self) -> ContentChunkSequence:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> ContentChunkSequence:
        memo[id(self)] = self
        return self

    def __repr__(self) -> str:
        if self._spill_path is None:
            return repr(self._chunks)
        return f"ContentChunkSequence(length={self._length}, spilled=True)"

    @staticmethod
    def _decode_record(record: bytes) -> ContentChunk:
        if len(record) != _CHUNK_RECORD.size:
            raise RuntimeError("content chunk manifest is truncated")
        index, offset, size, digest_bytes = _CHUNK_RECORD.unpack(record)
        digest = digest_bytes.hex()
        return ContentChunk(
            index=index,
            offset=offset,
            size=size,
            sha256=digest,
            cas_key=cas_key_for_sha256(digest),
        )


class _ContentChunkAccumulator:
    """Mutable construction helper that owns an unfinished spill file."""

    __slots__ = ("_chunks", "_length", "_spill_path", "_spill_stream")

    def __init__(self) -> None:
        self._chunks: list[ContentChunk] = []
        self._length = 0
        self._spill_path: str | None = None
        self._spill_stream: BinaryIO | None = None

    def append(self, chunk: ContentChunk) -> None:
        if not isinstance(chunk, ContentChunk):
            raise TypeError("chunks must contain only ContentChunk values")
        if (
            self._spill_stream is None
            and len(self._chunks) < MAX_IN_MEMORY_CONTENT_CHUNKS
        ):
            self._chunks.append(chunk)
            self._length += 1
            return
        if self._spill_stream is None:
            self._start_spill()
        assert self._spill_stream is not None
        self._spill_stream.write(self._encode_record(chunk))
        self._length += 1

    def __len__(self) -> int:
        return self._length

    def _start_spill(self) -> None:
        descriptor, path = tempfile.mkstemp(
            prefix="cognistore-content-",
            suffix=".chunks",
        )
        try:
            stream = os.fdopen(descriptor, "wb", buffering=_SPILL_BUFFER_SIZE)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            _remove_spill_file(path)
            raise
        try:
            for chunk in self._chunks:
                stream.write(self._encode_record(chunk))
        except BaseException:
            stream.close()
            _remove_spill_file(path)
            raise
        self._chunks.clear()
        self._spill_path = path
        self._spill_stream = stream

    def release(self) -> tuple[tuple[ContentChunk, ...], str | None, int]:
        if self._spill_stream is not None:
            self._spill_stream.close()
            self._spill_stream = None
        memory = tuple(self._chunks)
        self._chunks.clear()
        spill_path = self._spill_path
        self._spill_path = None
        return memory, spill_path, self._length

    def finish(self) -> ContentChunkSequence:
        return ContentChunkSequence._from_storage(*self.release())

    def close(self) -> None:
        if self._spill_stream is not None:
            self._spill_stream.close()
            self._spill_stream = None
        if self._spill_path is not None:
            _remove_spill_file(self._spill_path)
            self._spill_path = None

    @staticmethod
    def _encode_record(chunk: ContentChunk) -> bytes:
        return _CHUNK_RECORD.pack(
            chunk.index,
            chunk.offset,
            chunk.size,
            bytes.fromhex(chunk.sha256),
        )


@dataclass(frozen=True)
class ObjectContent:
    """One complete, versioned raw-byte identity and its deterministic layout."""

    schema_version: int
    representation: str
    digest_algorithm: str
    sha256: str
    size: int
    cas_key: str
    chunking_algorithm: str
    chunking_version: int
    chunk_size: int
    chunks: Sequence[ContentChunk]

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != CONTENT_IDENTITY_SCHEMA_VERSION
        ):
            raise ValueError(
                f"unsupported content identity schema version: {self.schema_version!r}"
            )
        if self.representation != CONTENT_REPRESENTATION:
            raise ValueError(f"unsupported content representation: {self.representation!r}")
        if self.digest_algorithm != DIGEST_ALGORITHM:
            raise ValueError(f"unsupported content digest algorithm: {self.digest_algorithm!r}")
        if self.chunking_algorithm != CHUNKING_ALGORITHM:
            raise ValueError(f"unsupported chunking algorithm: {self.chunking_algorithm!r}")
        if (
            isinstance(self.chunking_version, bool)
            or not isinstance(self.chunking_version, int)
            or self.chunking_version != CHUNKING_VERSION
        ):
            raise ValueError(f"unsupported chunking version: {self.chunking_version!r}")

        digest = _validate_sha256(self.sha256, field="object sha256")
        size = _validate_integer(self.size, field="object size", minimum=0)
        chunk_size = _validate_integer(
            self.chunk_size,
            field="chunk size",
            minimum=1,
        )
        if self.cas_key != cas_key_for_sha256(digest):
            raise ValueError("object cas_key does not match its SHA-256 digest")

        # Detach from caller-owned storage while preserving a fixed descriptor
        # bound for manifests containing arbitrarily many chunks.
        chunks = ContentChunkSequence.from_iterable(self.chunks)
        object.__setattr__(self, "chunks", chunks)

        if size == 0:
            if chunks:
                raise ValueError("empty content must not contain chunks")
            return
        if not chunks:
            raise ValueError("non-empty content must contain at least one chunk")

        expected_offset = 0
        for expected_index, chunk in enumerate(chunks):
            if chunk.index != expected_index:
                raise ValueError("chunk indexes must be contiguous and start at zero")
            if chunk.offset != expected_offset:
                raise ValueError("chunk offsets must be contiguous and start at zero")
            if chunk.size > chunk_size:
                raise ValueError("chunk size exceeds the configured canonical chunk size")
            if expected_index < len(chunks) - 1 and chunk.size != chunk_size:
                raise ValueError("only the final chunk may be shorter than chunk_size")
            expected_offset += chunk.size

        if expected_offset != size:
            raise ValueError("chunk sizes do not cover the complete object size")

    def __deepcopy__(self, memo: dict[int, Any]) -> ObjectContent:
        """Share this fully immutable value and its owned spill manifest."""

        memo[id(self)] = self
        return self

    def to_metadata(self) -> dict[str, object]:
        """Return a fresh JSON-safe summary without duplicating chunk mappings."""

        return {
            "schema_version": self.schema_version,
            "representation": self.representation,
            "digest_algorithm": self.digest_algorithm,
            "sha256": self.sha256,
            "size": self.size,
            "cas_key": self.cas_key,
            "chunking_algorithm": self.chunking_algorithm,
            "chunking_version": self.chunking_version,
            "chunk_size": self.chunk_size,
            "chunk_count": len(self.chunks),
        }


class ContentIdentityBuilder:
    """Build canonical object and chunk identities without retaining source bytes."""

    def __init__(
        self,
        chunk_size: int = DEFAULT_CONTENT_CHUNK_SIZE,
        chunking_version: int = CHUNKING_VERSION,
    ) -> None:
        self.chunk_size = _validate_integer(
            chunk_size,
            field="chunk_size",
            minimum=1,
        )
        if (
            isinstance(chunking_version, bool)
            or not isinstance(chunking_version, int)
            or chunking_version != CHUNKING_VERSION
        ):
            raise ValueError(f"unsupported chunking version: {chunking_version!r}")
        self.chunking_version = chunking_version

    @staticmethod
    def _read(reader: ContentReader, requested: int) -> bytes:
        part = reader.read(requested)
        if not isinstance(part, bytes):
            raise TypeError("content stream read() must return bytes")
        if len(part) > requested:
            raise ValueError("content stream returned more bytes than requested")
        return part

    def _read_exact_chunk(
        self,
        reader: ContentReader,
        size: int,
        *,
        bytes_read: int,
        expected_size: int,
    ) -> bytearray:
        chunk = bytearray(size)
        offset = 0
        while offset < size:
            part = self._read(reader, size - offset)
            if not part:
                observed = bytes_read + offset
                raise ContentSizeMismatchError(
                    f"content stream ended after {observed} bytes; "
                    f"expected {expected_size}"
                )
            chunk[offset : offset + len(part)] = part
            offset += len(part)
        return chunk

    def build(self, reader: ContentReader, *, expected_size: int) -> ObjectContent:
        """Hash exactly ``expected_size`` bytes and derive the canonical layout."""

        expected_size = _validate_integer(
            expected_size,
            field="expected_size",
            minimum=0,
        )
        object_digest = hashlib.sha256()
        chunks = _ContentChunkAccumulator()
        offset = 0

        try:
            while offset < expected_size:
                current_size = min(self.chunk_size, expected_size - offset)
                payload = self._read_exact_chunk(
                    reader,
                    current_size,
                    bytes_read=offset,
                    expected_size=expected_size,
                )
                object_digest.update(payload)
                chunk_digest = hashlib.sha256(payload).hexdigest()
                chunks.append(
                    ContentChunk(
                        index=len(chunks),
                        offset=offset,
                        size=current_size,
                        sha256=chunk_digest,
                        cas_key=cas_key_for_sha256(chunk_digest),
                    )
                )
                offset += current_size

            extra = self._read(reader, 1)
            if extra:
                raise ContentSizeMismatchError(
                    f"content stream contains more than the expected {expected_size} bytes"
                )

            stored_chunks = chunks.finish()
            digest = object_digest.hexdigest()
            return ObjectContent(
                schema_version=CONTENT_IDENTITY_SCHEMA_VERSION,
                representation=CONTENT_REPRESENTATION,
                digest_algorithm=DIGEST_ALGORITHM,
                sha256=digest,
                size=expected_size,
                cas_key=cas_key_for_sha256(digest),
                chunking_algorithm=CHUNKING_ALGORITHM,
                chunking_version=self.chunking_version,
                chunk_size=self.chunk_size,
                chunks=stored_chunks,
            )
        finally:
            chunks.close()
