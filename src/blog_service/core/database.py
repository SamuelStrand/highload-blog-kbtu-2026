"""PostgreSQL connection/session factories and shared model metadata."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from blog_service.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        isolation_level="READ COMMITTED",
        pool_size=settings.db_pool_size,
        max_overflow=0,
        pool_timeout=1,
        pool_pre_ping=True,
        connect_args={
            "timeout": 3,
            "server_settings": {
                "application_name": "sis2_blog",
                "timezone": "UTC",
                "lock_timeout": "1000",
                "statement_timeout": "3000",
            },
        },
    )


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session
