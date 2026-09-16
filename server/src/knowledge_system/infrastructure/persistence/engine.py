"""已批准Psycopg 3异步引擎与Session工厂。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@dataclass(frozen=True, slots=True)
class DatabaseEngineSettings:
    url: str
    pool_size: int = 5
    max_overflow: int = 5
    pool_timeout_seconds: float = 10.0
    pool_recycle_seconds: int = 1_800
    pool_pre_ping: bool = True

    def __post_init__(self) -> None:
        if make_url(self.url).drivername != "postgresql+psycopg":
            raise ValueError("DATABASE_DRIVER_NOT_APPROVED")
        if self.pool_size < 1 or self.max_overflow < 0:
            raise ValueError("DATABASE_POOL_SIZE_INVALID")
        if self.pool_timeout_seconds <= 0 or self.pool_recycle_seconds <= 0:
            raise ValueError("DATABASE_POOL_TIMEOUT_INVALID")


def build_async_engine(settings: DatabaseEngineSettings) -> AsyncEngine:
    return create_async_engine(
        settings.url,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.pool_timeout_seconds,
        pool_recycle=settings.pool_recycle_seconds,
        pool_pre_ping=settings.pool_pre_ping,
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
