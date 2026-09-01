from __future__ import annotations

import gc
import hashlib
import tracemalloc
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

import cognistore.core.content_identity as content_identity_module
from cognistore.core.content_identity import (
    CAS_KEY_VERSION,
    CHUNKING_ALGORITHM,
    CHUNKING_VERSION,
    CONTENT_IDENTITY_SCHEMA_VERSION,
    CONTENT_REPRESENTATION,
    DEFAULT_CONTENT_CHUNK_SIZE,
    DIGEST_ALGORITHM,
    MAX_IN_MEMORY_CONTENT_CHUNKS,
    ContentChunk,
    ContentChunkSequence,
    ContentIdentityBuilder,
    ContentSizeMismatchError,
    cas_key_for_sha256,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _cas_key(data: bytes) -> str:
    digest = _sha256(data)
    return f"cas/v{CAS_KEY_VERSION}/sha256/{digest[:2]}/{digest[2:]}"


def test_known_empty_and_one_byte_vectors() -> None:
    builder = ContentIdentityBuilder(chunk_size=4)

    empty = builder.build(BytesIO(b""), expected_size=0)
    one_byte = builder.build(BytesIO(b"\0"), expected_size=1)

    assert empty.sha256 == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert empty.cas_key == (
        "cas/v1/sha256/e3/"
        "b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert empty.chunks == ()

    assert one_byte.sha256 == (
        "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d"
    )
    assert one_byte.chunks == (
        ContentChunk(
            index=0,
            offset=0,
            size=1,
            sha256=one_byte.sha256,
            cas_key=one_byte.cas_key,
        ),
    )


def test_fixed_size_chunks_have_deterministic_boundaries_and_known_digests() -> None:
    content = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(b"abcdefghij"),
        expected_size=10,
    )

    assert content.sha256 == "72399361da6a7754fec986dca5b7cbaf1c810a28ded4abaf56b2106d06cb78b0"
    assert [(chunk.index, chunk.offset, chunk.size) for chunk in content.chunks] == [
        (0, 0, 4),
        (1, 4, 4),
        (2, 8, 2),
    ]
    assert [chunk.sha256 for chunk in content.chunks] == [
        "88d4266fd4e6338d13b845fcf289579d209c897823b9217da3e161936f031589",
        "e5e088a0b66163a0a26a5e053d2a4496dc16ab6e0e3dd1adf2d16aa84a078c9d",
        "c9df9c3f2963b19b9b95f58c4d33b053fa9f8586dd6ee04126e52a868f882108",
    ]
    assert [chunk.cas_key for chunk in content.chunks] == [
        cas_key_for_sha256(chunk.sha256) for chunk in content.chunks
    ]


def test_exact_boundary_has_no_trailing_empty_chunk() -> None:
    content = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(b"abcdefgh"),
        expected_size=8,
    )

    assert [(chunk.offset, chunk.size) for chunk in content.chunks] == [(0, 4), (4, 4)]


class _PatternReader:
    def __init__(self, data: bytes, pattern: tuple[int, ...]) -> None:
        self.data = data
        self.pattern = pattern
        self.offset = 0
        self.calls = 0

    def read(self, size: int = -1, /) -> bytes:
        if self.offset == len(self.data):
            return b""
        offered = self.pattern[self.calls % len(self.pattern)]
        self.calls += 1
        amount = min(size, offered, len(self.data) - self.offset)
        result = self.data[self.offset : self.offset + amount]
        self.offset += amount
        return result


def test_short_reads_do_not_change_chunk_boundaries_or_identity() -> None:
    data = bytes(range(251)) * 17
    builder = ContentIdentityBuilder(chunk_size=97)

    complete_reads = builder.build(BytesIO(data), expected_size=len(data))
    short_reads = builder.build(
        _PatternReader(data, (1, 3, 2, 11, 5)),
        expected_size=len(data),
    )

    assert short_reads == complete_reads


def test_repeated_chunks_share_byte_address_but_keep_distinct_extents() -> None:
    content = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(b"abcdabcd"),
        expected_size=8,
    )

    assert content.chunks[0].sha256 == content.chunks[1].sha256
    assert content.chunks[0].cas_key == content.chunks[1].cas_key
    assert (content.chunks[0].index, content.chunks[0].offset) == (0, 0)
    assert (content.chunks[1].index, content.chunks[1].offset) == (1, 4)


