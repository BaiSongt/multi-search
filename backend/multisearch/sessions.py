import asyncio
import hmac
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import scrypt
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .contracts import Role
from .errors import ApiError
from .settings import secret

_SESSION_FORMAT = re.compile(r"mss_([a-f0-9]{32})\.[A-Za-z0-9_-]{43}")
TENANT_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
SESSION_COOKIE = "multisearch_session"
_DUMMY_HASH = f"scrypt-v1${'0' * 32}${'0' * 64}"

_ACTIVE_SESSION = """
    FROM browser_sessions AS s
    JOIN tenants AS t ON t.id = s.tenant_id
    JOIN users AS u ON u.id = s.user_id
    JOIN memberships AS m ON (m.tenant_id, m.user_id) = (s.tenant_id, s.user_id)
    WHERE s.id = :id AND s.revoked_at IS NULL AND s.expires_at > now()
        AND t.enabled AND u.enabled AND m.enabled
"""


def _password_digest(password: str, salt: bytes) -> bytes:
    # scrypt 的固定成本保存在版本定义中，旧记录可在以后迁移。
    return scrypt(
        password.encode(), salt=salt, n=2**15, r=8, p=3, maxmem=64 * 1024 * 1024, dklen=32
    )


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"scrypt-v1${salt.hex()}${_password_digest(password, salt).hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        version, salt, digest = stored.split("$")
        if version == "scrypt-v1" and len(salt) == 32 and len(digest) == 64:
            return hmac.compare_digest(
                _password_digest(password, bytes.fromhex(salt)).hex(), digest
            )
    except ValueError:
        pass
    return False


def _invalid_session() -> ApiError:
    return ApiError(401, "AUTH_REQUIRED", "需要有效的登录会话")


@dataclass(frozen=True)
class SessionIdentity:
    tenant_id: UUID
    tenant: str
    user_id: UUID
    email: str
    role: Role
    session_id: UUID
    expires_at: datetime
    csrf_token: str = field(repr=False)

    def require_key_manager(self) -> None:
        if self.role not in {Role.DEVELOPER, Role.TENANT_ADMIN, Role.PLATFORM_ADMIN}:
            raise ApiError(403, "FORBIDDEN", "当前角色无权管理密钥")

    @property
    def manages_tenant_keys(self) -> bool:
        return self.role in {Role.TENANT_ADMIN, Role.PLATFORM_ADMIN}


