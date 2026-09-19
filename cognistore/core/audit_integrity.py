"""Versioned audit evidence and externally retainable integrity checkpoints.

A checkpoint must be retained outside the database to detect a privileged
attacker replacing both history and its local head. Baselines certify only the
bytes present when integrity tracking was installed, not their earlier origin.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .audit import AuditEvent

ALGORITHM = "sha256-v1"
GENESIS_HASH = "0" * 64


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def event_digest(event: AuditEvent) -> str:
    return digest(asdict(event))


def tombstone_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(value["event_id"]),
        "replay_digest": value["replay_digest"],
        "causation_id": None if value["causation_id"] is None else str(value["causation_id"]),
        "expires_at": value["expires_at"],
    }


@dataclass(frozen=True)
class AuditCheckpoint:
    tenant_id: str
    sequence: int
    entry_hash: str
    algorithm: str = ALGORITHM

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, str) or not self.tenant_id:
            raise ValueError("checkpoint tenant_id must be a non-empty string")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("checkpoint sequence must be a nonnegative integer")
        if (
            not isinstance(self.entry_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.entry_hash) is None
        ):
            raise ValueError("checkpoint entry_hash must be lowercase SHA-256 hex")
        if self.algorithm != ALGORITHM:
            raise ValueError("unsupported audit integrity algorithm")
        if self.sequence == 0 and self.entry_hash != GENESIS_HASH:
            raise ValueError("empty checkpoint must contain the genesis hash")


@dataclass(frozen=True)
class AuditIntegrityResult:
    valid: bool
    tenant_id: str
    checked_entries: int
    checked_events: int
    pruned_events: int
    checkpoint: AuditCheckpoint
    anchored: bool
    issues: tuple[str, ...]


def parse_checkpoint(value: AuditCheckpoint | Mapping[str, Any]) -> AuditCheckpoint:
    if isinstance(value, AuditCheckpoint):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("checkpoint must be an audit checkpoint object")
    try:
        return AuditCheckpoint(**dict(value))
    except TypeError as exc:
        raise ValueError("invalid checkpoint fields") from exc


def make_entry(
    tenant_id: str,
    sequence: int,
    previous_hash: str,
    *,
    kind: str,
    event_id: str,
    payload_digest: str,
    recorded_at: str,
    move_id: str | None = None,
    move_sequence: int | None = None,
) -> dict[str, Any]:
    entry = {
        "sequence": sequence,
        "kind": kind,
        "event_id": event_id,
        "payload_digest": payload_digest,
        "previous_hash": previous_hash,
        "recorded_at": recorded_at,
        "move_id": move_id,
        "move_sequence": move_sequence,
    }
    entry["entry_hash"] = digest({"algorithm": ALGORITHM, "tenant_id": tenant_id, **entry})
    return entry


def verify_snapshot(
    tenant_id: str,
    entries: Sequence[Mapping[str, Any]],
    events: Mapping[str, AuditEvent],
    tombstones: Mapping[str, Mapping[str, Any]],
    head: AuditCheckpoint,
    checkpoint: AuditCheckpoint | Mapping[str, Any] | None = None,
    *,
    initial_issues: Sequence[str] = (),
) -> AuditIntegrityResult:
    anchor = None if checkpoint is None else parse_checkpoint(checkpoint)
    issues = list(initial_issues)
    if anchor is not None and anchor.tenant_id != tenant_id:
        raise ValueError("checkpoint belongs to another tenant")
    previous = GENESIS_HASH
    expected = 1
    births: dict[str, Mapping[str, Any]] = {}
    removals: dict[str, Mapping[str, Any]] = {}
    hashes = {0: GENESIS_HASH}
    for position, item in enumerate(entries, start=1):
        seq = item.get("sequence")
        text_fields = (
            "kind",
            "event_id",
            "payload_digest",
            "previous_hash",
            "entry_hash",
            "recorded_at",
        )
        if (
            type(seq) is not int
            or seq < 1
            or any(not isinstance(item.get(field), str) for field in text_fields)
        ):
            issues.append(f"invalid_entry:{position}")
            continue
        if seq != expected:
            issues.append(f"sequence_gap:{expected}")
        if item["previous_hash"] != previous:
            issues.append(f"previous_hash_mismatch:{seq}")
        recomputed = make_entry(
            tenant_id,
            seq,
            item["previous_hash"],
            kind=item["kind"],
            event_id=item["event_id"],
            payload_digest=item["payload_digest"],
            recorded_at=item["recorded_at"],
            move_id=item["move_id"],
            move_sequence=item["move_sequence"],
        )
        if item["entry_hash"] != recomputed["entry_hash"]:
            issues.append(f"entry_hash_mismatch:{seq}")
        key = item["event_id"]
        if item["kind"] in ("event", "baseline_event"):
            if key in births or key in removals:
                issues.append(f"duplicate_event:{seq}")
            births[key] = item
        elif item["kind"] in ("retention", "baseline_tombstone"):
            if key in removals or (item["kind"] == "retention" and key not in births):
                issues.append(f"invalid_retention:{seq}")
            removals[key] = item
        elif item["kind"] == "baseline_move_head":
            payload = {
                "move_id": item["move_id"],
                "last_sequence": item["move_sequence"],
                "last_event_id": key,
            }
            if digest(payload) != item["payload_digest"]:
                issues.append(f"baseline_move_head_mismatch:{seq}")
        else:
            issues.append(f"unknown_entry_kind:{seq}")
        previous, expected = item["entry_hash"], seq + 1
        hashes[seq] = previous
    last_sequence = entries[-1]["sequence"] if entries else 0
    if head.sequence != last_sequence or head.entry_hash != previous:
        issues.append("head_mismatch")
    if anchor is not None and hashes.get(anchor.sequence) != anchor.entry_hash:
        issues.append("checkpoint_mismatch")
    for key, item in births.items():
        event = events.get(key)
        if key in removals:
            if event is not None:
                issues.append(f"retained_event_reappeared:{item['sequence']}")
        elif event is None:
            issues.append(f"missing_event:{item['sequence']}")
        elif event_digest(event) != item["payload_digest"]:
            issues.append(f"event_digest_mismatch:{item['sequence']}")
    for key, item in removals.items():
        tombstone = tombstones.get(key)
        if tombstone is None:
            issues.append(f"missing_tombstone:{item['sequence']}")
        elif digest(tombstone_payload(tombstone)) != item["payload_digest"]:
            issues.append(f"tombstone_digest_mismatch:{item['sequence']}")
    if set(events) - set(births):
        issues.append("untracked_events")
    if set(tombstones) - set(removals):
        issues.append("untracked_tombstones")
    return AuditIntegrityResult(
        not issues,
        tenant_id,
        len(entries),
        len(events),
        len(tombstones),
        head,
        anchor is not None,
        tuple(issues),
    )


def export_snapshot(
    tenant_id: str,
    entries: Sequence[Mapping[str, Any]],
    events: Mapping[str, AuditEvent],
    tombstones: Mapping[str, Mapping[str, Any]],
    head: AuditCheckpoint,
    *,
    after_sequence: int = 0,
    limit: int = 1000,
    checkpoint: AuditCheckpoint | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if type(after_sequence) is not int or after_sequence < 0:
        raise ValueError("after_sequence must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    anchor = head if checkpoint is None else parse_checkpoint(checkpoint)
    if anchor.tenant_id != tenant_id:
        raise ValueError("checkpoint belongs to another tenant")
    if anchor.sequence > head.sequence or after_sequence > anchor.sequence:
        raise ValueError("export bounds exceed the current audit head")
    hashes = {0: GENESIS_HASH, **{entry["sequence"]: entry["entry_hash"] for entry in entries}}
    if hashes.get(anchor.sequence) != anchor.entry_hash:
        raise ValueError("export checkpoint no longer matches audit history")
    births = {
        item["event_id"]: item["sequence"]
        for item in entries
        if item["kind"] in ("event", "baseline_event")
    }
    if any(
        item["kind"] == "retention"
        and item["sequence"] > anchor.sequence
        and births.get(item["event_id"], anchor.sequence + 1) <= anchor.sequence
        for item in entries
    ):
        raise ValueError("audit retention changed the export snapshot; restart the export")
    records = []
    for item in entries:
        if after_sequence < item["sequence"] <= anchor.sequence:
            event = events.get(item["event_id"])
            tombstone = tombstones.get(item["event_id"])
            records.append(
                {
                    **item,
                    "event": None if event is None else asdict(event),
                    "tombstone": None if tombstone is None else tombstone_payload(tombstone),
                }
            )
            if len(records) == limit:
                break
    next_sequence = records[-1]["sequence"] if records else after_sequence
    return {
        "records": records,
        "checkpoint": asdict(anchor),
        "next_sequence": next_sequence,
        "complete": next_sequence == anchor.sequence,
    }


def move_state_issues(
    entries: Sequence[Mapping[str, Any]],
    events: Mapping[str, AuditEvent],
    heads: Mapping[str, tuple[int, str]],
    sequences: Mapping[str, int | None] | None = None,
) -> list[str]:
    """Bind mutable causal bookkeeping to the immutable, tenant-local ledger."""
    expected: dict[str, tuple[int, str]] = {}
    issues = []
    for item in entries:
        kind = item["kind"]
        if kind not in ("event", "baseline_event", "baseline_move_head"):
            continue
        key, move, sequence = item["event_id"], item["move_id"], item["move_sequence"]
        event = events.get(key)
        if kind != "baseline_move_head" and event is not None:
            if event.move_id != move or sequences is not None and sequences.get(key) != sequence:
                issues.append(f"move_metadata_mismatch:{item['sequence']}")
        if move is None:
            if sequence is not None:
                issues.append(f"invalid_move_sequence:{item['sequence']}")
            continue
        if type(sequence) is not int or sequence < 1:
            issues.append(f"invalid_move_sequence:{item['sequence']}")
            continue
        previous = expected.get(move)
        if kind == "event":
            if sequence != (1 if previous is None else previous[0] + 1):
                issues.append(f"move_sequence_gap:{item['sequence']}")
            if previous is not None and event is not None and event.causation_id != previous[1]:
                issues.append(f"move_causation_mismatch:{item['sequence']}")
        if previous is None or sequence >= previous[0]:
            expected[move] = (sequence, key)
    if dict(heads) != expected:
        issues.append("move_head_mismatch")
    return issues
