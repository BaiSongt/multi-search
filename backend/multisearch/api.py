from fastapi import APIRouter, Request

from .auth import SearchKey, SessionWrite
from .contracts import ErrorBody, SearchRequest, SearchResponse
from .job_api import worker_ready
from .job_contracts import JobKind
from .metering import SearchPrincipal

router = APIRouter(prefix="/api/v1")


@router.get("/capabilities")
async def capabilities(request: Request):
    document = request.app.state.search.capabilities()
    document["fetch"] = (
        "available" if await worker_ready(request.app.state.redis, JobKind.FETCH) else "unavailable"
    )
    document["ocr"] = "unavailable"
    return document


@router.get("/providers", responses={401: {"model": ErrorBody}, 503: {"model": ErrorBody}})
async def providers(request: Request, identity: SearchKey):
    return {"providers": request.app.state.search.provider_status()}


@router.post(
    "/search",
    response_model=SearchResponse,
    responses={
        status: {"model": ErrorBody} for status in (401, 403, 409, 413, 422, 429, 502, 503, 504)
    },
)
async def search(request: Request, body: SearchRequest, identity: SearchKey) -> SearchResponse:
    request.state.request_id = body.request_id or request.state.request_id
    response = await request.app.state.application.machine_search(
        body, identity, request.state.request_id
    )
    request.state.request_id = response.request_id
    return response


@router.post("/playground", response_model=SearchResponse)
async def playground(request: Request, body: SearchRequest, actor: SessionWrite) -> SearchResponse:
    actor.require_key_manager()
    request.state.request_id = body.request_id or request.state.request_id
    response = await request.app.state.application.search(
        body,
        SearchPrincipal(actor.tenant_id, actor.user_id, actor.user_id, "session"),
        request.state.request_id,
    )
    request.state.request_id = response.request_id
    return response
