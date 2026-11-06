import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from fastapi.security.utils import get_authorization_scheme_param
from mcp.server import Server, ServerRequestContext
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .contracts import ImageSearchResult, InputModel, Modality, Mode, SearchRequest, SearchResponse
from .errors import ApiError
from .identity import MachineIdentity
from .job_contracts import FetchRequest, JobAccepted, JobStatus, JobView
from .mcp_results import MCPResultPage, MCPResultReference, MCPResultStore, ResultID

type SearchCallback = Callable[[SearchRequest, MachineIdentity, str], Awaitable[SearchResponse]]
type AuthenticateCallback = Callable[[str, Request], Awaitable[MachineIdentity]]
type FetchCallback = Callable[[FetchRequest, MachineIdentity, str], Awaitable[JobAccepted]]
type ResearchCallback = Callable[[SearchRequest, MachineIdentity, str], Awaitable[JobAccepted]]
type JobCallback = Callable[[UUID, MachineIdentity], Awaitable[JobView]]
type ToolOutput = SearchResponse | JobAccepted | JobView

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_MAX_REQUEST_BYTES = 32_768
_MAX_TOOL_RESULT_BYTES = 16_384


@dataclass(frozen=True)
class MCPJobs:
    fetch: FetchCallback
    research: ResearchCallback
    get: JobCallback
    cancel: JobCallback


class _JobReference(InputModel):
    job_id: UUID


class _StartResearch(SearchRequest):
    mode: Literal[Mode.DEEP] = Mode.DEEP


class _ResultPageInput(InputModel):
    result_id: ResultID
    page: int = Field(ge=0, le=4095, strict=True)


class _ResultPreview(BaseModel):
    result_id: str = Field(max_length=128)
    kind: Literal["text", "image"]
    title: str = Field(max_length=160)
    rights_status: str | None = None


class MCPDeferredResult(BaseModel):
    delivery: Literal["deferred"]
    kind: Literal["search", "job"]
    status: JobStatus
    request_id: str | None = Field(default=None, max_length=128)
    trace_id: str | None = Field(default=None, max_length=128)
    job_id: UUID | None = None
    result_count: int | None = None
    partial: bool
    preview: list[_ResultPreview] = Field(default_factory=list, max_length=3)
    result_ref: MCPResultReference


def _tool_input[Model: InputModel](model: type[Model], arguments: dict[str, object]) -> Model:
    try:
        return model.model_validate(arguments)
    except ValidationError:
        raise ApiError(422, "INVALID_REQUEST", "请求参数验证失败") from None


def _json_result(payload: str, *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        is_error=is_error,
        content=[TextContent(type="text", text=payload)],
        structured_content=json.loads(payload),
    )


def _fits(result: CallToolResult) -> bool:
    return len(result.model_dump_json(by_alias=True, exclude_unset=True).encode()) <= (
        _MAX_TOOL_RESULT_BYTES
    )


def _bounded_json_result(payload: str) -> CallToolResult:
    result = _json_result(payload)
    if not _fits(result):
        raise ApiError(413, "MCP_RESULT_TOO_LARGE", "MCP 工具响应超出交付上限")
    return result


def _tool_error(error: ApiError, request_id: str) -> CallToolResult:
    body = error.body(request_id)
    result = _json_result(body.model_dump_json(exclude_none=True), is_error=True)
    if _fits(result):
        return result
    body = body.model_copy(
        update={
            "provider_runs": None,
            "request_id": request_id[:128],
            "message": body.message[:256],
        }
    )
    return _json_result(body.model_dump_json(exclude_none=True), is_error=True)


def _deferred(
    response: SearchResponse | JobView, reference: MCPResultReference
) -> MCPDeferredResult:
    common = {
        "delivery": "deferred",
        "status": response.status,
        "request_id": response.request_id[:128] if response.request_id else None,
        "trace_id": response.trace_id[:128] if response.trace_id else None,
        "result_ref": reference,
    }
    if isinstance(response, JobView):
        return MCPDeferredResult(
            **common,
            kind="job",
            job_id=response.job_id,
            partial=response.status == JobStatus.PARTIAL,
        )
    return MCPDeferredResult(
        **common,
        kind="search",
        partial=response.partial,
        result_count=response.result_count,
        preview=[
            _ResultPreview(
                result_id=hit.result_id,
                kind=hit.kind,
                title=hit.title[:160],
                rights_status=hit.image.rights_status
                if isinstance(hit, ImageSearchResult)
                else None,
            )
            for hit in response.results[:3]
            if len(hit.result_id) <= 128
        ],
    )


