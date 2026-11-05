from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, SecretStr, StrictBool, StringConstraints, model_validator

from .auth import SessionUser, SessionWrite, console_origin, require_console_origin
from .contracts import InputModel, Role, SearchRequest
from .errors import ApiError
from .management_identity import _limit_login
from .ops_store import OpsStore
from .query_retention import QueryRetentionState, QueryRetentionStore
from .routing_policy import RoutingRules, RoutingVersion, resolve_mode, search_limits
from .sessions import EMAIL_PATTERN, SessionIdentity

router = APIRouter(prefix="/api/v1", tags=["运维与成员"])
_OPS = {Role.PLATFORM_ADMIN, Role.OPERATIONS_ADMIN}
PageSize = Annotated[int, Query(ge=1, le=100)]
Version = Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
CacheScope = Literal["all", "web", "news", "image", "academic", "code", "local"]


class Member(BaseModel):
    user_id: UUID
    email: str
    role: Role
    enabled: bool
    account_enabled: bool
    has_password: bool


class MemberPage(BaseModel):
    items: list[Member]
    next_cursor: UUID | None


class MemberPatch(InputModel):
    role: Role | None = None
    enabled: StrictBool | None = None

    @model_validator(mode="after")
    def has_change(self) -> Self:
        if self.role is None and self.enabled is None:
            raise ValueError("需要提供角色或启用状态")
        return self


class Invitation(BaseModel):
    id: UUID
    tenant_id: UUID
    email: str
    role: Role
    status: Literal["pending", "expired", "revoked", "accepted"]
    created_at: datetime
    expires_at: datetime


class InvitationPage(BaseModel):
    items: list[Invitation]
    next_cursor: UUID | None


class InvitationCreated(BaseModel):
    invitation: Invitation
    token: str = Field(repr=False)


class InvitationCreate(InputModel):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)
    role: Role = Role.DEVELOPER
    expires_in_hours: int = Field(default=72, strict=True, ge=1, le=168)


class InvitationAccept(InputModel):
    token: SecretStr = Field(min_length=80, max_length=80)
    password: SecretStr = Field(min_length=12, max_length=128)


class InvitationAccepted(BaseModel):
    tenant: str
    email: str


class RoutingSample(InputModel):
    mode: Literal["auto", "local", "quick", "standard"] = "auto"
    max_latency_ms: int | None = Field(default=None, strict=True, ge=100, le=900_000)
    max_results: int | None = Field(default=None, strict=True, ge=1, le=30)


class RoutingPreview(InputModel):
    policy: RoutingRules
    request: RoutingSample = RoutingSample()


class RoutingPublish(InputModel):
    policy: RoutingRules
    expected_version: Version
    reason: Reason


class RoutingRollback(InputModel):
    version: Version
    expected_version: Version
    reason: Reason


class RoutingHistory(BaseModel):
    active: RoutingVersion
    items: list[RoutingVersion]
    next_cursor: int | None


class RoutingActive(BaseModel):
    active: RoutingVersion


class CacheInvalidate(InputModel):
    tenant_id: UUID | None = None
    type: CacheScope
    confirm: StrictBool


class CacheStorage(BaseModel):
    keys: int | None
    bytes: int | None
    complete: bool
    available: bool


class CacheStatus(BaseModel):
    tenant_id: UUID
    search_requests: int
    cache_hits: int
    hit_rate: float | None
    window_days: int
    epochs: dict[str, int]
    storage: CacheStorage


class CacheInvalidated(BaseModel):
    tenant_id: UUID
    type: CacheScope
    epoch: int


class QueryRetentionUpdate(InputModel):
    tenant_id: UUID
    retention_hours: Annotated[int, Field(strict=True, ge=0, le=24)]
    expected_version: Version
    confirm: StrictBool


def _store(request: Request) -> OpsStore:
    return request.app.state.ops


def _require_ops(actor: SessionIdentity) -> None:
    if actor.role not in _OPS:
        raise ApiError(403, "FORBIDDEN", "当前角色无权读取运维配置")


def _platform_tenant(actor: SessionIdentity, tenant_id: UUID) -> UUID:
    if actor.role != Role.PLATFORM_ADMIN:
        raise ApiError(403, "FORBIDDEN", "需要平台管理员权限")
    return tenant_id


