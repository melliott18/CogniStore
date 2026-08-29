"""Pytest configuration and fixtures for opt-in integration tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
import sqlalchemy as sa

from cognistore.db.engine import normalize_database_url


class _PostgresDsn(str):
    """Keep configured credentials out of pytest's fixture-value output."""

    def __repr__(self) -> str:
        redacted = sa.engine.make_url(self).render_as_string(hide_password=True)
        return repr(redacted)


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: requires a separately managed external service",
    )


@pytest.fixture
def postgres_dsn() -> Iterator[str]:
    """Create an isolated PostgreSQL database for one integration test.

    The configured database is used only as an administrative connection; test
    migrations and catalog writes happen in a uniquely named sibling database.
    This prevents a local test run from modifying the database named in the
    environment variable and gives every migration test a genuine clean slate.
    """

    configured_dsn = os.environ.get("COGNISTORE_TEST_POSTGRES_DSN")
    if not configured_dsn:
        pytest.skip("COGNISTORE_TEST_POSTGRES_DSN is not configured")

    admin_url = sa.engine.make_url(normalize_database_url(configured_dsn))
    if admin_url.get_backend_name() != "postgresql":
        pytest.fail("COGNISTORE_TEST_POSTGRES_DSN must be a PostgreSQL URL")

    database_name = f"cognistore_test_{uuid4().hex}"
    test_url = admin_url.set(database=database_name)
    admin_engine = sa.create_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    quoted_database = admin_engine.dialect.identifier_preparer.quote(database_name)
    created = False
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f"CREATE DATABASE {quoted_database}")
        created = True
        yield _PostgresDsn(test_url.render_as_string(hide_password=False))
    finally:
        if created:
            with admin_engine.connect() as connection:
                connection.exec_driver_sql(f"DROP DATABASE {quoted_database} WITH (FORCE)")
        admin_engine.dispose()
