"""Validate access JWTs against a configured issuer's public signing keys.

Only public keys are cached. Tokens, claim dictionaries, and principals are
request-local, and all authentication failures have the same safe message.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from cognistore.auth.principal import Principal

_ASYMMETRIC_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)
_MAX_TOKEN_BYTES = 16_384
_MAX_DOCUMENT_BYTES = 1_048_576
_MAX_JWKS_KEYS = 100
_MAX_KID_LENGTH = 256
_REQUIRED_CLAIMS = ("iss", "aud", "sub", "exp")


class AuthenticationError(Exception):
    """The credential cannot be authenticated; contains no credential details."""

    def __init__(self) -> None:
        super().__init__("Invalid bearer token")


def _https_url(value: object, *, issuer: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or any(character.isspace() or ord(character) < 32 for character in value)
        or "\\" in value
        or "#" in value
        or (issuer and "?" in value)
    ):
        raise ValueError("Authentication URLs must be absolute HTTPS URLs")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or "@" in parsed.netloc
            or "%" in parsed.netloc
            or parsed.port == 0
            or not httpx.URL(value).host
        ):
            raise ValueError("Invalid authentication URL")
    except (ValueError, httpx.InvalidURL):
        raise ValueError("Authentication URLs must be absolute HTTPS URLs") from None
    return value


def _strings(value: object, *, field: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= maximum:
        raise ValueError(f"{field} must be a nonempty sequence")
    if any(not isinstance(item, str) or not item.strip() or len(item) > 128 for item in value):
        raise ValueError(f"{field} must contain nonempty strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} must not contain duplicates")
    return tuple(value)


@dataclass(frozen=True)
class JWTAuthConfig:
    issuer: str
    audience: str
    algorithms: tuple[str, ...] = ("RS256",)
    jwks_uri: str | None = None
    required_claims: tuple[str, ...] = ("sub", "exp", "iat")
    leeway_seconds: float = 30
    cache_ttl_seconds: float = 300
    refresh_interval_seconds: float = 30
    timeout_seconds: float = 5

    def __post_init__(self) -> None:
        _https_url(self.issuer, issuer=True)
        if self.jwks_uri is not None:
            _https_url(self.jwks_uri)
        if (
            not isinstance(self.audience, str)
            or not self.audience.strip()
            or self.audience != self.audience.strip()
            or len(self.audience) > 2048
        ):
            raise ValueError("JWT audience must be a nonempty string")
        algorithms = _strings(self.algorithms, field="algorithms", maximum=10)
        if not set(algorithms) <= _ASYMMETRIC_ALGORITHMS:
            raise ValueError("JWT algorithms must be supported asymmetric signing algorithms")
        object.__setattr__(self, "algorithms", algorithms)
        object.__setattr__(
            self,
            "required_claims",
            _strings(self.required_claims, field="required_claims", maximum=32),
        )
        for name, upper, allow_zero in (
            ("leeway_seconds", 300, True),
            ("cache_ttl_seconds", 3600, False),
            ("refresh_interval_seconds", 300, False),
            ("timeout_seconds", 30, False),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value < 0
                or (not allow_zero and value == 0)
                or value > upper
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} is outside the supported range")
        if self.refresh_interval_seconds > self.cache_ttl_seconds:
            raise ValueError("refresh_interval_seconds must not exceed cache_ttl_seconds")


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous duplicate fields in issuer documents and JWTs."""
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON field")
        result[name] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("Nonfinite JSON number")


