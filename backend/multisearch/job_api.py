from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from .auth import SearchKey, SessionUser, SessionWrite
from .contracts import Role, SearchRequest
from .errors import ApiError
from .job_contracts import FetchRequest, JobAccepted, JobKind, JobStatus, JobView

router = APIRouter(prefix="/api/v1")
_JOB_KINDS = tuple(JobKind)
_OPS = {Role.PLATFORM_ADMIN, Role.OPERATIONS_ADMIN}


@router.post("/fetch", status_code=202, response_model=JobAccepted)
async def fetch(body: FetchRequest, request: Request, identity: SearchKey) -> JobAccepted:
    request.state.request_id = body.request_id or request.state.request_id
    return await request.app.state.job_application.fetch(body, identity, request.state.request_id)


@router.post("/research", status_code=202, response_model=JobAccepted)
async def research(body: SearchRequest, request: Request, identity: SearchKey) -> JobAccepted:
    request.state.request_id = body.request_id or request.state.request_id
    return await request.app.state.job_application.research(
        body, identity, request.state.request_id
    )


@router.get("/jobs/{job_id}", response_model=JobView)
async def job(job_id: UUID, request: Request, identity: SearchKey) -> JobView:
    return await request.app.state.job_application.get(job_id, identity)


@router.post("/jobs/{job_id}/cancel", response_model=JobView)
async def cancel(job_id: UUID, request: Request, identity: SearchKey) -> JobView:
    return await request.app.state.job_application.cancel(job_id, identity)


@router.get("/console/jobs/{job_id}", response_model=JobView)
async def console_job(job_id: UUID, request: Request, actor: SessionUser) -> JobView:
    actor.require_key_manager()
    return await request.app.state.jobs.get(
        job_id, actor.tenant_id, (JobKind.RESEARCH,), owner_id=actor.user_id
    )


@router.post("/console/jobs/{job_id}/cancel", response_model=JobView)
async def console_cancel(job_id: UUID, request: Request, actor: SessionWrite) -> JobView:
    actor.require_key_manager()
    return await request.app.state.jobs.cancel(
        job_id, actor.tenant_id, (JobKind.RESEARCH,), owner_id=actor.user_id
    )


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
