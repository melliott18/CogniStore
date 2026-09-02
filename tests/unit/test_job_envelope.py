from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from cognistore.jobs.models import (
    ATTEMPT_OFFSET_METADATA,
    DEAD_LETTER_CHAIN_METADATA,
    JOB_SCHEMA_VERSION_V1,
    JOB_SCHEMA_VERSION_V2,
    REDRIVE_COUNT_METADATA,
    DeadLetterDisposition,
    DeadLetterRecord,
    JobEnvelope,
    JobEnvelopeError,
)


@pytest.mark.parametrize(
    "schema_version", (JOB_SCHEMA_VERSION_V1, JOB_SCHEMA_VERSION_V2)
)
def test_job_envelope_round_trips_supported_schema_versions(
    schema_version: int,
) -> None:
    envelope = JobEnvelope.create(
        "test.echo",
        {"message": "hello"},
        schema_version=schema_version,
    )

    restored = JobEnvelope.from_bytes(envelope.to_bytes())

    assert restored == envelope
    assert restored.schema_version == schema_version


def test_job_envelope_round_trip_preserves_correlation_metadata() -> None:
    job_id = str(uuid4())
    envelope = JobEnvelope.create(
        "catalog.scan",
        {"tier": "hot", "bucket": "documents", "prefix": "reports/"},
        job_id=job_id,
        correlation_id="request-42",
        metadata={"traceparent": "00-abc-def-01"},
        created_at=datetime(2026, 8, 13, 12, 30, tzinfo=timezone.utc),
    )

    restored = JobEnvelope.from_bytes(envelope.to_bytes())

    assert restored == envelope
    assert restored.job_id == job_id
    assert restored.correlation_id == "request-42"
    assert restored.metadata == {"traceparent": "00-abc-def-01"}
    assert restored.created_at == "2026-08-13T12:30:00Z"


def test_job_id_is_the_default_correlation_id() -> None:
    envelope = JobEnvelope.create("test.echo", {"message": "hello"})

    assert envelope.correlation_id == envelope.job_id


@pytest.mark.parametrize(
    "mutation, message",
    [
        ({"schema_version": 99}, "unsupported schema_version"),
        ({"job_id": "not-a-uuid"}, "job_id must be a UUID"),
        ({"payload": ["not", "an", "object"]}, "payload must be a JSON object"),
        ({"created_at": "2026-08-13"}, "must include a timezone"),
        ({"job_type": "test.echo\r\nNats-Msg-Id: forged"}, "must not contain CR or LF"),
        ({"correlation_id": "trace\nforged"}, "must not contain CR or LF"),
        ({"job_type": "\ud800"}, "must be valid UTF-8"),
        ({"correlation_id": "\udfff"}, "must be valid UTF-8"),
    ],
)
def test_invalid_job_envelopes_are_rejected(mutation: dict, message: str) -> None:
    value = json.loads(JobEnvelope.create("test.echo", {}).to_bytes())
    value.update(mutation)

    with pytest.raises(JobEnvelopeError, match=message):
        JobEnvelope.from_bytes(json.dumps(value).encode())


def test_unknown_wire_fields_are_rejected() -> None:
    value = json.loads(JobEnvelope.create("test.echo", {}).to_bytes())
    value["credentials"] = "must-not-cross-worker-boundary"

    with pytest.raises(JobEnvelopeError, match="unknown fields: credentials"):
        JobEnvelope.from_bytes(json.dumps(value).encode())


def test_excessively_nested_payload_is_reported_as_a_malformed_envelope() -> None:
    # The stdlib encoder has the same recursion guard, so form the otherwise
    # valid JSON wire payload directly.
    raw = JobEnvelope.create("test.echo", {}).to_bytes().replace(
        b'"payload":{}',
        b'"payload":{"nested":' + (b"[" * 1_100) + (b"]" * 1_100) + b"}",
    )

    with pytest.raises(JobEnvelopeError, match="UTF-8 JSON|nesting is too deep"):
        JobEnvelope.from_bytes(raw)


