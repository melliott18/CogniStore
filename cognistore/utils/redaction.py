"""Dependency-neutral helpers for removing sensitive values."""

from __future__ import annotations

import base64
import json
import re
import threading
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any
from urllib.parse import unquote_plus

REDACTED = "[REDACTED]"


class RedactedValue:
    """Marker for opaque runtime secrets understood by recursive redaction."""

    __slots__ = ()


_known_values: set[str] = set()
_known_values_lock = threading.RLock()


def register_secret_value(value: str | bytes) -> None:
    """Remember opaque material and JSON leaves for process-lifetime redaction.

    Historical versions remain protected after rotation. This registry is never
    persisted and is intentionally not an API for discovering secret values.
    """

    representations: set[str] = set()
    if isinstance(value, bytes):
        representations.update((base64.b64encode(value).decode("ascii"), value.hex()))
        representations.update((repr(value), repr(value)[2:-1]))
        try:
            text = value.decode("utf-8")
        except UnicodeError:
            text = ""
    else:
        text = value
    if text:
        representations.add(text)
        representations.add(json.dumps(text, ensure_ascii=True)[1:-1])
        representations.add(json.dumps(text, ensure_ascii=False)[1:-1])
        try:
            parsed = json.loads(text)
        except (ValueError, RecursionError):
            parsed = None

        def leaves(item: Any) -> None:
            if isinstance(item, str) and item:
                representations.add(item)
                representations.add(json.dumps(item, ensure_ascii=True)[1:-1])
                representations.add(json.dumps(item, ensure_ascii=False)[1:-1])
            elif isinstance(item, Mapping):
                for child in item.values():
                    leaves(child)
            elif isinstance(item, list):
                for child in item:
                    leaves(child)

        try:
            leaves(parsed)
        except RecursionError:
            pass
    with _known_values_lock:
        _known_values.update(item for item in representations if item and item != REDACTED)


