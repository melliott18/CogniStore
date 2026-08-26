from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, List, Mapping, Optional

from .catalog import Catalog, ObjectRecord, ScanFence
from .move_jobs import (
    MoveJob,
    MoveJobLeaseError,
    MoveJobState,
    MoveJobTransition,
    validate_move_job_transition,
)

_GET_MOVE_JOB_SQL = """
    SELECT idempotency_key, src_tier, dst_tier, bucket, object_key,
           expected_size, source_metadata, state, owner_id, lease_expires_at,
           transferred_size, source_size, source_checksum, destination_size,
           destination_checksum, destination_generation, verification_details, terminal_reason,
           created_at, updated_at
    FROM move_jobs
    WHERE idempotency_key=?
"""
_LIST_MOVE_JOBS_SQL = """
    SELECT idempotency_key, src_tier, dst_tier, bucket, object_key,
           expected_size, source_metadata, state, owner_id, lease_expires_at,
           transferred_size, source_size, source_checksum, destination_size,
           destination_checksum, destination_generation, verification_details, terminal_reason,
           created_at, updated_at
    FROM move_jobs
"""
_SCAN_MOVE_JOBS_SQL = """
    SELECT idempotency_key, src_tier, dst_tier, bucket, object_key,
           expected_size, source_metadata, state, owner_id, lease_expires_at,
           transferred_size, source_size, source_checksum, destination_size,
           destination_checksum, destination_generation, verification_details, terminal_reason,
           created_at, updated_at
    FROM move_jobs
    WHERE bucket=? AND object_key=?
    ORDER BY created_at, updated_at, idempotency_key
"""


