from fastapi import APIRouter, Request

from .auth import SearchKey
from .contracts import ErrorBody, SearchRequest, SearchResponse

router = APIRouter(prefix="/api/v1")


@router.get("/capabilities")
async def capabilities(request: Request):
    return request.app.state.search.capabilities()


@router.get("/providers", responses={401: {"model": ErrorBody}, 503: {"model": ErrorBody}})
async def providers(request: Request, identity: SearchKey):
    return {"providers": request.app.state.search.provider_status()}


@router.post(
    "/search",
    response_model=SearchResponse,
    responses={status: {"model": ErrorBody} for status in (401, 403, 413, 422, 429, 502, 503, 504)},
)
async def search(request: Request, body: SearchRequest, identity: SearchKey) -> SearchResponse:
    request.state.request_id = body.request_id or request.state.request_id
    identity.require_search(body.modalities)
    return await request.app.state.search.search(
        body, str(identity.tenant_id), request.state.request_id
    )
