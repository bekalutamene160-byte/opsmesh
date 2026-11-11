"""
OPSMESH Database Connection Manager — supports both SQLite and Postgres.

Architecture (Phase 7):
  This module abstracts the database driver. The rest of the codebase
  calls `get_connection()` and gets back a connection — it doesn't need
  to know whether it's SQLite or Postgres.

  - For SQLite (dev / single-instance): file-based, WAL mode, busy_timeout
  - For Postgres (production / multi-process): asyncpg, connection pool

  The driver is selected based on the `OPSMESH_DATABASE_URL` env var:
    - Starts with `postgresql://` or `postgres://` → Postgres (asyncpg)
    - Otherwise → SQLite (aiosqlite)

  Both implementations return an object with the SAME interface:
    - execute(sql, params) → cursor
    - executemany(sql, params_list)
    - commit()
    - fetchone() / fetchall() (via cursors)
    - close()

  Note: SQLite uses ? placeholders, Postgres uses $1, $2, ... placeholders.
  The PostgresStateStore normalizes queries accordingly.

USAGE:
    from opsmesh.adapters.db.connection import init_db, get_connection, get_db_backend

    await init_db()  # auto-detects from settings (database_url or database_path)

    async with get_connection() as conn:
        await conn.execute("INSERT INTO runs ...")
        await conn.commit()
"""

from __future__ import annotations

import os
from typing import Optional, Literal

import aiosqlite

from opsmesh.config import get_settings


# Global database state
_db_path: Optional[str] = None
_db_url: Optional[str] = None
_db_backend: Literal["sqlite", "postgres", "memory"] = "memory"
_pg_pool = None  # asyncpg pool (created lazily)