@pytest.mark.parametrize("chunk_size", [0, -1, True, 1.5, "4", None, 2**63])
def test_builder_rejects_invalid_chunk_sizes(chunk_size: object) -> None:
    with pytest.raises(ValueError, match="chunk_size.*positive integer"):
        ContentIdentityBuilder(chunk_size=chunk_size)  # type: ignore[arg-type]


@pytest.mark.parametrize("expected_size", [-1, True, 1.5, "4", None, 2**63])
def test_builder_rejects_invalid_expected_sizes(expected_size: object) -> None:
    with pytest.raises(ValueError, match="expected_size.*non-negative integer"):
        ContentIdentityBuilder().build(  # type: ignore[arg-type]
            BytesIO(b""),
            expected_size=expected_size,
        )


@pytest.mark.parametrize("chunking_version", [0, 2, True, 1.0, "1", None])
def test_builder_rejects_unsupported_chunking_versions(chunking_version: object) -> None:
    with pytest.raises(ValueError, match="unsupported chunking version"):
        ContentIdentityBuilder(chunking_version=chunking_version)  # type: ignore[arg-type]


class _InvalidReader:
    def __init__(self, result: object) -> None:
        self.result = result

    def read(self, _size: int = -1, /) -> Any:
        return self.result


def test_builder_rejects_non_bytes_stream_results() -> None:
    with pytest.raises(TypeError, match=r"read\(\).*return bytes"):
        ContentIdentityBuilder().build(_InvalidReader(bytearray(b"x")), expected_size=1)


class _OverreadReader:
    def read(self, size: int = -1, /) -> bytes:
        return b"x" * (size + 1)


def test_builder_rejects_reads_larger_than_requested() -> None:
    with pytest.raises(ValueError, match="more bytes than requested"):
        ContentIdentityBuilder(chunk_size=4).build(_OverreadReader(), expected_size=4)


def test_builder_rejects_short_and_long_streams() -> None:
    builder = ContentIdentityBuilder(chunk_size=4)

    with pytest.raises(ContentSizeMismatchError, match="ended after 3 bytes; expected 4"):
        builder.build(BytesIO(b"abc"), expected_size=4)
    with pytest.raises(ContentSizeMismatchError, match="more than the expected 3 bytes"):
        builder.build(BytesIO(b"abcd"), expected_size=3)


def test_object_content_is_immutable_detached_and_returns_fresh_metadata() -> None:
    built = ContentIdentityBuilder(chunk_size=4).build(BytesIO(b"abc"), expected_size=3)
    caller_chunks = list(built.chunks)
    detached = replace(built, chunks=caller_chunks)  # type: ignore[arg-type]
    caller_chunks.clear()

    assert detached.chunks == built.chunks
    assert isinstance(detached.chunks, ContentChunkSequence)
    assert isinstance(detached.chunks, Sequence)
    with pytest.raises(FrozenInstanceError):
        detached.size = 10  # type: ignore[misc]

    first = detached.to_metadata()
    second = detached.to_metadata()
    first["sha256"] = "mutated"
    assert second == {
        "schema_version": CONTENT_IDENTITY_SCHEMA_VERSION,
        "representation": CONTENT_REPRESENTATION,
        "digest_algorithm": DIGEST_ALGORITHM,
        "sha256": _sha256(b"abc"),
        "size": 3,
        "cas_key": _cas_key(b"abc"),
        "chunking_algorithm": CHUNKING_ALGORITHM,
        "chunking_version": CHUNKING_VERSION,
        "chunk_size": 4,
        "chunk_count": 1,
    }


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema_version": 2}, "schema version"),
        ({"representation": "normalized-text"}, "representation"),
        ({"digest_algorithm": "sha512"}, "digest algorithm"),
        ({"chunking_algorithm": "content-defined"}, "chunking algorithm"),
        ({"chunking_version": 2}, "chunking version"),
    ],
)
def test_object_content_rejects_unsupported_contract_versions(
    changes: dict[str, object],
    message: str,
) -> None:
    content = ContentIdentityBuilder(chunk_size=4).build(BytesIO(b"abc"), expected_size=3)

    with pytest.raises(ValueError, match=message):
        replace(content, **changes)


