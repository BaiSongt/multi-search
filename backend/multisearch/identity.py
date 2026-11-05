import hmac
import re
import secrets
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .contracts import Modality, Role
from .errors import ApiError

KEY_FORMAT = re.compile(r"msk_([a-f0-9]{32})\.[A-Za-z0-9_-]{43}")
SEARCH_SCOPES = ("search:text", "search:image")


def key_hash(raw: str, pepper: str) -> bytes:
    return hmac.digest(pepper.encode(), raw.encode(), "sha256")


@dataclass(frozen=True)
class MachineIdentity:
    key_id: UUID
    tenant_id: UUID
    owner_id: UUID
    scopes: frozenset[str]

    def require_search(self, modalities: list[Modality]) -> None:
        needed = {f"search:{modality}" for modality in modalities if modality != Modality.VIDEO}
        if not needed <= self.scopes:
            raise ApiError(403, "FORBIDDEN", "API Key 缺少请求模态所需的权限")


class IdentityStore:
    def __init__(self, db: AsyncEngine, pepper: str):
        self.db = db
        self.pepper = pepper

    async def authenticate(self, raw: str) -> MachineIdentity:
        match = KEY_FORMAT.fullmatch(raw)
        if match is None:
            raise ApiError(401, "AUTH_REQUIRED", "需要有效的 API Key")
        key_id = UUID(hex=match[1])
        async with self.db.connect() as connection:
            result = await connection.execute(
                text("""
                SELECT k.tenant_id, k.owner_id, k.secret_hash, k.scopes
                FROM api_keys AS k
                JOIN tenants AS t ON k.tenant_id = t.id
                JOIN users AS u ON k.owner_id = u.id
                JOIN memberships AS m ON (m.tenant_id, m.user_id) = (k.tenant_id, k.owner_id)
                WHERE k.id = :id AND k.revoked_at IS NULL AND k.expires_at > now()
                    AND t.enabled AND u.enabled AND m.enabled
            """),
                {"id": key_id},
            )
            row = result.mappings().one_or_none()
        if row is None or not hmac.compare_digest(row["secret_hash"], key_hash(raw, self.pepper)):
            raise ApiError(401, "AUTH_REQUIRED", "需要有效的 API Key")
        return MachineIdentity(key_id, row["tenant_id"], row["owner_id"], frozenset(row["scopes"]))

    async def issue(self, tenant: str, email: str, scopes: list[str], ttl_hours: int) -> str:
        key_id = uuid4()
        raw = f"msk_{key_id.hex}.{secrets.token_urlsafe(32)}"
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
            await connection.execute(
                text("""
                INSERT INTO api_keys (id, tenant_id, owner_id, secret_hash, prefix, suffix, scopes, expires_at)
                VALUES (:id, :tenant_id, :owner_id, :secret_hash, :prefix, :suffix, :scopes,
                        now() + make_interval(hours => :hours))
            """),
                {
                    **identity,
                    "id": key_id,
                    "secret_hash": key_hash(raw, self.pepper),
                    "prefix": raw[:12],
                    "suffix": raw[-4:],
                    "scopes": scopes,
                    "hours": ttl_hours,
                },
            )
            await connection.execute(
                text("""
                INSERT INTO audit_events (tenant_id, actor_type, action, target_id, details)
                VALUES (:tenant_id, 'local_operator', 'key.issue', :id,
                        jsonb_build_object('scopes', CAST(:scopes AS TEXT[]), 'ttl_hours', CAST(:hours AS INTEGER)))
            """),
                {"tenant_id": tenant_id, "id": key_id, "scopes": scopes, "hours": ttl_hours},
            )
        return raw

    async def revoke(self, key_id: UUID) -> bool:
        async with self.db.begin() as connection:
            tenant_id = await connection.scalar(
                text("""
                UPDATE api_keys SET revoked_at = now() WHERE id = :id AND revoked_at IS NULL
                RETURNING tenant_id
            """),
                {"id": key_id},
            )
            if tenant_id is None:
                return False
            await connection.execute(
                text("""
                INSERT INTO audit_events (tenant_id, actor_type, action, target_id)
                VALUES (:tenant_id, 'local_operator', 'key.revoke', :id)
            """),
                {"tenant_id": tenant_id, "id": key_id},
            )
        return True
