"""Shared pytest fixtures — the real-Postgres test harness (introduced for M5 write).

The write stage (M5) is the first code that persists to Postgres, and it uses
Postgres-specific column types (pgvector `vector(768)`, `jsonb`, `uuid`) that SQLite
can't stand in for. So its tests run against a REAL Postgres — a throwaway container
the suite boots and destroys itself (`testcontainers`), isolated from the dev database.

Two fixtures, two scopes:

  `_pg_url` (session scope, SYNC): boots ONE `pgvector/pgvector:pg16` container for the
      whole test session and applies every `infra/db/migrations/*.sql` (filename order,
      like apply-migrations.ps1) into it once, then hands back an asyncpg URL.
      It's a *sync* fixture on purpose —
      testcontainers is synchronous, and pytest-asyncio pins fixture event loops to
      *function* scope (see pytest.ini), so a session-scoped *async* fixture would
      bind to a loop the per-test loops don't share ("different loop" errors).
      The one-off schema apply therefore runs in its own short-lived loop via
      `asyncio.run`, entirely inside this fixture, so it never touches a test's loop.

  `db_session` (function scope, ASYNC): opens a fresh async engine + connection bound
      to the test's own event loop, begins a transaction, yields an `AsyncSession`
      joined to it, and ROLLS BACK at teardown — so tests never see each other's
      writes and the expensive setup (container boot + schema) is paid only once. It
      only
      *flushes* (never commits — M6 owns the commit), so the outer rollback cleanly
      discards everything each test wrote.

Schema apply uses a RAW asyncpg connection, not the SQLAlchemy engine: SQLAlchemy's
asyncpg dialect speaks the extended-query protocol, which rejects a multi-statement
script; asyncpg's own `execute` uses the simple-query protocol and runs the whole
`.sql` file in one shot.

These DB tests ERROR loudly (they don't SKIP) if Docker isn't running — the same "a SKIP
is a false green" stance the PDF tests take when poppler/tesseract are absent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer

# repo-root/infra/db/migrations/ — conftest.py is at backend/tests/, so parents[2] is
# the repo root. We apply EVERY *.sql here in filename order, mirroring what
# apply-migrations.ps1 does for the dev DB — so the test container is built by the same
# construction path (same files, same order) as the real database. A new numbered
# migration (003, 004, …) auto-reaches the test schema with no harness edit, which
# removes the "forgot to register a migration -> tests go green against a stale schema"
# drift class (a false green this project explicitly guards against).
_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "infra" / "db" / "migrations"
# pgvector's image = stock Postgres 16 + the `vector` extension preinstalled, which the
# schema's `CREATE EXTENSION IF NOT EXISTS vector` needs.
_IMAGE = "pgvector/pgvector:pg16"


@pytest.fixture(scope="session")
def _pg_url() -> Iterator[str]:
    migrations = sorted(_MIGRATIONS_DIR.glob("*.sql"))

    # driver="asyncpg" only shapes the URL string get_connection_url() returns;
    # readiness is probed by exec'ing psql inside the container, so no driver is needed.
    with PostgresContainer(_IMAGE, driver="asyncpg") as pg:

        async def _apply_schema() -> None:
            # driver=None -> a plain postgresql:// DSN that asyncpg.connect accepts. Its
            # execute() runs a whole multi-statement file via the simple-query path, so
            # we apply each migration in order on one connection (002 creates the tables
            # 003 then ALTERs, so filename order matters).
            conn = await asyncpg.connect(pg.get_connection_url(driver=None))
            try:
                for sql_file in migrations:
                    await conn.execute(sql_file.read_text(encoding="utf-8"))
            finally:
                await conn.close()

        asyncio.run(_apply_schema())
        yield pg.get_connection_url()
    # container (and its data) destroyed on __exit__.


@pytest.fixture
async def db_session(_pg_url: str) -> AsyncGenerator[AsyncSession, None]:
    # Engine per test (cheap next to the container boot) so the connection is created on
    # THIS test's event loop. Begin an outer transaction, bind a session to it, and roll
    # back at the end — nothing the test wrote survives.
    engine = create_async_engine(_pg_url)
    conn = await engine.connect()
    txn = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await txn.rollback()
        await conn.close()
        await engine.dispose()


@pytest.fixture
async def session_factory(
    _pg_url: str,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """A committing session factory bound to the test container — for the M6 test.

    `db_session` above isolates tests by wrapping everything in ONE outer transaction
    it rolls back, which works only because M1–M5 never commit. But M6 *owns the commit*
    (it walks `documents.status` through several short transactions), so its test must
    let real commits land and then observe them from a *separate* session/connection —
    exactly what a future Celery worker and an HTTP poller would do. This fixture hands
    back an `async_sessionmaker` (so both the test and the orchestrator-under-test can
    each open their own committing session on the same engine), and restores isolation
    the only way that survives a commit: TRUNCATE both tables at teardown.
    """
    engine = create_async_engine(_pg_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        # CASCADE + every committed-to table so a run leaves nothing for the next test.
        # query_logs (004) joined the list at Q10: /ask commits an audit row, and it has
        # no FK to documents, so a CASCADE from the other two would never reach it.
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE chunks, documents, query_logs CASCADE"))
        await engine.dispose()