def test_object_content_rejects_inconsistent_chunk_layouts_and_cas_keys() -> None:
    content = ContentIdentityBuilder(chunk_size=4).build(
        BytesIO(b"abcdefgh"),
        expected_size=8,
    )

    with pytest.raises(ValueError, match="object cas_key"):
        replace(content, cas_key=_cas_key(b"different"))
    with pytest.raises(ValueError, match="chunk indexes"):
        replace(content, chunks=(replace(content.chunks[0], index=1), content.chunks[1]))
    with pytest.raises(ValueError, match="chunk offsets"):
        replace(content, chunks=(content.chunks[0], replace(content.chunks[1], offset=5)))
    with pytest.raises(ValueError, match="complete object size"):
        replace(content, size=9)
    with pytest.raises(ValueError, match="chunk cas_key"):
        replace(content.chunks[0], cas_key=_cas_key(b"different"))


class _GeneratedReader:
    def __init__(self, size: int) -> None:
        self.remaining = size

    def read(self, size: int = -1, /) -> bytes:
        amount = self.remaining if size < 0 else min(size, self.remaining)
        self.remaining -= amount
        return b"x" * amount


def test_builder_memory_does_not_scale_with_source_payload() -> None:
    chunk_size = 64 * 1024
    object_size = 128 * chunk_size

    tracemalloc.start()
    try:
        content = ContentIdentityBuilder(chunk_size=chunk_size).build(
            _GeneratedReader(object_size),
            expected_size=object_size,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert content.size == object_size
    assert len(content.chunks) == 128
    assert peak < chunk_size * 8


def test_spilled_chunk_sequence_preserves_sequence_and_copy_semantics() -> None:
    chunk_count = MAX_IN_MEMORY_CONTENT_CHUNKS + 1
    chunk_size = 8
    content = ContentIdentityBuilder(chunk_size=chunk_size).build(
        _GeneratedReader(chunk_count * chunk_size),
        expected_size=chunk_count * chunk_size,
    )

    assert isinstance(content.chunks, ContentChunkSequence)
    assert content.chunks.is_spilled
    assert len(content.chunks) == chunk_count
    assert content.chunks[0].index == 0
    assert content.chunks[-1].index == chunk_count - 1
    assert content.chunks[1:4] == tuple(content.chunks[index] for index in range(1, 4))
    assert content.chunks[::-1][0] == content.chunks[-1]
    with pytest.raises(AttributeError, match="immutable"):
        content.chunks._length = 0  # type: ignore[misc]

    spill_path_value = content.chunks._spill_path
    assert spill_path_value is not None
    spill_path = Path(spill_path_value)
    assert spill_path.exists()
    copied = deepcopy(content)
    assert copied is content
    del content
    gc.collect()
    assert spill_path.exists()
    del copied
    gc.collect()
    assert not spill_path.exists()


def test_spilled_chunk_manifest_memory_is_bounded_by_descriptor_cap() -> None:
    chunk_count = MAX_IN_MEMORY_CONTENT_CHUNKS * 64
    chunk_size = 64

    tracemalloc.start()
    try:
        content = ContentIdentityBuilder(chunk_size=chunk_size).build(
            _GeneratedReader(chunk_count * chunk_size),
            expected_size=chunk_count * chunk_size,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert isinstance(content.chunks, ContentChunkSequence)
    assert content.chunks.is_spilled
    assert len(content.chunks) == chunk_count
    # A retained Python descriptor per chunk exceeds this by several times;
    # the fixed-width spill manifest remains effectively constant at this scale.
    assert peak < 1024 * 1024


def test_builder_failure_removes_an_unfinished_spill_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[Path] = []
    original_mkstemp = content_identity_module.tempfile.mkstemp

    def tracked_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        descriptor, path = original_mkstemp(*args, **kwargs)
        paths.append(Path(path))
        return descriptor, path

    monkeypatch.setattr(content_identity_module.tempfile, "mkstemp", tracked_mkstemp)
    chunk_size = 8
    expected_size = (MAX_IN_MEMORY_CONTENT_CHUNKS + 1) * chunk_size
    with pytest.raises(ContentSizeMismatchError, match="more than the expected"):
        ContentIdentityBuilder(chunk_size=chunk_size).build(
            _GeneratedReader(expected_size + 1),
            expected_size=expected_size,
        )

    assert paths
    assert all(not path.exists() for path in paths)


def test_default_chunk_size_is_one_mibibyte() -> None:
    assert DEFAULT_CONTENT_CHUNK_SIZE == 1024 * 1024
