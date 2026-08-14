from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from cognistore.jobs.models import JobEnvelope, JobEnvelopeError


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