class JWTAuthenticator:
    """Synchronous, thread-safe verifier with bounded JWKS refreshes.

    A new key ID can trigger an immediate refresh after normal cache loading;
    subsequent unknown IDs share a cooldown. Expired keys are never a fallback
    for a failed refresh. Caller-supplied HTTP clients remain caller-owned.
    """

    def __init__(
        self,
        config: JWTAuthConfig,
        *,
        http_client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._clock = clock
        self._client = (
            http_client
            if http_client is not None
            else httpx.Client(
                timeout=config.timeout_seconds, follow_redirects=False, trust_env=False
            )
        )
        self._owns_client = http_client is None
        self._lock = threading.Lock()
        self._closed = False
        self._keys: dict[tuple[str, str], Any] = {}
        self._expires_at = 0.0
        self._last_failure_at: float | None = None
        self._last_unknown_refresh_at: float | None = None
        self._jwks_uri = config.jwks_uri

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._keys.clear()
            if self._owns_client:
                self._client.close()

    def authenticate(self, token: str) -> Principal:
        try:
            if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_BYTES:
                raise AuthenticationError()
            # Strict parsing avoids duplicate/NaN claims being interpreted
            # differently by the verifier and other JWT consumers.
            parts = token.encode("ascii").split(b".")
            if len(parts) != 3:
                raise AuthenticationError()
            decoded_parts = []
            for part in parts:
                decoded = jwt.utils.base64url_decode(part)
                # Older supported PyJWT versions accept ignored characters,
                # padding, and noncanonical pad bits in compact JWT segments.
                if not part or jwt.utils.base64url_encode(decoded) != part:
                    raise AuthenticationError()
                decoded_parts.append(decoded)
            for decoded in decoded_parts[:2]:
                document = json.loads(
                    decoded,
                    object_pairs_hook=_json_object,
                    parse_constant=_invalid_constant,
                )
                if not isinstance(document, dict):
                    raise AuthenticationError()
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            kid = header.get("kid")
            if (
                not isinstance(algorithm, str)
                or algorithm not in self.config.algorithms
                or not isinstance(kid, str)
                or not kid.strip()
                or len(kid) > _MAX_KID_LENGTH
                or "crit" in header
                or header.get("b64", True) is not True
            ):
                raise AuthenticationError()
            # Only kid selects a configured issuer key. Token-supplied jku,
            # x5u, jwk, and x5c values are never used for key discovery.
            key = self._key(kid, algorithm)
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(self.config.algorithms),
                issuer=self.config.issuer,
                audience=self.config.audience,
                leeway=self.config.leeway_seconds,
                options={
                    "require": list(
                        dict.fromkeys((*_REQUIRED_CLAIMS, *self.config.required_claims))
                    ),
                },
            )
            for name in ("exp", "iat", "nbf"):
                if name in claims:
                    value = claims[name]
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                    ):
                        raise AuthenticationError()
            if not isinstance(claims["sub"], str) or not claims["sub"].strip():
                raise AuthenticationError()
            for name in ("client_id", "azp"):
                if name in claims and (
                    not isinstance(claims[name], str) or not claims[name].strip()
                ):
                    raise AuthenticationError()
            return Principal(
                issuer=self.config.issuer,
                subject=claims["sub"],
                client_id=claims.get("client_id", claims.get("azp")),
            )
        except (
            jwt.PyJWTError,
            httpx.HTTPError,
            ValueError,
            TypeError,
            OverflowError,
            RuntimeError,
        ):
            raise AuthenticationError() from None

    def _key(self, kid: str, algorithm: str) -> Any:
        with self._lock:
            if self._closed:
                raise AuthenticationError()
            now = self._clock()
            fresh = bool(self._keys) and now < self._expires_at
            if fresh and (kid, algorithm) in self._keys:
                return self._keys[kid, algorithm]
            interval = self.config.refresh_interval_seconds
            if self._last_failure_at is not None and now - self._last_failure_at < interval:
                raise AuthenticationError()
            if fresh:
                if (
                    self._last_unknown_refresh_at is not None
                    and now - self._last_unknown_refresh_at < interval
                ):
                    raise AuthenticationError()
                self._last_unknown_refresh_at = now
            try:
                keys = self._fetch_keys()
            except (
                AuthenticationError,
                jwt.PyJWTError,
                httpx.HTTPError,
                ValueError,
                TypeError,
                OverflowError,
                RuntimeError,
            ):
                self._last_failure_at = self._clock()
                raise AuthenticationError() from None
            self._keys = keys
            self._expires_at = self._clock() + self.config.cache_ttl_seconds
            self._last_failure_at = None
            if (kid, algorithm) not in keys:
                # A first request with an unknown kid already fetched a JWKS;
                # do not allow a second immediate miss to fetch it again.
                self._last_unknown_refresh_at = self._clock()
                raise AuthenticationError()
            return keys[kid, algorithm]

    def _document(self, url: str) -> dict[str, Any]:
        deadline = self._clock() + self.config.timeout_seconds
        with self._client.stream(
            "GET",
            url,
            follow_redirects=False,
            timeout=self.config.timeout_seconds,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        ) as response:
            response.raise_for_status()
            # Refuse compression to keep both memory and total-duration bounds
            # effective before HTTPX's decompressor can expand/buffer data.
            if (
                response.headers.get("content-encoding", "identity").lower() != "identity"
                or self._clock() > deadline
            ):
                raise AuthenticationError()
            content_length = response.headers.get("content-length")
            if content_length is not None and int(content_length) > _MAX_DOCUMENT_BYTES:
                raise AuthenticationError()
            body = bytearray()
            # No chunk_size: inspect each transport chunk, including trickled
            # one-byte responses, before a chunk aggregator can hide its delay.
            for chunk in response.iter_bytes():
                if self._clock() > deadline or len(body) + len(chunk) > _MAX_DOCUMENT_BYTES:
                    raise AuthenticationError()
                body.extend(chunk)
        document = json.loads(
            body, object_pairs_hook=_json_object, parse_constant=_invalid_constant
        )
        if not isinstance(document, dict):
            raise AuthenticationError()
        return document

    def _fetch_keys(self) -> dict[tuple[str, str], Any]:
        if self._jwks_uri is None:
            metadata = self._document(
                self.config.issuer.rstrip("/") + "/.well-known/openid-configuration"
            )
            if metadata.get("issuer") != self.config.issuer:
                raise AuthenticationError()
            self._jwks_uri = _https_url(metadata.get("jwks_uri"))
        document = self._document(self._jwks_uri)
        entries = document.get("keys")
        if not isinstance(entries, list) or not 1 <= len(entries) <= _MAX_JWKS_KEYS:
            raise AuthenticationError()
        keys: dict[tuple[str, str], Any] = {}
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise AuthenticationError()
            kid = entry.get("kid")
            if not isinstance(kid, str) or not kid.strip() or len(kid) > _MAX_KID_LENGTH:
                continue
            if kid in seen:
                raise AuthenticationError()
            seen.add(kid)
            if entry.get("use", "sig") != "sig":
                continue
            operations = entry.get("key_ops", ["verify"])
            if not isinstance(operations, list) or any(
                not isinstance(op, str) for op in operations
            ):
                raise AuthenticationError()
            if "verify" not in operations:
                continue
            # Public JWKS must not make private or symmetric material usable.
            if any(field in entry for field in ("d", "p", "q", "dp", "dq", "qi", "oth", "k")):
                raise AuthenticationError()
            if any(
                not isinstance(entry[field], str) or len(entry[field]) > 4096
                for field in ("n", "e", "x", "y")
                if field in entry
            ):
                raise AuthenticationError()
            for algorithm in self.config.algorithms:
                if "alg" in entry and entry["alg"] != algorithm:
                    continue
                if algorithm.startswith(("RS", "PS")) and entry.get("kty") != "RSA":
                    continue
                if algorithm.startswith("ES") and (
                    entry.get("kty") != "EC"
                    or entry.get("crv")
                    != {"ES256": "P-256", "ES384": "P-384", "ES512": "P-521"}[algorithm]
                ):
                    continue
                if algorithm == "EdDSA" and (
                    entry.get("kty") != "OKP" or entry.get("crv") not in ("Ed25519", "Ed448")
                ):
                    continue
                key = jwt.PyJWK.from_dict(entry, algorithm=algorithm).key
                if isinstance(key, rsa.RSAPublicKey) and not 2048 <= key.key_size <= 16_384:
                    raise AuthenticationError()
                keys[kid, algorithm] = key
        if not keys:
            raise AuthenticationError()
        return keys
