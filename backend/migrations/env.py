import asyncio

from alembic import context
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from multisearch.settings import Settings


def run_migrations(connection):
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = create_async_engine(Settings.from_env().database_url)
    try:
        async with engine.begin() as connection:
            # 在同一事务中锁定迁移，避免并行启动时重复创建表。
            await connection.execute(text("SELECT pg_advisory_xact_lock(1498139727)"))
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(dialect_name="postgresql")
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
