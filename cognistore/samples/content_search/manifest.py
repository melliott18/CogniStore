"""Validated manifest and packaged assets for the content-search sample."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Protocol, cast

SAMPLE_MANIFEST_SCHEMA_VERSION = 1
MAX_SAMPLE_MANIFEST_BYTES = 256 * 1_024
MAX_SAMPLE_OBJECT_BYTES = 16 * 1_024 * 1_024
MAX_SAMPLE_CHECKSUM_MANIFEST_BYTES = 64 * 1_024
_QUERY_NAMES = frozenset({"keyword", "vector", "ask"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE = re.compile(
    r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$",
    re.ASCII,
)


class SampleManifestError(ValueError):
    """The checked sample corpus is malformed or fails its integrity check."""


class _ReadableResource(Protocol):
    def joinpath(self, *descendants: str) -> _ReadableResource: ...

    def read_bytes(self) -> bytes: ...


def _text(value: object, *, field_name: str, maximum_bytes: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SampleManifestError(
            f"{field_name} must be a non-empty string without outer whitespace"
        )
    if "\0" in value or any(ord(character) < 32 for character in value):
        raise SampleManifestError(f"{field_name} must not contain control characters")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SampleManifestError(f"{field_name} must be valid UTF-8") from exc
    if len(encoded) > maximum_bytes:
        raise SampleManifestError(
            f"{field_name} must be at most {maximum_bytes} UTF-8 bytes"
        )
    return value


def _relative_source(value: object) -> str:
    source = _text(value, field_name="object.source", maximum_bytes=1_024)
    if "\\" in source:
        raise SampleManifestError("object.source must use POSIX path separators")
    path = PurePosixPath(source)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SampleManifestError("object.source must be a safe relative path")
    return source


def _json_mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise SampleManifestError(f"{field_name} must be an object with string keys")
    detached = deepcopy(dict(value))
    try:
        json.dumps(
            detached,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SampleManifestError(f"{field_name} must contain JSON values") from exc
    return MappingProxyType(detached)


def _mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise SampleManifestError(f"{field_name} must be an object with string keys")
    return cast(Mapping[str, object], value)


def _exact_fields(
    value: Mapping[str, object],
    expected: set[str] | frozenset[str],
    *,
    field_name: str,
) -> None:
    missing = sorted(expected.difference(value))
    unknown = sorted(set(value).difference(expected))
    if missing:
        raise SampleManifestError(f"{field_name} is missing field(s): {', '.join(missing)}")
    if unknown:
        raise SampleManifestError(f"{field_name} has unknown field(s): {', '.join(unknown)}")


@dataclass(frozen=True)
class SampleObject:
    source: str
    key: str
    tier: str
    media_type: str
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _relative_source(self.source))
        object.__setattr__(
            self,
            "key",
            _text(self.key, field_name="object.key", maximum_bytes=8_192),
        )
        object.__setattr__(
            self,
            "tier",
            _text(self.tier, field_name="object.tier", maximum_bytes=256),
        )
        media_type = _text(
            self.media_type,
            field_name="object.media_type",
            maximum_bytes=255,
        ).lower()
        if _MEDIA_TYPE.fullmatch(media_type) is None:
            raise SampleManifestError("object.media_type must be a canonical media type")
        object.__setattr__(self, "media_type", media_type)
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            raise SampleManifestError("object.sha256 must be a lowercase SHA-256 digest")


@dataclass(frozen=True)
class SampleQuery:
    text: str
    filters: Mapping[str, object]
    expected_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "text",
            _text(self.text, field_name="query.text", maximum_bytes=16_384),
        )
        object.__setattr__(
            self,
            "filters",
            _json_mapping(self.filters, field_name="query.filters"),
        )
        if isinstance(self.expected_keys, (str, bytes, bytearray)):
            raise SampleManifestError("query.expected_keys must be a list of keys")
        keys = tuple(self.expected_keys)
        if not keys:
            raise SampleManifestError("query.expected_keys must not be empty")
        for key in keys:
            _text(key, field_name="query.expected_keys item", maximum_bytes=8_192)
        if len(set(keys)) != len(keys):
            raise SampleManifestError("query.expected_keys must be unique")
        object.__setattr__(self, "expected_keys", keys)


@dataclass(frozen=True)
class ContentSearchManifest:
    name: str
    license: str
    bucket: str
    objects: tuple[SampleObject, ...]
    queries: Mapping[str, SampleQuery]
    schema_version: int = field(default=SAMPLE_MANIFEST_SCHEMA_VERSION)

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != SAMPLE_MANIFEST_SCHEMA_VERSION
        ):
            raise SampleManifestError(
                f"manifest.schema_version must be {SAMPLE_MANIFEST_SCHEMA_VERSION}"
            )
        object.__setattr__(
            self,
            "name",
            _text(self.name, field_name="manifest.name", maximum_bytes=256),
        )
        if self.license != "MIT":
            raise SampleManifestError("manifest.license must be 'MIT'")
        object.__setattr__(
            self,
            "bucket",
            _text(self.bucket, field_name="manifest.bucket", maximum_bytes=1_024),
        )
        objects = tuple(self.objects)
        if not objects or any(not isinstance(item, SampleObject) for item in objects):
            raise SampleManifestError("manifest.objects must contain sample objects")
        keys = [item.key for item in objects]
        if len(set(keys)) != len(keys):
            raise SampleManifestError("manifest object keys must be unique within its bucket")
        object.__setattr__(self, "objects", objects)

        if not isinstance(self.queries, Mapping):
            raise SampleManifestError("manifest.queries must be an object")
        queries = dict(self.queries)
        if set(queries) != _QUERY_NAMES or any(
            not isinstance(item, SampleQuery) for item in queries.values()
        ):
            raise SampleManifestError(
                "manifest.queries must contain keyword, vector, and ask query objects"
            )
        unknown_expected = sorted(
            {
                key
                for query in queries.values()
                for key in query.expected_keys
                if key not in set(keys)
            }
        )
        if unknown_expected:
            raise SampleManifestError(
                "query expectations reference unknown object key(s): "
                + ", ".join(unknown_expected)
            )
        object.__setattr__(self, "queries", MappingProxyType(queries))


@dataclass(frozen=True)
class ContentSearchCorpus:
    manifest: ContentSearchManifest
    payloads: Mapping[str, bytes]

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, ContentSearchManifest):
            raise SampleManifestError("corpus.manifest must be a ContentSearchManifest")
        if not isinstance(self.payloads, Mapping):
            raise SampleManifestError("corpus.payloads must be a mapping")
        payloads = dict(self.payloads)
        expected_sources = {item.source for item in self.manifest.objects}
        if set(payloads) != expected_sources:
            raise SampleManifestError("corpus payloads must exactly match manifest sources")
        for source, payload in payloads.items():
            if not isinstance(source, str) or not isinstance(payload, bytes):
                raise SampleManifestError("corpus payloads must map source names to bytes")
            if len(payload) > MAX_SAMPLE_OBJECT_BYTES:
                raise SampleManifestError(
                    f"sample object {source!r} exceeds {MAX_SAMPLE_OBJECT_BYTES} bytes"
                )
        for item in self.manifest.objects:
            observed = hashlib.sha256(payloads[item.source]).hexdigest()
            if observed != item.sha256:
                raise SampleManifestError(
                    f"sample object {item.source!r} does not match its manifest SHA-256"
                )
        object.__setattr__(self, "payloads", MappingProxyType(payloads))

    def payload_for(self, item: SampleObject) -> bytes:
        if item not in self.manifest.objects:
            raise KeyError("sample object does not belong to this corpus")
        return self.payloads[item.source]

    def object_for_key(self, key: str) -> SampleObject:
        for item in self.manifest.objects:
            if item.key == key:
                return item
        raise KeyError(key)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise SampleManifestError(f"manifest contains duplicate field {key!r}")
        value[key] = item
    return value


def _sample_object(value: object) -> SampleObject:
    raw = _mapping(value, field_name="manifest.objects item")
    fields = {"source", "key", "tier", "media_type", "sha256"}
    _exact_fields(raw, fields, field_name="manifest.objects item")
    return SampleObject(
        source=raw["source"],  # type: ignore[arg-type]
        key=raw["key"],  # type: ignore[arg-type]
        tier=raw["tier"],  # type: ignore[arg-type]
        media_type=raw["media_type"],  # type: ignore[arg-type]
        sha256=raw["sha256"],  # type: ignore[arg-type]
    )


def _sample_query(value: object) -> SampleQuery:
    raw = _mapping(value, field_name="manifest query")
    fields = {"text", "filters", "expected_keys"}
    _exact_fields(raw, fields, field_name="manifest query")
    expected = raw["expected_keys"]
    if not isinstance(expected, list):
        raise SampleManifestError("query.expected_keys must be a list")
    return SampleQuery(
        text=raw["text"],  # type: ignore[arg-type]
        filters=_mapping(raw["filters"], field_name="query.filters"),
        expected_keys=tuple(expected),
    )


def _parse_manifest(payload: bytes) -> ContentSearchManifest:
    if len(payload) > MAX_SAMPLE_MANIFEST_BYTES:
        raise SampleManifestError(
            f"sample manifest exceeds {MAX_SAMPLE_MANIFEST_BYTES} bytes"
        )
    try:
        raw_value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except SampleManifestError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SampleManifestError("sample manifest must be valid UTF-8 JSON") from exc
    raw = _mapping(raw_value, field_name="manifest")
    fields = {"schema_version", "name", "license", "bucket", "objects", "queries"}
    _exact_fields(raw, fields, field_name="manifest")
    raw_objects = raw["objects"]
    if not isinstance(raw_objects, list):
        raise SampleManifestError("manifest.objects must be a list")
    raw_queries = _mapping(raw["queries"], field_name="manifest.queries")
    return ContentSearchManifest(
        schema_version=raw["schema_version"],  # type: ignore[arg-type]
        name=raw["name"],  # type: ignore[arg-type]
        license=raw["license"],  # type: ignore[arg-type]
        bucket=raw["bucket"],  # type: ignore[arg-type]
        objects=tuple(_sample_object(item) for item in raw_objects),
        queries={name: _sample_query(value) for name, value in raw_queries.items()},
    )


def _parse_checksum_manifest(payload: bytes) -> Mapping[str, str]:
    if len(payload) > MAX_SAMPLE_CHECKSUM_MANIFEST_BYTES:
        raise SampleManifestError(
            f"sample checksum manifest exceeds {MAX_SAMPLE_CHECKSUM_MANIFEST_BYTES} bytes"
        )
    try:
        lines = payload.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise SampleManifestError("sample checksum manifest must be ASCII") from exc
    checksums: dict[str, str] = {}
    for line in lines:
        pieces = line.split("  ", 1)
        if len(pieces) != 2 or _SHA256.fullmatch(pieces[0]) is None:
            raise SampleManifestError("sample checksum manifest has an invalid entry")
        filename = _relative_source(pieces[1])
        if filename in checksums:
            raise SampleManifestError(
                f"sample checksum manifest repeats {filename!r}"
            )
        checksums[filename] = pieces[0]
    if not checksums:
        raise SampleManifestError("sample checksum manifest must not be empty")
    return MappingProxyType(checksums)


def read_sample_corpus(
    manifest_path: str | Path | None = None,
) -> ContentSearchCorpus:
    """Read and integrity-check the packaged corpus or an explicit manifest."""

    root: _ReadableResource
    manifest_resource: _ReadableResource
    manifest_name: str
    if manifest_path is None:
        root = cast(
            _ReadableResource,
            resources.files("cognistore.samples.content_search").joinpath("assets"),
        )
        manifest_resource = root.joinpath("manifest.json")
        manifest_name = "manifest.json"
    else:
        manifest_file = Path(manifest_path)
        root = manifest_file.parent
        manifest_resource = manifest_file
        manifest_name = manifest_file.name
    try:
        manifest_payload = manifest_resource.read_bytes()
        checksums = _parse_checksum_manifest(root.joinpath("SHA256SUMS").read_bytes())
    except OSError as exc:
        raise SampleManifestError(
            "could not read the sample manifest or SHA256SUMS"
        ) from exc
    manifest = _parse_manifest(manifest_payload)
    expected_filenames = {manifest_name, *(item.source for item in manifest.objects)}
    if set(checksums) != expected_filenames:
        raise SampleManifestError(
            "sample checksum manifest must cover exactly the manifest and source objects"
        )
    if hashlib.sha256(manifest_payload).hexdigest() != checksums[manifest_name]:
        raise SampleManifestError("sample manifest does not match SHA256SUMS")

    payloads: dict[str, bytes] = {}
    for item in manifest.objects:
        if item.source in payloads:
            continue
        try:
            payloads[item.source] = root.joinpath(*PurePosixPath(item.source).parts).read_bytes()
        except OSError as exc:
            raise SampleManifestError(
                f"could not read sample object {item.source!r}"
            ) from exc
        if hashlib.sha256(payloads[item.source]).hexdigest() != checksums[item.source]:
            raise SampleManifestError(
                f"sample object {item.source!r} does not match SHA256SUMS"
            )
    return ContentSearchCorpus(manifest, payloads)


__all__ = [
    "MAX_SAMPLE_MANIFEST_BYTES",
    "MAX_SAMPLE_OBJECT_BYTES",
    "MAX_SAMPLE_CHECKSUM_MANIFEST_BYTES",
    "SAMPLE_MANIFEST_SCHEMA_VERSION",
    "ContentSearchCorpus",
    "ContentSearchManifest",
    "SampleManifestError",
    "SampleObject",
    "SampleQuery",
    "read_sample_corpus",
]
