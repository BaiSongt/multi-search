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
from pydantic import TypeAdapter, ValidationError
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .contracts import ErrorBody, InputModel, Modality, Mode, SearchRequest, SearchResponse
from .errors import ApiError
from .identity import MachineIdentity
from .job_contracts import FetchRequest, JobAccepted, JobView

type SearchCallback = Callable[[SearchRequest, MachineIdentity, str], Awaitable[SearchResponse]]
type AuthenticateCallback = Callable[[str], Awaitable[MachineIdentity]]
type FetchCallback = Callable[[FetchRequest, MachineIdentity, str], Awaitable[JobAccepted]]
type ResearchCallback = Callable[[SearchRequest, MachineIdentity, str], Awaitable[JobAccepted]]
type JobCallback = Callable[[UUID, MachineIdentity], Awaitable[JobView]]
type ToolOutput = SearchResponse | JobAccepted | JobView

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_MAX_REQUEST_BYTES = 32_768
_MAX_TOOL_TEXT_BYTES = 16_384


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


def _tool_input[Model: InputModel](model: type[Model], arguments: dict[str, object]) -> Model:
    try:
        return model.model_validate(arguments)
    except ValidationError:
        raise ApiError(422, "INVALID_REQUEST", "请求参数验证失败") from None


def _error_body(error: ApiError, request_id: str) -> ErrorBody:
    return ErrorBody(
        request_id=request_id,
        code=error.code,
        message=str(error),
        retryable=error.retryable,
        retry_after_ms=error.retry_after_ms,
    )


def _tool_error(error: ApiError, request_id: str) -> CallToolResult:
    body = _error_body(error, request_id)
    return CallToolResult(
        is_error=True,
        structured_content=body.model_dump(mode="json", exclude_none=True),
        content=[TextContent(type="text", text=body.model_dump_json(exclude_none=True))],
    )


def _tool_result(response: ToolOutput) -> CallToolResult:
    data = response.model_dump(mode="json")
    model_text = response.model_dump_json()
    if len(model_text.encode("utf-8")) > _MAX_TOOL_TEXT_BYTES:
        # 将较长的结果保留在 structuredContent，避免为模型复制大量摘要和链接。
        if isinstance(response, SearchResponse):
            preview = {
                "request_id": response.request_id,
                "status": response.status,
                "result_count": response.result_count,
                "preview": [
                    {"result_id": hit.result_id, "kind": hit.kind, "title": hit.title}
                    for hit in response.results[:5]
                ],
                "message": "完整结果见 structuredContent。图片候选不代表获得使用授权。",
            }
        else:
            preview = {
                "job_id": str(response.job_id),
                "status": response.status,
                "message": "完整任务状态和结果见 structuredContent。",
            }
        model_text = json.dumps(preview, ensure_ascii=False)
    return CallToolResult(
        content=[TextContent(type="text", text=model_text)],
        structured_content=data,
    )


def create_mcp(search: SearchCallback, *, version: str, jobs: MCPJobs | None = None) -> Server:
    output_schema = (
        {"type": "object", **TypeAdapter(SearchResponse | JobAccepted).json_schema()}
        if jobs is not None
        else SearchResponse.model_json_schema()
    )
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
                output_schema=JobView.model_json_schema(),
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
        except ApiError as error:
            return _tool_error(error, request_id)
        if isinstance(response, SearchResponse):
            http.state.request_id = response.request_id
        return _tool_result(response)

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
            state["mcp_identity"] = await self.authenticate(credential)
        except ApiError as error:
            headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else {}
            if error.retry_after_ms is not None:
                headers["Retry-After"] = str((error.retry_after_ms + 999) // 1000)
            body = _error_body(error, state["request_id"])
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
