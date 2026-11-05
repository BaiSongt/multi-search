from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .auth import SearchKey, SessionWrite
from .contracts import ErrorBody, Mode, SearchRequest, SearchResponse
from .job_contracts import JobAccepted, JobKind
from .metering import SearchPrincipal
from .research import research_limits

router = APIRouter(prefix="/api/v1")


@router.get("/capabilities")
async def capabilities(request: Request):
    document = request.app.state.search.capabilities()
    jobs = request.app.state.job_application
    document["fetch"] = "available" if await jobs.available(JobKind.FETCH) else "unavailable"
    document["fetch_dynamic"] = (
        "available" if await jobs.available_dynamic_fetch() else "unavailable"
    )
    document["research"] = "available" if await jobs.available(JobKind.RESEARCH) else "unavailable"
    document["modes"]["deep"] = document["research"]
    if document["research"] == "available":
        document["limits"]["modes"]["deep"] = research_limits()
    document["ocr"] = "unavailable"
    return document


@router.get("/providers", responses={401: {"model": ErrorBody}, 503: {"model": ErrorBody}})
async def providers(request: Request, identity: SearchKey):
    return {"providers": request.app.state.search.provider_status()}


@router.post(
    "/search",
    response_model=SearchResponse,
    responses={
        202: {"model": JobAccepted},
        **{
            status: {"model": ErrorBody} for status in (401, 403, 409, 413, 422, 429, 502, 503, 504)
        },
    },
)
async def search(
    request: Request, body: SearchRequest, identity: SearchKey
) -> SearchResponse | JSONResponse:
    request.state.request_id = body.request_id or request.state.request_id
    if body.mode == Mode.DEEP:
        job = await request.app.state.job_application.research(
            body, identity, request.state.request_id
        )
        return JSONResponse(job.model_dump(mode="json"), status_code=202)
    response = await request.app.state.application.machine_search(
        body, identity, request.state.request_id
    )
    request.state.request_id = response.request_id
    return response


@router.post("/playground", response_model=SearchResponse, responses={202: {"model": JobAccepted}})
async def playground(
    request: Request, body: SearchRequest, actor: SessionWrite
) -> SearchResponse | JSONResponse:
    actor.require_key_manager()
    request.state.request_id = body.request_id or request.state.request_id
    if body.mode == Mode.DEEP:
        job = await request.app.state.job_application.research_session(
            body, actor, request.state.request_id
        )
        return JSONResponse(job.model_dump(mode="json"), status_code=202)
    response = await request.app.state.application.search(
        body,
        SearchPrincipal(actor.tenant_id, actor.user_id, actor.user_id, "session"),
        request.state.request_id,
    )
    request.state.request_id = response.request_id
    return response