class SessionStore:
    def __init__(self, db: AsyncEngine, pepper: str):
        self.db = db
        self.pepper = pepper.encode()
        self._password_workers = asyncio.Semaphore(2)

    def _digest(self, domain: str, raw: str) -> bytes:
        return hmac.digest(self.pepper, f"{domain}:{raw}".encode(), "sha256")

    def _identity(self, raw: str, row) -> SessionIdentity:
        return SessionIdentity(
            tenant_id=row["tenant_id"],
            tenant=row["tenant"],
            user_id=row["user_id"],
            email=row["email"],
            role=Role(row["role"]),
            session_id=row["id"],
            expires_at=row["expires_at"],
            csrf_token=self._digest("csrf", raw).hex(),
        )

    async def provision(
        self, tenant: str, email: str, role: Role, password: str, *, bootstrap: bool = False
    ) -> bool:
        if not re.fullmatch(TENANT_PATTERN, tenant):
            raise ValueError("租户标识格式无效")
        email = email.strip().lower()
        if len(email) > 254 or not re.fullmatch(EMAIL_PATTERN, email):
            raise ValueError("邮箱格式无效")
        if not 12 <= len(password) <= 128:
            raise ValueError("密码需要 12 至 128 个字符")
        password_hash = await asyncio.to_thread(_hash_password, password)
        async with self.db.begin() as connection:
            if bootstrap:
                # 引导仅创建首位平台管理员。串行化启动，避免副本重复引导。
                await connection.execute(text("SELECT pg_advisory_xact_lock(1498139728)"))
                present = await connection.scalar(
                    text("""
                    SELECT EXISTS (
                        SELECT 1 FROM memberships m JOIN users u ON m.user_id = u.id
                        JOIN tenants t ON t.id = m.tenant_id
                        WHERE m.role = 'platform_admin' AND u.password_hash IS NOT NULL
                            AND t.enabled AND u.enabled AND m.enabled
                    )
                """)
                )
                if present:
                    return False
            tenant_id = await connection.scalar(
                text("""
                    INSERT INTO tenants (id, slug) VALUES (:id, :tenant)
                    ON CONFLICT (slug) DO UPDATE SET slug = EXCLUDED.slug
                    WHERE tenants.enabled RETURNING id
                """),
                {"id": uuid4(), "tenant": tenant},
            )
            user_id = await connection.scalar(
                text("""
                    INSERT INTO users (id, email, password_hash) VALUES (:id, :email, :password_hash)
                    ON CONFLICT (email) DO UPDATE SET password_hash = EXCLUDED.password_hash
                    WHERE users.enabled AND users.password_hash IS NULL RETURNING id
                """),
                {"id": uuid4(), "email": email, "password_hash": password_hash},
            )
            if tenant_id is None or user_id is None:
                raise ValueError("租户或账户已存在凭据，或已被停用")
            changed = await connection.scalar(
                text("""
                    INSERT INTO memberships (tenant_id, user_id, role)
                    VALUES (:tenant_id, :user_id, :role)
                    ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = EXCLUDED.role
                    WHERE memberships.enabled RETURNING user_id
                """),
                {"tenant_id": tenant_id, "user_id": user_id, "role": role.value},
            )
            if changed is None:
                raise ValueError("租户成员已停用")
            await connection.execute(
                text("""
                    INSERT INTO audit_events (tenant_id, actor_type, action, target_id, details)
                    VALUES (:tenant_id, 'local_operator', 'user.provision', :user_id,
                            jsonb_build_object('role', CAST(:role AS TEXT)))
                """),
                {"tenant_id": tenant_id, "user_id": user_id, "role": role.value},
            )
        return True

    async def disable(self, tenant: str, email: str) -> bool:
        async with self.db.begin() as connection:
            params = {"tenant": tenant, "email": email}
            disabled = (
                (
                    await connection.execute(
                        text("""
                    UPDATE memberships m SET enabled = FALSE
                    FROM tenants t, users u
                    WHERE m.tenant_id = t.id AND m.user_id = u.id AND m.enabled
                        AND t.slug = :tenant AND u.email = :email
                    RETURNING m.tenant_id, m.user_id
                """),
                        params,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if disabled is None:
                return False
            await connection.execute(
                text("""
                INSERT INTO audit_events (tenant_id, actor_type, action, target_id)
                VALUES (:tenant_id, 'local_operator', 'user.disable', :user_id)
            """),
                dict(disabled),
            )
            return True

    async def _password_matches(self, password: str, stored: str) -> bool:
        if self._password_workers.locked():
            raise ApiError(429, "RATE_LIMITED", "登录服务繁忙", retryable=True, retry_after_ms=1000)
        async with self._password_workers:
            task = asyncio.create_task(asyncio.to_thread(_verify_password, password, stored))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # 密码计算完成后再释放内存占用名额。
                await task
                raise

    async def login(self, tenant: str, email: str, password: str) -> tuple[str, SessionIdentity]:
        params = {"tenant": tenant, "email": email}
        async with self.db.connect() as connection:
            credentials = (
                (
                    await connection.execute(
                        text("""
                    SELECT m.tenant_id, m.user_id, m.role, t.slug AS tenant, u.email, u.password_hash
                    FROM users u
                    JOIN memberships m ON m.user_id = u.id
                    JOIN tenants t ON t.id = m.tenant_id
                    WHERE u.email = :email AND t.slug = :tenant AND u.enabled AND t.enabled
                        AND m.enabled AND u.password_hash IS NOT NULL
                """),
                        params,
                    )
                )
                .mappings()
                .one_or_none()
            )
        stored = credentials["password_hash"] if credentials is not None else _DUMMY_HASH
        valid = await self._password_matches(password, stored)
        if not valid or credentials is None:
            raise ApiError(401, "AUTH_REQUIRED", "租户、邮箱或密码无效")
        async with self.db.begin() as connection:
            current = (
                (
                    await connection.execute(
                        text("""
                    SELECT m.tenant_id, m.user_id, m.role, t.slug AS tenant, u.email
                    FROM memberships m
                    JOIN users u ON u.id = m.user_id
                    JOIN tenants t ON t.id = m.tenant_id
                    WHERE (m.tenant_id, m.user_id) = (:tenant_id, :user_id)
                        AND u.password_hash = :password_hash AND m.enabled AND u.enabled AND t.enabled
                    FOR SHARE OF m, u, t
                """),
                        {
                            "tenant_id": credentials["tenant_id"],
                            "user_id": credentials["user_id"],
                            "password_hash": stored,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current is None:
                raise ApiError(401, "AUTH_REQUIRED", "租户、邮箱或密码无效")
            await connection.execute(
                text("""
                DELETE FROM browser_sessions WHERE id IN (
                    SELECT id FROM browser_sessions WHERE absolute_expires_at <= now()
                    ORDER BY absolute_expires_at LIMIT 100 FOR UPDATE SKIP LOCKED
                )
            """)
            )
            raw, actor = await self._open(connection, current)
            await self._audit(connection, actor, "session.login")
        return raw, actor

    async def _open(self, connection: AsyncConnection, row) -> tuple[str, SessionIdentity]:
        session_id = uuid4()
        raw = f"mss_{session_id.hex}.{secrets.token_urlsafe(32)}"
        expires_at = await connection.scalar(
            text("""
                INSERT INTO browser_sessions
                    (id, tenant_id, user_id, secret_hash, expires_at, absolute_expires_at)
                SELECT :id, :tenant_id, :user_id, :hash,
                       least(now() + interval '8 hours', deadline), deadline
                FROM (SELECT coalesce(:absolute, now() + interval '7 days') AS deadline) cutoff
                RETURNING expires_at
            """),
            {
                "id": session_id,
                "tenant_id": row["tenant_id"],
                "user_id": row["user_id"],
                "hash": self._digest("session", raw),
                "absolute": row.get("absolute_expires_at"),
            },
        )
        return raw, self._identity(raw, {**row, "id": session_id, "expires_at": expires_at})

    async def authenticate(self, raw: str) -> SessionIdentity:
        match = _SESSION_FORMAT.fullmatch(raw)
        if match is None:
            raise _invalid_session()
        async with self.db.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            """
                    SELECT s.id, s.tenant_id, s.user_id, s.secret_hash, s.expires_at,
                           m.role, t.slug AS tenant, u.email
                """
                            + _ACTIVE_SESSION
                        ),
                        {"id": UUID(hex=match[1])},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None or not hmac.compare_digest(row["secret_hash"], self._digest("session", raw)):
            raise _invalid_session()
        return self._identity(raw, row)

    async def assert_actor(
        self, connection: AsyncConnection, actor: SessionIdentity, *, rotate: bool = False
    ):
        lock = " FOR UPDATE OF s FOR SHARE OF m, u, t" if rotate else " FOR SHARE OF s, m, u, t"
        row = (
            (
                await connection.execute(
                    text(
                        """
                SELECT s.id, s.tenant_id, s.user_id, s.absolute_expires_at,
                       m.role, t.slug AS tenant, u.email
            """
                        + _ACTIVE_SESSION
                        + lock
                    ),
                    {"id": actor.session_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise _invalid_session()
        if (row["tenant_id"], row["user_id"], row["role"]) != (
            actor.tenant_id,
            actor.user_id,
            actor.role,
        ):
            raise ApiError(403, "FORBIDDEN", "账户角色已变更，请重试")
        return row

    async def refresh(self, actor: SessionIdentity) -> tuple[str, SessionIdentity]:
        async with self.db.begin() as connection:
            row = await self.assert_actor(connection, actor, rotate=True)
            await self._close(connection, actor)
            raw, refreshed = await self._open(connection, row)
            await self._audit(connection, refreshed, "session.refresh")
        return raw, refreshed

    async def logout(self, actor: SessionIdentity) -> None:
        async with self.db.begin() as connection:
            await self.assert_actor(connection, actor, rotate=True)
            await self._close(connection, actor)
            await self._audit(connection, actor, "session.logout")

    async def _close(self, connection: AsyncConnection, actor: SessionIdentity) -> None:
        await connection.execute(
            text("UPDATE browser_sessions SET revoked_at = now() WHERE id = :id"),
            {"id": actor.session_id},
        )

    async def _audit(
        self, connection: AsyncConnection, actor: SessionIdentity, action: str
    ) -> None:
        await connection.execute(
            text("""
                INSERT INTO audit_events (tenant_id, actor_type, actor_id, action, target_id)
                VALUES (:tenant_id, 'session', :user_id, :action, :id)
            """),
            {
                "tenant_id": actor.tenant_id,
                "user_id": actor.user_id,
                "action": action,
                "id": actor.session_id,
            },
        )


async def bootstrap_identity(store: SessionStore) -> bool:
    email = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "")
    password = secret("BOOTSTRAP_ADMIN_PASSWORD")
    if not (email or password):
        return False
    if not email or not password:
        raise ValueError("管理员引导需要同时配置邮箱和密码")
    return await store.provision(
        os.getenv("BOOTSTRAP_ADMIN_TENANT", "platform"),
        email,
        Role.PLATFORM_ADMIN,
        password,
        bootstrap=True,
    )