@router.get("/team/members", response_model=MemberPage)
async def members(
    request: Request, actor: SessionUser, limit: PageSize = 30, cursor: UUID | None = None
):
    return await _store(request).members(actor, actor.tenant_id, limit, cursor)


@router.get("/admin/tenants/{tenant_id}/members", response_model=MemberPage)
async def platform_members(
    tenant_id: UUID,
    request: Request,
    actor: SessionUser,
    limit: PageSize = 30,
    cursor: UUID | None = None,
):
    return await _store(request).members(actor, _platform_tenant(actor, tenant_id), limit, cursor)


@router.patch("/team/members/{user_id}", response_model=Member)
async def update_member(user_id: UUID, body: MemberPatch, request: Request, actor: SessionWrite):
    return await _store(request).update_member(
        actor,
        actor.tenant_id,
        user_id,
        role=body.role,
        enabled=body.enabled,
    )


@router.patch("/admin/tenants/{tenant_id}/members/{user_id}", response_model=Member)
async def platform_update_member(
    tenant_id: UUID,
    user_id: UUID,
    body: MemberPatch,
    request: Request,
    actor: SessionWrite,
):
    return await _store(request).update_member(
        actor,
        _platform_tenant(actor, tenant_id),
        user_id,
        role=body.role,
        enabled=body.enabled,
    )


@router.get("/team/invitations", response_model=InvitationPage)
async def invitations(
    request: Request, actor: SessionUser, limit: PageSize = 30, cursor: UUID | None = None
):
    return await _store(request).invitations(actor, actor.tenant_id, limit, cursor)


@router.get("/admin/tenants/{tenant_id}/invitations", response_model=InvitationPage)
async def platform_invitations(
    tenant_id: UUID,
    request: Request,
    actor: SessionUser,
    limit: PageSize = 30,
    cursor: UUID | None = None,
):
    return await _store(request).invitations(
        actor, _platform_tenant(actor, tenant_id), limit, cursor
    )


@router.post("/team/invitations", response_model=InvitationCreated, status_code=201)
async def invite(body: InvitationCreate, request: Request, actor: SessionWrite):
    return await _store(request).invite(
        actor,
        actor.tenant_id,
        body.email.lower(),
        body.role,
        body.expires_in_hours,
    )


@router.post(
    "/admin/tenants/{tenant_id}/invitations", response_model=InvitationCreated, status_code=201
)
async def platform_invite(
    tenant_id: UUID, body: InvitationCreate, request: Request, actor: SessionWrite
):
    return await _store(request).invite(
        actor,
        _platform_tenant(actor, tenant_id),
        body.email.lower(),
        body.role,
        body.expires_in_hours,
    )


@router.post("/team/invitations/{invitation_id}/revoke", response_model=Invitation)
async def revoke_invitation(invitation_id: UUID, request: Request, actor: SessionWrite):
    return await _store(request).revoke_invitation(actor, actor.tenant_id, invitation_id)


@router.post(
    "/admin/tenants/{tenant_id}/invitations/{invitation_id}/revoke", response_model=Invitation
)
async def platform_revoke_invitation(
    tenant_id: UUID,
    invitation_id: UUID,
    request: Request,
    actor: SessionWrite,
):
    return await _store(request).revoke_invitation(
        actor,
        _platform_tenant(actor, tenant_id),
        invitation_id,
    )


@router.post(
    "/auth/invitations/accept",
    response_model=InvitationAccepted,
    status_code=201,
    dependencies=[Depends(require_console_origin)],
)
async def accept_invitation(body: InvitationAccept, request: Request):
    token = body.token.get_secret_value()
    await _limit_login(request, "invitation_probe", token[:36])

    async def check_attempt(email: str) -> None:
        await _limit_login(request, "invitation", email)

    return await _store(request).accept_invitation(
        token,
        body.password.get_secret_value(),
        check_attempt,
    )


@router.get("/admin/routing", response_model=RoutingHistory)
async def routing_history(
    request: Request,
    actor: SessionUser,
    limit: PageSize = 30,
    cursor: Annotated[int | None, Query(ge=0, le=2**31 - 1)] = None,
):
    _require_ops(actor)
    return await _store(request).routing_history(limit, cursor)


