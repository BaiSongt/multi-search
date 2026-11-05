from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from redis.exceptions import RedisError

from .auth import SearchKey, SessionUser, SessionWrite
from .contracts import Role, Scope
from .errors import ApiError
from .job_contracts import FetchRequest, JobAccepted, JobKind, JobStatus, JobView
from .metering import SearchPrincipal
from .safe_fetch import validate_fetch_url

router = APIRouter(prefix="/api/v1")
_JOB_KINDS = tuple(JobKind)
_OPS = {Role.PLATFORM_ADMIN, Role.OPERATIONS_ADMIN}


def _kinds(scopes: frozenset[str]) -> tuple[JobKind, ...]:
    kinds = tuple(kind for kind in _JOB_KINDS if kind.value in scopes)
    if not kinds:
        raise ApiError(403, "FORBIDDEN", "API Key 缺少抓取或研究权限")
    return kinds


async def worker_ready(redis, kind: JobKind) -> bool:
    try:
        return bool(await redis.exists(f"multisearch:worker:{kind.value}"))
    except (RedisError, OSError, TimeoutError):
        return False


@router.post("/fetch", status_code=202, response_model=JobAccepted)
async def fetch(body: FetchRequest, request: Request, identity: SearchKey) -> JobAccepted:
    if "fetch" not in identity.scopes:
        raise ApiError(403, "FORBIDDEN", "API Key 缺少 fetch 作用域")
    if body.network_scope != Scope.PUBLIC:
        raise ApiError(422, "SOURCE_UNAVAILABLE", "抓取仅支持公开网络")
    for url in body.targets():
        validate_fetch_url(url, body.domain_allowlist, body.domain_denylist)
    if not await worker_ready(request.app.state.redis, JobKind.FETCH):
        raise ApiError(503, "WORKER_UNAVAILABLE", "抓取 Worker 暂不可用", retryable=True)
    principal = SearchPrincipal(
        identity.tenant_id, identity.owner_id, identity.key_id, "key", identity.limits
    )
    request.state.request_id = body.request_id or request.state.request_id
    job = await request.app.state.jobs.create(
        principal,
        JobKind.FETCH,
        body.model_dump(mode="json", exclude={"request_id", "idempotency_key"}),
        request.state.request_id,
        body.idempotency_key,
        body.max_latency_ms,
        len(body.targets()),
    )
    return JobAccepted(
        job_id=job.job_id,
        status=job.status,
        status_url=f"/api/v1/jobs/{job.job_id}",
        budget={"max_latency_ms": body.max_latency_ms, "max_pages": body.max_pages},
    )


@router.get("/jobs/{job_id}", response_model=JobView)
async def job(job_id: UUID, request: Request, identity: SearchKey) -> JobView:
    return await request.app.state.jobs.get(job_id, identity.tenant_id, _kinds(identity.scopes))


@router.post("/jobs/{job_id}/cancel", response_model=JobView)
async def cancel(job_id: UUID, request: Request, identity: SearchKey) -> JobView:
    return await request.app.state.jobs.cancel(job_id, identity.tenant_id, _kinds(identity.scopes))


def _admin_tenant(actor, selected: UUID | None = None) -> UUID | None:
    if actor.role not in _OPS:
        raise ApiError(403, "FORBIDDEN", "当前角色无权查看任务")
    if actor.role == Role.PLATFORM_ADMIN:
        return selected
    if selected is not None and selected != actor.tenant_id:
        raise ApiError(403, "FORBIDDEN", "无权访问其他租户的任务")
    return actor.tenant_id


@router.get("/admin/jobs")
async def admin_jobs(
    request: Request,
    actor: SessionUser,
    status: JobStatus | None = None,
    kind: JobKind | None = None,
    tenant_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
):
    return await request.app.state.jobs.list(
        _admin_tenant(actor, tenant_id), status, kind, limit, cursor
    )


@router.get("/admin/jobs/{job_id}", response_model=JobView)
async def admin_job(job_id: UUID, request: Request, actor: SessionUser) -> JobView:
    return await request.app.state.jobs.get(job_id, _admin_tenant(actor), _JOB_KINDS)


@router.post("/admin/jobs/{job_id}/cancel", response_model=JobView)
async def admin_cancel(job_id: UUID, request: Request, actor: SessionWrite) -> JobView:
    return await request.app.state.jobs.cancel(job_id, _admin_tenant(actor), _JOB_KINDS)
