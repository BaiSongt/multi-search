from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .contracts import Role
from .errors import ApiError
from .ops_store import _audit
from .policy import lock_tenant
from .sessions import SessionIdentity, SessionStore

QUERY_RETENTION_UPDATED = "settings.query_retention.update"


class QueryRetentionChange(BaseModel):
    version: int
    before_hours: int
    after_hours: int
    occurred_at: datetime


class QueryRetentionState(BaseModel):
    tenant_id: UUID
    retention_hours: int
    version: int
    updated_at: datetime | None
    changes: list[QueryRetentionChange]


class QueryRetentionStore:
    def __init__(self, db: AsyncEngine, sessions: SessionStore):
        self.db = db
        self.sessions = sessions

    async def _read(self, connection: AsyncConnection, tenant_id: UUID) -> QueryRetentionState:
        result = await connection.execute(
            text("""
                SELECT t.id AS tenant_id, COALESCE(p.retention_hours, 0) AS retention_hours,
                    COALESCE(p.version, 0) AS version, p.updated_at,
                    (
                        SELECT COALESCE(jsonb_agg(
                            jsonb_build_object('version', details -> 'version',
                                'before_hours', details -> 'before_hours',
                                'after_hours', details -> 'after_hours', 'occurred_at', occurred_at)
                            ORDER BY id DESC
                        ), '[]'::JSONB)
                        FROM (
                            SELECT id, details, occurred_at FROM audit_events
                            WHERE tenant_id = t.id AND action = 'settings.query_retention.update'
                            ORDER BY id DESC LIMIT 10
                        ) AS recent
                    ) AS changes
                FROM tenants t LEFT JOIN tenant_query_retention p ON p.tenant_id = t.id
                WHERE t.id = :tenant
            """),
            {"tenant": tenant_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise ApiError(404, "TENANT_NOT_FOUND", "租户不存在")
        return QueryRetentionState.model_validate(row)

    async def get(self, actor: SessionIdentity, tenant_id: UUID) -> QueryRetentionState:
        if actor.role != Role.PLATFORM_ADMIN and (
            actor.role != Role.OPERATIONS_ADMIN or actor.tenant_id != tenant_id
        ):
            raise ApiError(403, "FORBIDDEN", "无权读取该租户的查询保留设置")
        async with self.db.connect() as connection:
            return await self._read(connection, tenant_id)

    async def update(
        self, actor: SessionIdentity, tenant_id: UUID, retention_hours: int, expected_version: int
    ) -> QueryRetentionState:
        if actor.role != Role.PLATFORM_ADMIN:
            raise ApiError(403, "FORBIDDEN", "需要平台管理员权限")
        async with self.db.begin() as connection:
            await lock_tenant(connection, tenant_id)
            await self.sessions.assert_actor(connection, actor)
            before = await self._read(connection, tenant_id)
            if before.version != expected_version:
                raise ApiError(409, "SETTINGS_VERSION_CONFLICT", "保留设置已变更，请重新预览")
            if before.retention_hours == retention_hours:
                return before
            last_audited_version = before.changes[0].version if before.changes else 0
            version = max(before.version, last_audited_version) + 1
            await connection.execute(
                text("""
                    INSERT INTO tenant_query_retention (tenant_id, retention_hours, version)
                    VALUES (:tenant, :hours, :version)
                    ON CONFLICT (tenant_id) DO UPDATE SET retention_hours = EXCLUDED.retention_hours,
                        version = EXCLUDED.version, updated_at = EXCLUDED.updated_at
                """),
                {"tenant": tenant_id, "hours": retention_hours, "version": version},
            )
            await _audit(
                connection,
                tenant_id,
                actor.user_id,
                QUERY_RETENTION_UPDATED,
                tenant_id,
                {
                    "version": version,
                    "before_hours": before.retention_hours,
                    "after_hours": retention_hours,
                },
            )
            return await self._read(connection, tenant_id)
