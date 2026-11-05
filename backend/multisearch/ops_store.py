import asyncio
import hmac
import json
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .contracts import Role
from .errors import ApiError
from .policy import lock_tenant
from .routing_policy import DEFAULT_ROUTING, RoutingRules, RoutingVersion
from .sessions import (
    SessionIdentity,
    SessionStore,
    lock_platform_members,
    require_platform_admin_after,
    require_tenant_admin_after,
    revoke_member_credentials,
)

CACHE_SCOPES = ("all", "web", "news", "image", "academic", "code", "local")
_INVITE_FORMAT = re.compile(r"msi_([a-f0-9]{32})\.[A-Za-z0-9_-]{43}")
_TENANT_ROLES = {Role.DEVELOPER, Role.TENANT_ADMIN, Role.AUDITOR}
_MEMBER_FIELDS = """
    m.user_id, u.email, m.role, m.enabled, u.enabled AS account_enabled,
    u.password_hash IS NOT NULL AS has_password
"""
_INVITATION_FIELDS = """
    id, tenant_id, email, role, created_at, expires_at,
    CASE WHEN accepted_at IS NOT NULL THEN 'accepted'
         WHEN revoked_at IS NOT NULL THEN 'revoked'
         WHEN expires_at <= now() THEN 'expired'
         ELSE 'pending' END AS status
"""
_ROUTING_FIELDS = "version, policy, created_at, actor_id, reason, rollback_of"
_ROUTING_AUDIT_TARGET = uuid5(NAMESPACE_URL, "multisearch:routing")


async def _one(connection: AsyncConnection, sql: str, params: dict):
    return (await connection.execute(text(sql), params)).mappings().one_or_none()


def _team_authorized(actor: SessionIdentity, tenant_id: UUID) -> bool:
    return actor.role == Role.PLATFORM_ADMIN or (
        actor.role == Role.TENANT_ADMIN and actor.tenant_id == tenant_id
    )


def _assignable(actor_role: Role, target_role: Role) -> bool:
    return actor_role == Role.PLATFORM_ADMIN or (
        actor_role == Role.TENANT_ADMIN and target_role in _TENANT_ROLES
    )


def _forbidden() -> ApiError:
    return ApiError(403, "FORBIDDEN", "当前角色无权执行此操作")


def _invitation_unavailable() -> ApiError:
    return ApiError(404, "INVITATION_NOT_FOUND", "邀请无效或已失效")


async def _audit(
    connection: AsyncConnection,
    tenant_id: UUID,
    actor_id: UUID,
    action: str,
    target: UUID,
    details: dict,
    *,
    actor_type: str = "session",
) -> None:
    await connection.execute(
        text("""
            INSERT INTO audit_events (tenant_id, actor_type, actor_id, action, target_id, details)
            VALUES (:tenant, :actor_type, :actor, :action, :target, CAST(:details AS JSONB))
        """),
        {
            "tenant": tenant_id,
            "actor": actor_id,
            "action": action,
            "target": target,
            "actor_type": actor_type,
            "details": json.dumps(details, ensure_ascii=False),
        },
    )


