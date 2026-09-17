"""Runtime secret and envelope-key contracts with bounded, fail-closed caching."""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Protocol

from cognistore.utils.redaction import REDACTED, RedactedValue, register_secret_value


class SecretError(RuntimeError):
    """A safe failure at the secret boundary; never include provider details."""


class SecretUnavailableError(SecretError):
    """A temporary provider failure; expired material is never returned."""

    retryable = True


class SecretAccessError(SecretError):
    """A denied, missing, or unusable secret requiring operator intervention."""


class SecretConfigurationError(ValueError):
    """Invalid reference or configuration, with no reflected input values."""


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\0" not in value


def _positive(value: object) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value) and value > 0
    )


@dataclass(frozen=True, repr=False)
class SecretReference:
    provider: str
    name: str
    version: str | None = None
    field: str | None = None

    def __post_init__(self) -> None:
        if not _text(self.provider) or not _text(self.name):
            raise SecretConfigurationError("Secret reference requires provider and name")
        if any(value is not None and not _text(value) for value in (self.version, self.field)):
            raise SecretConfigurationError("Secret reference version and field must be text")

    def __repr__(self) -> str:
        return "SecretReference([REDACTED])"


@dataclass(frozen=True, repr=False)
class KeyReference:
    provider: str
    name: str
    ciphertext: bytes = field(repr=False)
    context: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not _text(self.provider) or not _text(self.name):
            raise SecretConfigurationError("Key reference requires provider and name")
        if not isinstance(self.ciphertext, bytes) or not self.ciphertext:
            raise SecretConfigurationError("Key reference requires wrapped key bytes")
        if not isinstance(self.context, tuple) or any(
            not isinstance(pair, tuple) or len(pair) != 2
            or not _text(pair[0]) or not _text(pair[1]) for pair in self.context
        ):
            raise SecretConfigurationError("Key context must contain text pairs")
        if len(dict(self.context)) != len(self.context):
            raise SecretConfigurationError("Key context contains duplicate names")
        object.__setattr__(self, "context", tuple(sorted(self.context)))

    def __repr__(self) -> str:
        return "KeyReference([REDACTED])"


class SecretValue(RedactedValue):
    """Opaque material; revealing it is an explicit action at a client boundary.

    This is deliberately not a dataclass: generic dataclass serialization must
    not unwrap it. Registered material remains redacted after cache eviction or
    rotation, for the lifetime of this process.
    """

    __slots__ = ("__value", "version", "ttl_seconds")

    def __init__(
        self, value: str | bytes, version: str | None = None, ttl_seconds: float | None = None,
    ) -> None:
        if not isinstance(value, (str, bytes)) or not value:
            raise SecretAccessError("Secret provider returned empty or unsupported material")
        if version is not None and not _text(version):
            raise SecretAccessError("Secret provider returned an invalid version")
        if ttl_seconds is not None and not _positive(ttl_seconds):
            raise SecretAccessError("Secret provider returned an invalid lifetime")
        self.__value = value
        self.version = version
        self.ttl_seconds = ttl_seconds
        register_secret_value(value)

    def reveal(self) -> str | bytes:
        return self.__value

    def text(self) -> str:
        if isinstance(self.__value, str):
            return self.__value
        try:
            return self.__value.decode("utf-8")
        except UnicodeError:
            pass
        raise SecretAccessError("Secret material is not UTF-8 text")

    def __repr__(self) -> str:
        return f"SecretValue({REDACTED})"

    def __str__(self) -> str:
        return REDACTED


class SecretProvider(Protocol):
    def fetch(self, reference: SecretReference) -> SecretValue: ...


class KeyProvider(Protocol):
    """Unwrap an existing data key; never export a KMS master key."""

    def unwrap(self, reference: KeyReference) -> SecretValue: ...


