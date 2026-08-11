from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from .catalog import ObjectRecord


class SQLiteCatalog:
    """SQLite-backed catalog storing objects and their current placement.

    Schema:
      objects(bucket TEXT, key TEXT, size INTEGER, tier TEXT, metadata TEXT JSON,
              PRIMARY KEY(bucket, key))
    """

    def __init__(self, db_path: str | Path, *, read_only: bool = False) -> None:
        self.db_path = str(db_path)
        self.read_only = read_only
        if read_only:
            if self.db_path == ":memory:":
                raise ValueError("An in-memory SQLite catalog cannot be opened read-only")
            uri = f"{Path(self.db_path).expanduser().resolve().as_uri()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True)
            self._conn.execute("PRAGMA query_only = ON")
            return

        self._conn = sqlite3.connect(self.db_path)
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
        self._conn.commit()

    def close(self) -> None:
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

    def get(self, bucket: str, key: str) -> Optional[ObjectRecord]:
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
        cur = self._conn.execute(
            "UPDATE objects SET tier=? WHERE bucket=? AND key=?",
            (tier, bucket, key),
        )
        if cur.rowcount == 0:
            raise KeyError(f"Object not found: {bucket}/{key}")
        self._conn.commit()

    def delete(self, bucket: str, key: str) -> None:
        self._conn.execute("DELETE FROM objects WHERE bucket=? AND key=?", (bucket, key))
        self._conn.commit()

    def list(self, bucket: str, prefix: str = "") -> List[ObjectRecord]:
        like = f"{prefix}%"
        cur = self._conn.execute(
            "SELECT bucket, key, size, tier, metadata FROM objects WHERE bucket=? AND key LIKE ?",
            (bucket, like),
        )
        out: List[ObjectRecord] = []
        for row in cur.fetchall():
            md = json.loads(row[4]) if row[4] else {}
            out.append(ObjectRecord(bucket=row[0], key=row[1], size=row[2], tier=row[3], metadata=md))
        return out
