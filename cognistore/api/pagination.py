"""Opaque, query-bound keyset cursors for public collection endpoints."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping


class CursorError(ValueError):
    """Raised when a page cursor is malformed or belongs to another query."""


def _fingerprint(filters: Mapping[str, object]) -> str:
    payload = json.dumps(
        dict(filters),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def encode_cursor(*, resource: str, filters: Mapping[str, object], last_key: str) -> str:
    payload = json.dumps(
        {
            "filters": _fingerprint(filters),
            "last_key": last_key,
            "resource": resource,
            "version": 1,
        },
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(
    cursor: str | None,
    *,
    resource: str,
    filters: Mapping[str, object],
) -> str | None:
    if cursor is None:
        return None
    if not cursor or len(cursor) > 16_384:
        raise CursorError("cursor must be a non-empty bounded token")
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(
            cursor + padding,
            altchars=b"-_",
            validate=True,
        )
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise CursorError("cursor is malformed") from exc
    if not isinstance(value, dict) or set(value) != {
        "filters",
        "last_key",
        "resource",
        "version",
    }:
        raise CursorError("cursor is malformed")
    if value["version"] != 1 or value["resource"] != resource:
        raise CursorError("cursor is not valid for this resource")
    if value["filters"] != _fingerprint(filters):
        raise CursorError("cursor does not match the requested filters")
    last_key = value["last_key"]
    if not isinstance(last_key, str) or not last_key:
        raise CursorError("cursor is malformed")
    return last_key
