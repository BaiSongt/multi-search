from collections.abc import Iterable
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .contracts import ErrorBody, ProviderRun, unresolved_provider_reasons


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after_ms: int | None = None,
        provider_runs: Iterable[ProviderRun | dict] = (),
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms
        self.provider_runs = [ProviderRun.model_validate(run) for run in provider_runs]

    def body(self, request_id: str) -> ErrorBody:
        return ErrorBody(
            request_id=request_id,
            code=self.code,
            message=str(self),
            retryable=self.retryable,
            retry_after_ms=self.retry_after_ms,
            provider_runs=self.provider_runs or None,
            provider_reason_codes=unresolved_provider_reasons(self.provider_runs),
        )


class SearchBodyLimit:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] not in {"POST", "PATCH", "PUT"}
            or not scope["path"].startswith("/api/v1/")
        ):
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            chunk = await receive()
            if chunk["type"] == "http.disconnect":
                return
            part = chunk.get("body", b"")
            if len(body) + len(part) > 32_768:
                error = ErrorBody(
                    request_id=scope["state"]["request_id"],
                    code="INVALID_REQUEST",
                    message="请求体超过 32 KiB",
                )
                await JSONResponse(error.model_dump(exclude_none=True), 413)(scope, receive, send)
                return
            body.extend(part)
            if not chunk.get("more_body", False):
                break

        first = True

        async def replay() -> Message:
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def install_errors(app: FastAPI) -> None:
    app.add_middleware(SearchBodyLimit)

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        if request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError):
        body = error.body(request.state.request_id)
        headers = {}
        if error.status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        if error.retry_after_ms is not None:
            headers["Retry-After"] = str((error.retry_after_ms + 999) // 1000)
        return JSONResponse(body.model_dump(exclude_none=True), error.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, error: RequestValidationError):
        return await api_error(request, ApiError(422, "INVALID_REQUEST", "请求参数验证失败"))

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        return await api_error(request, ApiError(error.status_code, "HTTP_ERROR", "请求无法处理"))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception):
        response = await api_error(request, ApiError(500, "INTERNAL_ERROR", "内部服务错误"))
        response.headers["X-Request-ID"] = request.state.request_id
        return response
