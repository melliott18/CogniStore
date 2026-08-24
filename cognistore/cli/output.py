"""Stable CLI payloads and secret-safe output helpers.

The CLI uses stdout as a machine-readable channel when JSON output is selected.
This module keeps that channel to one JSON document and centralizes the redaction
used by both JSON output and verbose diagnostics.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any, TextIO
from urllib.parse import unquote_plus

SCHEMA_NAME = "cognistore.cli"
SCHEMA_VERSION = 1
REDACTED = "[REDACTED]"

_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_SEPARATOR = re.compile(r"[^a-z0-9]+")

# These are identifiers or usage measurements, not authentication material.
# Keep the list explicit so a broad "*_key" or "*token*" rule cannot hide
# fields that operators need for correlation and capacity accounting.
_BENIGN_KEY_NAMES = {
    "cached_tokens",
    "completion_tokens",
    "correlation_id",
    "idempotency_key",
    "input_tokens",
    "max_tokens",
    "output_tokens",
    "profile",
    "profile_name",
    "prompt_tokens",
    "reasoning_tokens",
    "token_budget",
    "token_count",
    "token_counts",
    "token_limit",
    "token_type",
    "token_usage",
    "total_tokens",
}

_SECRET_KEY_NAMES = {
    "api_key",
    "apikey",
    "auth",
    "auth_token",
    "authorization",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
    "bearer_token",
    "client_assertion",
    "client_secret",
    "cookie",
    "credential",
    "credentials",
    "creds",
    "database_password",
    "id_token",
    "jwt",
    "nats_creds",
    "nkey_seed",
    "oauth_token",
    "passphrase",
    "passwd",
    "password",
    "private_key",
    "proxy_authorization",
    "pwd",
    "refresh_token",
    "secret",
    "secret_access_key",
    "secret_key",
    "security_token",
    "session_token",
    "set_cookie",
    "signature",
    "token",
}

_SECRET_KEY_PARTS = {
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "creds",
    "passphrase",
    "passwd",
    "password",
    "pwd",
    "secret",
    "secrets",
}

_TOKEN_METRIC_WORDS = {
    "budget",
    "cached",
    "completion",
    "count",
    "counts",
    "input",
    "limit",
    "max",
    "num",
    "number",
    "output",
    "prompt",
    "reasoning",
    "total",
    "type",
    "usage",
    "used",
}

_URL_USERINFO = re.compile(
    r"(?P<scheme>\b[A-Za-z][A-Za-z0-9+.-]*://)(?P<userinfo>[^/@\s?#]+)@"
)
_URL_QUERY_PAIR = re.compile(
    r"(?P<prefix>[?&;])(?P<key>[^?&;=#\s]+)(?P<equals>=)(?P<value>[^&#;\s]*)"
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----.*?"
    r"-----END(?: [A-Z0-9]+)* PRIVATE KEY-----",
    flags=re.IGNORECASE | re.DOTALL,
)
_AUTHORIZATION_VALUE = re.compile(
    r"(?P<prefix>(?<![\w.-])(?:proxy[-_.]?authorization|authorization)\s*[:=]\s*)"
    r"(?P<value>[^\r\n,]+)",
    flags=re.IGNORECASE,
)
_COOKIE_VALUE = re.compile(
    r"(?P<prefix>(?<![\w.-])(?:set[-_.]?cookie|cookie)\s*[:=]\s*)"
    r"(?P<value>[^\r\n]+)",
    flags=re.IGNORECASE,
)
_BEARER_CREDENTIAL = re.compile(
    r"\b(?P<scheme>Bearer|Basic)\s+(?P<credential>[A-Za-z0-9._~+/=-]+)",
    flags=re.IGNORECASE,
)
_KEY_VALUE = re.compile(
    r"(?=(?P<assignment>"
    r"(?P<prefix>(?<![\w.-])(?P<quote>[\"']?)(?P<key>[A-Za-z_]"
    r"[A-Za-z0-9_.-]*)(?P=quote)\s*(?P<separator>[:=])\s*)"
    r"(?P<value>\[REDACTED\]|\"(?:\\.|[^\"\\])*\"|"
    r"'(?:\\.|[^'\\])*'|[^\s,;&}\]\)]+)"
    r"))"
)


def _normalize_key(key: str) -> tuple[str, frozenset[str]]:
    decoded = unquote_plus(key)
    separated = _CAMEL_CASE_BOUNDARY.sub("_", decoded).lower()
    normalized = _KEY_SEPARATOR.sub("_", separated).strip("_")
    return normalized, frozenset(part for part in normalized.split("_") if part)


def _is_secret_key(key: str) -> bool:
    normalized, parts = _normalize_key(key)
    if normalized in _BENIGN_KEY_NAMES:
        return False
    if normalized in _SECRET_KEY_NAMES or parts.intersection(_SECRET_KEY_PARTS):
        return True
    if "private" in parts and "key" in parts:
        return True
    if "access" in parts and "key" in parts:
        return True
    if "api" in parts and "key" in parts:
        return True
    if ("signing" in parts or "encryption" in parts) and "key" in parts:
        return True
    if "token" in parts or "tokens" in parts:
        return not bool(parts.intersection(_TOKEN_METRIC_WORDS))
    return False


def _is_secret_query_key(key: str) -> bool:
    normalized, _ = _normalize_key(key)
    return _is_secret_key(key) or normalized in {
        "code",
        "saml_response",
        "sig",
        "x_amz_signature",
    }


def _redact_quoted_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return f"{value[0]}{REDACTED}{value[-1]}"
    return REDACTED


def _redact_key_values(text: str) -> str:
    replacements: list[tuple[int, int, str]] = []
    for match in _KEY_VALUE.finditer(text):
        if not _is_secret_key(match.group("key")):
            continue
        replacements.append(
            (
                match.start("assignment"),
                match.end("assignment"),
                f"{match.group('prefix')}{_redact_quoted_value(match.group('value'))}",
            )
        )

    if not replacements:
        return text

    result: list[str] = []
    cursor = 0
    for start, end, replacement in replacements:
        if start < cursor:
            continue
        result.extend((text[cursor:start], replacement))
        cursor = end
    result.append(text[cursor:])
    return "".join(result)


def redact_text(text: str) -> str:
    """Return *text* with common credential representations removed.

    Redaction covers credential-bearing URLs, sensitive URL query parameters,
    private-key PEM blocks, authorization/cookie headers, and ``key=value`` or
    ``key: value`` diagnostics. The original string is never changed.
    """

    redacted = _PRIVATE_KEY_BLOCK.sub(REDACTED, str(text))
    redacted = _URL_USERINFO.sub(
        lambda match: f"{match.group('scheme')}{REDACTED}@",
        redacted,
    )

    def redact_query_pair(match: re.Match[str]) -> str:
        value = REDACTED if _is_secret_query_key(match.group("key")) else match.group("value")
        return (
            f"{match.group('prefix')}{match.group('key')}"
            f"{match.group('equals')}{value}"
        )

    redacted = _URL_QUERY_PAIR.sub(redact_query_pair, redacted)
    redacted = _AUTHORIZATION_VALUE.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}",
        redacted,
    )
    redacted = _COOKIE_VALUE.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}",
        redacted,
    )
    redacted = _BEARER_CREDENTIAL.sub(
        lambda match: f"{match.group('scheme')} {REDACTED}",
        redacted,
    )

    return _redact_key_values(redacted)


def redact(value: Any) -> Any:
    """Recursively copy *value* while replacing sensitive values.

    Mapping keys determine whether their whole value is sensitive. All string
    values are additionally inspected for URLs and textual key/value secrets.
    Lists, tuples, sets, and other sequences are returned as new containers, so
    callers can safely retain or reuse the original input.
    """

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            key: REDACTED if isinstance(key, str) and _is_secret_key(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, set):
        return {redact(item) for item in value}
    if isinstance(value, frozenset):
        return frozenset(redact(item) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    return value


def _build_payload(command: str | None, status: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(fields)
    payload.update(
        {
            "schema": SCHEMA_NAME,
            "schema_version": SCHEMA_VERSION,
            "command": command,
            "status": status,
        }
    )
    return redact(payload)


def result_payload(command: str | None, status: str, **fields: Any) -> dict[str, Any]:
    """Build a redacted, versioned result envelope for a CLI command."""

    return _build_payload(command, status, fields)


def error_payload(
    command: str | None,
    error: BaseException | str,
    *,
    exit_code: int = 1,
    retryable: bool = False,
    error_type: str | None = None,
) -> dict[str, Any]:
    """Build a versioned error envelope with compatibility-friendly flat fields."""

    inferred_type = type(error).__name__ if isinstance(error, BaseException) else "Error"
    resolved_type = inferred_type if error_type is None else error_type
    return _build_payload(
        command,
        "error",
        {
            "error_type": resolved_type,
            "error": str(error),
            "exit_code": exit_code,
            "retryable": retryable,
        },
    )


def emit_json(payload: Mapping[str, Any], stream: TextIO | None = None) -> None:
    """Write exactly one redacted, sorted JSON object followed by one newline."""

    target = sys.stdout if stream is None else stream
    document = json.dumps(redact(payload), sort_keys=True)
    target.write(f"{document}\n")


def _render_diagnostic(value: Any) -> str:
    safe_value = redact(value)
    if isinstance(safe_value, str):
        return safe_value
    try:
        return json.dumps(safe_value, sort_keys=True)
    except (TypeError, ValueError):
        return redact_text(str(safe_value))


def emit_verbose(
    message: Any,
    *details: Any,
    enabled: bool,
    stream: TextIO | None = None,
    **fields: Any,
) -> None:
    """Emit a redacted diagnostic to stderr when verbose output is enabled."""

    if not enabled:
        return
    target = sys.stderr if stream is None else stream
    parts = [_render_diagnostic(message)]
    parts.extend(_render_diagnostic(detail) for detail in details)
    if fields:
        parts.append(_render_diagnostic(fields))
    target.write(f"{' '.join(parts)}\n")


class VerboseReporter:
    """Small callable wrapper for consistently gated verbose diagnostics."""

    def __init__(self, enabled: bool = False, stream: TextIO | None = None) -> None:
        self.enabled = enabled
        self.stream = stream

    def emit(self, message: Any, *details: Any, **fields: Any) -> None:
        emit_verbose(
            message,
            *details,
            enabled=self.enabled,
            stream=self.stream,
            **fields,
        )

    __call__ = emit
    report = emit


__all__ = [
    "REDACTED",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "VerboseReporter",
    "emit_json",
    "emit_verbose",
    "error_payload",
    "redact",
    "redact_text",
    "result_payload",
]
