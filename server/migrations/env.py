"""Alembic环境：仅允许已批准的Psycopg 3 URL。"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

from knowledge_system.infrastructure.persistence import (  # noqa: F401
    Base,
    attempt_models,
    foundation_models,
    planning_models,
    task_models,
)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = os.environ.get("KNOWLEDGE_DATABASE_URL")
    if not url:
        raise RuntimeError("KNOWLEDGE_DATABASE_URL_REQUIRED")
    if make_url(url).drivername != "postgresql+psycopg":
        raise RuntimeError("DATABASE_DRIVER_NOT_APPROVED")
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run_sync_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_online())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