class SecretResolver:
    """Thread-safe LRU cache; no stale-on-error or implicit environment fallback.

    Expiry starts before the fetch, so provider latency cannot extend a lease.
    The lock also prevents concurrent duplicate refreshes and serializes explicit
    invalidation. A value already leased by an operation is not revoked in place.
    """

    def __init__(
        self,
        providers: Mapping[str, SecretProvider],
        *,
        key_providers: Mapping[str, KeyProvider] | None = None,
        cache_ttl_seconds: float = 300,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not _positive(cache_ttl_seconds):
            raise SecretConfigurationError("Secret cache TTL must be finite and positive")
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise SecretConfigurationError("Secret cache capacity must be a positive integer")
        if any(not _text(name) for name in (*providers, *(key_providers or {}))):
            raise SecretConfigurationError("Secret provider names must be non-empty text")
        self._providers = dict(providers)
        self._key_providers = dict(key_providers or {})
        self._ttl = float(cache_ttl_seconds)
        self._capacity = max_entries
        self._clock = clock
        self._lock = threading.RLock()
        self._cache: OrderedDict[SecretReference | KeyReference, tuple[float, SecretValue]] = (
            OrderedDict()
        )
        self._closed = False

    def _resolve(self, reference: SecretReference | KeyReference) -> SecretValue:
        from .diagnostics import secret_operation

        with self._lock:
            if self._closed:
                raise SecretConfigurationError("Secret resolver is closed")
            start = self._clock()
            cached = self._cache.get(reference)
            if cached is not None and start < cached[0]:
                self._cache.move_to_end(reference)
                return cached[1]
            self._cache.pop(reference, None)
            if isinstance(reference, KeyReference):
                key_provider = self._key_providers.get(reference.provider)
                if key_provider is None:
                    raise SecretConfigurationError("Unknown key provider")
                fetch = partial(key_provider.unwrap, reference)
            else:
                provider = self._providers.get(reference.provider)
                if provider is None:
                    raise SecretConfigurationError("Unknown secret provider")
                fetch = partial(provider.fetch, reference)
            # Do not trust exception messages from third-party implementations.
            failure: type[Exception] | None = None
            try:
                with secret_operation():
                    value = fetch()
                if not isinstance(value, SecretValue):
                    raise SecretAccessError("Invalid secret provider result")
            except SecretConfigurationError:
                failure = SecretConfigurationError
            except SecretAccessError:
                failure = SecretAccessError
            except Exception:
                failure = SecretUnavailableError
            if failure is not None:
                # Raise outside the handler: no hidden __context__ can leak into
                # job classifiers or exception introspection.
                raise failure("Secret resolution failed")
            ttl = min(self._ttl, value.ttl_seconds) if value.ttl_seconds is not None else self._ttl
            expires = start + ttl
            if self._clock() >= expires:
                raise SecretUnavailableError("Secret expired during resolution")
            self._cache[reference] = (expires, value)
            while len(self._cache) > self._capacity:
                self._cache.popitem(last=False)
            return value

    def resolve(self, reference: SecretReference) -> SecretValue:
        if not isinstance(reference, SecretReference):
            raise SecretConfigurationError("Expected a secret reference")
        return self._resolve(reference)

    def resolve_key(self, reference: KeyReference) -> SecretValue:
        if not isinstance(reference, KeyReference):
            raise SecretConfigurationError("Expected a key reference")
        return self._resolve(reference)

    def resolve_many(self, references: Iterable[SecretReference]) -> tuple[SecretValue, ...]:
        with self._lock:
            refs = tuple(references)
            values: list[SecretValue] = []
            deadlines: list[float] = []
            for reference in refs:
                values.append(self.resolve(reference))
                deadlines.append(self._cache[reference][0])
            now = self._clock()
            if any(now >= deadline for deadline in deadlines):
                raise SecretUnavailableError("Secret bundle expired during resolution")
            return tuple(values)

    def invalidate(self, reference: SecretReference | KeyReference | None = None) -> None:
        with self._lock:
            if reference is None:
                self._cache.clear()
            else:
                # Also discard alternate field projections for this secret.
                for cached in list(self._cache):
                    if cached == reference or (
                        isinstance(reference, SecretReference)
                        and isinstance(cached, SecretReference)
                        and (cached.provider, cached.name, cached.version)
                        == (reference.provider, reference.name, reference.version)
                    ):
                        del self._cache[cached]

    def close(self) -> None:
        from .diagnostics import secret_operation

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cache.clear()
            seen: set[int] = set()
            for provider in (*self._providers.values(), *self._key_providers.values()):
                if id(provider) in seen:
                    continue
                seen.add(id(provider))
                close = getattr(provider, "close", None)
                if close is not None:
                    try:
                        with secret_operation():
                            close()
                    except Exception:
                        pass  # Cleanup cannot surface provider diagnostics.
