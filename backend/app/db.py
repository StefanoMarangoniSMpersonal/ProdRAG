"""Async database engine and session factory.

We use SQLAlchemy 2.x's async API on top of asyncpg. This is the same access
path retrieval will use later, so we establish the "async everywhere" pattern
now rather than starting sync and rewriting.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_settings = get_settings()

# One engine per process; it manages the connection pool.
engine: AsyncEngine = create_async_engine(
    _settings.database_url,
    echo=False,
    pool_pre_ping=True,  # transparently drops dead connections from the pool
)

# expire_on_commit=False keeps ORM objects usable after commit (matters once we
# have models; harmless now).
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a session and always closes it."""
    async with SessionLocal() as session:
        yield session
