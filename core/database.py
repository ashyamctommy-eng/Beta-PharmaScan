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

from core.config import settings


_IS_SQLITE = settings.DATABASE_URL.startswith("sqlite")

_engine_kwargs: dict = {"echo": settings.DEBUG}
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
