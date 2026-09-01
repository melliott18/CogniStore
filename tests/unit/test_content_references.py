from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cognistore.core.content_identity import cas_key_for_sha256
from cognistore.core.content_references import (
    INVALID_UNREFERENCED_TIMESTAMP,
    ContentReferenceSnapshot,
    build_content_reference_report,
)

_DIGEST = "a" * 64


def _snapshot(*, unreferenced_at: str | None) -> ContentReferenceSnapshot:
    return ContentReferenceSnapshot(
        sha256=_DIGEST,
        cas_key=cas_key_for_sha256(_DIGEST),
        size=1,
        stored_reference_count=0,
        expected_object_reference_count=0,
        expected_chunk_reference_count=0,
        unreferenced_at=unreferenced_at,
    )


def test_reclamation_grace_boundary_is_inclusive() -> None:
    snapshot = _snapshot(unreferenced_at="2026-09-01T00:00:00.000000Z")

    before = build_content_reference_report(
        (snapshot,),
        grace_period_seconds=60,
        now="2026-09-01T00:00:59.999999Z",
    )
    at_boundary = build_content_reference_report(
        (snapshot,),
        grace_period_seconds=60,
        now="2026-09-01T00:01:00.000000Z",
    )

    assert not before.entries[0].reclamation_eligible
    assert at_boundary.entries[0].reclamation_eligible


@pytest.mark.parametrize(
    "grace_period",
    (-1, True, float("nan"), float("inf"), "60"),
)
def test_reconciliation_rejects_invalid_grace_periods(grace_period: object) -> None:
    with pytest.raises(ValueError, match="finite non-negative number"):
        build_content_reference_report(
            (),
            grace_period_seconds=grace_period,  # type: ignore[arg-type]
            now=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )


def test_invalid_persisted_timestamp_is_reported_without_mutation() -> None:
    snapshot = _snapshot(unreferenced_at="not-a-timestamp")

    report = build_content_reference_report(
        (snapshot,),
        grace_period_seconds=0,
        now="2026-09-01T00:00:00.000000Z",
    )

    assert not report.consistent
    assert report.entries[0].unreferenced_at == "not-a-timestamp"
    assert report.entries[0].issues == (INVALID_UNREFERENCED_TIMESTAMP,)
    assert not report.entries[0].reclamation_eligible