async def _tool_result(
    response: ToolOutput,
    identity: MachineIdentity,
    result_store: Callable[[], MCPResultStore] | None,
    *,
    cancelling: bool,
) -> CallToolResult:
    serialized = response.model_dump_json()
    payload = serialized.encode()
    if len(payload) <= _MAX_TOOL_RESULT_BYTES:
        result = _json_result(serialized)
        if _fits(result):
            return result
    if cancelling and isinstance(response, JobView):
        # 取消已落库，返回可重新查询的任务状态，避免因正文暂存失败误报取消失败。
        return _bounded_json_result(response.model_copy(update={"result": None}).model_dump_json())
    if result_store is None or isinstance(response, JobAccepted):
        raise ApiError(
            503, "MCP_RESULT_STORE_UNAVAILABLE", "MCP 长结果暂时无法交付", retryable=True
        )
    reference = await result_store().put(identity, payload)
    return _bounded_json_result(_deferred(response, reference).model_dump_json())


def create_mcp(
    search: SearchCallback,
    *,
    version: str,
    jobs: MCPJobs | None = None,
    result_store: Callable[[], MCPResultStore] | None = None,
) -> Server:
    search_output = SearchResponse | JobAccepted if jobs is not None else SearchResponse
    job_output = JobView
    if result_store is not None:
        search_output |= MCPDeferredResult
        job_output |= MCPDeferredResult
    output_schema = {"type": "object", **TypeAdapter(search_output).json_schema()}
    image_schema = SearchRequest.model_json_schema()
    del image_schema["properties"]["modalities"]
    read_search = ToolAnnotations(read_only_hint=True, open_world_hint=True)
    enqueue = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
    annotations = enqueue if jobs is not None else read_search
    deep_note = " 深度模式提交异步任务。" if jobs is not None else ""
    tools = {
        "multi_search": Tool(
            name="multi_search",
            description="检索网页、新闻和图片，返回候选、来源和追溯 ID。" + deep_note,
            input_schema=SearchRequest.model_json_schema(),
            output_schema=output_schema,
            annotations=annotations,
        ),
        "image_search": Tool(
            name="image_search",
            description="检索图片，返回原图、预览、来源页和版权状态。搜索结果不代表获得使用授权。"
            + deep_note,
            input_schema=image_schema,
            output_schema=output_schema,
            annotations=annotations,
        ),
    }
    if jobs is not None:
        tools.update(
            web_fetch=Tool(
                name="web_fetch",
                description="提交公开来源提取任务，返回任务标识与预算。",
                input_schema=FetchRequest.model_json_schema(),
                output_schema=JobAccepted.model_json_schema(),
                annotations=enqueue,
            ),
            start_deep_research=Tool(
                name="start_deep_research",
                description="提交有预算上限的深度研究任务，返回任务标识。",
                input_schema=_StartResearch.model_json_schema(),
                output_schema=JobAccepted.model_json_schema(),
                annotations=enqueue,
            ),
            get_search_job=Tool(
                name="get_search_job",
                description="查询当前租户内、当前 API Key 获准访问的任务。",
                input_schema=_JobReference.model_json_schema(),
                output_schema={"type": "object", **TypeAdapter(job_output).json_schema()},
                annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
            ),
            cancel_search_job=Tool(
                name="cancel_search_job",
                description="取消当前租户内、当前 API Key 获准管理的任务。",
                input_schema=_JobReference.model_json_schema(),
                output_schema=JobView.model_json_schema(),
                annotations=ToolAnnotations(
                    read_only_hint=False, destructive_hint=True, open_world_hint=False
                ),
            ),
        )
    if result_store is not None:
        tools["get_result_page"] = Tool(
            name="get_result_page",
            description="用产生结果的 API Key 读取长结果的一页。逐页解码 Base64 后按页码拼接 UTF-8 JSON。",
            input_schema=_ResultPageInput.model_json_schema(),
            output_schema=MCPResultPage.model_json_schema(),
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        )

    async def list_tools(
        context: ServerRequestContext, params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        return ListToolsResult(tools=list(tools.values()))

    async def call_tool(
        context: ServerRequestContext, params: CallToolRequestParams
    ) -> CallToolResult:
        http = context.request
        identity = getattr(http.state, "mcp_identity", None) if http is not None else None
        request_id = http.state.request_id if http is not None else uuid4().hex
        if identity is None:
            return _tool_error(ApiError(401, "AUTH_REQUIRED", "需要有效的 API Key"), request_id)
        args = params.arguments or {}
        if params.name not in tools or (params.name == "image_search" and "modalities" in args):
            return _tool_error(ApiError(422, "INVALID_REQUEST", "工具或参数不可用"), request_id)
        if params.name == "image_search":
            args = {**args, "modalities": [Modality.IMAGE]}
        try:
            if result_store is not None and params.name == "get_result_page":
                page = _tool_input(_ResultPageInput, args)
                part = await result_store().get(identity, page.result_id, page.page)
                return _bounded_json_result(part.model_dump_json())
            if jobs is not None and params.name in {"get_search_job", "cancel_search_job"}:
                reference = _tool_input(_JobReference, args)
                lookup = jobs.get if params.name == "get_search_job" else jobs.cancel
                response = await lookup(reference.job_id, identity)
            elif jobs is not None and params.name == "web_fetch":
                fetch = _tool_input(FetchRequest, args)
                request_id = fetch.request_id or request_id
                http.state.request_id = request_id
                response = await jobs.fetch(fetch, identity, request_id)
            else:
                request_model = (
                    _StartResearch if params.name == "start_deep_research" else SearchRequest
                )
                body = _tool_input(request_model, args)
                request_id = body.request_id or request_id
                http.state.request_id = request_id
                dispatch = jobs.research if jobs is not None and body.mode == Mode.DEEP else search
                response = await dispatch(body, identity, request_id)
            if isinstance(response, SearchResponse):
                request_id = http.state.request_id = response.request_id
            return await _tool_result(
                response, identity, result_store, cancelling=params.name == "cancel_search_job"
            )
        except ApiError as error:
            return _tool_error(error, request_id)

    return Server("MultiSearch", version=version, on_list_tools=list_tools, on_call_tool=call_tool)


class _MachineAuth:
    def __init__(self, app: ASGIApp, authenticate: AuthenticateCallback):
        self.app = app
        self.authenticate = authenticate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        if "request_id" not in state:
            state["request_id"] = uuid4().hex

        async def send_uncached(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["X-Request-ID"] = state["request_id"]
            await send(message)

        values = Headers(scope=scope).getlist("authorization")
        scheme, credential = get_authorization_scheme_param(values[0] if len(values) == 1 else "")
        try:
            if scheme.lower() != "bearer" or not credential:
                raise ApiError(401, "AUTH_REQUIRED", "需要有效的 API Key")
            state["mcp_identity"] = await self.authenticate(credential, Request(scope))
        except ApiError as error:
            headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else {}
            if error.retry_after_ms is not None:
                headers["Retry-After"] = str((error.retry_after_ms + 999) // 1000)
            body = error.body(state["request_id"])
            response = JSONResponse(body.model_dump(exclude_none=True), error.status, headers)
            await response(scope, receive, send_uncached)
            return

        await self.app(scope, receive, send_uncached)


def create_mcp_app(
    server: Server,
    authenticate: AuthenticateCallback,
    *,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
) -> ASGIApp:
    security = TransportSecuritySettings(
        allowed_hosts=allowed_hosts
        if allowed_hosts is not None
        else [host for base in _LOOPBACK_HOSTS for host in (base, f"{base}:*")],
        allowed_origins=allowed_origins
        if allowed_origins is not None
        else [
            origin for host in _LOOPBACK_HOSTS for origin in (f"http://{host}", f"http://{host}:*")
        ],
    )
    transport = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=_MAX_REQUEST_BYTES,
        transport_security=security,
    )
    return _MachineAuth(transport, authenticate)
