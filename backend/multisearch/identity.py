import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from ipaddress import IPv4Network, IPv6Network, ip_network
from math import ceil
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .client_address import parse_client_address
from .contracts import Modality, Role
from .errors import ApiError
from .policy import lock_tenant, tenant_quota
from .sessions import SessionIdentity, SessionStore

KEY_FORMAT = re.compile(r"msk_([a-f0-9]{32})\.[A-Za-z0-9_-]{43}")
KEY_SCOPES = ("search:text", "search:image", "fetch", "research")
MAX_KEY_NETWORKS = 32
_KEY_COLUMNS = """
    id, owner_id, name, environment, prefix, suffix, scopes, limits, created_at,
    expires_at, last_used_at, revoked_at, rotation_expires_at, ip_allowlist::TEXT[] AS ip_allowlist,
    CASE
        WHEN revoked_at IS NOT NULL THEN 'revoked'
        WHEN coalesce(rotation_expires_at, expires_at) <= now() THEN 'expired'
        WHEN rotation_expires_at IS NOT NULL THEN 'rotating'
        ELSE 'active'
    END AS status
"""
_KEY_ACCESS = """
    tenant_id = :tenant_id AND (:all_keys OR owner_id = :user_id)
"""


def key_hash(raw: str, pepper: str) -> bytes:
    return hmac.digest(pepper.encode(), raw.encode(), "sha256")


def normalize_key_allowlist(values: list[str]) -> list[str]:
    if len(values) > MAX_KEY_NETWORKS:
        raise ValueError("IP 白名单最多包含 32 项")
    networks = []
    for value in values:
        _, separator, prefix = value.partition("/")
        if "%" in value or (separator and not re.fullmatch(r"[0-9]{1,3}", prefix)):
            raise ValueError("IP 白名单需要 IPv4、IPv6 地址或 CIDR")
        try:
            network = ip_network(value)
        except ValueError:
            raise ValueError("IP 白名单需要有效地址或主机位为零的 CIDR") from None
        if isinstance(network, IPv6Network) and network.prefixlen >= 96:
            mapped = network.network_address.ipv4_mapped
            if mapped is not None:
                network = IPv4Network((mapped, network.prefixlen - 96))
        networks.append(str(network))
    if len(networks) != len(set(networks)):
        raise ValueError("IP 白名单不得包含重复地址或网络")
    return networks


@dataclass(frozen=True)
class MachineIdentity:
    key_id: UUID
    tenant_id: UUID
    owner_id: UUID
    scopes: frozenset[str]
    limits: dict[str, int] = field(default_factory=dict)

    def require_search(self, modalities: list[Modality]) -> None:
        needed = {f"search:{modality}" for modality in modalities if modality != Modality.VIDEO}
        if not needed <= self.scopes:
            raise ApiError(403, "FORBIDDEN", "API Key 缺少请求模态所需的权限")


class KeyView(BaseModel):
    id: UUID
    owner_id: UUID
    name: str
    environment: Literal["dev", "test", "prod"]
    prefix: str
    suffix: str
    scopes: list[str]
    limits: dict[str, int]
    ip_allowlist: list[str] = Field(default_factory=list)
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    rotation_expires_at: datetime | None
    status: Literal["active", "expired", "revoked", "rotating"]


