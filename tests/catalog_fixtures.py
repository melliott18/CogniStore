"""Shared catalog fixtures that predate the migration-managed SQL DAL."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path


def create_prototype_sqlite_catalog(path: Path) -> None:
    """Create the exact catalog layout written by the prototype SQLite DAL."""

    with contextlib.closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE objects (
                bucket TEXT NOT NULL,
                key TEXT NOT NULL,
                size INTEGER NOT NULL,
                tier TEXT NOT NULL,
                metadata TEXT,
                PRIMARY KEY(bucket, key)
            );
            CREATE TABLE move_jobs (
                idempotency_key TEXT PRIMARY KEY,
                src_tier TEXT NOT NULL,
                dst_tier TEXT NOT NULL,
                bucket TEXT NOT NULL,
                object_key TEXT NOT NULL,
                expected_size INTEGER NOT NULL,
                source_metadata TEXT NOT NULL,
                state TEXT NOT NULL,
                owner_id TEXT,
                lease_expires_at TEXT,
                transferred_size INTEGER,
                source_size INTEGER,
                source_checksum TEXT,
                destination_size INTEGER,
                destination_checksum TEXT,
                destination_generation TEXT,
                verification_details TEXT NOT NULL DEFAULT '[]',
                terminal_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX move_jobs_state_idx
                ON move_jobs(state, updated_at);
            CREATE INDEX move_jobs_object_idx
                ON move_jobs(bucket, object_key);
            CREATE TABLE move_job_transitions (
                idempotency_key TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                from_state TEXT,
                to_state TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(idempotency_key, sequence),
                FOREIGN KEY(idempotency_key) REFERENCES move_jobs(idempotency_key)
            );
            CREATE TABLE scheduled_runs (
                job_id TEXT PRIMARY KEY,
                state TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO objects(bucket, key, size, tier, metadata) VALUES(?,?,?,?,?)",
            (
                "bucket",
                "reports/annual.pdf",
                41,
                "hot",
                json.dumps({"labels": ["finance"], "generation": "source:v1"}),
            ),
        )
        connection.execute(
            """
            INSERT INTO move_jobs(
                idempotency_key, src_tier, dst_tier, bucket, object_key,
                expected_size, source_metadata, state, owner_id,
                lease_expires_at, transferred_size, source_size,
                source_checksum, destination_size, destination_checksum,
                destination_generation, verification_details, terminal_reason,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "move:annual-report",
                "hot",
                "warm",
                "bucket",
                "reports/annual.pdf",
                41,
                json.dumps({"generation": "source:v1"}),
                "failed",
                None,
                None,
                41,
                41,
                "abc123",
                41,
                "abc123",
                "destination:v1",
                json.dumps(["size matched", "checksum matched"]),
                "source cleanup was fenced",
                "2026-08-01T00:00:00.000000Z",
                "2026-08-01T00:00:01.000000Z",
            ),
        )
        connection.executemany(
            """
            INSERT INTO move_job_transitions(
                idempotency_key, sequence, from_state, to_state, reason, created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            [
                (
                    "move:annual-report",
                    1,
                    None,
                    "prepared",
                    "move prepared",
                    "2026-08-01T00:00:00.000000Z",
                ),
                (
                    "move:annual-report",
                    2,
                    "prepared",
                    "failed",
                    "source cleanup was fenced",
                    "2026-08-01T00:00:01.000000Z",
                ),
            ],
        )
        connection.execute(
            "INSERT INTO scheduled_runs(job_id, state) VALUES(?, ?)",
            ("scheduled:one", "reserved"),
        )
        connection.commit()
