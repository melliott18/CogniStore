"""Read-only reconciliation contracts for shared content-addressed blobs."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_RECLAMATION_GRACE_PERIOD_SECONDS = 7 * 24 * 60 * 60

REFERENCE_COUNT_MISMATCH = "reference_count_mismatch"
REFERENCED_BLOB_MARKED_UNREFERENCED = "referenced_blob_marked_unreferenced"
UNREFERENCED_BLOB_MISSING_TIMESTAMP = "unreferenced_blob_missing_timestamp"
INVALID_UNREFERENCED_TIMESTAMP = "invalid_unreferenced_timestamp"

_LOWERCASE_HEX = frozenset("0123456789abcdef")


def _nonnegative_integer(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > 2**63 - 1
    ):
        raise ValueError(
            f"{field} must be a non-negative integer no greater than {2**63 - 1}"
        )
    return value


def _canonical_timestamp(value: str | datetime, field: str) -> tuple[str, datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    else:  # pragma: no cover - narrowed by the public signatures
        raise ValueError(f"{field} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    normalized = parsed.astimezone(timezone.utc)
    canonical = normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return canonical, normalized


def _grace_period_seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("grace_period_seconds must be a finite non-negative number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError("grace_period_seconds must be a finite non-negative number")
    try:
        timedelta(seconds=normalized)
    except OverflowError as exc:
        raise ValueError("grace_period_seconds exceeds the supported timestamp range") from exc
    return normalized


@dataclass(frozen=True)
class ContentReferenceSnapshot:
    """Raw persisted and topology-derived state for one canonical digest."""

    sha256: str
    cas_key: str
    size: int
    stored_reference_count: int
    expected_object_reference_count: int
    expected_chunk_reference_count: int
    unreferenced_at: str | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(character not in _LOWERCASE_HEX for character in self.sha256)
        ):
            raise ValueError("sha256 must be a lowercase 64-character SHA-256 digest")
        if not isinstance(self.cas_key, str) or not self.cas_key:
            raise ValueError("cas_key must be a non-empty string")
        _nonnegative_integer(self.size, "size")
        _nonnegative_integer(self.stored_reference_count, "stored_reference_count")
        _nonnegative_integer(
            self.expected_object_reference_count,
            "expected_object_reference_count",
        )
        _nonnegative_integer(
            self.expected_chunk_reference_count,
            "expected_chunk_reference_count",
        )
        expected = (
            self.expected_object_reference_count + self.expected_chunk_reference_count
        )
        if expected > 2**63 - 1:
            raise ValueError(
                "expected reference count must be no greater than "
                f"{2**63 - 1}"
            )
        if self.unreferenced_at is not None and not isinstance(
            self.unreferenced_at, str
        ):
            raise ValueError("unreferenced_at must be an ISO-8601 timestamp or null")


@dataclass(frozen=True)
class ContentReferenceEntry:
    sha256: str
    cas_key: str
    size: int
    stored_reference_count: int
    expected_object_reference_count: int
    expected_chunk_reference_count: int
    expected_reference_count: int
    unreferenced_at: str | None
    reclamation_eligible: bool
    issues: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a fresh JSON-safe CLI representation."""

        return {
            "sha256": self.sha256,
            "cas_key": self.cas_key,
            "size": self.size,
            "stored_reference_count": self.stored_reference_count,
            "expected_object_reference_count": self.expected_object_reference_count,
            "expected_chunk_reference_count": self.expected_chunk_reference_count,
            "expected_reference_count": self.expected_reference_count,
            "unreferenced_at": self.unreferenced_at,
            "reclamation_eligible": self.reclamation_eligible,
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class ContentReferenceReport:
    generated_at: str
    grace_period_seconds: float
    consistent: bool
    total_blobs: int
    referenced_blobs: int
    unreferenced_blobs: int
    eligible_blobs: int
    entries: tuple[ContentReferenceEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a fresh JSON-safe CLI representation."""

        return {
            "generated_at": self.generated_at,
            "grace_period_seconds": self.grace_period_seconds,
            "consistent": self.consistent,
            "total_blobs": self.total_blobs,
            "referenced_blobs": self.referenced_blobs,
            "unreferenced_blobs": self.unreferenced_blobs,
            "eligible_blobs": self.eligible_blobs,
            "entries": [entry.to_dict() for entry in self.entries],
        }


def build_content_reference_report(
    snapshots: Iterable[ContentReferenceSnapshot],
    *,
    grace_period_seconds: float,
    now: str | datetime | None = None,
) -> ContentReferenceReport:
    """Evaluate raw reference snapshots without changing catalog state."""

    grace = _grace_period_seconds(grace_period_seconds)
    generated_at, generated_datetime = _canonical_timestamp(
        datetime.now(timezone.utc) if now is None else now,
        "now",
    )
    try:
        cutoff = generated_datetime - timedelta(seconds=grace)
    except OverflowError as exc:
        raise ValueError("grace_period_seconds exceeds the supported timestamp range") from exc
    entries: list[ContentReferenceEntry] = []
    seen: set[str] = set()

    for snapshot in snapshots:
        if not isinstance(snapshot, ContentReferenceSnapshot):
            raise ValueError("snapshots must contain only ContentReferenceSnapshot values")
        if snapshot.sha256 in seen:
            raise ValueError(f"duplicate content reference snapshot for sha256 {snapshot.sha256}")
        seen.add(snapshot.sha256)
        expected = (
            snapshot.expected_object_reference_count
            + snapshot.expected_chunk_reference_count
        )
        issues: list[str] = []
        if snapshot.stored_reference_count != expected:
            issues.append(REFERENCE_COUNT_MISMATCH)
        if expected > 0 and snapshot.unreferenced_at is not None:
            issues.append(REFERENCED_BLOB_MARKED_UNREFERENCED)
        elif expected == 0 and snapshot.unreferenced_at is None:
            issues.append(UNREFERENCED_BLOB_MISSING_TIMESTAMP)

        old_enough = False
        if snapshot.unreferenced_at is not None:
            try:
                _, unreferenced_datetime = _canonical_timestamp(
                    snapshot.unreferenced_at,
                    "unreferenced_at",
                )
            except ValueError:
                issues.append(INVALID_UNREFERENCED_TIMESTAMP)
            else:
                old_enough = unreferenced_datetime <= cutoff
        eligible = (
            snapshot.stored_reference_count == 0
            and expected == 0
            and not issues
            and old_enough
        )
        entries.append(
            ContentReferenceEntry(
                sha256=snapshot.sha256,
                cas_key=snapshot.cas_key,
                size=snapshot.size,
                stored_reference_count=snapshot.stored_reference_count,
                expected_object_reference_count=(
                    snapshot.expected_object_reference_count
                ),
                expected_chunk_reference_count=(
                    snapshot.expected_chunk_reference_count
                ),
                expected_reference_count=expected,
                unreferenced_at=snapshot.unreferenced_at,
                reclamation_eligible=eligible,
                issues=tuple(issues),
            )
        )

    entries.sort(key=lambda entry: entry.sha256)
    referenced_blobs = sum(entry.expected_reference_count > 0 for entry in entries)
    unreferenced_blobs = len(entries) - referenced_blobs
    eligible_blobs = sum(entry.reclamation_eligible for entry in entries)
    return ContentReferenceReport(
        generated_at=generated_at,
        grace_period_seconds=grace,
        consistent=all(not entry.issues for entry in entries),
        total_blobs=len(entries),
        referenced_blobs=referenced_blobs,
        unreferenced_blobs=unreferenced_blobs,
        eligible_blobs=eligible_blobs,
        entries=tuple(entries),
    )


__all__ = [
    "DEFAULT_RECLAMATION_GRACE_PERIOD_SECONDS",
    "INVALID_UNREFERENCED_TIMESTAMP",
    "REFERENCE_COUNT_MISMATCH",
    "REFERENCED_BLOB_MARKED_UNREFERENCED",
    "UNREFERENCED_BLOB_MISSING_TIMESTAMP",
    "ContentReferenceEntry",
    "ContentReferenceReport",
    "ContentReferenceSnapshot",
    "build_content_reference_report",
]