class SQLiteCatalog(Catalog):
    """SQLite-backed catalog storing objects and their current placement.

    Schema:
      objects(bucket TEXT, key TEXT, size INTEGER, tier TEXT, metadata TEXT JSON,
              PRIMARY KEY(bucket, key))
    """

    def __init__(self, db_path: str | Path, *, read_only: bool = False) -> None:
        self.db_path = str(db_path)
        self.read_only = read_only
        self._lock = threading.RLock()
        if read_only:
            if self.db_path == ":memory:":
                raise ValueError("An in-memory SQLite catalog cannot be opened read-only")
            uri = f"{Path(self.db_path).expanduser().resolve().as_uri()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._conn.execute("PRAGMA query_only = ON")
            return

        # Worker handlers run blocking storage/catalog operations in a thread
        # so the asyncio health and shutdown loop stays responsive. Access is
        # serialized below because a sqlite connection is not concurrently
        # re-entrant even when cross-thread use is enabled.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS objects (
                bucket TEXT NOT NULL,
                key TEXT NOT NULL,
                size INTEGER NOT NULL,
                tier TEXT NOT NULL,
                metadata TEXT,
                PRIMARY KEY(bucket, key)
            )
            """
        )
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS move_jobs (
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
            CREATE INDEX IF NOT EXISTS move_jobs_state_idx
                ON move_jobs(state, updated_at);
            CREATE INDEX IF NOT EXISTS move_jobs_object_idx
                ON move_jobs(bucket, object_key);
            CREATE TABLE IF NOT EXISTS move_job_transitions (
                idempotency_key TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                from_state TEXT,
                to_state TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(idempotency_key, sequence),
                FOREIGN KEY(idempotency_key) REFERENCES move_jobs(idempotency_key)
            );
            """
        )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(move_jobs)")
            }
            if "destination_generation" not in columns:
                self._conn.execute(
                    "ALTER TABLE move_jobs ADD COLUMN destination_generation TEXT"
                )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def upsert(
        self,
        bucket: str,
        key: str,
        size: int,
        tier: str,
        metadata: Optional[dict] = None,
    ) -> None:
        md = json.dumps(metadata or {})
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO objects(bucket, key, size, tier, metadata)
                VALUES(?,?,?,?,?)
                ON CONFLICT(bucket, key) DO UPDATE SET
                    size=excluded.size,
                    tier=excluded.tier,
                    metadata=excluded.metadata
                """,
                (bucket, key, size, tier, md),
            )
            self._conn.commit()

    def capture_scan_fence(self, bucket: str, key: str) -> ScanFence:
        """Capture the durable move state that precedes a storage read."""

        with self._lock:
            jobs = self._select_scan_move_jobs(bucket, key)
        return ScanFence(
            bucket=bucket,
            key=key,
            move_jobs=self._scan_move_job_fingerprints(jobs),
        )

    def upsert_scan_observation(
        self,
        bucket: str,
        key: str,
        *,
        size: int,
        tier: str,
        generation: str,
        metadata: Optional[dict],
        fence: ScanFence,
    ) -> bool:
        """Atomically fence a scan write against durable move transitions."""

        if (fence.bucket, fence.key) != (bucket, key):
            raise ValueError("scan fence does not identify the observed object")
        if not isinstance(generation, str) or not generation:
            raise ValueError("scan observation requires a non-empty generation")

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                jobs = self._select_scan_move_jobs(bucket, key)
                if (
                    self._scan_move_job_fingerprints(jobs) != fence.move_jobs
                    or not self._scan_observation_is_authoritative(
                        jobs, tier=tier, generation=generation
                    )
                ):
                    self._conn.commit()
                    return False
                self._conn.execute(
                    """
                    INSERT INTO objects(bucket, key, size, tier, metadata)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(bucket, key) DO UPDATE SET
                        size=excluded.size,
                        tier=excluded.tier,
                        metadata=excluded.metadata
                    """,
                    (bucket, key, size, tier, json.dumps(metadata or {})),
                )
                self._conn.commit()
                return True
            except BaseException:
                self._conn.rollback()
                raise

    def get(self, bucket: str, key: str) -> Optional[ObjectRecord]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT bucket, key, size, tier, metadata FROM objects WHERE bucket=? AND key=?",
                (bucket, key),
            )
            row = cur.fetchone()
        if not row:
            return None
        md = json.loads(row[4]) if row[4] else {}
        return ObjectRecord(bucket=row[0], key=row[1], size=row[2], tier=row[3], metadata=md)

    def update_placement(self, bucket: str, key: str, tier: str) -> None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE objects SET tier=? WHERE bucket=? AND key=?",
                (tier, bucket, key),
            )
            if cur.rowcount == 0:
                raise KeyError(f"Object not found: {bucket}/{key}")
            self._conn.commit()

    def upsert_placement(
        self,
        bucket: str,
        key: str,
        *,
        size: int,
        tier: str,
        checksum: Optional[str] = None,
    ) -> None:
        """Commit verified placement data without replacing other metadata."""

        with self._lock:
            row = self._conn.execute(
                "SELECT metadata FROM objects WHERE bucket=? AND key=?",
                (bucket, key),
            ).fetchone()
            metadata = json.loads(row[0]) if row and row[0] else {}
            if checksum is not None:
                metadata["sha256"] = checksum
            self._conn.execute(
                """
                INSERT INTO objects(bucket, key, size, tier, metadata)
                VALUES(?,?,?,?,?)
                ON CONFLICT(bucket, key) DO UPDATE SET
                    size=excluded.size,
                    tier=excluded.tier,
                    metadata=excluded.metadata
                """,
                (bucket, key, size, tier, json.dumps(metadata)),
            )
            self._conn.commit()

    def delete(self, bucket: str, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM objects WHERE bucket=? AND key=?", (bucket, key))
            self._conn.commit()

    def list(self, bucket: str, prefix: str = "") -> List[ObjectRecord]:
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT bucket, key, size, tier, metadata
                FROM objects
                WHERE bucket=?
                  AND substr(CAST(key AS BLOB), 1, length(CAST(? AS BLOB)))
                      = CAST(? AS BLOB)
                """,
                (bucket, prefix, prefix),
            )
            rows = cur.fetchall()
        out: List[ObjectRecord] = []
        for row in rows:
            md = json.loads(row[4]) if row[4] else {}
            out.append(ObjectRecord(bucket=row[0], key=row[1], size=row[2], tier=row[3], metadata=md))
        return out

    def claim_move_job(
        self,
        idempotency_key: str,
        *,
        src_tier: str,
        dst_tier: str,
        bucket: str,
        key: str,
        expected_size: int,
        source_metadata: Mapping[str, Any],
        owner_id: str,
        now: str,
        lease_expires_at: str,
    ) -> MoveJob:
        """Create or atomically claim a durable move job."""

        if not idempotency_key.strip():
            raise ValueError("idempotency_key must be a non-empty string")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    _GET_MOVE_JOB_SQL,
                    (idempotency_key,),
                ).fetchone()
                if row is None:
                    self._conn.execute(
                        """
                        INSERT INTO move_jobs(
                            idempotency_key, src_tier, dst_tier, bucket,
                            object_key, expected_size, source_metadata, state,
                            owner_id, lease_expires_at, created_at, updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            idempotency_key,
                            src_tier,
                            dst_tier,
                            bucket,
                            key,
                            expected_size,
                            json.dumps(dict(source_metadata)),
                            MoveJobState.PREPARED.value,
                            owner_id,
                            lease_expires_at,
                            now,
                            now,
                        ),
                    )
                    self._conn.execute(
                        """
                        INSERT INTO move_job_transitions(
                            idempotency_key, sequence, from_state, to_state,
                            reason, created_at
                        ) VALUES(?,1,NULL,?,?,?)
                        """,
                        (
                            idempotency_key,
                            MoveJobState.PREPARED.value,
                            "move prepared",
                            now,
                        ),
                    )
                else:
                    existing = self._move_job_from_row(row)
                    Catalog._assert_same_move(
                        existing,
                        src_tier=src_tier,
                        dst_tier=dst_tier,
                        bucket=bucket,
                        key=key,
                    )
                    if existing.state.terminal:
                        self._conn.commit()
                        return existing
                    if (
                        existing.owner_id not in (None, owner_id)
                        and existing.lease_expires_at is not None
                        and existing.lease_expires_at > now
                    ):
                        raise MoveJobLeaseError(
                            f"Move job {idempotency_key!r} is leased by "
                            f"{existing.owner_id!r} until "
                            f"{existing.lease_expires_at}"
                        )
                    self._conn.execute(
                        """
                        UPDATE move_jobs
                        SET owner_id=?, lease_expires_at=?, updated_at=?
                        WHERE idempotency_key=?
                        """,
                        (owner_id, lease_expires_at, now, idempotency_key),
                    )
                claimed_row = self._conn.execute(
                    _GET_MOVE_JOB_SQL,
                    (idempotency_key,),
                ).fetchone()
                assert claimed_row is not None
                self._conn.commit()
                return self._move_job_from_row(claimed_row)
            except BaseException:
                self._conn.rollback()
                raise

    def get_move_job(self, idempotency_key: str) -> MoveJob | None:
        with self._lock:
            row = self._conn.execute(
                _GET_MOVE_JOB_SQL,
                (idempotency_key,),
            ).fetchone()
        return None if row is None else self._move_job_from_row(row)

    def list_move_jobs(
        self,
        *,
        states: set[MoveJobState] | None = None,
        idempotency_prefix: str | None = None,
    ) -> List[MoveJob]:
        if states is not None and not states:
            return []

        predicates: list[str] = []
        parameters: list[Any] = []
        if states is not None:
            state_values = sorted(state.value for state in states)
            placeholders = ",".join("?" for _ in state_values)
            predicates.append(f"state IN ({placeholders})")
            parameters.extend(state_values)
        if idempotency_prefix is not None:
            # BLOB prefix comparison preserves Python's literal, case-sensitive
            # ``str.startswith`` semantics, including embedded NUL characters.
            # In particular, %, _, and \\ have no wildcard or escape behavior.
            predicates.append(
                "substr(CAST(idempotency_key AS BLOB), 1, "
                "length(CAST(? AS BLOB))) = CAST(? AS BLOB)"
            )
            parameters.extend((idempotency_prefix, idempotency_prefix))

        query = _LIST_MOVE_JOBS_SQL
        if predicates:
            query += " WHERE " + " AND ".join(predicates)
        query += " ORDER BY created_at, idempotency_key"
        with self._lock:
            rows = self._conn.execute(query, parameters).fetchall()
        return [self._move_job_from_row(row) for row in rows]

    def list_move_job_transitions(
        self, idempotency_key: str
    ) -> List[MoveJobTransition]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT sequence, idempotency_key, from_state, to_state,
                       reason, created_at
                FROM move_job_transitions
                WHERE idempotency_key=?
                ORDER BY sequence
                """,
                (idempotency_key,),
            ).fetchall()
        return [
            MoveJobTransition(
                sequence=row[0],
                idempotency_key=row[1],
                from_state=None if row[2] is None else MoveJobState(row[2]),
                to_state=MoveJobState(row[3]),
                reason=row[4],
                created_at=row[5],
            )
            for row in rows
        ]

    def renew_move_job_lease(
        self,
        idempotency_key: str,
        *,
        owner_id: str,
        expected_state: MoveJobState | None,
        now: str,
        lease_expires_at: str,
    ) -> MoveJob:
        """Atomically extend an owned move lease without changing its phase."""

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    _GET_MOVE_JOB_SQL,
                    (idempotency_key,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Move job not found: {idempotency_key}")
                job = self._move_job_from_row(row)
                if expected_state is None and job.state.terminal:
                    self._conn.commit()
                    return job
                if job.owner_id != owner_id:
                    raise MoveJobLeaseError(
                        f"Move job {idempotency_key!r} is not owned by {owner_id!r}"
                    )
                if expected_state is not None and job.state != expected_state:
                    raise RuntimeError(
                        f"Move job {idempotency_key!r} is {job.state.value}, "
                        f"expected {expected_state.value}"
                    )
                self._conn.execute(
                    """
                    UPDATE move_jobs
                    SET lease_expires_at=?, updated_at=?
                    WHERE idempotency_key=? AND owner_id=?
                    """,
                    (
                        lease_expires_at,
                        now,
                        idempotency_key,
                        owner_id,
                    ),
                )
                renewed_row = self._conn.execute(
                    _GET_MOVE_JOB_SQL,
                    (idempotency_key,),
                ).fetchone()
                assert renewed_row is not None
                self._conn.commit()
                return self._move_job_from_row(renewed_row)
            except BaseException:
                self._conn.rollback()
                raise

    def transition_move_job(
        self,
        idempotency_key: str,
        *,
        owner_id: str,
        expected_state: MoveJobState,
        to_state: MoveJobState,
        reason: str,
        now: str,
        lease_expires_at: str,
        updates: Mapping[str, Any] | None = None,
    ) -> MoveJob:
        validate_move_job_transition(expected_state, to_state)
        allowed_updates = {
            "transferred_size", "source_size", "source_checksum",
            "destination_size", "destination_checksum", "destination_generation",
            "verification_details", "terminal_reason",
        }
        changes = dict(updates or {})
        unknown = changes.keys() - allowed_updates
        if unknown:
            raise ValueError(f"Unsupported move-job updates: {', '.join(sorted(unknown))}")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                job = self._select_owned_move_job(
                    idempotency_key, owner_id, expected_state
                )
                self._conn.execute(
                    """
                    UPDATE move_jobs
                    SET state=?, owner_id=?, lease_expires_at=?, updated_at=?,
                        transferred_size=?, source_size=?, source_checksum=?,
                        destination_size=?, destination_checksum=?, destination_generation=?,
                        verification_details=?, terminal_reason=?
                    WHERE idempotency_key=?
                    """,
                    (
                        to_state.value,
                        None if to_state.terminal else owner_id,
                        None if to_state.terminal else lease_expires_at,
                        now,
                        changes.get("transferred_size", job.transferred_size),
                        changes.get("source_size", job.source_size),
                        changes.get("source_checksum", job.source_checksum),
                        changes.get("destination_size", job.destination_size),
                        changes.get(
                            "destination_checksum", job.destination_checksum
                        ),
                        changes.get(
                            "destination_generation", job.destination_generation
                        ),
                        json.dumps(
                            list(
                                changes.get(
                                    "verification_details",
                                    job.verification_details,
                                )
                            )
                        ),
                        changes.get("terminal_reason", job.terminal_reason),
                        idempotency_key,
                    ),
                )
                self._insert_move_transition(
                    idempotency_key,
                    expected_state,
                    to_state,
                    reason,
                    now,
                )
                row = self._conn.execute(
                    _GET_MOVE_JOB_SQL,
                    (idempotency_key,),
                ).fetchone()
                assert row is not None
                self._conn.commit()
                return self._move_job_from_row(row)
            except BaseException:
                self._conn.rollback()
                raise

    def commit_move_job_placement(
        self,
        idempotency_key: str,
        *,
        owner_id: str,
        size: int,
        tier: str,
        checksum: str,
        now: str,
        lease_expires_at: str,
    ) -> MoveJob:
        """Commit placement and the verified checkpoint in one transaction."""

        validate_move_job_transition(
            MoveJobState.VERIFIED, MoveJobState.COMMITTED
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                job = self._select_owned_move_job(
                    idempotency_key, owner_id, MoveJobState.VERIFIED
                )
                row = self._conn.execute(
                    "SELECT metadata FROM objects WHERE bucket=? AND key=?",
                    (job.bucket, job.key),
                ).fetchone()
                metadata = json.loads(row[0]) if row and row[0] else {}
                metadata["sha256"] = checksum
                self._conn.execute(
                    """
                    INSERT INTO objects(bucket, key, size, tier, metadata)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(bucket, key) DO UPDATE SET
                        size=excluded.size,
                        tier=excluded.tier,
                        metadata=excluded.metadata
                    """,
                    (job.bucket, job.key, size, tier, json.dumps(metadata)),
                )
                self._conn.execute(
                    """
                    UPDATE move_jobs
                    SET state=?, lease_expires_at=?, updated_at=?
                    WHERE idempotency_key=?
                    """,
                    (
                        MoveJobState.COMMITTED.value,
                        lease_expires_at,
                        now,
                        idempotency_key,
                    ),
                )
                self._insert_move_transition(
                    idempotency_key,
                    MoveJobState.VERIFIED,
                    MoveJobState.COMMITTED,
                    "catalog placement committed",
                    now,
                )
                updated_row = self._conn.execute(
                    _GET_MOVE_JOB_SQL,
                    (idempotency_key,),
                ).fetchone()
                assert updated_row is not None
                self._conn.commit()
                return self._move_job_from_row(updated_row)
            except BaseException:
                self._conn.rollback()
                raise

    def _select_owned_move_job(
        self,
        idempotency_key: str,
        owner_id: str,
        expected_state: MoveJobState,
    ) -> MoveJob:
        row = self._conn.execute(
            _GET_MOVE_JOB_SQL,
            (idempotency_key,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Move job not found: {idempotency_key}")
        job = self._move_job_from_row(row)
        if job.owner_id != owner_id:
            raise MoveJobLeaseError(
                f"Move job {idempotency_key!r} is not owned by {owner_id!r}"
            )
        if job.state != expected_state:
            raise RuntimeError(
                f"Move job {idempotency_key!r} is {job.state.value}, "
                f"expected {expected_state.value}"
            )
        return job

    def _select_scan_move_jobs(self, bucket: str, key: str) -> List[MoveJob]:
        rows = self._conn.execute(_SCAN_MOVE_JOBS_SQL, (bucket, key)).fetchall()
        return [self._move_job_from_row(row) for row in rows]

    def _insert_move_transition(
        self,
        idempotency_key: str,
        from_state: MoveJobState,
        to_state: MoveJobState,
        reason: str,
        now: str,
    ) -> None:
        next_sequence = self._conn.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1
            FROM move_job_transitions
            WHERE idempotency_key=?
            """,
            (idempotency_key,),
        ).fetchone()[0]
        self._conn.execute(
            """
            INSERT INTO move_job_transitions(
                idempotency_key, sequence, from_state, to_state, reason, created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                idempotency_key,
                next_sequence,
                from_state.value,
                to_state.value,
                reason,
                now,
            ),
        )

    @staticmethod
    def _move_job_from_row(row: tuple[Any, ...]) -> MoveJob:
        return MoveJob(
            idempotency_key=row[0],
            src_tier=row[1],
            dst_tier=row[2],
            bucket=row[3],
            key=row[4],
            expected_size=row[5],
            source_metadata=json.loads(row[6]),
            state=MoveJobState(row[7]),
            owner_id=row[8],
            lease_expires_at=row[9],
            transferred_size=row[10],
            source_size=row[11],
            source_checksum=row[12],
            destination_size=row[13],
            destination_checksum=row[14],
            destination_generation=row[15],
            verification_details=tuple(json.loads(row[16])),
            terminal_reason=row[17],
            created_at=row[18],
            updated_at=row[19],
        )
