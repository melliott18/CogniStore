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
from cognistore.encryption import require_at_rest
from cognistore.utils.redaction import redact

REPORT_VERSION = 1
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
            raise ValueError("unsupported consistency report version")
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
            CREATE TABLE audit (sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL);
        """)
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
    connection.execute("INSERT INTO audit(payload) VALUES(?)", (encode(redact(asdict(event))),))


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
    }


def report_summary(report_path: Path, *, tenant_id: str,
                   binding_id: str | None = None) -> dict[str, Any]:
    with open_report(report_path, read_only=True) as connection:
        state = read_state(connection)
        verify_binding(state, tenant_id, binding_id)
        return summarize(connection, state)


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
            output.write(encode(redact({"type": "report", **summary})) + "\n")
            for statement, kind in (
                ("SELECT payload FROM findings ORDER BY sequence", "finding"),
                ("SELECT payload FROM audit ORDER BY sequence", "audit"),
            ):
                for (payload,) in connection.execute(statement):
                    output.write(encode(redact({"type": kind, **json.loads(payload)})) + "\n")
            output.flush()
            connection.commit()
            return summary
        except BaseException:
            connection.rollback()
            raise