def test_oversized_json_integer_is_reported_as_a_malformed_envelope() -> None:
    raw = JobEnvelope.create("test.echo", {}).to_bytes().replace(
        b'"payload":{}', b'"payload":{"value":' + (b"9" * 5_000) + b"}"
    )

    with pytest.raises(JobEnvelopeError, match="UTF-8 JSON"):
        JobEnvelope.from_bytes(raw)


def test_dead_letter_round_trip_preserves_malformed_bytes_and_diagnostics() -> None:
    record = DeadLetterRecord.create(
        failed_at=datetime(2026, 8, 17, 9, 30, tzinfo=timezone.utc),
        disposition=DeadLetterDisposition.TERMINAL,
        retryable=False,
        category="invalid",
        classification_reason="malformed envelope",
        attempt=1,
        max_attempts=7,
        cumulative_attempt=1,
        source_stream="COGNISTORE_JOBS",
        source_published_at=datetime(2026, 8, 17, 9, tzinfo=timezone.utc),
        source_consumer="cognistore-workers",
        stream_sequence=19,
        consumer_sequence=4,
        exception_type="cognistore.jobs.models.JobEnvelopeError",
        exception_message="job envelope must be UTF-8 JSON",
        traceback="traceback with parser context",
        raw_data=b"\xffnot-json\x00",
        headers={"CogniStore-Correlation-Id": "request-42"},
        job=None,
    )

    restored = DeadLetterRecord.from_bytes(record.to_bytes())

    assert restored == record
    assert restored.raw_data == b"\xffnot-json\x00"
    assert restored.audit_chain == (record.dead_letter_id,)
    assert restored.job is None


def test_redrive_preserves_logical_identity_and_extends_audit_chain() -> None:
    original = JobEnvelope.create(
        "policy.run",
        {"bucket": "documents"},
        correlation_id="request-42",
        metadata={"traceparent": "00-abc-def-01"},
        created_at=datetime(2026, 8, 17, 9, tzinfo=timezone.utc),
    )
    first = DeadLetterRecord.create(
        failed_at=datetime(2026, 8, 17, 9, 1, tzinfo=timezone.utc),
        disposition=DeadLetterDisposition.EXHAUSTED,
        retryable=True,
        category="timeout",
        classification_reason="operation timed out",
        attempt=3,
        max_attempts=3,
        cumulative_attempt=3,
        source_stream="COGNISTORE_JOBS",
        source_published_at=datetime(2026, 8, 17, 9, tzinfo=timezone.utc),
        source_consumer="cognistore-workers",
        stream_sequence=20,
        consumer_sequence=5,
        exception_type="builtins.TimeoutError",
        exception_message="timed out",
        traceback="timeout traceback",
        raw_data=original.to_bytes(),
        headers={},
        job=original,
    )

    redriven = first.job_for_redrive()

    assert redriven.job_id == original.job_id
    assert redriven.correlation_id == original.correlation_id
    assert redriven.created_at == original.created_at
    assert redriven.payload == original.payload
    assert redriven.metadata["traceparent"] == "00-abc-def-01"
    assert redriven.metadata[ATTEMPT_OFFSET_METADATA] == "3"
    assert redriven.metadata[REDRIVE_COUNT_METADATA] == "1"
    assert json.loads(redriven.metadata[DEAD_LETTER_CHAIN_METADATA]) == [
        first.dead_letter_id
    ]

    second = DeadLetterRecord.create(
        failed_at=datetime(2026, 8, 17, 9, 2, tzinfo=timezone.utc),
        disposition=DeadLetterDisposition.TERMINAL,
        retryable=False,
        category="invalid",
        classification_reason="invalid request",
        attempt=1,
        max_attempts=3,
        cumulative_attempt=4,
        source_stream="COGNISTORE_JOBS",
        source_published_at=datetime(2026, 8, 17, 9, 30, tzinfo=timezone.utc),
        source_consumer="cognistore-workers",
        stream_sequence=21,
        consumer_sequence=6,
        exception_type="builtins.ValueError",
        exception_message="invalid after redrive",
        traceback="value traceback",
        raw_data=redriven.to_bytes(),
        headers={},
        job=redriven,
    )
    assert second.audit_chain == (first.dead_letter_id, second.dead_letter_id)
    assert second.redrive_count == 1
