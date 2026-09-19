"""Offline verification of a complete, checkpoint-bound audit export."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict
from typing import Any

from .audit import AuditEvent
from .audit_integrity import (
    AuditCheckpoint,
    AuditIntegrityResult,
    parse_checkpoint,
    tombstone_payload,
    verify_snapshot,
)

_ENTRY_FIELDS = frozenset({
    "sequence", "kind", "event_id", "payload_digest", "previous_hash", "entry_hash",
    "recorded_at", "move_id", "move_sequence",
})
_TOMBSTONE_FIELDS = frozenset({"event_id", "replay_digest", "causation_id", "expires_at"})


def verify_audit_export(
    records: Iterable[Mapping[str, Any]],
    checkpoint: AuditCheckpoint | Mapping[str, Any],
) -> AuditIntegrityResult:
    """Verify all exported pages, in order, against an independently saved head.

    A single partial page cannot pass as a complete archive. Structurally
    malformed records raise ValueError; missing/altered evidence returns an
    invalid result. Trust in the supplied checkpoint belongs to its custodian,
    not to the archive containing it.
    """
    anchor = parse_checkpoint(checkpoint)
    entries: list[dict[str, Any]] = []
    events: dict[str, AuditEvent] = {}
    tombstones: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    try:
        source = iter(records)
    except TypeError as exc:
        raise ValueError("audit export records must be iterable") from exc
    for position, record in enumerate(source):
        # The trusted head also bounds the complete archive's record count.
        # Consume at most one excess item, including from streaming iterators.
        if position >= anchor.sequence:
            issues.append("export_exceeds_checkpoint")
            break
        if not isinstance(record, Mapping) or set(record) != _ENTRY_FIELDS | {"event", "tombstone"}:
            raise ValueError("invalid audit export record fields")
        entry = {name: record[name] for name in _ENTRY_FIELDS}
        if type(entry["sequence"]) is not int or not isinstance(entry["event_id"], str):
            raise ValueError("invalid audit export record identity")
        entries.append(entry)
        identifier = entry["event_id"]
        try:
            if record["event"] is not None:
                event = AuditEvent(**record["event"])
                if asdict(event) != dict(record["event"]):
                    raise ValueError("event payload is not canonical")
                if event.event_id != identifier:
                    issues.append(f"export_event_identity_mismatch:{entry['sequence']}")
                if identifier in events and events[identifier] != event:
                    issues.append(f"export_event_payload_conflict:{entry['sequence']}")
                events[identifier] = event
            if record["tombstone"] is not None:
                if (
                    not isinstance(record["tombstone"], Mapping)
                    or set(record["tombstone"]) != _TOMBSTONE_FIELDS
                ):
                    raise ValueError("invalid tombstone fields")
                tombstone = tombstone_payload(record["tombstone"])
                if tombstone != dict(record["tombstone"]):
                    raise ValueError("tombstone payload is not canonical")
                if tombstone["event_id"] != identifier:
                    issues.append(f"export_tombstone_identity_mismatch:{entry['sequence']}")
                if identifier in tombstones and tombstones[identifier] != tombstone:
                    issues.append(f"export_tombstone_payload_conflict:{entry['sequence']}")
                tombstones[identifier] = tombstone
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid audit export payload") from exc
    try:
        return verify_snapshot(
            anchor.tenant_id, entries, events, tombstones, anchor, anchor,
            initial_issues=issues,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid audit export evidence") from exc
