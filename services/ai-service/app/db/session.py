"""Async SQLAlchemy engine and request-scoped sessions."""

from collections.abc import AsyncGenerator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


def create_db_engine(settings: Settings):
    return create_async_engine(settings.database_url_async, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db(request: Request) -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency: one session per request (uses app.state from lifespan)."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.async_session_factory
    async with factory() as session:
        yield session