def _changes(before: dict, after: dict, prefix: str = "") -> list[dict]:
    changes = []
    for key, value in after.items():
        old_value = before[key]
        field = prefix + key
        if isinstance(value, dict):
            changes.extend(_changes(old_value, value, field + "."))
        elif old_value != value:
            changes.append({"field": field, "before": old_value, "after": value})
    return changes


@router.post("/admin/routing/test")
async def preview_routing(body: RoutingPreview, request: Request, actor: SessionWrite):
    _require_ops(actor)
    current = await _store(request).active_routing()
    sample = SearchRequest(query="routing preview", **body.request.model_dump())
    mode = resolve_mode(sample, body.policy)
    latency, results = search_limits(body.policy, mode)
    return {
        "mode": mode,
        "max_latency_ms": min(sample.max_latency_ms or latency, latency),
        "max_results": min(sample.max_results or results, results),
        "changes": _changes(
            current.policy.model_dump(mode="json"), body.policy.model_dump(mode="json")
        ),
    }


def _activate(request: Request, version: RoutingVersion) -> RoutingActive:
    search = request.app.state.search
    if version.version > search.routing.version:
        search.routing = version
    return RoutingActive(active=version)


@router.post("/admin/routing/publish", response_model=RoutingActive)
async def publish_routing(body: RoutingPublish, request: Request, actor: SessionWrite):
    version = await _store(request).publish_routing(
        actor,
        body.expected_version,
        body.reason,
        policy=body.policy,
    )
    return _activate(request, version)


@router.post("/admin/routing/rollback", response_model=RoutingActive)
async def rollback_routing(body: RoutingRollback, request: Request, actor: SessionWrite):
    version = await _store(request).publish_routing(
        actor,
        body.expected_version,
        body.reason,
        rollback_of=body.version,
    )
    return _activate(request, version)


@router.get("/admin/cache", response_model=CacheStatus)
async def cache_status(request: Request, actor: SessionUser, tenant_id: UUID | None = None):
    return await _store(request).cache_status(actor, tenant_id or actor.tenant_id)


@router.post("/admin/cache/invalidate", response_model=CacheInvalidated)
async def invalidate_cache(body: CacheInvalidate, request: Request, actor: SessionWrite):
    if not body.confirm:
        raise ApiError(422, "INVALID_REQUEST", "需要确认缓存范围失效")
    return await _store(request).invalidate_cache(
        actor, body.tenant_id or actor.tenant_id, body.type
    )


@router.get("/admin/settings")
async def settings(request: Request, actor: SessionUser):
    _require_ops(actor)
    origin, secure = console_origin()
    search = request.app.state.search
    return {
        "environment": {
            "api_version": request.app.version,
            "providers": sorted(search.providers),
            "public_search": True,
            "enable_images": request.app.state.settings.enable_images,
        },
        "security": {
            "console_origin": origin,
            "secure_session_cookie": secure,
            "require_csrf": True,
            "session_ttl_hours": 8,
            "session_absolute_ttl_days": 7,
        },
        "retention": {
            "usage_metadata_days": None,
            "idempotency_window_minutes": 1440,
        },
    }


@router.get("/admin/settings/query-retention", response_model=QueryRetentionState)
async def query_retention(
    request: Request, actor: SessionUser, tenant_id: UUID | None = None
) -> QueryRetentionState:
    store = QueryRetentionStore(request.app.state.db, request.app.state.identity.sessions)
    return await store.get(actor, tenant_id or actor.tenant_id)


@router.patch("/admin/settings/query-retention", response_model=QueryRetentionState)
async def update_query_retention(
    body: QueryRetentionUpdate, request: Request, actor: SessionWrite
) -> QueryRetentionState:
    if actor.role != Role.PLATFORM_ADMIN:
        raise ApiError(403, "FORBIDDEN", "需要平台管理员权限")
    if not body.confirm:
        raise ApiError(422, "INVALID_REQUEST", "需要确认查询原文保留设置")
    store = QueryRetentionStore(request.app.state.db, request.app.state.identity.sessions)
    return await store.update(actor, body.tenant_id, body.retention_hours, body.expected_version)
