from __future__ import annotations

import copy
import io
import json

from cognistore.cli.output import (
    REDACTED,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    VerboseReporter,
    emit_json,
    error_payload,
    redact,
    redact_cli_arguments,
    redact_text,
    result_payload,
)


def test_result_payload_has_stable_envelope_and_cannot_override_reserved_fields() -> None:
    payload = result_payload(
        "move",
        "completed",
        schema="caller.schema",
        schema_version=99,
        count=1,
    )

    assert payload == {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "command": "move",
        "status": "completed",
        "count": 1,
    }


def test_redact_copies_nested_data_and_preserves_benign_identifiers() -> None:
    original = {
        "profile": "production",
        "idempotency_key": "move-123",
        "correlation_id": "trace-456",
        "usage": {"prompt_tokens": 12, "completion_tokens": 7},
        "password": "hunter2",
        "auth": "basic-secret",
        "jwt": "header.payload.signature",
        "nkeySeed": "SUANATSSEED",
        "nested": [
            {
                "clientSecret": "do-not-print",
                "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE",
                "private-key": "key material",
            },
            ("safe", {"refresh_token": "refresh-me"}),
        ],
    }
    snapshot = copy.deepcopy(original)

    safe = redact(original)

    assert original == snapshot
    assert safe is not original
    assert safe["profile"] == "production"
    assert safe["idempotency_key"] == "move-123"
    assert safe["correlation_id"] == "trace-456"
    assert safe["usage"] == {"prompt_tokens": 12, "completion_tokens": 7}
    assert safe["password"] == REDACTED
    assert safe["auth"] == REDACTED
    assert safe["jwt"] == REDACTED
    assert safe["nkeySeed"] == REDACTED
    assert safe["nested"][0] == {
        "clientSecret": REDACTED,
        "AWS_ACCESS_KEY_ID": REDACTED,
        "private-key": REDACTED,
    }
    assert safe["nested"][1] == ("safe", {"refresh_token": REDACTED})


def test_redact_text_scrubs_http_and_nats_credentials_and_secret_query_values() -> None:
    text = (
        "http=https://alice:p%40ss@example.test/api?profile=prod&access_token=http-secret "
        "nats=nats://service:nats-secret@nats.example.test:4222?name=worker&token=nats-token"
    )

    safe = redact_text(text)

    assert "alice" not in safe
    assert "p%40ss" not in safe
    assert "http-secret" not in safe
    assert "service" not in safe
    assert "nats-secret" not in safe
    assert "nats-token" not in safe
    assert "https://[REDACTED]@example.test/api?profile=prod&access_token=[REDACTED]" in safe
    assert "nats://[REDACTED]@nats.example.test:4222?name=worker&token=[REDACTED]" in safe
    assert "]]" not in safe
    assert redact_text(safe) == safe


def test_redact_text_scrubs_assignments_headers_and_private_keys() -> None:
    diagnostic = (
        'profile=prod password="open sesame" Authorization: Bearer abc.def.ghi\n'
        "Cookie: session=top-secret; theme=dark\n"
        "private_key=-----BEGIN PRIVATE KEY-----\nkey-material\n"
        "-----END PRIVATE KEY-----"
    )

    safe = redact_text(diagnostic)

    assert "profile=prod" in safe
    assert "open sesame" not in safe
    assert "abc.def.ghi" not in safe
    assert "top-secret" not in safe
    assert "key-material" not in safe
    assert safe.count(REDACTED) >= 4


def test_cli_argument_redaction_preserves_tokens_and_covers_sensitive_forms() -> None:
    arguments = [
        "--password",
        "open sesame",
        "--client-secret=attached-secret",
        "-p",
        "-leading-dash-secret",
        "--password",
        "first-secret",
        "--password",
        "second-secret",
        "--profile",
        "production",
    ]

    safe = redact_cli_arguments(arguments)

    assert arguments[1] == "open sesame"
    assert safe == [
        "--password",
        REDACTED,
        f"--client-secret={REDACTED}",
        "-p",
        REDACTED,
        "--password",
        REDACTED,
        "--password",
        REDACTED,
        "--profile",
        "production",
    ]
    rendered = redact_text(
        "unrecognized arguments: --password text-secret "
        "--client-secret=attached-text-secret -p short-text-secret"
    )
    for secret in ("text-secret", "attached-text-secret", "short-text-secret"):
        assert secret not in rendered


def test_cli_argument_redaction_preserves_positionals_after_end_of_options() -> None:
    arguments = ["put", "bucket", "key", "--", "-psecret-file"]

    assert redact_cli_arguments(arguments) == arguments


def test_error_payload_uses_exception_type_and_redacts_exception_message() -> None:
    payload = error_payload(
        "enqueue",
        RuntimeError("connection failed: password=queue-secret"),
        exit_code=70,
        retryable=True,
    )

    assert payload == {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "command": "enqueue",
        "status": "error",
        "error_type": "RuntimeError",
        "error": f"connection failed: password={REDACTED}",
        "exit_code": 70,
        "retryable": True,
    }


def test_error_payload_accepts_string_unknown_command_and_explicit_type() -> None:
    payload = error_payload(None, "bad token: leaked", error_type="ConfigurationError")

    assert payload["command"] is None
    assert payload["status"] == "error"
    assert payload["error_type"] == "ConfigurationError"
    assert payload["error"] == f"bad token: {REDACTED}"
    assert payload["exit_code"] == 1
    assert payload["retryable"] is False


def test_emit_json_writes_one_sorted_redacted_document_without_mutation() -> None:
    stream = io.StringIO()
    payload = {"z": 2, "password": "hidden", "a": {"token": "also-hidden"}}
    snapshot = copy.deepcopy(payload)

    emit_json(payload, stream=stream)

    assert payload == snapshot
    assert stream.getvalue() == (
        '{"a": {"token": "[REDACTED]"}, "password": "[REDACTED]", "z": 2}\n'
    )
    assert json.loads(stream.getvalue()) == {
        "a": {"token": REDACTED},
        "password": REDACTED,
        "z": 2,
    }


def test_emit_json_uses_stdout_only(capsys) -> None:
    emit_json(result_payload("ls", "completed", objects=["a", "b"]))

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert json.loads(captured.out)["objects"] == ["a", "b"]


def test_verbose_reporter_is_gated_redacted_and_stderr_only(capsys) -> None:
    quiet = VerboseReporter(enabled=False)
    quiet("connecting", {"password": "quiet-secret"})
    assert capsys.readouterr() == ("", "")

    verbose = VerboseReporter(enabled=True)
    verbose(
        "connecting to nats://worker:nats-secret@example.test:4222",
        config={"profile": "prod", "access_token": "api-secret"},
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "nats-secret" not in captured.err
    assert "api-secret" not in captured.err
    assert "profile" in captured.err
    assert captured.err.endswith("\n")