class IdentityStore:
    def __init__(self, db: AsyncEngine, pepper: str):
        self.db = db
        self.pepper = pepper
        self.sessions = SessionStore(db, pepper)

    async def authenticate(self, raw: str, *, peer: str | None = None) -> MachineIdentity:
        match = KEY_FORMAT.fullmatch(raw)
        if match is None:
            raise ApiError(401, "AUTH_REQUIRED", "需要有效的 API Key")
        key_id = UUID(hex=match[1])
        address = parse_client_address(peer)
        async with self.db.connect() as connection:
            result = await connection.execute(
                text("""
                SELECT k.tenant_id, k.owner_id, k.secret_hash, k.scopes, k.limits,
                    cardinality(k.ip_allowlist) = 0 OR
                    CAST(CAST(:peer AS TEXT) AS INET) <<= ANY(k.ip_allowlist) AS peer_allowed
                FROM api_keys AS k
                JOIN tenants AS t ON k.tenant_id = t.id
                JOIN users AS u ON k.owner_id = u.id
                JOIN memberships AS m ON (m.tenant_id, m.user_id) = (k.tenant_id, k.owner_id)
                WHERE k.id = :id AND k.revoked_at IS NULL
                    AND coalesce(k.rotation_expires_at, k.expires_at) > now()
                    AND t.enabled AND u.enabled AND m.enabled
            """),
                {"id": key_id, "peer": str(address) if address is not None else None},
            )
            row = result.mappings().one_or_none()
        if (
            row is None
            or not hmac.compare_digest(row["secret_hash"], key_hash(raw, self.pepper))
            or not row["peer_allowed"]
        ):
            raise ApiError(401, "AUTH_REQUIRED", "需要有效的 API Key")
        return MachineIdentity(
            key_id, row["tenant_id"], row["owner_id"], frozenset(row["scopes"]), row["limits"]
        )

    async def issue(self, tenant: str, email: str, scopes: list[str], ttl_hours: int) -> str:
        async with self.db.begin() as connection:
            tenant_id = await connection.scalar(
                text("""
                INSERT INTO tenants (id, slug) VALUES (:id, :slug)
                ON CONFLICT (slug) DO UPDATE SET slug = EXCLUDED.slug
                WHERE tenants.enabled RETURNING id
            """),
                {"id": uuid4(), "slug": tenant},
            )
            owner_id = await connection.scalar(
                text("""
                INSERT INTO users (id, email) VALUES (:id, :email)
                ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
                WHERE users.enabled RETURNING id
            """),
                {"id": uuid4(), "email": email},
            )
            if tenant_id is None or owner_id is None:
                raise ValueError("租户或用户已停用")
            identity = {"tenant_id": tenant_id, "owner_id": owner_id}
            await connection.execute(
                text("""
                INSERT INTO memberships (tenant_id, user_id, role) VALUES (:tenant_id, :owner_id, :role)
                ON CONFLICT (tenant_id, user_id) DO NOTHING
            """),
                {**identity, "role": Role.DEVELOPER.value},
            )
            active = await connection.scalar(
                text("""
                SELECT enabled FROM memberships WHERE tenant_id = :tenant_id AND user_id = :owner_id
                FOR SHARE
            """),
                identity,
            )
            if not active:
                raise ValueError("租户成员已停用")
            key, raw = await self._insert(
                connection, tenant_id, owner_id, "CLI key", "dev", scopes, ttl_hours, {}
            )
            await self._audit(
                connection,
                tenant_id,
                None,
                "key.issue",
                key.id,
                {"scopes": scopes, "ttl_hours": ttl_hours},
            )
        return raw

    async def revoke(self, key_id: UUID) -> bool:
        async with self.db.begin() as connection:
            tenant_id = await connection.scalar(
                text("SELECT tenant_id FROM api_keys WHERE id = :id"), {"id": key_id}
            )
            if tenant_id is None:
                return False
            await lock_tenant(connection, tenant_id)
            changed = await connection.scalar(
                text("""
                UPDATE api_keys SET revoked_at = now() WHERE id = :id AND revoked_at IS NULL
                RETURNING id
            """),
                {"id": key_id},
            )
            if changed is None:
                return False
            await self._audit(connection, tenant_id, None, "key.revoke", key_id, {})
        return True

    async def _insert(
        self,
        connection: AsyncConnection,
        tenant_id: UUID,
        owner_id: UUID,
        name: str,
        environment: str,
        scopes: list[str],
        ttl_hours: int,
        limits: dict[str, int],
        *,
        ip_allowlist: list[str] | None = None,
    ) -> tuple[KeyView, str]:
        key_id = uuid4()
        raw = f"msk_{key_id.hex}.{secrets.token_urlsafe(32)}"
        result = await connection.execute(
            text(f"""
                INSERT INTO api_keys
                    (id, tenant_id, owner_id, name, environment, secret_hash, prefix, suffix,
                     scopes, limits, ip_allowlist, expires_at)
                VALUES (:id, :tenant_id, :owner_id, :name, :environment, :secret_hash,
                        :prefix, :suffix, :scopes, CAST(:limits AS JSONB),
                        CAST(CAST(:ip_allowlist AS TEXT[]) AS CIDR[]),
                        now() + make_interval(hours => :hours))
                RETURNING {_KEY_COLUMNS}
            """),
            {
                "tenant_id": tenant_id,
                "owner_id": owner_id,
                "id": key_id,
                "name": name,
                "environment": environment,
                "secret_hash": key_hash(raw, self.pepper),
                "prefix": raw[:12],
                "suffix": raw[-4:],
                "scopes": scopes,
                "limits": json.dumps(limits),
                "ip_allowlist": ip_allowlist if ip_allowlist is not None else [],
                "hours": ttl_hours,
            },
        )
        return KeyView.model_validate(result.mappings().one()), raw

    async def _audit(
        self,
        connection: AsyncConnection,
        tenant_id: UUID,
        actor_id: UUID | None,
        action: str,
        key_id: UUID,
        details: dict,
    ) -> None:
        await connection.execute(
            text("""
                INSERT INTO audit_events (tenant_id, actor_type, actor_id, action, target_id, details)
                VALUES (:tenant_id, :actor_type, :actor_id, :action, :id, CAST(:details AS JSONB))
            """),
            {
                "tenant_id": tenant_id,
                "actor_type": "session" if actor_id else "local_operator",
                "actor_id": actor_id,
                "action": action,
                "id": key_id,
                "details": json.dumps(details, default=str),
            },
        )

    @staticmethod
    def _key_params(actor: SessionIdentity) -> dict:
        actor.require_key_manager()
        return {
            "tenant_id": actor.tenant_id,
            "user_id": actor.user_id,
            "all_keys": actor.manages_tenant_keys,
        }

    async def _visible_key(
        self, connection: AsyncConnection, params: dict, key_id: UUID, *, lock: bool = False
    ) -> KeyView:
        clause = " FOR UPDATE" if lock else ""
        row = (
            (
                await connection.execute(
                    text(
                        f"SELECT {_KEY_COLUMNS} FROM api_keys WHERE {_KEY_ACCESS} AND id = :id{clause}"
                    ),
                    {**params, "id": key_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ApiError(404, "KEY_NOT_FOUND", "密钥不存在")
        return KeyView.model_validate(row)

    async def list_keys(
        self, actor: SessionIdentity, limit: int, cursor: UUID | None
    ) -> tuple[list[KeyView], UUID | None]:
        params = self._key_params(actor)
        async with self.db.connect() as connection:
            next_page = ""
            if cursor is not None:
                previous = await self._visible_key(connection, params, cursor)
                params.update(cursor_at=previous.created_at, cursor_id=previous.id)
                next_page = " AND (created_at, id) < (:cursor_at, :cursor_id)"
            result = await connection.execute(
                text(f"""
                    SELECT {_KEY_COLUMNS} FROM api_keys
                    WHERE {_KEY_ACCESS}{next_page}
                    ORDER BY created_at DESC, id DESC LIMIT :limit
                """),
                {**params, "limit": limit + 1},
            )
            rows = [KeyView.model_validate(row) for row in result.mappings()]
        return rows[:limit], rows[limit - 1].id if len(rows) > limit else None

    async def create_key(
        self,
        actor: SessionIdentity,
        name: str,
        environment: str,
        scopes: list[str],
        ttl_hours: int,
        limits: dict[str, int],
        max_concurrent: int,
        *,
        ip_allowlist: list[str] | None = None,
    ) -> tuple[KeyView, str]:
        actor.require_key_manager()
        async with self.db.begin() as connection:
            await lock_tenant(connection, actor.tenant_id)
            await self.sessions.assert_actor(connection, actor)
            policy = await tenant_quota(connection, actor.tenant_id, max_concurrent)
            if any(value > policy[name] for name, value in limits.items()):
                raise ApiError(422, "INVALID_REQUEST", "密钥额度不得超过租户额度")
            key, raw = await self._insert(
                connection,
                actor.tenant_id,
                actor.user_id,
                name,
                environment,
                scopes,
                ttl_hours,
                limits,
                ip_allowlist=ip_allowlist,
            )
            await self._audit(
                connection,
                actor.tenant_id,
                actor.user_id,
                "key.issue",
                key.id,
                {
                    "scopes": scopes,
                    "ttl_hours": ttl_hours,
                    "environment": environment,
                    "limits": limits,
                    "ip_allowlist_count": len(key.ip_allowlist),
                },
            )
        return key, raw

    async def revoke_key(self, actor: SessionIdentity, key_id: UUID) -> KeyView:
        params = self._key_params(actor)
        async with self.db.begin() as connection:
            await lock_tenant(connection, actor.tenant_id)
            await self.sessions.assert_actor(connection, actor)
            key = await self._visible_key(connection, params, key_id, lock=True)
            if key.revoked_at is not None:
                return key
            result = await connection.execute(
                text(
                    f"UPDATE api_keys SET revoked_at = now() WHERE id = :id RETURNING {_KEY_COLUMNS}"
                ),
                {"id": key_id},
            )
            await self._audit(connection, actor.tenant_id, actor.user_id, "key.revoke", key_id, {})
            return KeyView.model_validate(result.mappings().one())

    async def rotate_key(
        self,
        actor: SessionIdentity,
        key_id: UUID,
        grace_seconds: int,
        ttl_hours: int | None,
        max_concurrent: int,
    ) -> tuple[KeyView, str]:
        params = self._key_params(actor)
        async with self.db.begin() as connection:
            await lock_tenant(connection, actor.tenant_id)
            await self.sessions.assert_actor(connection, actor)
            old = await self._visible_key(connection, params, key_id, lock=True)
            if old.status != "active":
                raise ApiError(409, "KEY_STATE_CONFLICT", "只能轮换有效且未轮换的密钥")
            if ttl_hours is None:
                ttl_hours = min(
                    720, max(1, ceil((old.expires_at - old.created_at).total_seconds() / 3600))
                )
            policy = await tenant_quota(connection, actor.tenant_id, max_concurrent)
            # 租户额度降低后仍允许轮换，继承值收紧到当前额度。
            limits = {name: min(value, policy[name]) for name, value in old.limits.items()}
            new, raw = await self._insert(
                connection,
                actor.tenant_id,
                old.owner_id,
                old.name,
                old.environment,
                old.scopes,
                ttl_hours,
                limits,
                ip_allowlist=old.ip_allowlist,
            )
            if grace_seconds:
                await connection.execute(
                    text("""
                        UPDATE api_keys SET rotation_expires_at =
                            least(expires_at, now() + make_interval(secs => :grace))
                        WHERE id = :id
                    """),
                    {"id": key_id, "grace": grace_seconds},
                )
            else:
                await connection.execute(
                    text("UPDATE api_keys SET revoked_at = now() WHERE id = :id"),
                    {"id": key_id},
                )
            await self._audit(
                connection,
                actor.tenant_id,
                actor.user_id,
                "key.rotate",
                key_id,
                {
                    "replacement_id": new.id,
                    "grace_seconds": grace_seconds,
                    "limits": limits,
                    "ip_allowlist_count": len(new.ip_allowlist),
                },
            )
        return new, raw
