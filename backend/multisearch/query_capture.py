"""按当前租户保留策略采集及清理查询原文。"""

from asyncio import sleep
from logging import getLogger

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

_BATCH_SIZE = 250
_PURGE_INTERVAL_S = 30
_BACKLOG_INTERVAL_S = 1
_logger = getLogger(__name__)

# 调用方在同一个租户锁和事务内提供刚写入的 admitted 用量行。
CAPTURE_ADMITTED_QUERY = """
    INSERT INTO retained_queries (usage_id, tenant_id, policy_version, query, expires_at)
    SELECT admitted.id, admitted.tenant_id, policy.version, CAST(:retained_query AS TEXT),
        admitted.created_at + make_interval(hours => policy.retention_hours)
    FROM admitted
    JOIN tenant_query_retention AS policy USING (tenant_id)
    WHERE policy.retention_hours > 0 AND CAST(:retained_query AS TEXT) IS NOT NULL
"""


def retainable_query(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 1024 or "\x00" in value:
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # 可选保留不应改变已有请求对 PostgreSQL TEXT 无法存放字符的处理。
        return None
    return value


async def purge_retained_queries(db: AsyncEngine) -> int:
    async with db.begin() as connection:
        return await connection.scalar(
            text("""
                WITH expired AS (
                    SELECT query.usage_id FROM retained_queries AS query
                    WHERE query.expires_at <= now()
                    ORDER BY query.expires_at, query.usage_id
                    LIMIT :batch FOR UPDATE OF query SKIP LOCKED
                ), obsolete AS (
                    SELECT query.usage_id FROM retained_queries AS query
                    JOIN tenant_query_retention AS policy USING (tenant_id)
                    WHERE query.policy_version < policy.version AND query.expires_at > now()
                    ORDER BY query.tenant_id, query.policy_version, query.usage_id
                    LIMIT :batch FOR UPDATE OF query SKIP LOCKED
                ), removed AS (
                    DELETE FROM retained_queries
                    WHERE usage_id IN (
                        SELECT usage_id FROM expired UNION ALL SELECT usage_id FROM obsolete
                    )
                    RETURNING 1
                )
                SELECT count(*) FROM removed
            """),
            {"batch": _BATCH_SIZE},
        )


async def maintain_retained_queries(db: AsyncEngine) -> None:
    while True:
        removed = 0
        try:
            removed = await purge_retained_queries(db)
        except (SQLAlchemyError, OSError):
            _logger.warning("暂时无法清理已失效的查询原文")
        await sleep(_BACKLOG_INTERVAL_S if removed >= _BATCH_SIZE else _PURGE_INTERVAL_S)
