"""
core/database.py
----------------
Async SQLAlchemy engine + session factory (SQLite via aiosqlite).
"""

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from core.config import settings


_IS_SQLITE = settings.DATABASE_URL.startswith("sqlite")

def _pool_class_for(pool_mode: str):
    """``NullPool`` (one connection per request) when the host sleeps idle services.

    None means SQLAlchemy's default pool. See DB_POOL_MODE in core/config.py: Railway's
    Serverless decides a service is idle from its outbound traffic, so an idle pool keeps
    the container awake and spends the free credit.
    """
    if (pool_mode or "").strip().lower() in ("null", "none", "no-pool", "nopool"):
        return NullPool
    return None


_engine_kwargs: dict = {"echo": settings.DEBUG}
_pool_class = _pool_class_for(settings.DB_POOL_MODE)
if _pool_class is not None:
    _engine_kwargs["poolclass"] = _pool_class
if _IS_SQLITE:
    # SQLite-only: passing this to asyncpg is a TypeError. Keeping it conditional is
    # what lets the same code run on Postgres, which a container host with an
    # ephemeral filesystem needs for its data to survive a restart.
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Create all tables on startup."""
    from models import resource  # noqa: F401 – registers ORM models

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
