from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from cognistore.db.schema import metadata

config = context.config
target_metadata = metadata


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=connection.get_execution_options().get(
            "schema_translate_map", {}
        ).get(None),
        # SQLite's JSON columns intentionally retain TEXT affinity for legacy
        # compatibility. PostgreSQL type drift remains fully checked.
        compare_type=connection.dialect.name != "sqlite",
        render_as_batch=connection.dialect.name == "sqlite",
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied = config.attributes.get("connection")
    if supplied is not None:
        _run(supplied)
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _run(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
