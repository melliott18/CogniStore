"""Stable CLI payloads and secret-safe output helpers.

The CLI uses stdout as a machine-readable channel when JSON output is selected.
This module keeps that channel to one JSON document and centralizes the redaction
used by both JSON output and verbose diagnostics.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import Any, TextIO

from cognistore.utils.redaction import REDACTED, redact, redact_cli_arguments, redact_text

SCHEMA_NAME = "cognistore.cli"
SCHEMA_VERSION = 1


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
    "redact_cli_arguments",
    "redact_text",
    "result_payload",
]