@lru_cache(maxsize=1)
def _known_pattern(values: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile("|".join(re.escape(value) for value in values))


def _redact_known_values(text: str) -> str:
    with _known_values_lock:
        values = tuple(sorted(_known_values, key=len, reverse=True))
    # A single pass preserves existing NUL bytes until after exact matching and
    # prevents short secrets from corrupting previously inserted redaction marks.
    return _known_pattern(values).sub(lambda _match: REDACTED, text) if values else text

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

_URL_USERINFO = re.compile(r"(?P<scheme>\b[A-Za-z][A-Za-z0-9+.-]*://)(?P<userinfo>[^/@\s?#]+)@")
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
_CLI_LONG_OPTION_VALUE = re.compile(
    r"(?P<option>(?<!\S)--(?P<key>[A-Za-z][A-Za-z0-9_.-]*))"
    r"(?:(?P<equals>=)(?P<attached>[^\s]*?)|(?P<space>\s+)(?P<separate>\S+))"
    r"(?=$|\s)",
)
_CLI_SHORT_OPTION_VALUE = re.compile(
    r"(?P<option>(?<!\S)-(?P<key>[pPkKsStT]))"
    r"(?:(?P<equals>=?)(?P<attached>[^\s]+)|(?P<space>\s+)(?P<separate>\S+))"
    r"(?=$|\s)",
)
_KEY_VALUE = re.compile(
    r"(?=(?P<assignment>"
    r"(?P<prefix>(?<![\w.-])(?P<quote>[\"']?)(?P<key>[A-Za-z_]"
    r"[A-Za-z0-9_.-]*)(?P=quote)\s*(?P<separator>[:=])\s*)"
    r"(?P<value>\[REDACTED\]|\"(?:\\.|[^\"\\])*\"|"
    r"'(?:\\.|[^'\\])*'|[^\s,;&}\]\)]+)"
    r"))"
)

# Unknown short options have no parser metadata that can reveal their meaning.
# Treat the conventional password/key/secret/token aliases conservatively so a
# typo such as ``-p VALUE`` cannot disclose the following command-line token.
_SECRET_SHORT_OPTIONS = frozenset("pPkKsStT")


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


def _redact_cli_option_text(text: str) -> str:
    """Redact sensitive option/value pairs embedded in diagnostic text."""

    def redact_long(match: re.Match[str]) -> str:
        if not _is_secret_key(match.group("key")):
            return match.group(0)
        separator = match.group("equals") or match.group("space") or ""
        return f"{match.group('option')}{separator}{REDACTED}"

    def redact_short(match: re.Match[str]) -> str:
        if match.group("key") not in _SECRET_SHORT_OPTIONS:
            return match.group(0)
        separator = match.group("equals") or match.group("space") or ""
        return f"{match.group('option')}{separator}{REDACTED}"

    return _CLI_SHORT_OPTION_VALUE.sub(
        redact_short,
        _CLI_LONG_OPTION_VALUE.sub(redact_long, text),
    )


def redact_cli_arguments(arguments: Sequence[str]) -> list[str]:
    """Copy CLI tokens while redacting values of sensitive-looking options.

    ``argparse`` flattens unknown arguments into one string before calling
    ``error()``, which loses token boundaries and can expose a value containing
    spaces. Sanitizing the original token list first keeps that boundary and
    also handles attached, repeated, short-option, and leading-dash values.
    """

    safe: list[str] = []
    index = 0
    while index < len(arguments):
        argument = str(arguments[index])
        if argument == "--":
            # Everything after the end-of-options marker is positional data.
            # Preserve it verbatim so a legitimate path such as ``-psecret``
            # is not mistaken for an unknown credential option.
            safe.extend(str(item) for item in arguments[index:])
            break
        long_option = argument.startswith("--") and len(argument) > 2
        if long_option:
            option, equals, _attached = argument.partition("=")
            if _is_secret_key(option[2:]):
                safe.append(f"{option}={REDACTED}" if equals else option)
                if not equals and index + 1 < len(arguments):
                    safe.append(REDACTED)
                    index += 2
                    while index < len(arguments) and not str(arguments[index]).startswith("-"):
                        safe.append(REDACTED)
                        index += 1
                    continue
                index += 1
                continue

        short_option = (
            len(argument) >= 2
            and argument[0] == "-"
            and argument[1] in _SECRET_SHORT_OPTIONS
            and not argument.startswith("--")
        )
        if short_option:
            option = argument[:2]
            attached = argument[2:]
            if attached:
                separator = "=" if attached.startswith("=") else ""
                safe.append(f"{option}{separator}{REDACTED}")
            else:
                safe.append(option)
                if index + 1 < len(arguments):
                    safe.append(REDACTED)
                    index += 2
                    while index < len(arguments) and not str(arguments[index]).startswith("-"):
                        safe.append(REDACTED)
                        index += 1
                    continue
            index += 1
            continue

        safe.append(argument)
        index += 1
    return safe


def redact_text(text: str) -> str:
    """Return *text* with common credential representations removed.

    Redaction covers credential-bearing URLs, sensitive URL query parameters,
    private-key PEM blocks, authorization/cookie headers, and ``key=value`` or
    ``key: value`` diagnostics. The original string is never changed.
    """

    redacted = _PRIVATE_KEY_BLOCK.sub(REDACTED, _redact_known_values(str(text)))
    redacted = _URL_USERINFO.sub(
        lambda match: f"{match.group('scheme')}{REDACTED}@",
        redacted,
    )

    def redact_query_pair(match: re.Match[str]) -> str:
        value = REDACTED if _is_secret_query_key(match.group("key")) else match.group("value")
        return f"{match.group('prefix')}{match.group('key')}{match.group('equals')}{value}"

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

    redacted = _redact_cli_option_text(redacted)
    return _redact_key_values(redacted).replace("\0", "[NUL]")


def redact(value: Any) -> Any:
    """Recursively copy *value* while replacing sensitive values.

    Mapping keys determine whether their whole value is sensitive. All string
    values are additionally inspected for URLs and textual key/value secrets.
    Lists, tuples, sets, and other sequences are returned as new containers, so
    callers can safely retain or reuse the original input.
    """

    if isinstance(value, RedactedValue):
        return REDACTED
    if isinstance(value, (bytes, bytearray)):
        # Binary diagnostic values have no safe schema; do not expose keys via
        # an opaque bytes representation even when they predate registration.
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            (redact_text(key) if isinstance(key, str) else key): (
                REDACTED if isinstance(key, str) and _is_secret_key(key) else redact(item)
            )
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


__all__ = [
    "REDACTED", "RedactedValue", "redact", "redact_cli_arguments", "redact_text",
    "register_secret_value",
]