class OpsStore:
    def __init__(self, db: AsyncEngine, redis: Redis, sessions: SessionStore, pepper: str):
        self.db = db
        self.redis = redis
        self.sessions = sessions
        self._pepper = pepper.encode()

    @staticmethod
    async def _tenant(connection: AsyncConnection, tenant_id: UUID, *, active: bool = False) -> str:
        row = await _one(
            connection,
            "SELECT slug, enabled FROM tenants WHERE id = :id",
            {"id": tenant_id},
        )
        if row is None:
            raise ApiError(404, "TENANT_NOT_FOUND", "租户不存在")
        if active and not row["enabled"]:
            raise ApiError(409, "TENANT_STATE_CONFLICT", "租户已停用")
        return row["slug"]

    @asynccontextmanager
    async def _team_transaction(
        self,
        actor: SessionIdentity,
        tenant_id: UUID,
    ) -> AsyncIterator[AsyncConnection]:
        if not _team_authorized(actor, tenant_id):
            raise _forbidden()
        async with self.db.begin() as connection:
            await lock_platform_members(connection)
            await lock_tenant(connection, tenant_id)
            await self.sessions.assert_actor(connection, actor)
            await self._tenant(connection, tenant_id, active=True)
            yield connection

    async def members(
        self,
        actor: SessionIdentity,
        tenant_id: UUID,
        limit: int,
        cursor: UUID | None,
    ) -> dict:
        if not _team_authorized(actor, tenant_id):
            raise _forbidden()
        async with self.db.connect() as connection:
            await self._tenant(connection, tenant_id)
            result = await connection.execute(
                text(f"""
                    SELECT {_MEMBER_FIELDS}
                    FROM memberships m JOIN users u ON m.user_id = u.id
                    WHERE m.tenant_id = :tenant AND (CAST(:cursor AS UUID) IS NULL OR m.user_id > :cursor)
                    ORDER BY m.user_id LIMIT :limit
                """),
                {"tenant": tenant_id, "cursor": cursor, "limit": limit + 1},
            )
            rows = [dict(row) for row in result.mappings()]
        return {
            "items": rows[:limit],
            "next_cursor": rows[limit - 1]["user_id"] if len(rows) > limit else None,
        }

    async def update_member(
        self,
        actor: SessionIdentity,
        tenant_id: UUID,
        user_id: UUID,
        *,
        role: Role | None,
        enabled: bool | None,
    ) -> dict:
        async with self._team_transaction(actor, tenant_id) as connection:
            params = {"tenant": tenant_id, "user": user_id}
            row = await _one(
                connection,
                f"""
                SELECT {_MEMBER_FIELDS}
                FROM memberships m JOIN users u ON m.user_id = u.id
                WHERE m.tenant_id = :tenant AND m.user_id = :user
                FOR UPDATE OF m FOR SHARE OF u
            """,
                params,
            )
            if row is None:
                raise ApiError(404, "MEMBER_NOT_FOUND", "成员不存在")
            old_role = Role(row["role"])
            new_role = role if role is not None else old_role
            new_enabled = enabled if enabled is not None else row["enabled"]
            if not all(_assignable(actor.role, candidate) for candidate in (old_role, new_role)):
                raise _forbidden()
            if old_role == new_role and row["enabled"] == new_enabled:
                return dict(row)
            await connection.execute(
                text("""
                    UPDATE memberships SET role = :role, enabled = :enabled
                    WHERE tenant_id = :tenant AND user_id = :user
                """),
                {**params, "role": new_role.value, "enabled": new_enabled},
            )
            was_admin = row["enabled"] and row["account_enabled"] and row["has_password"]
            if (
                was_admin
                and old_role == Role.PLATFORM_ADMIN
                and (not new_enabled or new_role != Role.PLATFORM_ADMIN)
            ):
                await require_platform_admin_after(connection)
            if (
                was_admin
                and actor.role == old_role == Role.TENANT_ADMIN
                and (not new_enabled or new_role != Role.TENANT_ADMIN)
            ):
                await require_tenant_admin_after(connection, tenant_id)
            credentials_revoked = not new_enabled or new_role != old_role
            if credentials_revoked:
                await revoke_member_credentials(connection, tenant_id, user_id)
            await _audit(
                connection,
                tenant_id,
                actor.user_id,
                "member.update",
                user_id,
                {
                    "before": {"role": old_role, "enabled": row["enabled"]},
                    "after": {"role": new_role, "enabled": new_enabled},
                    "credentials_revoked": credentials_revoked,
                },
            )
            return {**row, "role": new_role, "enabled": new_enabled}

    def _invite_hash(self, token: str) -> bytes:
        return hmac.digest(self._pepper, f"invite:{token}".encode(), "sha256")

    async def invitations(
        self,
        actor: SessionIdentity,
        tenant_id: UUID,
        limit: int,
        cursor: UUID | None,
    ) -> dict:
        if not _team_authorized(actor, tenant_id):
            raise _forbidden()
        async with self.db.connect() as connection:
            await self._tenant(connection, tenant_id)
            result = await connection.execute(
                text(f"""
                    SELECT {_INVITATION_FIELDS} FROM tenant_invitations
                    WHERE tenant_id = :tenant AND (
                        CAST(:cursor AS UUID) IS NULL OR (created_at, id) <
                        (SELECT created_at, id FROM tenant_invitations WHERE tenant_id = :tenant AND id = :cursor)
                    )
                    ORDER BY created_at DESC, id DESC LIMIT :limit
                """),
                {"tenant": tenant_id, "cursor": cursor, "limit": limit + 1},
            )
            rows = [dict(row) for row in result.mappings()]
        return {
            "items": rows[:limit],
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    async def invite(
        self,
        actor: SessionIdentity,
        tenant_id: UUID,
        email: str,
        role: Role,
        hours: int,
    ) -> dict:
        if not _assignable(actor.role, role):
            raise _forbidden()
        invitation_id = uuid4()
        token = f"msi_{invitation_id.hex}.{secrets.token_urlsafe(32)}"
        async with self._team_transaction(actor, tenant_id) as connection:
            params = {"tenant": tenant_id, "email": email}
            blocked = await connection.scalar(
                text("""
                    SELECT EXISTS (
                        SELECT 1 FROM memberships m JOIN users u ON u.id = m.user_id
                        WHERE m.tenant_id = :tenant AND u.email = :email
                        UNION ALL
                        SELECT 1 FROM tenant_invitations i
                        WHERE i.tenant_id = :tenant AND i.email = :email AND i.expires_at > now()
                            AND i.accepted_at IS NULL AND i.revoked_at IS NULL
                    )
                """),
                params,
            )
            if blocked:
                raise ApiError(409, "MEMBER_STATE_CONFLICT", "该成员已存在或已有待接受的邀请")
            row = await _one(
                connection,
                f"""
                INSERT INTO tenant_invitations
                    (id, tenant_id, issuer_tenant_id, issuer_id, email, role, token_hash, expires_at)
                VALUES (:id, :tenant, :issuer_tenant, :issuer, :email, :role, :hash,
                    now() + make_interval(hours => :hours))
                RETURNING {_INVITATION_FIELDS}
            """,
                {
                    **params,
                    "id": invitation_id,
                    "issuer_tenant": actor.tenant_id,
                    "issuer": actor.user_id,
                    "role": role.value,
                    "hash": self._invite_hash(token),
                    "hours": hours,
                },
            )
            await _audit(
                connection,
                tenant_id,
                actor.user_id,
                "invitation.create",
                invitation_id,
                {
                    "role": role,
                    "expires_in_hours": hours,
                },
            )
        return {"invitation": dict(row), "token": token}

    async def revoke_invitation(
        self,
        actor: SessionIdentity,
        tenant_id: UUID,
        invitation_id: UUID,
    ) -> dict:
        async with self._team_transaction(actor, tenant_id) as connection:
            params = {"tenant": tenant_id, "id": invitation_id}
            row = await _one(
                connection,
                f"""
                SELECT {_INVITATION_FIELDS} FROM tenant_invitations
                WHERE id = :id AND tenant_id = :tenant FOR UPDATE
            """,
                params,
            )
            if row is None:
                raise _invitation_unavailable()
            if not _assignable(actor.role, Role(row["role"])):
                raise _forbidden()
            if row["status"] == "accepted":
                raise ApiError(409, "INVITATION_STATE_CONFLICT", "已接受的邀请无法撤销")
            if row["status"] != "pending":
                return dict(row)
            revoked = await _one(
                connection,
                f"""
                UPDATE tenant_invitations SET revoked_at = now()
                WHERE id = :id AND tenant_id = :tenant
                RETURNING {_INVITATION_FIELDS}
            """,
                params,
            )
            await _audit(
                connection,
                tenant_id,
                actor.user_id,
                "invitation.revoke",
                invitation_id,
                {
                    "before": "pending",
                    "after": "revoked",
                },
            )
            return dict(revoked)

    async def _pending(self, connection: AsyncConnection, token: str, *, lock: bool = False):
        match = _INVITE_FORMAT.fullmatch(token)
        if not match:
            raise _invitation_unavailable()
        locking = " FOR UPDATE OF i" if lock else ""
        row = await _one(
            connection,
            """
            SELECT i.id, i.tenant_id, i.issuer_tenant_id, i.issuer_id,
                i.email, i.role, i.token_hash, t.slug
            FROM tenant_invitations i JOIN tenants t ON t.id = i.tenant_id
            WHERE i.id = :id AND i.accepted_at IS NULL AND i.revoked_at IS NULL
                AND i.expires_at > clock_timestamp() AND t.enabled
        """
            + locking,
            {"id": UUID(hex=match[1])},
        )
        if row is None or not hmac.compare_digest(row["token_hash"], self._invite_hash(token)):
            raise _invitation_unavailable()
        return row

    async def accept_invitation(
        self,
        token: str,
        password: str,
        check_attempt: Callable[[str], Awaitable[None]],
    ) -> dict:
        async with self.db.connect() as connection:
            pending = await self._pending(connection, token)
            account = await _one(
                connection,
                """
                SELECT id, password_hash, enabled FROM users WHERE email = :email
            """,
                {"email": pending["email"]},
            )
        await check_attempt(pending["email"])
        if account is None:
            password_hash = await self.sessions.hash_password(password)
        else:
            if not account["enabled"] or not account["password_hash"]:
                # 已存在的无密码账户只能由本机操作员初始化，跨租户邀请不得接管凭据。
                raise ApiError(409, "ACCOUNT_STATE_CONFLICT", "该账户需要部署操作员启用登录")
            password_hash = account["password_hash"]
            if not await self.sessions.check_password(password, password_hash):
                raise ApiError(401, "AUTH_REQUIRED", "账户密码无效")
        tenant_id = pending["tenant_id"]
        async with self.db.begin() as connection:
            await lock_platform_members(connection)
            await lock_tenant(connection, tenant_id)
            pending = await self._pending(connection, token, lock=True)
            role = Role(pending["role"])
            issuer_role = await connection.scalar(
                text("""
                    SELECT m.role FROM memberships m
                    JOIN users u ON u.id = m.user_id
                    JOIN tenants t ON t.id = m.tenant_id
                    WHERE m.tenant_id = :tenant AND m.user_id = :issuer
                        AND m.enabled AND u.enabled AND t.enabled AND u.password_hash IS NOT NULL
                """),
                {"tenant": pending["issuer_tenant_id"], "issuer": pending["issuer_id"]},
            )
            if (
                not issuer_role
                or not _assignable(Role(issuer_role), role)
                or (issuer_role != Role.PLATFORM_ADMIN and pending["issuer_tenant_id"] != tenant_id)
            ):
                raise _invitation_unavailable()
            if account is None:
                user_id = await connection.scalar(
                    text("""
                        INSERT INTO users (id, email, password_hash) VALUES (:id, :email, :password)
                        ON CONFLICT (email) DO NOTHING RETURNING id
                    """),
                    {"id": uuid4(), "email": pending["email"], "password": password_hash},
                )
            else:
                user_id = await connection.scalar(
                    text("""
                        SELECT id FROM users WHERE id = :id AND enabled AND password_hash = :password
                        FOR SHARE
                    """),
                    {"id": account["id"], "password": password_hash},
                )
            if user_id is None:
                raise ApiError(409, "ACCOUNT_STATE_CONFLICT", "账户状态已变更，请重试")
            member = await connection.scalar(
                text("""
                    INSERT INTO memberships (tenant_id, user_id, role)
                    VALUES (:tenant, :user, :role)
                    ON CONFLICT (tenant_id, user_id) DO NOTHING RETURNING user_id
                """),
                {"tenant": tenant_id, "user": user_id, "role": role.value},
            )
            if member is None:
                raise ApiError(409, "MEMBER_STATE_CONFLICT", "成员已存在")
            await connection.execute(
                text("UPDATE tenant_invitations SET accepted_at = now() WHERE id = :id"),
                {"id": pending["id"]},
            )
            await _audit(
                connection,
                tenant_id,
                user_id,
                "invitation.accept",
                pending["id"],
                {
                    "role": role,
                    "issuer_id": str(pending["issuer_id"]),
                },
                actor_type="invitation",
            )
        return {"tenant": pending["slug"], "email": pending["email"]}

    async def _active_routing(self, connection: AsyncConnection) -> RoutingVersion:
        row = await _one(
            connection,
            f"""
            SELECT {_ROUTING_FIELDS} FROM routing_versions ORDER BY version DESC LIMIT 1
        """,
            {},
        )
        return RoutingVersion.model_validate(row) if row is not None else DEFAULT_ROUTING

    async def active_routing(self) -> RoutingVersion:
        async with self.db.connect() as connection:
            return await self._active_routing(connection)

    async def routing_history(self, limit: int, cursor: int | None) -> dict:
        async with self.db.connect() as connection:
            active = await self._active_routing(connection)
            result = await connection.execute(
                text(f"""
                    SELECT {_ROUTING_FIELDS} FROM routing_versions
                    WHERE version <= :active AND (CAST(:cursor AS INTEGER) IS NULL OR version < :cursor)
                    ORDER BY version DESC LIMIT :limit
                """),
                {"active": active.version, "cursor": cursor, "limit": limit + 1},
            )
            items = [RoutingVersion.model_validate(row) for row in result.mappings()]
        if len(items) <= limit and (cursor is None or cursor > 0):
            items.append(DEFAULT_ROUTING)
        return {
            "active": active,
            "items": items[:limit],
            "next_cursor": items[limit - 1].version if len(items) > limit else None,
        }

    async def publish_routing(
        self,
        actor: SessionIdentity,
        expected_version: int,
        reason: str,
        *,
        policy: RoutingRules | None = None,
        rollback_of: int | None = None,
    ) -> RoutingVersion:
        if actor.role != Role.PLATFORM_ADMIN:
            raise _forbidden()
        async with self.db.begin() as connection:
            await self.sessions.assert_actor(connection, actor)
            await connection.execute(text("SELECT pg_advisory_xact_lock(1498139729)"))
            old = await self._active_routing(connection)
            if old.version != expected_version:
                raise ApiError(409, "ROUTING_VERSION_CONFLICT", "路由版本已变更，请重新预览")
            if rollback_of is not None:
                if rollback_of == old.version:
                    raise ApiError(409, "ROUTING_VERSION_CONFLICT", "目标版本已在使用")
                if rollback_of == 0:
                    policy = DEFAULT_ROUTING.policy
                else:
                    previous = await connection.scalar(
                        text("SELECT policy FROM routing_versions WHERE version = :version"),
                        {"version": rollback_of},
                    )
                    if previous is None:
                        raise ApiError(404, "ROUTING_VERSION_NOT_FOUND", "路由版本不存在")
                    policy = RoutingRules.model_validate(previous)
            if policy is None:
                raise ValueError("需要提供路由策略")
            row = await _one(
                connection,
                f"""
                INSERT INTO routing_versions (policy, actor_id, reason, rollback_of)
                VALUES (CAST(:policy AS JSONB), :actor, :reason, :rollback_of)
                RETURNING {_ROUTING_FIELDS}
            """,
                {
                    "policy": policy.model_dump_json(),
                    "actor": actor.user_id,
                    "reason": reason,
                    "rollback_of": rollback_of,
                },
            )
            version = RoutingVersion.model_validate(row)
            action = "routing.publish" if rollback_of is None else "routing.rollback"
            await _audit(
                connection,
                actor.tenant_id,
                actor.user_id,
                action,
                _ROUTING_AUDIT_TARGET,
                {
                    "before": {
                        "version": old.version,
                        "policy": old.policy.model_dump(mode="json"),
                    },
                    "after": {"version": version.version, "policy": policy.model_dump(mode="json")},
                    "rollback_of": rollback_of,
                    "reason": reason,
                },
            )
            return version

    @staticmethod
    def _cache_authorized(actor: SessionIdentity, tenant_id: UUID) -> bool:
        return actor.role == Role.PLATFORM_ADMIN or (
            actor.role == Role.OPERATIONS_ADMIN and actor.tenant_id == tenant_id
        )

    async def _cache_epochs(self, connection: AsyncConnection, tenant_id: UUID) -> dict[str, int]:
        rows = await connection.execute(
            text("""
                SELECT scope, epoch FROM search_cache_epochs
                WHERE tenant_id = :tenant LIMIT :limit
            """),
            {"tenant": tenant_id, "limit": len(CACHE_SCOPES)},
        )
        return dict(rows.tuples().all())

    async def cache_epochs(self, tenant_id: UUID | str) -> dict[str, int]:
        async with self.db.connect() as connection:
            return await self._cache_epochs(connection, UUID(str(tenant_id)))

    async def _cache_storage(self, tenant_id: UUID) -> dict:
        key_limit = 500
        selected = set()
        complete = False
        cursor = 0
        try:
            async with asyncio.timeout(1.5):
                for _ in range(32):
                    cursor, keys = await self.redis.scan(
                        cursor=cursor,
                        match=f"multisearch:search:v1:{tenant_id}:*",
                        count=100,
                    )
                    # SCAN 可能重复返回键，容量显示为测量到的下界。
                    selected.update(keys)
                    if len(selected) > key_limit:
                        selected = set(sorted(selected)[:key_limit])
                        break
                    if cursor == 0:
                        complete = True
                        break
                async with self.redis.pipeline(transaction=False) as pipeline:
                    for key in selected:
                        pipeline.memory_usage(key, samples=1)
                    sizes = await pipeline.execute()
        except (RedisError, OSError, TimeoutError):
            return {"keys": None, "bytes": None, "complete": False, "available": False}
        present = [size for size in sizes if size is not None]
        return {
            "keys": len(present),
            "bytes": sum(present),
            "complete": complete,
            "available": True,
        }

    async def cache_status(self, actor: SessionIdentity, tenant_id: UUID) -> dict:
        if not self._cache_authorized(actor, tenant_id):
            raise _forbidden()
        async with self.db.connect() as connection:
            await self._tenant(connection, tenant_id)
            epochs = await self._cache_epochs(connection, tenant_id)
            totals = await _one(
                connection,
                """
                SELECT count(*) AS search_requests, count(*) FILTER (WHERE cache_hit) AS cache_hits
                FROM usage_events
                WHERE tenant_id = :tenant AND created_at >= now() - interval '30 days'
            """,
                {"tenant": tenant_id},
            )
        requests, hits = totals["search_requests"], totals["cache_hits"]
        return {
            "tenant_id": tenant_id,
            "search_requests": requests,
            "cache_hits": hits,
            "hit_rate": hits / requests if requests else None,
            "window_days": 30,
            "epochs": {scope: epochs.get(scope, 0) for scope in CACHE_SCOPES},
            "storage": await self._cache_storage(tenant_id),
        }

    async def invalidate_cache(self, actor: SessionIdentity, tenant_id: UUID, scope: str) -> dict:
        if not self._cache_authorized(actor, tenant_id):
            raise _forbidden()
        if scope not in CACHE_SCOPES:
            raise ApiError(422, "INVALID_REQUEST", "缓存范围无效")
        async with self.db.begin() as connection:
            await lock_tenant(connection, tenant_id)
            await self.sessions.assert_actor(connection, actor)
            await self._tenant(connection, tenant_id)
            epoch = await connection.scalar(
                text("""
                    INSERT INTO search_cache_epochs (tenant_id, scope, epoch)
                    VALUES (:tenant, :scope, 1)
                    ON CONFLICT (tenant_id, scope) DO UPDATE SET epoch = search_cache_epochs.epoch + 1
                    RETURNING epoch
                """),
                {"tenant": tenant_id, "scope": scope},
            )
            await _audit(
                connection,
                tenant_id,
                actor.user_id,
                "cache.invalidate",
                tenant_id,
                {
                    "scope": scope,
                    "before": epoch - 1,
                    "after": epoch,
                },
            )
        return {"tenant_id": tenant_id, "type": scope, "epoch": epoch}
