import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, Query, Request
from pydantic import Field, StrictInt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from .auth import SessionUser, SessionWrite
from .contracts import InputModel, Modality, Mode, Role, Source
from .errors import ApiError
from .policy import ProviderPolicy, effective_quota, lock_tenant, tenant_quota, validate_limits
from .reporting import ReportScope
from .sessions import lock_platform_members, require_platform_admin_after

router = APIRouter(prefix="/api/v1")
OPS = {Role.PLATFORM_ADMIN, Role.OPERATIONS_ADMIN}
AUDIT = OPS | {Role.AUDITOR, Role.TENANT_ADMIN}
PageSize = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=256)]
RequestStatus = Literal["running", "completed", "partial", "failed", "cancelled"]


class ProviderPatch(InputModel):
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=-100, le=100)
    requests_per_minute: int | None = Field(default=None, ge=1, le=120)


class QuotaPatch(InputModel):
    quota: dict[str, StrictInt] = Field(max_length=20)


class TenantPatch(InputModel):
    enabled: bool


class TenantCreate(InputModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")


def _require(role: Role, allowed: set[Role]) -> None:
    if role not in allowed:
        raise ApiError(403, "FORBIDDEN", "当前角色无权执行此操作")


def _patch(model: InputModel) -> dict:
    fields = model.model_dump(exclude_none=True)
    if not fields:
        raise ApiError(422, "INVALID_REQUEST", "需要提供要修改的字段")
    return fields


async def _audit(
    connection: AsyncConnection,
    *,
    actor_id: UUID,
    tenant_id: UUID,
    action: str,
    target_id: UUID,
    before: dict,
    after: dict,
    provider_id: str | None = None,
) -> None:
    details = {"before": before, "after": after}
    if provider_id is not None:
        details["provider_id"] = provider_id
    await connection.execute(
        text("""
        INSERT INTO audit_events (tenant_id, actor_type, actor_id, action, target_id, details)
        VALUES (:tenant, 'session', :actor, :action, :target, CAST(:details AS JSONB))
    """),
        {
            "tenant": tenant_id,
            "actor": actor_id,
            "action": action,
            "target": target_id,
            "details": json.dumps(details, ensure_ascii=False),
        },
    )


@router.get("/usage")
async def usage(request: Request, actor: SessionUser):
    _require(actor.role, OPS | {Role.DEVELOPER, Role.TENANT_ADMIN, Role.AUDITOR})
    owner = actor.user_id if actor.role == Role.DEVELOPER else None
    return await request.app.state.reports.usage(ReportScope(actor.tenant_id, owner))


@router.get("/admin/overview")
async def overview(request: Request, actor: SessionUser):
    _require(actor.role, OPS)
    tenant = None if actor.role == Role.PLATFORM_ADMIN else actor.tenant_id
    return await request.app.state.reports.overview(ReportScope(tenant))


@router.get("/admin/providers")
async def providers(request: Request, actor: SessionUser):
    _require(actor.role, OPS)
    metrics = await request.app.state.reports.providers()
    return {
        "providers": [
            {
                **provider,
                "usage": metrics.get(
                    provider["provider_id"],
                    {
                        "runs": 0,
                        "errors": 0,
                        "rate_limited": 0,
                        "p50_ms": None,
                        "p95_ms": None,
                    },
                ),
            }
            for provider in request.app.state.search.provider_status()
        ]
    }


@router.patch("/admin/providers/{provider_id}")
async def update_provider(
    provider_id: str, body: ProviderPatch, request: Request, actor: SessionWrite
):
    _require(actor.role, {Role.PLATFORM_ADMIN})
    service = request.app.state.search
    if provider_id not in service.provider_policies:
        raise ApiError(404, "PROVIDER_NOT_FOUND", "Provider 不存在")
    changes = _patch(body)
    async with request.app.state.db.begin() as connection:
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:provider, 174))"),
            {"provider": provider_id},
        )
        row = (
            (
                await connection.execute(
                    text("""
            SELECT enabled, priority, requests_per_minute FROM provider_policies WHERE provider_id = :id
        """),
                    {"id": provider_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        old = dict(row) if row else {"enabled": True, "priority": 0, "requests_per_minute": 120}
        updated = {**old, **changes}
        await connection.execute(
            text("""
            INSERT INTO provider_policies (provider_id, enabled, priority, requests_per_minute)
            VALUES (:id, :enabled, :priority, :requests_per_minute)
            ON CONFLICT (provider_id) DO UPDATE SET enabled = EXCLUDED.enabled,
                priority = EXCLUDED.priority, requests_per_minute = EXCLUDED.requests_per_minute,
                updated_at = now()
        """),
            {"id": provider_id, **updated},
        )
        await _audit(
            connection,
            actor_id=actor.user_id,
            tenant_id=actor.tenant_id,
            action="provider.update",
            target_id=uuid5(NAMESPACE_URL, f"multisearch:provider:{provider_id}"),
            before=old,
            after=updated,
            provider_id=provider_id,
        )
    service.provider_policies[provider_id] = ProviderPolicy(**updated)
    return {
        "provider": next(p for p in service.provider_status() if p["provider_id"] == provider_id)
    }


@router.get("/admin/requests")
async def requests(
    request: Request,
    actor: SessionUser,
    limit: PageSize = 30,
    cursor: Cursor = None,
    tenant_id: UUID | None = None,
    status: RequestStatus | None = None,
    mode: Mode | None = None,
    modality: Modality | None = None,
    source_type: Source | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
):
    _require(actor.role, OPS)
    if actor.role != Role.PLATFORM_ADMIN:
        if tenant_id is not None and tenant_id != actor.tenant_id:
            raise ApiError(403, "FORBIDDEN", "无权查看其他租户的请求")
        tenant_id = actor.tenant_id
    if (
        (since and since.tzinfo is None)
        or (until and until.tzinfo is None)
        or (since and until and since >= until)
    ):
        raise ApiError(422, "INVALID_REQUEST", "时间范围需要带时区且起点早于终点")
    return await request.app.state.reports.requests(
        ReportScope(tenant_id),
        limit,
        cursor,
        status=status,
        mode=mode,
        modality=modality,
        source_type=source_type,
        since=since,
        until=until,
    )


@router.get("/admin/tenants")
async def tenants(
    request: Request, actor: SessionUser, limit: PageSize = 30, cursor: Cursor = None
):
    _require(actor.role, {Role.PLATFORM_ADMIN})
    async with request.app.state.db.connect() as connection:
        result = await connection.execute(
            text("""
            SELECT t.id, t.slug, t.enabled, COALESCE(q.quota, '{}'::JSONB) AS quota,
                (SELECT COUNT(*) FROM usage_events u WHERE u.tenant_id = t.id AND
                    u.created_at >= now() - interval '30 days') AS search_requests
            FROM tenants t LEFT JOIN quota_policies q ON q.tenant_id = t.id
            WHERE CAST(:cursor AS TEXT) IS NULL OR t.slug > :cursor
            ORDER BY t.slug LIMIT :limit
        """),
            {"cursor": cursor, "limit": limit + 1},
        )
        items = [dict(row) for row in result.mappings()]
    for item in items:
        item["quota"] = effective_quota(item["quota"], request.app.state.settings.max_concurrent)
    return {
        "items": items[:limit],
        "next_cursor": items[limit - 1]["slug"] if len(items) > limit else None,
    }


@router.post("/admin/tenants", status_code=201)
async def create_tenant(body: TenantCreate, request: Request, actor: SessionWrite):
    _require(actor.role, {Role.PLATFORM_ADMIN})
    async with request.app.state.db.begin() as connection:
        tenant_id = await connection.scalar(
            text("""
            INSERT INTO tenants (id, slug) VALUES (:id, :slug)
            ON CONFLICT (slug) DO NOTHING RETURNING id
        """),
            {"id": uuid4(), "slug": body.slug},
        )
        if tenant_id is None:
            raise ApiError(409, "TENANT_STATE_CONFLICT", "租户名称已存在")
        await _audit(
            connection,
            actor_id=actor.user_id,
            tenant_id=tenant_id,
            action="tenant.create",
            target_id=tenant_id,
            before={},
            after={"slug": body.slug, "enabled": True},
        )
    return {
        "id": tenant_id,
        "slug": body.slug,
        "enabled": True,
        "search_requests": 0,
        "quota": effective_quota({}, request.app.state.settings.max_concurrent),
    }


@router.patch("/admin/tenants/{tenant_id}")
async def update_tenant(tenant_id: UUID, body: TenantPatch, request: Request, actor: SessionWrite):
    _require(actor.role, {Role.PLATFORM_ADMIN})
    if not body.enabled and tenant_id == actor.tenant_id:
        raise ApiError(409, "TENANT_STATE_CONFLICT", "不能停用当前登录所在的租户")
    async with request.app.state.db.begin() as connection:
        await lock_platform_members(connection)
        await lock_tenant(connection, tenant_id)
        await request.app.state.identity.sessions.assert_actor(connection, actor)
        old = await connection.scalar(
            text("SELECT enabled FROM tenants WHERE id = :id"), {"id": tenant_id}
        )
        if old is None:
            raise ApiError(404, "TENANT_NOT_FOUND", "租户不存在")
        await connection.execute(
            text("UPDATE tenants SET enabled = :enabled WHERE id = :id"),
            {"id": tenant_id, "enabled": body.enabled},
        )
        if not body.enabled:
            await require_platform_admin_after(connection)
        await _audit(
            connection,
            actor_id=actor.user_id,
            tenant_id=tenant_id,
            action="tenant.update",
            target_id=tenant_id,
            before={"enabled": old},
            after={"enabled": body.enabled},
        )
    return {"id": tenant_id, "enabled": body.enabled}


@router.patch("/admin/tenants/{tenant_id}/quota")
async def update_quota(tenant_id: UUID, body: QuotaPatch, request: Request, actor: SessionWrite):
    _require(actor.role, {Role.PLATFORM_ADMIN})
    max_concurrent = request.app.state.settings.max_concurrent
    try:
        changes = validate_limits(body.quota)
    except ValueError as error:
        raise ApiError(422, "INVALID_REQUEST", str(error)) from None
    if not changes or changes.get("concurrency", 0) > max_concurrent:
        raise ApiError(422, "INVALID_REQUEST", "配额变更不能为空且并发不能超过进程上限")
    async with request.app.state.db.begin() as connection:
        await lock_tenant(connection, tenant_id)
        exists = await connection.scalar(
            text("SELECT id FROM tenants WHERE id = :id"), {"id": tenant_id}
        )
        if exists is None:
            raise ApiError(404, "TENANT_NOT_FOUND", "租户不存在")
        old = await tenant_quota(connection, tenant_id, max_concurrent)
        await connection.execute(
            text("""
            INSERT INTO quota_policies (tenant_id, quota) VALUES (:id, CAST(:quota AS JSONB))
            ON CONFLICT (tenant_id) DO UPDATE SET quota = quota_policies.quota || EXCLUDED.quota,
                version = quota_policies.version + 1, updated_at = now()
        """),
            {"id": tenant_id, "quota": json.dumps(changes)},
        )
        updated = await tenant_quota(connection, tenant_id, max_concurrent)
        await _audit(
            connection,
            actor_id=actor.user_id,
            tenant_id=tenant_id,
            action="quota.update",
            target_id=tenant_id,
            before=old,
            after=updated,
        )
    return {"id": tenant_id, "quota": updated}


@router.get("/admin/audit")
async def audit(
    request: Request,
    actor: SessionUser,
    limit: PageSize = 30,
    cursor: Cursor = None,
    tenant_id: UUID | None = None,
):
    _require(actor.role, AUDIT)
    if actor.role != Role.PLATFORM_ADMIN:
        if tenant_id is not None and tenant_id != actor.tenant_id:
            raise ApiError(403, "FORBIDDEN", "无权查看其他租户的审计")
        tenant_id = actor.tenant_id
    try:
        before = int(cursor) if cursor is not None else None
        if before is not None and not 1 <= before < 2**63:
            raise ValueError
    except ValueError:
        raise ApiError(422, "INVALID_REQUEST", "无效分页游标") from None
    async with request.app.state.db.connect() as connection:
        result = await connection.execute(
            text("""
            SELECT id, tenant_id, actor_type, actor_id, action, target_id, details, occurred_at
            FROM audit_events
            WHERE (CAST(:tenant AS UUID) IS NULL OR tenant_id = :tenant)
                AND (CAST(:cursor AS BIGINT) IS NULL OR id < :cursor)
            ORDER BY id DESC LIMIT :limit
        """),
            {"tenant": tenant_id, "cursor": before, "limit": limit + 1},
        )
        items = [dict(row) for row in result.mappings()]
    return {
        "items": items[:limit],
        "next_cursor": str(items[limit - 1]["id"]) if len(items) > limit else None,
    }