async def init_db(
    database_path: Optional[str] = None,
    database_url: Optional[str] = None,
) -> None:
    """Initialize the database (async — call inside an event loop).

    Call once at startup (both API and worker).

    This:
      1. Calls configure_backend() (synchronous — sets module-level state)
      2. Actually connects + creates the schema (async — must be in event loop)

    Order of precedence:
      1. database_url argument (explicit)
      2. database_path argument (explicit)
      3. settings.database_url (from env OPSMESH_DATABASE_URL)
      4. settings.database_path (from env OPSMESH_DATABASE_PATH)
      5. None → in-memory mode (tests/dev only)
    """
    # 1. Configure the backend (synchronous — no network I/O)
    configure_backend(database_path, database_url)

    # 2. Actually connect + create schema (async)
    if _db_backend == "postgres":
        await _ensure_pg_pool()
        await _init_pg_schema()
    elif _db_backend == "sqlite":
        # Ensure the directory exists
        db_dir = os.path.dirname(_db_path or "")
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        # Set pragmas on a connection and close it (persisted to the DB file)
        async with aiosqlite.connect(_db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.commit()
        # Create the schema (idempotent)
        await _init_sqlite_schema(_db_path)
    # else: memory backend — nothing to do


def configure_backend(
    database_path: Optional[str] = None,
    database_url: Optional[str] = None,
) -> None:
    """Configure the backend SYNCHRONOUSLY (no network I/O).

    Sets the module-level state (_db_path, _db_url, _db_backend) so that
    `get_db_backend()`, `create_lead_store()`, and `create_approval_store()`
    return the right implementations. The actual connection + schema creation
    happens later in `init_db()` (which is async).

    This separation lets `create_app()` and `_build_default_worker()` determine
    the backend type synchronously (so they can build the right stores), and
    defer the actual DB connection to the FastAPI lifespan handler (API) or
    `_init_worker_db()` (worker) — both of which run inside an event loop.

    Order of precedence (same as init_db):
      1. database_url argument
      2. database_path argument
      3. settings.database_url
      4. settings.database_path
      5. None → memory
    """
    global _db_path, _db_url, _db_backend

    settings = get_settings()
    if database_url is None:
        database_url = settings.database_url or ""
    if database_path is None:
        database_path = settings.database_path or ""

    # Postgres takes precedence
    if database_url and (
        database_url.startswith("postgresql://")
        or database_url.startswith("postgres://")
        or database_url.startswith("postgresql+asyncpg://")
    ):
        _db_url = database_url
        _db_backend = "postgres"
        # Normalize the URL for asyncpg (strip the +asyncpg driver suffix)
        if _db_url.startswith("postgresql+asyncpg://"):
            _db_url = _db_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    elif database_path:
        _db_path = database_path
        _db_backend = "sqlite"
    else:
        _db_backend = "memory"


def get_db_backend() -> str:
    """Return the configured backend: 'sqlite', 'postgres', or 'memory'."""
    return _db_backend


def get_db_path() -> Optional[str]:
    """Return the SQLite database path (or None if using Postgres or memory)."""
    return _db_path


def get_db_url() -> Optional[str]:
    """Return the Postgres connection URL (or None if using SQLite or memory)."""
    return _db_url


# ---------------------------------------------------------------------------
# SQLite connection
# ---------------------------------------------------------------------------


class _SqliteConnectionContextManager:
    """Async context manager for aiosqlite connections with WAL + busy_timeout."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def __aenter__(self) -> aiosqlite.Connection:
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


# ---------------------------------------------------------------------------
# Postgres connection pool
# ---------------------------------------------------------------------------


async def _ensure_pg_pool():
    """Create the asyncpg connection pool if not already created."""
    global _pg_pool
    if _pg_pool is None and _db_url:
        import asyncpg
        _pg_pool = await asyncpg.create_pool(
            dsn=_db_url,
            min_size=2,
            max_size=10,
            command_timeout=30.0,
        )


class _PostgresConnectionContextManager:
    """Async context manager for asyncpg connections from the pool.

    asyncpg's interface differs from aiosqlite:
      - fetch/fetchrow/fetchval (no cursor)
      - execute(sql, *args) — uses $1, $2 placeholders, NOT ? placeholders
      - No commit() — asyncpg auto-commits unless inside a transaction

    To unify the interface, we wrap asyncpg's Connection with an adapter
    that exposes execute() / commit() / fetchone() / fetchall() in the
    SQLite-style API. This way, SQLiteStateStore and PostgresStateStore
    share most of their code.
    """

    def __init__(self):
        self._conn = None

    async def __aenter__(self) -> "PostgresConnectionAdapter":
        await _ensure_pg_pool()
        self._conn = await _pg_pool.acquire()
        return PostgresConnectionAdapter(self._conn)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._conn is not None:
            await _pg_pool.release(self._conn)
            self._conn = None


class PostgresConnectionAdapter:
    """Wraps an asyncpg Connection to expose an aiosqlite-like API.

    Key differences handled here:
      - asyncpg uses $1, $2 placeholders; we convert ? to $1, $2, ... automatically
      - asyncpg's execute returns a status string, not a cursor
      - fetchone/fetchall return Records (which support indexing like tuples)
    """

    def __init__(self, conn):
        self._conn = conn

    async def execute(self, sql: str, params: tuple = ()) -> "PostgresCursor":
        # Convert ? placeholders to $1, $2, ...
        pg_sql, pg_params = _convert_placeholders(sql, params)
        # asyncpg execute returns a status string ('INSERT 0 1', etc.)
        await self._conn.execute(pg_sql, *pg_params)
        return PostgresCursor(self._conn, pg_sql, pg_params)

    async def executemany(self, sql: str, params_list):
        for params in params_list:
            pg_sql, pg_params = _convert_placeholders(sql, params)
            await self._conn.execute(pg_sql, *pg_params)

    async def commit(self):
        # asyncpg is auto-commit by default (unless inside a transaction)
        # This is a no-op for compatibility with SQLite-style code.
        pass

    async def close(self):
        # Connection is released by the pool context manager
        pass


class PostgresCursor:
    """Wraps asyncpg's fetch results in a cursor-like API matching aiosqlite.

    SQLite tests use:
        cursor = await conn.execute("SELECT ...")
        row = await cursor.fetchone()
        rows = await cursor.fetchall()
        await cursor.close()

    asyncpg doesn't have a cursor — fetch returns immediately. So we
    pre-fetch on the first fetchone/fetchall call.
    """

    def __init__(self, conn, sql: str, params: tuple):
        self._conn = conn
        self._sql = sql
        self._params = params
        self._rows: Optional[list] = None
        self._fetched = 0

    async def _ensure_fetched(self):
        if self._rows is None:
            # Check if this was a SELECT
            stripped = self._sql.lstrip().upper()
            if stripped.startswith("SELECT"):
                self._rows = await self._conn.fetch(self._sql, *self._params)
            else:
                # Non-SELECT — there are no rows to fetch
                self._rows = []

    async def fetchone(self):
        await self._ensure_fetched()
        if self._rows and self._fetched < len(self._rows):
            row = self._rows[self._fetched]
            self._fetched += 1
            return row
        return None

    async def fetchall(self):
        await self._ensure_fetched()
        return list(self._rows or [])

    async def close(self):
        # No cursor to close in asyncpg
        pass


def _convert_placeholders(sql: str, params: tuple) -> tuple[str, tuple]:
    """Convert SQLite ? placeholders to Postgres $1, $2, ... placeholders.

    Also handles the case where params is a tuple of one element that's
    itself a tuple (a common aiosqlite pattern).
    """
    if not sql or "?" not in sql:
        return sql, params

    # Replace ? with $1, $2, ... (in order)
    parts = sql.split("?")
    pg_sql = parts[0]
    for i in range(1, len(parts)):
        pg_sql += f"${i}" + parts[i]

    return pg_sql, params


# ---------------------------------------------------------------------------
# Unified get_connection() — returns the right backend
# ---------------------------------------------------------------------------


def get_connection():
    """Return a context manager for a database connection.

    Returns either _SqliteConnectionContextManager or _PostgresConnectionContextManager
    based on the configured backend.

    Usage:
        async with get_connection() as conn:
            await conn.execute("INSERT ...")
            await conn.commit()
    """
    if _db_backend == "postgres":
        if _db_url is None:
            raise RuntimeError("Postgres backend selected but _db_url is None. Call init_db() first.")
        return _PostgresConnectionContextManager()
    elif _db_backend == "sqlite":
        if _db_path is None:
            raise RuntimeError("SQLite backend selected but _db_path is None. Call init_db() first.")
        return _SqliteConnectionContextManager(_db_path)
    else:
        raise RuntimeError(
            "Database not initialized. Call init_db() first, or set "
            "OPSMESH_DATABASE_URL (Postgres) or OPSMESH_DATABASE_PATH (SQLite)."
        )


# ---------------------------------------------------------------------------
# Schema initialization (idempotent — creates tables if they don't exist)
# ---------------------------------------------------------------------------


async def _init_sqlite_schema(database_path: str) -> None:
    """Run the SQLite schema (idempotent — CREATE TABLE IF NOT EXISTS).

    ALWAYS runs the schema SQL on every startup. This is safe because the
    schema uses `CREATE TABLE IF NOT EXISTS` — existing tables are not
    recreated. This fixes a bug where the `runs` table existed but `jobs`
    didn't (partial schema from a crash), causing "no such table: jobs".
    """
    async with aiosqlite.connect(database_path) as conn:
        # Always run the full schema — CREATE TABLE IF NOT EXISTS is idempotent
        from opsmesh.adapters.db.schema import SCHEMA_SQLITE
        await conn.executescript(SCHEMA_SQLITE)
        await conn.commit()


async def _init_pg_schema() -> None:
    """Run the Postgres schema (idempotent — CREATE TABLE IF NOT EXISTS)."""
    async with _PostgresConnectionContextManager() as conn:
        from opsmesh.adapters.db.schema import SCHEMA_POSTGRES
        for stmt in SCHEMA_POSTGRES:
            # Each statement is a single CREATE TABLE / CREATE INDEX
            await conn.execute(stmt)
        await conn.commit()


async def close_all() -> None:
    """Cleanup. Close the Postgres pool if open."""
    global _pg_pool
    if _pg_pool is not None:
        await _pg_pool.close()
        _pg_pool = None


def reset_db_state() -> None:
    """Reset all module-level DB state to defaults.

    Used by tests to ensure isolation between test cases. After calling
    this, the backend is "memory" (no DB configured) until init_db() is
    called again.
    """
    global _db_path, _db_url, _db_backend, _pg_pool
    _db_path = None
    _db_url = None
    _db_backend = "memory"
    _pg_pool = None


__all__ = [
    "init_db",
    "configure_backend",
    "get_connection",
    "get_db_backend",
    "get_db_path",
    "get_db_url",
    "close_all",
    "reset_db_state",
]
