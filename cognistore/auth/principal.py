"""Small, token-free identity passed across request and job boundaries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

PRINCIPAL_METADATA = "cognistore.principal"
_principal: ContextVar[Principal | None] = ContextVar("cognistore_principal", default=None)


def _identity(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 2048
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"{name} must be a non-empty bounded identity without control characters")
    try:
        if len(value.encode("utf-8")) > 2048:
            raise ValueError(f"{name} exceeds the identity byte limit")
    except UnicodeEncodeError:
        raise ValueError(f"{name} must be valid UTF-8") from None


@dataclass(frozen=True)
class Principal:
    """An issuer-scoped subject, for both human and service access tokens.

    This is authenticated identity, not a role or a permission grant. A service
    uses its issuer-assigned subject just as a human does; client_id is optional
    client attribution and is never a substitute for the subject.
    """

    issuer: str
    subject: str
    client_id: str | None = None

    def __post_init__(self) -> None:
        _identity(self.issuer, "issuer")
        _identity(self.subject, "subject")
        if self.client_id is not None:
            _identity(self.client_id, "client_id")

    @property
    def actor_type(self) -> str:
        return "authenticated"

    @property
    def actor_id(self) -> str:
        """Stable audit identity, unambiguous across issuers and delimiters."""
        encoded = json.dumps([self.issuer, self.subject], ensure_ascii=True).encode("utf-8")
        return "principal:sha256:" + hashlib.sha256(encoded).hexdigest()

    def to_json(self) -> str:
        return json.dumps(
            {"issuer": self.issuer, "subject": self.subject, "client_id": self.client_id},
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> Principal:
        """Decode only the identity contract; reject claims/tokens and extra fields."""
        if not isinstance(value, str) or len(value) > 40_000:
            raise ValueError("Invalid principal metadata")
        try:
            decoded = json.loads(value)
            if not isinstance(decoded, dict) or set(decoded) != {"issuer", "subject", "client_id"}:
                raise ValueError("Invalid principal metadata")
            return cls(**decoded)
        except (TypeError, ValueError, RecursionError):
            raise ValueError("Invalid principal metadata") from None


def current_principal() -> Principal | None:
    """Return this operation's verified identity, including inside thread workers."""
    return _principal.get()


@contextmanager
def principal_context(principal: Principal | None) -> Iterator[None]:
    """Bind one operation's identity and restore the previous context on exit."""
    token = _principal.set(principal)
    try:
        yield
    finally:
        _principal.reset(token)
