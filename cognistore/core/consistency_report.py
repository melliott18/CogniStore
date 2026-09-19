"""Separate, bounded checkpoint and audit storage for read-only consistency scans."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, TextIO

from cognistore.core.audit import AuditContext, AuditEvent, AuditOutcome, AuditRetentionPolicy
from cognistore.core.audit_integrity import GENESIS_HASH, digest
from cognistore.encryption import require_at_rest
from cognistore.utils.redaction import redact

REPORT_VERSION = 2
APPLICATION_ID = 0x434F4E53


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False)


@contextmanager
def open_report(path: Path, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
    """Open only a recognized, existing report; never initialize arbitrary files."""
    require_at_rest("runtime")
    path = Path(path).resolve(strict=True)
    if not read_only:
        for candidate in (path, *(Path(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm"))):
            if candidate.exists() and (candidate.is_symlink() or candidate.stat().st_nlink > 1):
                raise ValueError("writable report and sidecars must not alias other files")
    connection = sqlite3.connect(path.as_uri() + ("?mode=ro" if read_only else "?mode=rw"),
                                 uri=True, timeout=1)
    try:
        if read_only:
            connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("not a consistency scan report")
        state = read_state(connection)
        if state.get("version") != REPORT_VERSION:
            raise ValueError("unsupported consistency report version; create a new scan report")
        connection.create_function("cognistore_report_audit_append", 0, lambda: 0)
        verify_report_integrity(connection)
        yield connection
    finally:
        connection.close()


def create_report(path: Path, state: dict[str, Any], actor_id: str) -> None:
    """Exclusive creation prevents a report from overwriting any existing data."""
    require_at_rest("runtime")
    for suffix in ("-journal", "-wal", "-shm"):
        if os.path.lexists(str(path) + suffix):
            raise FileExistsError("new report must not reuse existing SQLite sidecars")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(f"""
            PRAGMA application_id={APPLICATION_ID};
            CREATE TABLE checkpoint (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL);
            CREATE TABLE inventory (object_key TEXT PRIMARY KEY COLLATE BINARY);
            CREATE TABLE sightings (
                object_key TEXT NOT NULL, tier TEXT NOT NULL,
                PRIMARY KEY(object_key, tier)
            );
            CREATE TABLE findings (sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE audit (
                sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL,
                previous_hash TEXT NOT NULL, entry_hash TEXT NOT NULL
            );
            CREATE TABLE audit_head (
                id INTEGER PRIMARY KEY CHECK(id=1), sequence INTEGER NOT NULL,
                entry_hash TEXT NOT NULL
            );
            INSERT INTO audit_head VALUES(1, 0, '{GENESIS_HASH}');
            CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
                BEGIN SELECT RAISE(ABORT, 'consistency audit is append-only'); END;
            CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
                BEGIN SELECT RAISE(ABORT, 'consistency audit is append-only'); END;
            CREATE TRIGGER audit_insert_guard BEFORE INSERT ON audit
                WHEN cognistore_report_audit_append() != 1
                    OR NEW.sequence != (SELECT sequence + 1 FROM audit_head WHERE id=1)
                    OR NEW.previous_hash != (SELECT entry_hash FROM audit_head WHERE id=1)
                BEGIN SELECT RAISE(ABORT, 'consistency audit requires authorized append'); END;
            CREATE TRIGGER audit_head_no_delete BEFORE DELETE ON audit_head
                BEGIN SELECT RAISE(ABORT, 'consistency audit head cannot be deleted'); END;
            CREATE TRIGGER audit_head_insert_guard BEFORE INSERT ON audit_head
                BEGIN SELECT RAISE(ABORT, 'consistency audit head already exists'); END;
            CREATE TRIGGER audit_head_update_guard BEFORE UPDATE ON audit_head
                WHEN cognistore_report_audit_append() != 1
                BEGIN SELECT RAISE(ABORT, 'consistency audit head requires authorized append'); END;
        """)
        connection.create_function("cognistore_report_audit_append", 0, lambda: 0)
        with connection:
            connection.execute("INSERT INTO checkpoint VALUES(1, ?)", (encode(state),))
            append_event(connection, state, "consistency.started", actor_id)
    finally:
        connection.close()


def read_state(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute("SELECT state FROM checkpoint WHERE id=1").fetchone()
    if row is None:
        raise ValueError("consistency report checkpoint is missing")
    result = json.loads(row[0])
    if not isinstance(result, dict):
        raise ValueError("invalid consistency report checkpoint")
    return result


def save_state(connection: sqlite3.Connection, state: dict[str, Any]) -> None:
    connection.execute("UPDATE checkpoint SET state=? WHERE id=1", (encode(state),))


def append_event(connection: sqlite3.Connection, state: dict[str, Any],
                 event_type: str, actor_id: str, *, details: Mapping[str, Any] | None = None,
                 outcome: AuditOutcome = AuditOutcome.SUCCEEDED) -> None:
    # Serialize the previous event and durable head reads with publication. A
    # resumed scan and an export may append concurrently through different
    # connections; both must extend the same committed head.
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    previous = connection.execute("SELECT payload FROM audit ORDER BY sequence DESC LIMIT 1").fetchone()
    context = AuditContext(
        correlation_id=state["scan_id"], actor_type="operator", actor_id=actor_id,
        causation_id=json.loads(previous[0])["event_id"] if previous else None,
    )
    event = AuditEvent.create(
        event_type, outcome, context, retention=AuditRetentionPolicy(None),
        details={"tenant_id": state["scope"]["tenant_id"], "scope": state["scope"],
                 "phase": state["phase"], "checked": state["checked"], **(details or {})},
    )
    # This isolated audit log uses the same contract and redaction as operational
    # history, without writing audit or demand observations to the source catalog.
    payload = encode(redact(asdict(event)))
    head = connection.execute("SELECT sequence, entry_hash FROM audit_head WHERE id=1").fetchone()
    if head is None:
        raise ValueError("consistency audit integrity failure: missing head")
    sequence = head[0] + 1
    entry_hash = _report_event_hash(sequence, head[1], payload)
    connection.create_function("cognistore_report_audit_append", 0, lambda: 1)
    try:
        connection.execute(
            "INSERT INTO audit(sequence,payload,previous_hash,entry_hash) VALUES(?,?,?,?)",
            (sequence, payload, head[1], entry_hash),
        )
        connection.execute(
            "UPDATE audit_head SET sequence=?, entry_hash=? WHERE id=1",
            (sequence, entry_hash),
        )
    finally:
        connection.create_function("cognistore_report_audit_append", 0, lambda: 0)


def _report_event_hash(sequence: int, previous_hash: str, payload: str) -> str:
    return digest({
        "schema": "cognistore.consistency.audit.v1", "sequence": sequence,
        "previous_hash": previous_hash, "payload": json.loads(payload),
    })


def verify_report_integrity(
    connection: sqlite3.Connection,
    *,
    expected_checkpoint: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify one stable SQLite snapshot, optionally against a retained checkpoint."""
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        return _verify_report_integrity(connection, expected_checkpoint=expected_checkpoint)
    finally:
        if owns_transaction:
            connection.rollback()


def _verify_report_integrity(
    connection: sqlite3.Connection,
    *,
    expected_checkpoint: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify all retained report events against the durable head and an optional anchor.

    Callers can retain the returned checkpoint outside this report to detect a
    privileged rewrite or rollback of the entire file. This checks audit evidence,
    not the separate findings or scanner checkpoint tables.
    """
    previous_hash = GENESIS_HASH
    sequence = 0
    anchored = expected_checkpoint is None
    state = read_state(connection)
    if expected_checkpoint is not None:
        if (expected_checkpoint.get("scan_id") != state["scan_id"]
                or expected_checkpoint.get("tenant_id") != state["scope"]["tenant_id"]
                or expected_checkpoint.get("algorithm") != "sha256-v1"):
            raise ValueError("consistency audit integrity failure: checkpoint scope mismatch")
        anchored = (expected_checkpoint.get("sequence") == 0
                    and expected_checkpoint.get("entry_hash") == GENESIS_HASH)
    try:
        for number, payload, prior, recorded_hash in connection.execute(
            "SELECT sequence,payload,previous_hash,entry_hash FROM audit ORDER BY sequence"
        ):
            sequence += 1
            if (number != sequence or prior != previous_hash
                    or _report_event_hash(number, prior, payload) != recorded_hash):
                raise ValueError("consistency audit integrity failure: altered or missing event")
            event = json.loads(payload)
            if (event["correlation_id"] != state["scan_id"]
                    or event["details"]["tenant_id"] != state["scope"]["tenant_id"]):
                raise ValueError("consistency audit integrity failure: event scope mismatch")
            previous_hash = recorded_hash
            if expected_checkpoint is not None and number == expected_checkpoint.get("sequence"):
                anchored = recorded_hash == expected_checkpoint.get("entry_hash")
        head = connection.execute("SELECT sequence,entry_hash FROM audit_head WHERE id=1").fetchone()
        if head != (sequence, previous_hash) or sequence == 0 or not anchored:
            raise ValueError("consistency audit integrity failure: head or checkpoint mismatch")
    except (sqlite3.DatabaseError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("consistency audit integrity failure: malformed evidence") from error
    return {
        "algorithm": "sha256-v1", "scan_id": state["scan_id"],
        "tenant_id": state["scope"]["tenant_id"],
        "sequence": sequence, "entry_hash": previous_hash,
    }


def verify_binding(state: dict[str, Any], tenant_id: str, binding_id: str | None) -> None:
    if state["scope"]["tenant_id"] != tenant_id:
        raise ValueError("report belongs to a different tenant")
    if binding_id is not None and state["binding_id"] != binding_id:
        raise ValueError("report source or tenant binding does not match")


def summarize(connection: sqlite3.Connection, state: dict[str, Any]) -> dict[str, Any]:
    reasons: dict[str, int] = {}
    severities: dict[str, int] = {}
    for (payload,) in connection.execute("SELECT payload FROM findings ORDER BY sequence"):
        finding = json.loads(payload)
        reason, severity = finding["reason_code"], finding["severity"]
        reasons[reason] = reasons.get(reason, 0) + 1
        severities[severity] = severities.get(severity, 0) + 1
    return {
        "schema": "cognistore.consistency", "version": REPORT_VERSION,
        "scan_id": state["scan_id"], "scope": state["scope"],
        "phase": state["phase"], "complete": state["phase"] == "completed",
        "checked": state["checked"],
        "inventory_keys": connection.execute("SELECT COUNT(*) FROM inventory").fetchone()[0],
        "finding_count": sum(reasons.values()), "reason_counts": reasons,
        "severity_counts": severities, "read_only": True,
        "started_at": state["started_at"], "finished_at": state.get("finished_at"),
        "consistent": state["phase"] == "completed" and not reasons,
        "audit_checkpoint": verify_report_integrity(connection),
    }


def report_summary(report_path: Path, *, tenant_id: str,
                   binding_id: str | None = None) -> dict[str, Any]:
    with open_report(report_path, read_only=True) as connection:
        state = read_state(connection)
        verify_binding(state, tenant_id, binding_id)
        return summarize(connection, state)


def _exportable_audit_payload(payload: str) -> dict[str, Any]:
    event = json.loads(payload)
    if not isinstance(event, dict) or redact(event) != event:
        raise ValueError("consistency audit export requires redaction reconciliation")
    return event


def export_report(report_path: Path, output: TextIO, *, tenant_id: str,
                  actor_id: str = "cli", binding_id: str | None = None) -> dict[str, Any]:
    """Stream a scoped JSONL export; a failed stream cannot audit success."""
    with open_report(report_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            state = read_state(connection)
            verify_binding(state, tenant_id, binding_id)
            summary = summarize(connection, state)
            append_event(connection, state, "consistency.exported", actor_id)
            summary["audit_checkpoint"] = verify_report_integrity(connection)
            exported_summary = redact({"type": "report", **summary})
            if exported_summary["audit_checkpoint"] != summary["audit_checkpoint"]:
                raise ValueError("consistency audit export requires redaction reconciliation")
            # A secret provider may recognize new values after an event was
            # recorded. Do not disclose them or rewrite already-hashed evidence
            # while claiming that the original proof still verifies.
            for (payload,) in connection.execute("SELECT payload FROM audit ORDER BY sequence"):
                _exportable_audit_payload(payload)
            output.write(encode(exported_summary) + "\n")
            for (payload,) in connection.execute("SELECT payload FROM findings ORDER BY sequence"):
                output.write(encode(redact({"type": "finding", **json.loads(payload)})) + "\n")
            for sequence, payload, previous_hash, entry_hash in connection.execute(
                "SELECT sequence,payload,previous_hash,entry_hash FROM audit ORDER BY sequence"
            ):
                output.write(encode({
                    "type": "audit", **_exportable_audit_payload(payload),
                    "integrity": {"sequence": sequence, "previous_hash": previous_hash,
                                  "entry_hash": entry_hash},
                }) + "\n")
            output.flush()
            connection.commit()
            return summary
        except BaseException:
            connection.rollback()
            raise
