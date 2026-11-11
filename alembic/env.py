"""
OPSMESH Alembic migration environment.

Reads the database path from OPSMESH_DATABASE_PATH (or falls back to
opsmesh.db in the current directory). Runs migrations in synchronous mode
(Alembic doesn't support async).

USAGE:
    alembic upgrade head     # apply all migrations
    alembic downgrade -1     # rollback last migration (dev only)
    alembic current          # show current revision
    alembic history          # show migration history
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Read the database path from env, falling back to a default
db_path = os.environ.get("OPSMESH_DATABASE_PATH", "./opsmesh.db")
# Convert file path to SQLAlchemy URL
config = context.config
config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config)

# Target metadata (empty — we use raw SQL in migrations, not SQLAlchemy models)
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generate SQL without connecting)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # Required for SQLite (ALTER TABLE limitations)
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connect to DB and execute)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # Enable WAL mode + busy_timeout for the migration connection
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # Required for SQLite (ALTER TABLE limitations)
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
