from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool


def is_database_url(locator: str | Path) -> bool:
    return isinstance(locator, str) and "://" in locator


def normalize_database_url(locator: str | Path) -> str:
    if isinstance(locator, Path) or not is_database_url(locator):
        value = str(locator)
        if value == ":memory:":
            return "sqlite+pysqlite:///:memory:"
        return f"sqlite+pysqlite:///{Path(value).expanduser().resolve()}"
    if locator.startswith("postgres://"):
        return "postgresql+psycopg://" + locator[len("postgres://") :]
    if locator.startswith("postgresql://"):
        return "postgresql+psycopg://" + locator[len("postgresql://") :]
    if locator.startswith("sqlite://") and not locator.startswith("sqlite+pysqlite://"):
        return "sqlite+pysqlite://" + locator[len("sqlite://") :]
    return locator


def redact_database_url(locator: str | Path) -> str:
    url = normalize_database_url(locator)
    parsed = urlsplit(url)
    if parsed.password is None:
        return url
    username = parsed.username or ""
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    netloc = f"{username}:***@{hostname}{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def create_catalog_engine(
    locator: str | Path,
    *,
    read_only: bool = False,
) -> tuple[Engine, sqlite3.Connection | None]:
    url = normalize_database_url(locator)
    if url.startswith("sqlite+pysqlite://"):
        parsed_url = sa.engine.make_url(url)
        database = parsed_url.database
        if database in (None, "", ":memory:"):
            if read_only:
                raise ValueError("an in-memory SQL catalog cannot be opened read-only")
            connection = sqlite3.connect(":memory:", check_same_thread=False)
            connection.execute("PRAGMA foreign_keys = ON")
            engine = sa.create_engine(
                "sqlite+pysqlite://",
                creator=lambda: connection,
                poolclass=StaticPool,
            )
            return engine, connection
        engine_url: str | sa.URL = url
        if read_only:
            engine_url = sa.URL.create(
                "sqlite+pysqlite",
                database=f"file:{database}",
                query={"mode": "ro", "uri": "true"},
            )
        engine = sa.create_engine(
            engine_url,
            connect_args={"check_same_thread": False, "timeout": 5.0},
            pool_pre_ping=True,
        )

        @sa.event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA busy_timeout = 5000")
            if read_only:
                cursor.execute("PRAGMA query_only = ON")
            cursor.close()

        compatibility_connection = sqlite3.connect(
            f"file:{database}?mode=ro" if read_only else database,
            uri=read_only,
            check_same_thread=False,
            timeout=5.0,
        )
        compatibility_connection.execute("PRAGMA foreign_keys = ON")
        if read_only:
            compatibility_connection.execute("PRAGMA query_only = ON")
        return engine, compatibility_connection

    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("catalog URL must use sqlite or postgresql")
    connect_args: dict[str, Any] = {}
    if read_only:
        connect_args["options"] = "-c default_transaction_read_only=on"
    return (
        sa.create_engine(url, pool_pre_ping=True, connect_args=connect_args),
        None,
    )
